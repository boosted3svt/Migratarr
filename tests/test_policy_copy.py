import os
from pathlib import Path
import tempfile
import unittest

from migratarr.path_policy import rules, scan_paths
from migratarr.policy_copy import copy_filters, validate_copy_paths
from migratarr.transfer import rsync_arguments


def policy(boundaries=()):
    return rules([{"destination_target": "/", "mount_boundaries": list(boundaries)}], boundaries)


class PolicyCopyTests(unittest.TestCase):
    def test_filters_protect_reserved_paths_and_mounts_without_excluding_ordinary_data(self):
        patterns = copy_filters(policy(["/mnt/share", "/mnt/[literal]*"]), ["/mnt/share"])
        for path in ("etc/fstab", "etc/ssh", "boot/grub", "var/lib/migratarr", "mnt/share"):
            self.assertIn("/policy-case/" + path, patterns)
        self.assertIn("/policy-case/proc/*", patterns)
        self.assertIn("/policy-case/mnt/\\[literal\\]\\*/*", patterns)
        self.assertNotIn("/policy-case/tmp", patterns)
        self.assertNotIn("/policy-case/var/lib/docker", patterns)
        source = {"host": "192.0.2.10", "user": "root", "port": 22,
                  "identity_file": "/run/key", "known_hosts": "/run/hosts"}
        args = rsync_arguments(source, Path("/target/fixture"), "a" * 32, [], policy_exclusions=patterns)
        self.assertIn("/policy-case/etc/fstab", args)
        self.assertNotIn("--delete", args)

    def test_redirected_ancestor_and_external_hardlink_are_refused(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "etc").symlink_to("/outside")
            with self.assertRaisesRegex(ValueError, "ancestor"):
                validate_copy_paths({}, scan_paths(root), policy())
            (root / "etc").unlink()
            (root / "managed").write_text("data")
            os.link(root / "managed", root / "managed-link")
            entries = scan_paths(root)
            validate_copy_paths(entries, entries, policy())
            (root / "managed-link").unlink()
            (root / "etc").mkdir()
            os.link(root / "managed", root / "etc/fstab")
            with self.assertRaisesRegex(ValueError, "outside the copy scope"):
                validate_copy_paths({}, scan_paths(root), policy())
