import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def fixture_inventory(self, role):
        gib = 1024 ** 3
        return {
            "schema_version": 1, "role": role,
            "system": {"os": {"ID": "fixture", "PRETTY_NAME": "Example " + role + " OS"},
                       "architecture": "x86_64", "userspace_bits": 64, "boot_mode": "uefi"},
            "mounts": [{"target": "/", "device": "8:1", "root": "/", "filesystem": "ext4",
                        "kind": "local", "shared_device": False, "subtree_or_bind_candidate": False}],
            "space": [{"target": "/", "used_bytes": 30 * gib, "total_bytes": 100 * gib,
                       "available_bytes": 80 * gib}],
            "block_devices": {"blockdevices": [{"name": "fixture-disk", "type": "disk", "children": [
                {"name": "fixture-root", "type": "part", "mountpoints": ["/"],
                 "uuid": role + "-fixture-uuid", "partuuid": role + "-fixture-partuuid"}]}]},
            "services": [], "discovery": {"packages": [], "timers": [], "docker": None},
            "errors": [], "limitations": [],
        }

    def environment(self):
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        return environment

    def build(self, directory):
        archive = Path(directory) / "migratarr.pyz"
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "build.py"),
                                 "--output", str(archive)], text=True, capture_output=True,
                                env=self.environment(), timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return archive

    def test_checkout_launcher_works_from_another_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([str(ROOT / "migratarr"), "--help"], cwd=directory,
                                    env=self.environment(), text=True, capture_output=True,
                                    timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("wizard", result.stdout)

    def test_archive_contains_only_python_application_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = self.build(directory)
            with zipfile.ZipFile(archive) as source:
                names = {name for name in source.namelist() if not name.endswith("/")}
                expected = {"__main__.py"} | {
                    "migratarr/" + path.name for path in (ROOT / "src" / "migratarr").glob("*.py")
                }
                self.assertEqual(names, expected)
                self.assertTrue(all(name.endswith(".py") for name in names))
            result = subprocess.run([sys.executable, str(archive), "--help"], cwd=directory,
                                    env=self.environment(), text=True, capture_output=True,
                                    timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("wizard", result.stdout)

    def test_archive_preserves_cli_failure_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = self.build(directory)
            result = subprocess.run([sys.executable, str(archive), "assess", "--source",
                                     str(Path(directory) / "missing.json"), "--destination",
                                     str(Path(directory) / "also-missing.json")],
                                    cwd=directory, env=self.environment(), text=True,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn("migratarr:", result.stderr)

    def test_archive_can_load_collector_module_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = self.build(directory)
            # Load the archive as a package source, without invoking any source
            # SSH or destination inventory. The payload must come from the
            # archive itself rather than an adjacent editable checkout.
            program = (
                "import json,sys;sys.path.insert(0,sys.argv[1]);"
                "from migratarr.remote import _payload;"
                "print(json.dumps(sorted(json.loads(_payload(False))['modules'])))"
            )
            result = subprocess.run([sys.executable, "-I", "-c", program, str(archive)],
                                    cwd=directory, env=self.environment(), text=True,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("inventory", json.loads(result.stdout))

    def test_archive_runs_complete_offline_profile_outside_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            archive = self.build(temporary)
            configuration = temporary / "profile"
            configuration.mkdir()
            source = self.fixture_inventory("source")
            destination = self.fixture_inventory("destination")
            (configuration / "source.json").write_text(json.dumps(source))
            (configuration / "destination.json").write_text(json.dumps(destination))
            profile = configuration / "config.json"
            profile.write_text(json.dumps({
                "schema_version": 1,
                "source": {"mode": "inventory", "path": "source.json"},
                "destination": {"mode": "inventory", "path": "destination.json"},
                "output_dir": "results", "ignored_source_mounts": [],
            }))
            profile.chmod(0o600)
            cwd = temporary / "another-directory"
            cwd.mkdir()
            environment = self.environment()
            # The offline run does not need any external programs or host probes.
            environment["PATH"] = ""
            result = subprocess.run([sys.executable, str(archive), "run", "--config",
                                     str(profile), "--non-interactive"], cwd=cwd,
                                    env=environment, text=True, capture_output=True,
                                    timeout=10)
            self.assertEqual(result.returncode, 2, result.stderr)
            runs = list((configuration / "results").iterdir())
            self.assertEqual(len(runs), 1)
            report = runs[0]
            self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o700)
            expected = {"inventory-source.json", "inventory-destination.json", "assessment.json",
                        "plan.json", "summary.txt", "plan.txt"}
            self.assertTrue(expected <= {path.name for path in report.iterdir()})
            for name in expected:
                self.assertEqual(stat.S_IMODE((report / name).stat().st_mode), 0o600)
            self.assertEqual(json.loads((report / "inventory-source.json").read_text()), source)
            self.assertEqual(json.loads((report / "inventory-destination.json").read_text()), destination)
            assessment = json.loads((report / "assessment.json").read_text())
            plan = json.loads((report / "plan.json").read_text())
            self.assertFalse(assessment["migration_ready"])
            self.assertFalse(plan["migration_ready"])
            self.assertEqual(plan["status"], "draft_blocked")
            self.assertEqual(plan["source_os_to_import"], source["system"]["os"])
            self.assertFalse((cwd / "results").exists())

    def test_archive_rejects_secret_profile_fields_before_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            archive = self.build(temporary)
            profile = temporary / "invalid-profile.json"
            profile.write_text(json.dumps({
                "schema_version": 1,
                "source": {"mode": "ssh", "host": "do-not-connect.invalid", "user": "fixture"},
                "output_dir": "results", "password": "secret-value-must-never-be-printed",
            }))
            environment = self.environment()
            environment["PATH"] = ""
            result = subprocess.run([sys.executable, str(archive), "run", "--config",
                                     str(profile), "--non-interactive"], cwd=temporary,
                                    env=environment, text=True, capture_output=True,
                                    timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn("unsupported fields", result.stderr)
            self.assertNotIn("secret-value-must-never-be-printed", result.stdout + result.stderr)
            self.assertNotIn("Collecting", result.stdout)
            self.assertFalse((temporary / "results").exists())


if __name__ == "__main__":
    unittest.main()
