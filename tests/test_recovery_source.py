import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import uuid

from migratarr.profiles import save_profile
from migratarr.recovery import check_source
from migratarr.recovery_bundle import extra_network_commands
from migratarr.remote import SourceConnectionError


class RecoverySourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name)
        auth = self.runtime / "auth"
        auth.mkdir(mode=0o700)
        for name in ("source_key", "known_hosts"):
            (auth / name).write_text("fixture")
            (auth / name).chmod(0o600)
        save_profile(self.runtime / "profile.json", {"schema_version": 1,
            "source": {"mode": "ssh", "host": "source.invalid", "user": "root",
                       "identity_file": str(auth / "source_key"), "known_hosts": str(auth / "known_hosts")}})
        self.status = {"job_id": str(uuid.uuid4()), "phase": "rescue_ready", "resume_count": 1, "migration_ready": False}
        self.write_status()

    def write_status(self):
        path = self.runtime / "status.json"
        path.write_text(json.dumps(self.status))
        path.chmod(0o600)

    def test_restored_credentials_and_success_cannot_change_checkpoint(self):
        before = (self.runtime / "status.json").read_bytes()
        with patch("migratarr.recovery.collect_remote", return_value={"role": "source", "errors": []}) as collect:
            report = check_source(self.runtime)
        self.assertEqual(report["status"], "source_reachable")
        self.assertFalse(report["migration_ready"])
        self.assertEqual(collect.call_args.kwargs["identity_file"], str(self.runtime / "auth/source_key"))
        self.assertEqual(collect.call_args.kwargs["known_hosts"], str(self.runtime / "auth/known_hosts"))
        self.assertNotIn("password_auth", collect.call_args.kwargs)
        self.assertEqual(before, (self.runtime / "status.json").read_bytes())
        self.assertEqual(stat.S_IMODE((self.runtime / "source-check.json").stat().st_mode), 0o600)

    def test_incomplete_probes_remain_explicit(self):
        inventory = {"role": "source", "errors": [{"probe": "inaccessible"}]}
        with patch("migratarr.recovery.collect_remote", return_value=inventory):
            report = check_source(self.runtime)
        self.assertEqual(report["status"], "source_incomplete")
        self.assertEqual(report["inventory"]["errors"], inventory["errors"])

    def test_credentials_outside_runtime_or_symlinked_are_rejected_before_connecting(self):
        path = self.runtime / "profile.json"
        profile = json.loads(path.read_text())
        profile["source"]["identity_file"] = str(self.runtime / "outside-key")
        path.write_text(json.dumps(profile))
        with patch("migratarr.recovery.collect_remote") as collect:
            with self.assertRaisesRegex(ValueError, "resumed runtime"):
                check_source(self.runtime)
            collect.assert_not_called()
        profile["source"]["identity_file"] = str(self.runtime / "auth/source_key")
        path.write_text(json.dumps(profile))
        key = self.runtime / "auth/source_key"
        key.rename(self.runtime / "outside-key")
        key.symlink_to(self.runtime / "outside-key")
        with patch("migratarr.recovery.collect_remote") as collect:
            with self.assertRaises(OSError):
                check_source(self.runtime)
            collect.assert_not_called()

    def test_connection_failure_replaces_stale_success_and_keeps_job_blocked(self):
        with patch("migratarr.recovery.collect_remote", return_value={"errors": []}):
            check_source(self.runtime)
        with patch("migratarr.recovery.collect_remote", side_effect=SourceConnectionError("SSH host key verification failed.")):
            with self.assertRaises(SourceConnectionError):
                check_source(self.runtime)
        report = json.loads((self.runtime / "source-check.json").read_text())
        self.assertEqual(report["status"], "source_unreachable")
        self.assertNotIn("inventory", report)
        self.assertFalse(report["migration_ready"])
        self.assertEqual(json.loads((self.runtime / "status.json").read_text()), self.status)

    def test_invalid_runtime_rejected_before_connecting(self):
        for field, value in (("phase", "created"), ("resume_count", 0), ("migration_ready", True), ("job_id", "invalid")):
            original = self.status[field]
            self.status[field] = value
            self.write_status()
            with patch("migratarr.recovery.collect_remote") as collect:
                with self.assertRaises(ValueError):
                    check_source(self.runtime)
                collect.assert_not_called()
            self.status[field] = original

    def test_external_and_symlinked_credentials_rejected_before_connecting(self):
        path = self.runtime / "profile.json"
        profile = json.loads(path.read_text())
        profile["source"]["identity_file"] = "/external/key"
        path.write_text(json.dumps(profile))
        with patch("migratarr.recovery.collect_remote") as collect:
            with self.assertRaisesRegex(ValueError, "resumed runtime"):
                check_source(self.runtime)
            collect.assert_not_called()
        profile["source"]["identity_file"] = str(self.runtime / "auth/source_key")
        path.write_text(json.dumps(profile))
        key = self.runtime / "auth/source_key"
        key.unlink()
        key.symlink_to(self.runtime / "auth/known_hosts")
        with patch("migratarr.recovery.collect_remote") as collect:
            with self.assertRaises(OSError):
                check_source(self.runtime)
            collect.assert_not_called()

    def test_extra_network_rejects_shell_injection_and_duplicate_interfaces(self):
        for value in ("eth1;bad=192.0.2.20/24", "eth0=192.0.2.20/24", "eth1=192.0.2.20/24;bad", "eth1=::1/64", "eth1"):
            with self.assertRaises(ValueError):
                extra_network_commands([value])
        self.assertEqual(extra_network_commands(["eth1=192.0.2.20/24"]),
                         ["ip link set eth1 up", "ip addr add 192.0.2.20/24 dev eth1"])
        with self.assertRaises(ValueError):
            extra_network_commands(["eth1=192.0.2.20/24", "eth1=192.0.2.30/24"])


if __name__ == "__main__":
    unittest.main()
