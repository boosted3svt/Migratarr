import contextlib
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from migratarr.assessment import validate
from migratarr.cli import main
from migratarr.wizard import write_private
from test_migratarr import fixture


class WizardTests(unittest.TestCase):
    def invoke(self, args, destination=None):
        output, error = io.StringIO(), io.StringIO()
        with patch("migratarr.wizard.collect", return_value=destination or fixture("destination")), contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(["wizard", "--non-interactive", *args])
        return code, output.getvalue(), error.getvalue()

    def test_destination_only_reports_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            code, output, error = self.invoke(["--output-dir", directory])
            self.assertEqual(code, 2)
            self.assertEqual(error, "")
            runs = list(Path(directory).iterdir())
            self.assertEqual(len(runs), 1)
            self.assertEqual(stat.S_IMODE(runs[0].stat().st_mode), 0o700)
            inventory_path = runs[0] / "inventory-destination.json"
            validate(json.loads(inventory_path.read_text()), "destination")
            self.assertEqual(stat.S_IMODE(inventory_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((runs[0] / "summary.txt").stat().st_mode), 0o600)
            self.assertIn("BLOCKED", output)
            self.assertFalse((runs[0] / "assessment.json").exists())

    def test_offline_source_comparison_and_reruns_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            source.write_text(json.dumps(fixture("source")))
            output = Path(directory) / "reports"
            args = ["--source", str(source), "--output-dir", str(output)]
            for _ in range(2):
                self.assertEqual(self.invoke(args)[0], 2)
            runs = list(output.iterdir())
            self.assertEqual(len(runs), 2)
            for run in runs:
                result = json.loads((run / "assessment.json").read_text())
                self.assertFalse(result["migration_ready"])
                self.assertEqual(stat.S_IMODE((run / "assessment.json").stat().st_mode), 0o600)
                plan = json.loads((run / "plan.json").read_text())
                self.assertFalse(plan["migration_ready"])
                self.assertEqual(stat.S_IMODE((run / "plan.json").stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE((run / "plan.txt").stat().st_mode), 0o600)

    def test_plan_command_is_offline_and_never_execution_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            destination = Path(directory) / "destination.json"
            source.write_text(json.dumps(fixture("source")))
            destination.write_text(json.dumps(fixture("destination")))
            output = io.StringIO()
            with patch("migratarr.cli.collect_remote") as remote, patch("migratarr.cli.collect") as local, contextlib.redirect_stdout(output):
                code = main(["plan", "--source", str(source), "--destination", str(destination)])
            remote.assert_not_called()
            local.assert_not_called()
            self.assertEqual(code, 2)
            self.assertFalse(json.loads(output.getvalue())["migration_ready"])

    def test_bad_source_does_not_collect_or_create_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            source.write_text(json.dumps(fixture("destination")))
            output = Path(directory) / "reports"
            with patch("migratarr.wizard.collect") as collect:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = main(["wizard", "--source", str(source), "--output-dir", str(output), "--non-interactive"])
            self.assertEqual(code, 1)
            collect.assert_not_called()
            self.assertFalse(output.exists())

    def test_symlink_and_existing_report_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original"
            original.write_text("keep")
            link = Path(directory) / "link"
            link.symlink_to(original)
            for path in (original, link):
                with self.assertRaises(FileExistsError):
                    write_private(path, "replace")
            self.assertEqual(original.read_text(), "keep")

    def test_terminal_control_characters_escaped(self):
        destination = fixture("destination")
        destination["mounts"][0]["target"] = "/escape\x1b[31m"
        with tempfile.TemporaryDirectory() as directory:
            code, output, _ = self.invoke(["--output-dir", directory], destination)
        self.assertEqual(code, 2)
        self.assertNotIn("\x1b", output)
        self.assertIn("\\u001b", output)

    def test_interactive_blank_source_continues_destination_only(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("migratarr.wizard.collect", return_value=fixture("destination")), patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value=""), contextlib.redirect_stdout(io.StringIO()):
                code = main(["wizard", "--output-dir", directory])
            self.assertEqual(code, 2)

    def test_ssh_source_saved_privately_and_compared(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("migratarr.wizard.collect_remote", return_value=fixture("source")) as remote:
                code, output, error = self.invoke(["--source-host", "192.0.2.101", "--source-user", "operator", "--password-auth", "--inspect-source-docker", "--output-dir", directory])
            self.assertEqual(code, 2)
            self.assertEqual(error, "")
            remote.assert_called_once()
            self.assertEqual(remote.call_args.args, ("192.0.2.101", "operator"))
            self.assertTrue(remote.call_args.kwargs["password_auth"])
            self.assertTrue(remote.call_args.kwargs["inspect_docker"])
            report = next(Path(directory).iterdir()) / "inventory-source.json"
            validate(json.loads(report.read_text()), "source")
            self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
            self.assertIn("Comparison blockers", output)

    def test_failed_ssh_does_not_collect_destination_or_create_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "reports"
            with patch("migratarr.wizard.collect_remote", side_effect=ValueError("SSH authentication failed")), patch("migratarr.wizard.collect") as collect, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = main(["wizard", "--source-host", "source", "--source-user", "ray", "--non-interactive", "--output-dir", str(target)])
            self.assertEqual(code, 1)
            collect.assert_not_called()
            self.assertFalse(target.exists())

    def test_missing_source_user_and_orphan_ssh_options_rejected(self):
        for options in (["--source-host", "source"], ["--password-auth"], ["--sudo"], ["--source-user", "ray"]):
            with patch("migratarr.wizard.collect_remote") as remote, patch("migratarr.wizard.collect") as collect, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = main(["wizard", "--non-interactive", *options])
            self.assertEqual(code, 1)
            remote.assert_not_called()
            collect.assert_not_called()

    def test_source_ssh_json_output_and_incomplete_exit(self):
        source = fixture("source")
        source["errors"] = [{"probe": "cron", "error": "PermissionError"}]
        output = io.StringIO()
        with patch("migratarr.cli.collect_remote", return_value=source), contextlib.redirect_stdout(output):
            code = main(["source-ssh", "--host", "source", "--user", "ray"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue()), source)


if __name__ == "__main__":
    unittest.main()
