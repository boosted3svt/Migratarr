import json
import subprocess
import types
import unittest
from unittest.mock import patch

from migratarr.probes import block_devices, system_units, unit_files


def output(text, code=0):
    return types.SimpleNamespace(stdout=text, returncode=code)


class LegacyProbeTests(unittest.TestCase):
    def run_probe(self, function, responses, *args):
        errors = []
        with patch("migratarr.probes.subprocess.run", side_effect=responses) as run:
            result = function(*args, errors)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 20)
            self.assertNotIn("shell", call.kwargs)
        return result, errors, run

    def test_modern_blocks_normalize_tree_and_nullable_mountpoints(self):
        data = {"blockdevices": [{"name": "sda", "type": "disk", "size": 100,
                                  "mountpoints": [None], "private": "secret",
                                  "children": [{"name": "sda1", "type": "part", "size": 90, "mountpoints": ["/"]}]}]}
        result, errors, run = self.run_probe(block_devices, [output(json.dumps(data))])
        self.assertEqual(result["blockdevices"][0]["mountpoints"], [])
        self.assertEqual(result["blockdevices"][0]["children"][0]["mountpoints"], ["/"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertFalse(errors)
        self.assertEqual(run.call_count, 1)

    def test_old_lsblk_mountpoint_fallback(self):
        data = {"blockdevices": [{"name": "sda", "type": "disk", "size": "100", "mountpoint": None,
                                  "children": [{"name": "sda1", "type": "part", "size": "90", "mountpoint": "/"}]}]}
        result, errors, run = self.run_probe(block_devices, [output("", 1), output(json.dumps(data))])
        self.assertEqual(result["blockdevices"][0]["children"][0]["mountpoints"], ["/"])
        self.assertEqual(result["blockdevices"][0]["size"], 100)
        self.assertFalse(errors)
        self.assertIn("MOUNTPOINT,", run.call_args_list[1].args[0][-1])

    def test_old_systemd_loaded_units_without_descriptions(self):
        for kind in ("service", "timer"):
            text = "backup.{} loaded active running secret description\n".format(kind)
            result, errors, run = self.run_probe(system_units, [output("", 1), output(text)], kind)
            self.assertEqual(result, [{"unit": "backup." + kind, "load": "loaded", "active": "active", "sub": "running"}])
            self.assertNotIn("secret", json.dumps(result))
            self.assertFalse(errors)
            self.assertIn("--plain", run.call_args_list[1].args[0])
            self.assertIn("--no-legend", run.call_args_list[1].args[0])

    def test_old_systemd_unit_files_two_and_three_columns(self):
        text = "backup.service enabled\nbackup.timer disabled enabled\n"
        result, errors, _ = self.run_probe(unit_files, [output("", 1), output(text)])
        self.assertEqual(result, [{"unit_file": "backup.service", "state": "enabled"}, {"unit_file": "backup.timer", "state": "disabled"}])
        self.assertFalse(errors)

    def test_old_systemd_successful_text_table_retries_without_headers(self):
        for kind in ("service", "timer"):
            text = "backup.{} loaded active running private description\n".format(kind)
            old_output = "  UNIT LOAD ACTIVE SUB DESCRIPTION\n" + text + "\n1 loaded units listed.\n"
            result, errors, run = self.run_probe(system_units, [output(old_output), output(text)], kind)
            self.assertEqual(result, [{"unit": "backup." + kind, "load": "loaded", "active": "active", "sub": "running"}])
            self.assertFalse(errors)
            self.assertEqual(run.call_count, 2)
            self.assertIn("--no-legend", run.call_args_list[1].args[0])
            self.assertNotIn("private", json.dumps(result))

    def test_old_systemd_successful_unit_file_table_retries(self):
        text = "backup.service enabled\nbackup.timer disabled enabled\n"
        for header in ("UNIT FILE STATE", "UNIT FILE STATE VENDOR PRESET"):
            old_output = header + "\n" + text + "\n2 unit files listed.\n"
            result, errors, run = self.run_probe(unit_files, [output(old_output), output(text)])
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0], {"unit_file": "backup.service", "state": "enabled"})
            self.assertFalse(errors)
            self.assertEqual(run.call_count, 2)

    def test_headerless_successful_legacy_metadata_is_recognized(self):
        for function, args, text in (
            (system_units, ("service",), "● failed.service loaded failed failed private description\n"),
            (unit_files, (), "backup.timer enabled enabled\n"),
        ):
            result, errors, run = self.run_probe(function, [output(text), output(text)], *args)
            self.assertEqual(len(result), 1)
            self.assertFalse(errors)
            self.assertEqual(run.call_count, 2)

    def test_successful_legacy_table_with_failed_retry_is_unknown(self):
        result, errors, run = self.run_probe(system_units, [output("UNIT LOAD ACTIVE SUB DESCRIPTION\n"), output("", 1)], "service")
        self.assertIsNone(result)
        self.assertEqual(len(errors), 1)
        self.assertEqual(run.call_count, 2)

    def test_json_systemd_metadata_is_filtered(self):
        for function, args, data, expected in (
            (system_units, ("service",), [{"unit": "backup.service", "load": "loaded", "active": "active", "sub": "running", "description": "secret"}], "unit"),
            (unit_files, (), [{"unit_file": "backup.timer", "state": "enabled", "preset": "secret"}], "unit_file"),
        ):
            result, errors, run = self.run_probe(function, [output(json.dumps(data))], *args)
            self.assertIn(expected, result[0])
            self.assertNotIn("secret", json.dumps(result))
            self.assertFalse(errors)
            self.assertEqual(run.call_count, 1)

    def test_failed_fallback_is_unknown_not_empty(self):
        for function, args in ((block_devices, ()), (system_units, ("service",)), (unit_files, ())):
            result, errors, run = self.run_probe(function, [output("", 1), output("", 1)], *args)
            self.assertIsNone(result)
            self.assertEqual(len(errors), 1)
            self.assertEqual(run.call_count, 2)

    def test_missing_tool_and_timeout_do_not_retry_or_expose_output(self):
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired("systemctl", 20, output="secret")):
            result, errors, run = self.run_probe(system_units, [failure], "timer")
            self.assertIsNone(result)
            self.assertEqual(run.call_count, 1)
            self.assertNotIn("secret", json.dumps(errors))

    def test_malformed_modern_output_does_not_silently_fallback(self):
        for function, args, text in ((block_devices, (), "{}"), (system_units, ("service",), "{}"), (unit_files, (), "not JSON")):
            result, errors, run = self.run_probe(function, [output(text)], *args)
            self.assertIsNone(result)
            self.assertEqual(len(errors), 1)
            self.assertEqual(run.call_count, 1)

    def test_malformed_text_records_fail_closed(self):
        for function, args, text in ((system_units, ("service",), "unexpected heading\n"), (unit_files, (), "UNIT FILE STATE\n")):
            result, errors, _ = self.run_probe(function, [output("", 1), output(text)], *args)
            self.assertIsNone(result)
            self.assertTrue(errors)

    def test_empty_success_is_distinct_from_failure(self):
        result, errors, _ = self.run_probe(system_units, [output("", 1), output("")], "timer")
        self.assertEqual(result, [])
        self.assertFalse(errors)


if __name__ == "__main__":
    unittest.main()
