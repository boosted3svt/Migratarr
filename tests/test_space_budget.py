from pathlib import Path
import tempfile
import unittest

from migratarr.space_budget import temporary_budget
from migratarr.transfer_state import largest_fixture_file


class SpaceBudgetTests(unittest.TestCase):
    def test_reserve_and_entire_new_file_must_fit_without_credit_for_old_data(self):
        result = temporary_budget(120, 100, 20)
        self.assertEqual(result["status"], "sufficient")
        self.assertEqual(result["required_bytes"], 120)
        self.assertEqual(result["existing_target_bytes_credited"], 0)
        self.assertFalse(result["whole_tree_staging"])
        self.assertEqual(temporary_budget(119, 100, 20)["status"], "insufficient")

    def test_invalid_evidence_is_rejected(self):
        for value in (-1, True, 1.5, "100", None):
            for position in range(3):
                values = [100, 50, 20]
                values[position] = value
                with self.assertRaises(ValueError):
                    temporary_budget(*values)

    def test_file_evidence_excludes_mount_contents_and_does_not_follow_links(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "file").write_bytes(b"12345")
            excluded = root / "mounted"
            excluded.mkdir()
            (excluded / "larger").write_bytes(b"x" * 100)
            (root / "link").symlink_to(excluded / "larger")
            self.assertEqual(largest_fixture_file(root, [str(excluded)]), 5)
            with self.assertRaisesRegex(ValueError, "entry limit"):
                largest_fixture_file(root, max_entries=1)
