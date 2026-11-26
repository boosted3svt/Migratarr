import json
import stat
import subprocess
import types
import unittest
from unittest.mock import patch

from migratarr.discovery import DOCKER_SOCKET, discover


def output(stdout, returncode=0):
    return types.SimpleNamespace(stdout=stdout, returncode=returncode)


class DiscoveryTests(unittest.TestCase):
    def run_discovery(self, responses, docker=False):
        with patch("migratarr.discovery.subprocess.run", side_effect=responses) as run, patch(
            "migratarr.discovery.os.lstat", side_effect=FileNotFoundError
        ):
            result = discover(inspect_docker=docker)
        return result, run

    def base_responses(self):
        return [
            output("installed\tplexmediaserver\tamd64\t1.0\nconfig-files\tremoved\tall\t2\ninstalled\tdata\tall\t3\n"),
            output(json.dumps([{"unit": "backup.timer", "load": "loaded", "active": "active", "sub": "waiting", "description": "secret"}])),
            output(json.dumps([{"unit_file": "backup.service", "state": "enabled", "preset": "enabled", "secret": "hidden"}])),
        ]

    def test_packages_schedulers_filtered_and_docker_skipped(self):
        result, run = self.run_discovery(self.base_responses())
        self.assertEqual(result["package_architectures"], ["all", "amd64"])
        self.assertEqual(len(result["packages"]), 2)
        self.assertEqual(result["timers"][0]["unit"], "backup.timer")
        self.assertFalse(result["docker"]["requested"])
        self.assertEqual(run.call_count, 3)
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(result["errors"], [])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 20)
            self.assertNotIn("shell", call.kwargs)

    def test_missing_tools_do_not_establish_empty_inventory(self):
        result, _ = self.run_discovery([FileNotFoundError(), FileNotFoundError(), FileNotFoundError()])
        self.assertIsNone(result["packages"])
        self.assertIsNone(result["package_architectures"])
        self.assertIsNone(result["timers"])
        self.assertIsNone(result["unit_files"])
        self.assertEqual(len(result["errors"]), 3)

    def test_invalid_shapes_and_nonzero_exit_are_errors(self):
        result, _ = self.run_discovery([output("bad"), output("{}"), output("[]", returncode=1), output("", returncode=1)])
        self.assertEqual({entry["probe"] for entry in result["errors"]}, {"packages", "timers", "unit_files"})
        self.assertIsNone(result["timers"])
        result, _ = self.run_discovery([output(""), output('[{"unit":null}]'), output("null")])
        self.assertEqual({entry["probe"] for entry in result["errors"]}, {"timers", "unit_files"})

    def test_timeout_does_not_include_command_output(self):
        result, _ = self.run_discovery([
            subprocess.TimeoutExpired("dpkg-query", 20, output="private"),
            output("[]"), output("[]"),
        ])
        self.assertEqual(result["errors"][0]["error"], "command timed out")
        self.assertNotIn("private", json.dumps(result))

    def test_docker_allowlist_and_local_endpoint(self):
        responses = self.base_responses() + [
            output(json.dumps({"ServerVersion": "27", "Driver": "overlay2", "DockerRootDir": "/var/lib/docker",
                               "DriverStatus": [["Backing Filesystem", "extfs"], ["private", "secret"]],
                               "RegistryConfig": {"token": "secret"}, "Name": "private-host"})),
            output(json.dumps({"ID": "123", "Image": "plex", "Names": "plex", "State": "running", "Command": "secret", "Mounts": "secret"})),
        ]
        result, run = self.run_discovery(responses, docker=True)
        self.assertEqual(result["docker"]["info"]["DriverStatus"], [["Backing Filesystem", "extfs"]])
        self.assertEqual(result["docker"]["containers"], [{"ID": "123", "Image": "plex", "Names": "plex", "State": "running"}])
        self.assertNotIn("secret", json.dumps(result))
        for call in run.call_args_list[-2:]:
            self.assertEqual(call.args[0][:3], ["docker", "--host", DOCKER_SOCKET])

    def test_malformed_docker_output_is_not_absence(self):
        for info, containers in (("[]", "[]"), ('{"Driver": "overlay2"}', '{"ID": 2}')):
            result, _ = self.run_discovery(self.base_responses() + [output(info), output(containers)], docker=True)
            self.assertIsNone(result["docker"]["info"])
            self.assertIsNone(result["docker"]["containers"])
            self.assertEqual({e["probe"] for e in result["errors"]}, {"docker_info", "docker_containers"})

    def test_cron_entries_only_and_symlink_directories_rejected(self):
        class Directory:
            def __enter__(self):
                return iter([types.SimpleNamespace(name="backup"), types.SimpleNamespace(name=".placeholder")])

            def __exit__(self, *args):
                return False

        def lstat(path):
            if path == "/etc/cron.d":
                return types.SimpleNamespace(st_mode=stat.S_IFDIR)
            if path == "/etc/cron.daily":
                return types.SimpleNamespace(st_mode=stat.S_IFLNK)
            if path == "/var/spool/cron/crontabs":
                raise PermissionError
            raise FileNotFoundError

        with patch("migratarr.discovery.subprocess.run", side_effect=self.base_responses()), patch(
            "migratarr.discovery.os.lstat", side_effect=lstat
        ), patch("migratarr.discovery.os.scandir", return_value=Directory()) as scan:
            result = discover()
        self.assertEqual(result["cron"]["/etc/cron.d"]["entry_names"], [".placeholder", "backup"])
        self.assertIsNone(result["cron"]["/etc/cron.daily"])
        self.assertIsNone(result["cron"]["/var/spool/cron/crontabs"])
        scan.assert_called_once_with("/etc/cron.d")


if __name__ == "__main__":
    unittest.main()
