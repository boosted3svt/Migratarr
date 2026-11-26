import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("migratarr_installer", ROOT / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def test_install_update_and_run_without_checkout_or_pythonpath(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "prefix with spaces"
            target = installer.install(prefix)
            before = json.loads((prefix / "share/migratarr/install.json").read_text())
            self.assertEqual(before["sha256"], installer.fingerprint(target))
            receipt = prefix / "keep-existing-profile.json"
            receipt.write_text("keep")
            installer.install(prefix)
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
            result = subprocess.run([str(target), "--help"], cwd="/", env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(receipt.read_text(), "keep")

    def test_unmanaged_modified_and_symlink_commands_are_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "prefix"
            target = installer.install(prefix)
            target.write_text("edited by operator")
            with self.assertRaisesRegex(ValueError, "modified"):
                installer.install(prefix)
            self.assertEqual(target.read_text(), "edited by operator")
            target.unlink()
            outside = Path(directory) / "outside"
            outside.write_text("keep")
            target.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "unmanaged"):
                installer.install(prefix)
            self.assertEqual(outside.read_text(), "keep")

    def test_receipt_failure_restores_existing_command(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "prefix"
            target = installer.install(prefix)
            original = target.read_bytes()
            receipt = prefix / "share/migratarr/install.json"
            original_receipt = receipt.read_bytes()
            replace = os.replace
            def fail_receipt(source, destination):
                if Path(destination) == receipt:
                    raise OSError("simulated receipt failure")
                return replace(source, destination)
            with patch.object(installer.os, "replace", side_effect=fail_receipt):
                with self.assertRaises(OSError):
                    installer.install(prefix)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(receipt.read_bytes(), original_receipt)

    def test_new_directories_are_safe_with_group_writable_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            old = os.umask(0o002)
            try:
                target = installer.install(Path(directory) / "prefix")
            finally:
                os.umask(old)
            self.assertEqual(target.parent.stat().st_mode & 0o022, 0)


if __name__ == "__main__":
    unittest.main()
