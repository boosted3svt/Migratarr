import ast
import json
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from migratarr.transfer import SOURCE, fixture_context, mount_exclusions, rsync_arguments, source_guard, transfer_fixture


class TransferGuardTests(unittest.TestCase):
    def test_insufficient_space_checkpoints_failure_before_launching_rsync(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            job_path = root / "job"
            job_path.mkdir(mode=0o700)
            (job_path / ".lock").touch(mode=0o600)
            job = {"job_id": "11111111-1111-1111-1111-111111111111"}
            source = {"host": "192.0.2.10", "user": "root", "port": 22,
                      "identity_file": "/run/key", "known_hosts": "/run/known_hosts"}
            evidence = {"mounts": [], "fingerprint": "a" * 64, "largest_file_bytes": 100}
            with patch("migratarr.transfer.fixture_context", return_value=(job_path, job, source)), \
                    patch("migratarr.transfer.TARGET_BASE", str(root / "target")), \
                    patch("migratarr.transfer.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(evidence))), \
                    patch("migratarr.transfer.os.statvfs", return_value=SimpleNamespace(f_bavail=0, f_frsize=4096)), \
                    patch("migratarr.transfer.subprocess.Popen") as start:
                result = transfer_fixture("a" * 32)
            start.assert_not_called()
            self.assertEqual(result["failure_reason"], "insufficient_temporary_space")
            self.assertFalse(result["migration_ready"])
            self.assertEqual(json.loads((job_path / "transfer.json").read_text()), result)
            self.assertEqual(list((root / "target" / job["job_id"] / "data").iterdir()), [])

    def test_physical_host_refused_before_any_file_mutation_or_ssh(self):
        with patch("migratarr.transfer.guard_vm", side_effect=ValueError("physical host refused")), patch("migratarr.transfer.subprocess.run") as run, patch("pathlib.Path.mkdir") as mkdir:
            with self.assertRaisesRegex(ValueError, "physical host"):
                transfer_fixture("a" * 32)
            run.assert_not_called()
            mkdir.assert_not_called()

    def test_disk_root_cannot_be_used_instead_of_ram(self):
        with patch("migratarr.transfer.guard_vm"), patch("pathlib.Path.read_text", return_value="/dev/vda1 / ext4 rw 0 0\n"), patch("migratarr.transfer._json") as read:
            with self.assertRaisesRegex(ValueError, "RAM root"):
                fixture_context("a" * 32)
            read.assert_not_called()

    def test_unmounted_target_cannot_silently_write_into_ram(self):
        context = {"job_id": "11111111-1111-1111-1111-111111111111", "target_uuid": "22222222-2222-2222-2222-222222222222", "target_device": "/dev/vda1"}
        with patch("migratarr.transfer.guard_vm"), patch("pathlib.Path.read_text", return_value="rootfs / rootfs rw 0 0\n"), patch("migratarr.transfer._json", return_value=context), patch("migratarr.transfer.os.stat") as stat:
            with self.assertRaisesRegex(ValueError, "mounted ext4"):
                fixture_context("a" * 32)
            stat.assert_not_called()

    def test_mount_exclusions_cover_same_device_binds_and_preserve_mountpoint(self):
        result = mount_exclusions([SOURCE + "/remote", SOURCE + "/bind-duplicate"])
        self.assertIn("/bind-duplicate/*", result)
        self.assertIn("/remote/*", result)
        self.assertNotIn("/remote", result)
        for path in (SOURCE, SOURCE + "/../outside", SOURCE + "/unsafe[glob]", SOURCE + "/space\\040mount"):
            with self.assertRaises(ValueError):
                mount_exclusions([path])

    def test_transfer_options_cannot_delete_or_follow_symlinks_or_select_arbitrary_root(self):
        source = {"host": "192.0.2.10", "user": "root", "port": 22, "identity_file": "/run/key", "known_hosts": "/run/known hosts"}
        argv = rsync_arguments(source, Path("/target/fixture"), "a" * 32, [SOURCE + "/bind-duplicate"])
        for flag in ("--numeric-ids", "--hard-links", "--acls", "--xattrs", "--checksum", "--fsync"):
            self.assertIn(flag, argv)
        for flag in ("--delete", "--copy-links", "--inplace", "--keep-dirlinks", "--delay-updates"):
            self.assertNotIn(flag, argv)
        self.assertEqual(argv[-2:], ["192.0.2.10:" + SOURCE + "/", "/target/fixture/"])
        shell = shlex.split(argv[argv.index("-e") + 1])
        self.assertIn("StrictHostKeyChecking=yes", shell)
        self.assertIn("BatchMode=yes", shell)
        self.assertNotIn("ForwardAgent=yes", shell)
        with self.assertRaises(ValueError):
            rsync_arguments(source, Path("/target"), "a" * 32, [], -1)

    def test_streamed_source_guard_validates_vm_marker_and_mount_snapshot(self):
        code = source_guard("a" * 32, [SOURCE + "/remote"])
        ast.parse(code, feature_version=8)
        self.assertIn("migratarr_transfer_fixture", code)
        self.assertIn("assert mounts==", code)
        self.assertIn("os.execvp('rsync'", code)


if __name__ == "__main__":
    unittest.main()
