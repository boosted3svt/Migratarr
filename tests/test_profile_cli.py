import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from migratarr.cli import main
from migratarr.profiles import save_profile


class ProfileCliTests(unittest.TestCase):
    def test_missing_headless_profile_stops_before_any_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            with patch("migratarr.cli.run") as run, contextlib.redirect_stderr(io.StringIO()):
                code = main(["run", "--config", str(path), "--non-interactive"])
            self.assertEqual(code, 1)
            run.assert_not_called()
            self.assertFalse(path.exists())

    def test_password_profile_cannot_prompt_in_noninteractive_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            save_profile(path, {"schema_version": 1, "source": {"mode": "ssh", "host": "source.example", "user": "operator", "authentication": "password"}})
            with patch("migratarr.cli.run") as run, contextlib.redirect_stderr(io.StringIO()):
                code = main(["run", "--config", str(path), "--non-interactive"])
            self.assertEqual(code, 1)
            run.assert_not_called()

    def test_key_profile_passes_saved_inputs_without_prompts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            saved = save_profile(path, {"schema_version": 1, "source": {"mode": "ssh", "host": "source.example", "user": "operator", "identity_file": "key"}, "ignored_source_mounts": ["/mnt/duplicate"]})
            with patch("migratarr.cli.run", return_value=2) as run, patch("builtins.input", side_effect=AssertionError("unexpected prompt")), contextlib.redirect_stdout(io.StringIO()):
                code = main(["run", "--config", str(path), "--non-interactive"])
            self.assertEqual(code, 2)
            self.assertEqual(run.call_args.args[0], saved["output_dir"])
            self.assertFalse(run.call_args.kwargs["interactive"])
            self.assertEqual(run.call_args.kwargs["source_host"], "source.example")
            self.assertEqual(run.call_args.kwargs["ignored_source_mounts"], ["/mnt/duplicate"])
            self.assertEqual(run.call_args.kwargs["ssh_options"]["identity_file"], str(Path(directory) / "key"))

    def test_first_interactive_run_sets_up_profile_then_collects(self):
        profile = {"source": {"mode": "inventory", "path": "/fixture/source.json"}, "destination": {"mode": "local"}, "output_dir": "/fixture/reports", "ignored_source_mounts": []}
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdin.isatty", return_value=True), patch("migratarr.cli.setup_profile", return_value=profile) as setup, patch("migratarr.cli.run", return_value=2) as run, contextlib.redirect_stdout(io.StringIO()):
            path = Path(directory) / "missing.json"
            self.assertEqual(main(["run", "--config", str(path)]), 2)
        setup.assert_called_once_with(path)
        self.assertEqual(run.call_args.kwargs["source_path"], "/fixture/source.json")


if __name__ == "__main__":
    unittest.main()
