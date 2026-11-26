import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from migratarr.path_policy import classify_path, cleanup_candidates, logical_path, rules, scan_paths
from migratarr.planning import build_plan
from migratarr.cleanup import apply_cleanup, preview_cleanup, remove_candidate
from migratarr.recovery import _fingerprint, _write_json
from test_planning import inventory, mount


def policy(boundaries=()):
    return rules([{"source_target": "/", "destination_target": "/", "mount_boundaries": list(boundaries)}], boundaries)


class PathPolicyTests(unittest.TestCase):
    def test_plan_includes_literal_whole_root_rules_without_application_selection(self):
        source, destination = inventory("source"), inventory("destination")
        source["mounts"].append(mount("/var/lib/docker/overlay2/view/merged", "0:55", filesystem="overlay", kind="layered"))
        plan = build_plan(source, destination)
        p = plan["path_policy"]
        self.assertFalse(p["migration_ready"])
        for path in ("/opt/manual-app/bin/run", "/var/lib/docker/volumes/db/data", "/home/operator/.config/app", "/tmp/local-data"):
            self.assertEqual(classify_path(path, p), "copy_candidate")
        self.assertEqual(classify_path("/var/lib/docker/overlay2/view/merged/app", p), "exclude")
        self.assertFalse(p["temporary_space_policy"]["whole_tree_delayed_updates"])
        self.assertFalse(p["temporary_space_policy"]["qualified"])

    def test_protection_includes_ancestors_and_requires_explicit_reconciliation(self):
        p = policy()
        for path in ("/var/lib/migratarr/jobs/a/key", "/boot/migratarr/kernel", "/boot/grub/grub.cfg", "/boot/efi/EFI/ubuntu/shim.efi"):
            self.assertEqual(classify_path(path, p), "protect")
        for path in ("/", "/var", "/var/lib", "/boot", "/etc", "/etc/initramfs-tools"):
            self.assertEqual(classify_path(path, p), "container")
        for path in ("/etc/fstab", "/etc/netplan/00.yaml", "/etc/ssh/ssh_host_ed25519_key", "/etc/machine-id"):
            self.assertEqual(classify_path(path, p), "reconcile")
        self.assertEqual(classify_path("/var/lib/migratarr-old/data", p), "copy_candidate")

    def test_separate_local_filesystem_is_its_own_scope(self):
        source, destination = inventory("source"), inventory("destination")
        for value in (source, destination):
            value["mounts"].append(mount("/home", "8:3"))
        p = build_plan(source, destination)["path_policy"]
        self.assertEqual({scope["destination_target"] for scope in p["copy_scopes"]}, {"/", "/home"})
        self.assertEqual(classify_path("/home", p), "container")
        self.assertEqual(classify_path("/home/user/data", p), "copy_candidate")

    def test_ignored_mounts_and_literal_names_preserve_mountpoints(self):
        p = policy(("/mnt/[share]*", "/mnt/local-root"))
        self.assertEqual(classify_path("/mnt/[share]*", p), "mountpoint")
        self.assertEqual(classify_path("/mnt/[share]*/file", p), "exclude")
        self.assertEqual(classify_path("/mnt/share/file", p), "copy_candidate")
        self.assertEqual(classify_path("/mnt/local-root/etc", p), "exclude")
        for path in ("../etc", "/var/../etc", "/a//b", "/a/./b", "/a\nname"):
            with self.assertRaises(ValueError):
                logical_path(path)

    def test_unmapped_local_mount_is_not_inferred_as_root_copy(self):
        source = inventory("source")
        source["mounts"].append(mount("/srv/local", "8:5"))
        p = build_plan(source, inventory("destination"))["path_policy"]
        self.assertIn("/srv/local", p["unresolved_mounts"])
        self.assertEqual(classify_path("/srv/local/data", p), "exclude")


class CleanupPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()

    def test_preview_retains_protected_paths_and_selects_only_destination_extras(self):
        for root in (self.source, self.target):
            (root / "keep").write_text("keep")
        (self.target / "obsolete").mkdir()
        (self.target / "obsolete/file").write_text("old")
        (self.target / "var/lib/migratarr/jobs").mkdir(parents=True)
        (self.target / "var/lib/migratarr/jobs/key").write_text("protected")
        (self.target / "etc").mkdir()
        (self.target / "etc/fstab").write_text("retain for reconciliation")
        report = cleanup_candidates(scan_paths(self.source), scan_paths(self.target), policy())
        self.assertEqual([row["path"] for row in report["candidates"]], ["/obsolete/file", "/obsolete"])
        self.assertEqual(report["source_collisions"], [])
        self.assertTrue((self.target / "obsolete/file").exists())

    def test_source_recovery_collision_blocks_but_existing_efi_does_not(self):
        for path in ("boot/efi", "var/lib/migratarr"):
            (self.source / path).mkdir(parents=True)
        result = cleanup_candidates(scan_paths(self.source), {}, policy())
        self.assertIn("/var/lib/migratarr", result["source_collisions"])
        self.assertNotIn("/boot/efi", result["source_collisions"])

    def test_file_directory_conflict_is_explicit_rather_than_an_implicit_recursive_delete(self):
        (self.source / "state").write_text("source file")
        (self.target / "state").mkdir()
        (self.target / "state/local").write_text("destination state")
        result = cleanup_candidates(scan_paths(self.source), scan_paths(self.target), policy())
        self.assertEqual(result["type_conflicts"], ["/state"])
        self.assertTrue((self.target / "state/local").exists())

    def test_linked_candidate_requires_review_and_symlink_targets_are_not_scanned(self):
        outside = self.root / "protected"
        outside.write_text("keep")
        os.link(outside, self.target / "linked")
        (self.target / "external-link").symlink_to(self.source)
        result = cleanup_candidates(scan_paths(self.source), scan_paths(self.target), policy())
        self.assertEqual(result["hardlink_conflicts"], ["/linked"])
        self.assertEqual(len(scan_paths(self.target)), 3)

    def test_descriptor_removal_refuses_path_traversal_and_symlink_parent(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        (self.target / "old").mkdir()
        (self.target / "old/file").write_text("old")
        entry = scan_paths(self.target)["/old/file"]
        (self.target / "old/file").unlink()
        (self.target / "old").rmdir()
        (self.target / "old").symlink_to(outside)
        with self.assertRaises(OSError):
            remove_candidate(self.target, {"path": "/old/file", "evidence": entry})
        with self.assertRaises(ValueError):
            remove_candidate(self.target, {"path": "/../outside/keep", "evidence": entry})
        self.assertEqual((outside / "keep").read_text(), "keep")

    def test_leaf_contents_are_checked_even_if_metadata_appears_unchanged(self):
        path = self.target / "obsolete"
        path.write_bytes(b"original")
        expected = scan_paths(self.target)["/obsolete"]
        path.write_bytes(b"modified")
        os.utime(path, ns=(path.stat().st_atime_ns, expected["mtime_ns"]))
        # Simulate a filesystem clock coarsening ctime into the same tick.
        expected["ctime_ns"] = path.stat().st_ctime_ns
        with self.assertRaisesRegex(ValueError, "contents changed"):
            remove_candidate(self.target, {"path": "/obsolete", "evidence": expected})
        self.assertEqual(path.read_bytes(), b"modified")

    def test_namespace_evidence_is_bounded_and_mount_contents_are_not_hashed(self):
        mount = self.target / "mounted"
        mount.mkdir()
        (mount / "not-scanned").write_text("excluded")
        entries = scan_paths(self.target, [str(mount)], max_bytes=0)
        self.assertEqual(set(entries), {"/", "/mounted"})
        (self.target / "ordinary").write_bytes(b"contents")
        with self.assertRaisesRegex(ValueError, "byte limit"):
            scan_paths(self.target, [str(mount)], max_bytes=1)

    def test_cleanup_refuses_physical_host_before_inspection_or_deletion(self):
        with patch("migratarr.cleanup.fixture_context", side_effect=ValueError("physical host refused")), patch("migratarr.cleanup.snapshot") as scan, patch("migratarr.cleanup.remove_candidate") as remove:
            for callback in (lambda: preview_cleanup("a" * 32), lambda: apply_cleanup("a" * 32, "b" * 64)):
                with self.assertRaisesRegex(ValueError, "physical host"):
                    callback()
            scan.assert_not_called()
            remove.assert_not_called()

    def test_exact_digest_and_fresh_evidence_are_required_before_any_removal(self):
        job = self.root / "job"
        job.mkdir(mode=0o700)
        (job / ".lock").write_bytes(b"")
        (job / ".lock").chmod(0o600)
        path = job / "cleanup-preview.json"
        saved = {"schema_version": 1, "status": "preview_ready", "migration_ready": False,
                 "source_fingerprint": "source-before", "destination_fingerprint": "destination-before", "destination_mounts": []}
        _write_json(path, saved)
        digest = _fingerprint(path.read_bytes())
        with patch("migratarr.cleanup.fixture_context", return_value=(job, {}, {})), patch("migratarr.cleanup.remove_candidate") as remove:
            with patch("migratarr.cleanup.snapshot") as capture:
                with self.assertRaisesRegex(ValueError, "digest differs"):
                    apply_cleanup("a" * 32, "0" * 64)
                capture.assert_not_called()
            for field, value in (("source_fingerprint", "source-after"),
                                 ("destination_fingerprint", "destination-after"),
                                 ("destination_mounts", ["/new-mount"])):
                fresh = dict(saved)
                fresh[field] = value
                with patch("migratarr.cleanup.snapshot", return_value=fresh):
                    with self.assertRaisesRegex(ValueError, "stale or blocked"):
                        apply_cleanup("a" * 32, digest)
            remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
