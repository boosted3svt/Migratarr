import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from migratarr.transfer import failure_reason, save_transfer_checkpoint, clean_scratch
from migratarr.transfer_state import tree_fingerprint
from migratarr.recovery import _write_json


class SourceStabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "file").write_bytes(b"original")

    def test_reads_do_not_change_fingerprint(self):
        before = tree_fingerprint(self.root)
        (self.root / "file").read_bytes()
        self.assertEqual(before, tree_fingerprint(self.root))

    def test_same_size_same_mtime_mutation_still_changes_fingerprint(self):
        path = self.root / "file"
        info = path.stat()
        before = tree_fingerprint(self.root)
        path.write_bytes(b"modified")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertNotEqual(before, tree_fingerprint(self.root))

    def test_excluded_mount_contents_and_external_symlink_are_not_scanned(self):
        mount = self.root / "mount"
        mount.mkdir()
        (mount / "external").write_bytes(b"old")
        (self.root / "link").symlink_to(mount / "external")
        before = tree_fingerprint(self.root, [str(mount)])
        (mount / "external").write_bytes(b"new contents")
        self.assertEqual(before, tree_fingerprint(self.root, [str(mount)]))

    def test_entry_limit_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "entry limit"):
            tree_fingerprint(self.root, max_entries=1)


class TransferFailureTests(unittest.TestCase):
    def test_orphan_cleanup_is_confined_and_refuses_unexpected_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scratch = root / "scratch"
            scratch.mkdir(mode=0o700)
            outside = root / "protected"
            outside.write_text("keep")
            orphan = scratch / ".partial-file"
            orphan.write_text("partial")
            (scratch / "link").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "unexpected object"):
                clean_scratch(scratch)
            self.assertTrue(orphan.exists())
            self.assertEqual(outside.read_text(), "keep")
            (scratch / "link").unlink()
            self.assertEqual(clean_scratch(scratch), 1)
            self.assertEqual(list(scratch.iterdir()), [])
            self.assertEqual(outside.read_text(), "keep")

    def test_linked_scratch_file_cannot_change_protected_hardlink_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scratch = root / "scratch"
            scratch.mkdir(mode=0o700)
            outside = root / "protected"
            outside.write_text("keep")
            os.link(outside, scratch / "linked")
            with self.assertRaises(ValueError):
                clean_scratch(scratch)
            self.assertEqual(outside.stat().st_nlink, 2)

    def test_checkpoint_survives_enospc_by_releasing_reserve(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reserve = root / "reserve"
            reserve.write_bytes(b"reserved")
            reserve.chmod(0o600)
            checkpoint = root / "transfer.json"
            report = {"phase": "failed", "migration_ready": False}
            attempts = []

            def write(path, document, replace=False):
                attempts.append(reserve.exists())
                if reserve.exists():
                    raise OSError(errno.ENOSPC, "No space left on device")
                _write_json(path, document, replace)

            with patch("migratarr.transfer._write_json", side_effect=write):
                save_transfer_checkpoint(checkpoint, report, reserve)
            self.assertEqual(attempts, [True, False])
            saved = json.loads(checkpoint.read_text())
            self.assertEqual(saved["phase"], "failed")
            self.assertFalse(saved["migration_ready"])
            self.assertTrue(saved["checkpoint_reserve_released"])

    def test_unrelated_checkpoint_io_error_is_not_treated_as_full_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reserve = root / "reserve"
            reserve.write_bytes(b"keep")
            with patch("migratarr.transfer._write_json", side_effect=OSError(errno.EIO, "I/O error")):
                with self.assertRaises(OSError):
                    save_transfer_checkpoint(root / "transfer.json", {}, reserve)
            self.assertTrue(reserve.exists())

    def test_diagnostics_distinguish_space_connection_and_source_failures(self):
        self.assertEqual(failure_reason(11, "write failed: No space left on device (28)"), "space_exhausted")
        self.assertEqual(failure_reason(12, "connection unexpectedly closed"), "connection_lost")
        self.assertEqual(failure_reason(24, "some files vanished"), "source_changed")
        self.assertEqual(failure_reason(23, "permission denied"), "transfer_error")


if __name__ == "__main__":
    unittest.main()
