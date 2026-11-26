import copy
import json
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

from migratarr.profiles import save_profile
from migratarr.recovery import create_job, inspect_job, mark_prepared, resume_job
from migratarr.recovery_vm import guard_vm, grub_script, prepare_vm


def resume_worker(job, runtime, target):
    resume_job(job, runtime, target)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = self.root / "profile.json"
        save_profile(self.profile, {"schema_version": 1, "source": {"mode": "inventory", "path": "source.json"}})
        self.target = str(uuid.uuid4())
        self.plan = self.root / "plan.json"
        self.plan.write_text(json.dumps({"schema_version": 1, "scope": "entire_local_installation", "migration_ready": False,
            "filesystem_map": [{"destination": {"target": "/", "identifiers": {"uuid": self.target}}}]}))

    def create(self):
        return create_job(self.profile, self.plan, self.root / "jobs")

    def test_private_durable_job_cannot_enable_migration(self):
        created = self.create()
        path = Path(created["job_directory"])
        self.assertEqual(inspect_job(path)["phase"], "created")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in path.iterdir()))
        self.assertFalse(created["migration_ready"])

    def test_tampered_evidence_and_symlinks_are_rejected(self):
        created = self.create();path = Path(created["job_directory"])
        evidence = path / "plan.json"
        original = evidence.read_bytes()
        evidence.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            inspect_job(path)
        evidence.unlink()
        outside = self.root / "outside";outside.write_bytes(original);outside.chmod(0o600)
        evidence.symlink_to(outside)
        with self.assertRaises(OSError):
            inspect_job(path)

    def test_resume_requires_preparation_and_exact_target(self):
        created = self.create();job = created["job_directory"]
        with self.assertRaisesRegex(ValueError, "not prepared"):
            resume_job(job, self.root / "runtime", self.target)
        mark_prepared(job, {"kernel": "0" * 64, "initrd.img": "1" * 64})
        with self.assertRaisesRegex(ValueError, "not prepared"):
            resume_job(job, self.root / "runtime", str(uuid.uuid4()))
        first = resume_job(job, self.root / "runtime", self.target)
        second = resume_job(job, self.root / "runtime", self.target)
        self.assertEqual((first["resume_count"], second["resume_count"]), (1, 2))
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertFalse(second["migration_ready"])

    def test_concurrent_resumes_do_not_lose_checkpoint_updates(self):
        created = self.create();job = created["job_directory"]
        mark_prepared(job, {"kernel": "0" * 64, "initrd.img": "1" * 64})
        workers = [multiprocessing.Process(target=resume_worker, args=(job, self.root / ("runtime-" + str(i)), self.target)) for i in range(4)]
        for worker in workers:worker.start()
        for worker in workers:
            worker.join(10)
            if worker.is_alive():worker.terminate();worker.join()
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(inspect_job(job)["resume_count"], 4)

    def test_private_key_is_not_serialized_and_boot_password_mode_rejected(self):
        self.profile.unlink()
        save_profile(self.profile, {"schema_version": 1, "source": {"mode": "ssh", "host": "source.invalid", "user": "lab", "authentication": "password"}})
        with self.assertRaisesRegex(ValueError, "explicit unencrypted key"):
            self.create()
        self.assertFalse((self.root / "jobs").exists())

    def test_explicit_key_is_restored_to_ram_profile_without_serializing_secret(self):
        key = self.root / "source-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True, capture_output=True, timeout=10)
        hosts = self.root / "known_hosts";hosts.write_text("source.invalid " + Path(str(key) + ".pub").read_text());hosts.chmod(0o600)
        self.profile.unlink()
        save_profile(self.profile, {"schema_version": 1, "source": {"mode": "ssh", "host": "source.invalid", "user": "lab", "identity_file": str(key), "known_hosts": str(hosts)}})
        created = self.create();job = Path(created["job_directory"])
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", (job / "job.json").read_text() + (job / "profile.json").read_text())
        mark_prepared(job, {"kernel": "0" * 64, "initrd.img": "1" * 64})
        runtime = self.root / "runtime"
        resume_job(job, runtime, self.target)
        from migratarr.profiles import load_profile
        profile = load_profile(runtime / "profile.json")
        self.assertEqual(profile["source"]["identity_file"], str(runtime / "auth/source_key"))
        self.assertEqual((runtime / "auth/source_key").read_bytes(), key.read_bytes())
        self.assertEqual(stat.S_IMODE((runtime / "auth/source_key").stat().st_mode), 0o600)
        (self.root / "jobs").rename(self.root / "offline_jobs")
        self.assertEqual(Path(profile["source"]["identity_file"]).read_bytes(), key.read_bytes())

    def test_wrong_purpose_and_public_state_are_rejected(self):
        created = self.create();job = Path(created["job_directory"])
        job.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "private"):
            inspect_job(job)
        job.chmod(0o700)
        doc = json.loads((job / "job.json").read_text());doc["purpose"] = "migration"
        (job / "job.json").write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError, "unsupported recovery"):
            inspect_job(job)


class RecoveryVmGuardTests(unittest.TestCase):
    def test_physical_host_is_refused_before_reading_job_or_running_commands(self):
        with patch("os.geteuid", return_value=0), patch("pathlib.Path.read_text", return_value="physical-vendor"), patch("migratarr.recovery_vm._run") as run, patch("migratarr.recovery_vm.inspect_job") as inspect:
            with self.assertRaisesRegex(ValueError, "physical installation is not implemented"):
                prepare_vm("/irrelevant", "/kernel", "/initrd", "a" * 32)
        run.assert_not_called();inspect.assert_not_called()

    def test_root_permission_alone_does_not_authorize_vm_boot(self):
        with patch("os.geteuid", return_value=0), patch("pathlib.Path.read_text", return_value="QEMU"), patch("migratarr.recovery_vm._json", return_value={"purpose": "migratarr_disposable_vm", "token": "b" * 32}):
            with self.assertRaisesRegex(ValueError, "does not match"):
                guard_vm("a" * 32)

    def test_grub_entry_is_bound_to_valid_uuids_and_has_failure_marker(self):
        job, target, boot = (str(uuid.uuid4()) for _ in range(3))
        script = grub_script(job, target, boot)
        self.assertIn("set migratarr_attempted=1", script)
        self.assertIn("save_env migratarr_attempted", script)
        self.assertIn("migratarr.target_uuid=" + target, script)
        self.assertIn("--set=root " + boot, script)
        self.assertIn("linux /migratarr/", script)
        with self.assertRaises(ValueError):grub_script("id;unsafe", target)


if __name__ == "__main__":
    unittest.main()
