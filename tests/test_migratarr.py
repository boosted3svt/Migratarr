import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from migratarr.assessment import assess
from migratarr.cli import main
from migratarr.inventory import collect, parse_mountinfo


MOUNTS = """24 1 8:1 / / rw,relatime - ext4 /dev/sda1 rw
25 24 8:1 /data /srv/bind rw - ext4 /dev/sda1 rw
26 24 0:42 / /mnt/nas rw - nfs4 host:/secret rw
27 24 0:43 / /run rw - tmpfs tmpfs rw
28 24 8:2 / /media/local\\040disk rw - xfs /dev/sda2 rw
"""


def fixture(role):
    return {"schema_version": 1, "role": role,
            "system": {"architecture": "x86_64", "userspace_bits": 64,
                       "os": {"ID": "ubuntu", "VERSION_ID": "24.04"}},
            "mounts": parse_mountinfo(MOUNTS), "space": [], "errors": [], "limitations": []}


class MountTests(unittest.TestCase):
    def test_boundaries_and_mount_escape(self):
        mounts = parse_mountinfo(MOUNTS)
        self.assertTrue(mounts[0]["shared_device"])
        self.assertTrue(mounts[1]["subtree_or_bind_candidate"])
        self.assertEqual(mounts[2]["kind"], "remote")
        self.assertEqual(mounts[3]["kind"], "pseudo")
        self.assertEqual(mounts[4]["target"], "/media/local disk")
        self.assertNotIn("secret", json.dumps(mounts))

    def test_unknown_fails_closed(self):
        mounts = parse_mountinfo("1 0 0:1 / / rw - fuse.custom secret rw")
        self.assertEqual(mounts[0]["kind"], "unknown")

    def test_malformed_mounts_rejected(self):
        with self.assertRaises(ValueError):
            parse_mountinfo("not mountinfo")

    def test_namespace_mount_roots_are_not_filesystem_paths(self):
        report = fixture("source")
        report["mounts"] = parse_mountinfo("29 24 0:1 net:[4026532668] /run/docker/netns/example rw - nsfs nsfs rw")
        result = assess(report, fixture("destination"))
        self.assertEqual(report["mounts"][0]["kind"], "pseudo")
        self.assertFalse(result["migration_ready"])


class AssessmentTests(unittest.TestCase):
    def test_ignore_source_mount_recomputes_duplicates_without_changing_inventory(self):
        source = fixture("source")
        source["mounts"][1]["target"] = "/mnt/local-root"
        source["mounts"][1]["root"] = "/"
        original = copy.deepcopy(source)
        result = assess(source, fixture("destination"), ignored_source_mounts=["/mnt/local-root"])
        self.assertEqual(source, original)
        rows = result["mount_review"]["source"]
        self.assertNotIn("/mnt/local-root", [row["target"] for row in rows])
        self.assertNotIn("source_mount_mapping", {f["code"] for f in result["findings"]})
        self.assertEqual(result["ignored_source_mounts"], ["/mnt/local-root"])

    def test_known_generated_mounts_are_reviewed_and_bootstrap_mounts_do_not_block(self):
        source, destination = fixture("source"), fixture("destination")
        extra = parse_mountinfo("29 24 7:0 / /snap/example/1 ro - squashfs /dev/loop0 ro\n30 24 0:42 / /var/lib/docker/overlay2/example/merged rw - overlay overlay rw\n31 24 0:43 / /sys/fs/bpf rw - bpf bpf rw")
        for mount in extra:
            mount["kind"] = "unknown"  # Original saved inventory before classification update.
        source["mounts"].extend(extra)
        destination["mounts"].extend(extra)
        result = assess(source, destination)
        codes = {f["code"] for f in result["findings"]}
        self.assertNotIn("source_unknown_mounts", codes)
        self.assertNotIn("destination_unknown_mounts", codes)
        self.assertIn("source_image_backing_review", codes)
        self.assertIn("source_layered_backing_review", codes)
        self.assertFalse(result["migration_ready"])

    def test_ignored_mounts_reject_root_relative_and_control_characters(self):
        for path in ("/", "/mnt/..", "relative", "/mnt/secret\n"):
            with self.assertRaises(ValueError):
                assess(fixture("source"), fixture("destination"), ignored_source_mounts=[path])

    def test_probe_findings_include_failed_probe_and_path(self):
        source = fixture("source")
        source["errors"] = [{"probe": "cron", "error": "PermissionError", "path": "/var/spool/cron/crontabs"}]
        result = assess(source, fixture("destination"))
        finding = next(f for f in result["findings"] if f["code"] == "source_probe_errors")
        self.assertIn("cron: PermissionError at /var/spool/cron/crontabs", finding["message"])

    def test_never_execution_ready(self):
        result = assess(fixture("source"), fixture("destination"))
        self.assertFalse(result["migration_ready"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("source_mount_mapping", {f["code"] for f in result["findings"]})

    def test_architecture_and_bitness_must_both_match(self):
        for field, value in (("architecture", "aarch64"), ("userspace_bits", 32)):
            destination = fixture("destination")
            destination["system"][field] = value
            result = assess(fixture("source"), destination)
            self.assertIn("architecture_mismatch", {f["code"] for f in result["findings"]})

    def test_missing_evidence_and_unknown_root_block(self):
        source = fixture("source")
        source["errors"] = [{"probe": "lsblk", "error": "unavailable"}]
        source["mounts"][0]["kind"] = "unknown"
        codes = {f["code"] for f in assess(source, fixture("destination"))["findings"]}
        self.assertTrue({"source_probe_errors", "source_root_unqualified", "source_unknown_mounts"} <= codes)

    def test_invalid_inventories_rejected(self):
        for value in ([], {}, {"schema_version": 2}, fixture("destination")):
            with self.assertRaises(ValueError):
                assess(value, fixture("destination"))
        for field, value in (("mounts", [{}]), ("errors", None), ("system", {})):
            source = copy.deepcopy(fixture("source"))
            source[field] = value
            with self.assertRaises(ValueError):
                assess(source, fixture("destination"))


class CollectorTests(unittest.TestCase):
    def setUp(self):
        # Python 3.8's platform module may invoke uname through subprocess.
        # Keep these tests focused on Migratarr's explicit inventory probes.
        for field, value in (("machine", "x86_64"), ("release", "test-kernel")):
            mocked = patch("migratarr.inventory.platform." + field, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_missing_tools_reported_and_no_remote_stat(self):
        reads = {"/etc/os-release": 'ID=ubuntu\nVERSION_ID="24.04"\n',
                 "/proc/self/mountinfo": MOUNTS, "/proc/meminfo": "MemTotal: 1024 kB\n"}

        def fake_open(path, **kwargs):
            return io.StringIO(reads[path])

        with patch("builtins.open", side_effect=fake_open), patch("migratarr.inventory.subprocess.run", side_effect=FileNotFoundError) as run, patch("migratarr.inventory.os.statvfs", side_effect=PermissionError) as stat, patch("migratarr.inventory.discover", return_value={"errors": [], "limitations": []}), patch("migratarr.inventory.survey", return_value={"errors": [], "limitations": []}):
            result = collect("source")
        self.assertEqual(result["system"]["memory_bytes"], 1048576)
        self.assertIsNone(result["services"])
        self.assertTrue(result["errors"])
        self.assertEqual({call.args[0] for call in stat.call_args_list}, {"/", "/srv/bind", "/media/local disk"})
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ["lsblk", "ip", "systemctl"])

    def test_destination_skips_all_application_discovery(self):
        reads = {"/etc/os-release": "ID=ubuntu\n", "/proc/self/mountinfo": "", "/proc/meminfo": "MemTotal: 1024 kB\n"}
        with patch("builtins.open", side_effect=lambda path, **kwargs: io.StringIO(reads[path])), patch("migratarr.inventory.subprocess.run", side_effect=FileNotFoundError) as run, patch("migratarr.inventory.discover") as discover, patch("migratarr.inventory.survey") as survey:
            report = collect("destination")
        discover.assert_not_called()
        survey.assert_not_called()
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ["lsblk", "ip"])
        self.assertIsNone(report["services"])
        self.assertIsNone(report["discovery"])
        self.assertEqual(report["collection_scope"], "destination_infrastructure")

    def test_destination_docker_option_rejected_before_probes(self):
        with patch("migratarr.inventory.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "only to --role source"):
                collect("destination", inspect_docker=True)
        run.assert_not_called()


class CliTests(unittest.TestCase):
    def test_offline_assessment_exit_and_output(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for role in ("source", "destination"):
                path = Path(directory) / f"{role}.json"
                path.write_text(json.dumps(fixture(role)))
                paths.append(str(path))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["assess", "--source", paths[0], "--destination", paths[1]])
            self.assertEqual(code, 2)
            self.assertFalse(json.loads(output.getvalue())["migration_ready"])

    def test_invalid_json_has_no_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text("{")
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                code = main(["assess", "--source", str(path), "--destination", str(path)])
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", error.getvalue())


if __name__ == "__main__":
    unittest.main()
