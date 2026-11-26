from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from migratarr.host_trust import ensure_host_trust


class HostTrustTests(unittest.TestCase):
    def test_known_host_does_not_trigger_authentication_or_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "known_hosts"
            path.write_text("verified")
            source = {"host": "192.0.2.10", "user": "operator", "port": 22, "known_hosts": str(path)}
            with patch("migratarr.host_trust.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
                ensure_host_trust(source)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0], "ssh-keygen")
            self.assertEqual(path.read_text(), "verified")

    def test_first_host_trust_uses_attended_openssh_without_password_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ssh/known_hosts"
            source = {"host": "192.0.2.10", "user": "operator", "port": 2222, "known_hosts": str(path)}
            def run(argv, **kwargs):
                if argv[0] == "ssh":
                    path.write_text("accepted host key")
                    self.assertIn("StrictHostKeyChecking=ask", argv)
                    self.assertIn("PreferredAuthentications=none", argv)
                    self.assertIn("PasswordAuthentication=no", argv)
                    return subprocess.CompletedProcess(argv, 255)
                self.assertIn("[192.0.2.10]:2222", argv)
                return subprocess.CompletedProcess(argv, 0)
            with patch("migratarr.host_trust.subprocess.run", side_effect=run):
                ensure_host_trust(source)
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_rejected_host_does_not_start_assessment(self):
        with tempfile.TemporaryDirectory() as directory:
            source = {"host": "192.0.2.10", "user": "operator", "port": 22, "known_hosts": str(Path(directory) / "known_hosts")}
            with patch("migratarr.host_trust.subprocess.run", return_value=subprocess.CompletedProcess([], 255)):
                with self.assertRaisesRegex(ValueError, "not accepted"):
                    ensure_host_trust(source)


if __name__ == "__main__":
    unittest.main()
