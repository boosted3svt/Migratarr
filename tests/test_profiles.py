import copy
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from migratarr.profiles import load_profile, save_profile, setup_profile


def profile():
    return {"schema_version": 1, "source": {"mode": "ssh", "host": "source.example", "user": "operator"}}


class ProfileTests(unittest.TestCase):
    def test_saved_profile_is_private_normalized_and_repeatable_from_other_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "profile.json"
            data = profile()
            data["source"]["identity_file"] = "keys/id"
            data["source"]["known_hosts"] = "verified_hosts"
            data["ignored_source_mounts"] = ["/mnt/duplicate/", "/mnt/duplicate"]
            original = copy.deepcopy(data)
            normalized = save_profile(path, data)
            self.assertEqual(normalized["source"]["identity_file"], str(path.parent / "keys/id"))
            self.assertEqual(normalized["source"]["known_hosts"], str(path.parent / "verified_hosts"))
            self.assertEqual(normalized["output_dir"], str(path.parent / "reports"))
            self.assertEqual(normalized["ignored_source_mounts"], ["/mnt/duplicate"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(load_profile(path), normalized)
            self.assertEqual(data, original)

    def test_offline_profile_paths_are_relative_to_profile_not_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            data = {"schema_version": 1, "source": {"mode": "inventory", "path": "source.json"},
                    "destination": {"mode": "inventory", "path": "destination.json"}}
            result = save_profile(path, data)
            self.assertEqual(result["source"]["path"], str(Path(directory) / "source.json"))
            self.assertEqual(result["destination"]["path"], str(Path(directory) / "destination.json"))

    def test_existing_files_and_symlinks_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original"
            original.write_text("keep")
            link = Path(directory) / "profile.json"
            link.symlink_to(original)
            for path in (original, link):
                with self.assertRaises(FileExistsError):
                    save_profile(path, profile())
            self.assertEqual(original.read_text(), "keep")

    def test_unknown_fields_and_password_storage_rejected_without_echoing_secret(self):
        for location in ("root", "source", "destination"):
            data = profile()
            if location == "root":
                data["password"] = "private-secret"
            elif location == "source":
                data["source"]["password"] = "private-secret"
            else:
                data["destination"] = {"mode": "local", "token": "private-secret"}
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError) as caught:
                    save_profile(Path(directory) / "profile.json", data)
                self.assertNotIn("private-secret", str(caught.exception))
                self.assertFalse((Path(directory) / "profile.json").exists())

    def test_malformed_schema_types_controls_and_auth_are_rejected(self):
        cases = []
        for key, value in (("port", True), ("port", 0), ("port", 65536), ("sudo", "yes"),
                           ("inspect_docker", 1), ("authentication", "private-secret"),
                           ("identity_file", "/key\nprivate-secret"), ("host", "-oProxyCommand=bad"),
                           ("known_hosts", False), ("known_hosts", ""),
                           ("user", "operator;bad")):
            data = profile()
            data["source"][key] = value
            cases.append(data)
        data = profile(); data["schema_version"] = True; cases.append(data)
        data = profile(); data["ignored_source_mounts"] = ["/"]; cases.append(data)
        data = profile(); data["source"].update(authentication="password", identity_file="key"); cases.append(data)
        cases.extend([[], {}, {"schema_version": 1, "source": []}])
        with tempfile.TemporaryDirectory() as directory:
            for index, data in enumerate(cases):
                with self.subTest(index=index), self.assertRaises(ValueError):
                    save_profile(Path(directory) / str(index), data)

    def test_invalid_json_is_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text('{"password":"private-secret"')
            with self.assertRaisesRegex(ValueError, "invalid JSON") as caught:
                load_profile(path)
            self.assertNotIn("private-secret", str(caught.exception))

    def test_setup_saves_settings_and_never_prompts_for_password(self):
        prompts = []
        replies = iter(["source.example", "operator", "", "password", "", "", "", "/mnt/duplicate", ""])
        def answer(prompt):
            prompts.append(prompt)
            return next(replies)
        with tempfile.TemporaryDirectory() as directory, patch("migratarr.remote.collect_remote") as remote:
            path = Path(directory) / "profile.json"
            result = setup_profile(path, input_fn=answer)
            self.assertEqual(result, load_profile(path))
            self.assertEqual(result["source"]["authentication"], "password")
            self.assertIsNone(result["source"]["identity_file"])
            self.assertEqual(result["ignored_source_mounts"], ["/mnt/duplicate"])
            remote.assert_not_called()
        self.assertFalse(any(prompt.lower().startswith("password") for prompt in prompts))

    def test_setup_defaults_resolve_input_at_call_time(self):
        with tempfile.TemporaryDirectory() as directory, patch("builtins.input", side_effect=["source.example", "operator", "", "", "", "", "", "", "", ""]):
            result = setup_profile(Path(directory) / "profile.json")
        self.assertEqual(result["source"]["authentication"], "key")
        self.assertFalse(result["source"]["sudo"])
        self.assertTrue(result["source"]["inspect_docker"])


if __name__ == "__main__":
    unittest.main()
