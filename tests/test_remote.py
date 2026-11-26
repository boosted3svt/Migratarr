import json
import ast
from pathlib import Path
import shlex
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

from migratarr.remote import BOOTSTRAP, SourceConnectionError, collect_remote
from test_migratarr import fixture


def response(report=None, *, returncode=0, stdout=None, stderr=""):
    return types.SimpleNamespace(returncode=returncode,
                                 stdout=json.dumps(report or fixture("source")) if stdout is None else stdout,
                                 stderr=stderr)


class RemoteTests(unittest.TestCase):
    def test_shipped_collector_uses_python38_syntax(self):
        from migratarr.remote import BOOTSTRAP, _payload
        payload = json.loads(_payload(False))
        for text in [BOOTSTRAP, *payload["modules"].values()]:
            ast.parse(text, feature_version=8)

    def test_bootstrap_accepts_python38_and_rejects_older_versions(self):
        from migratarr.remote import BOOTSTRAP
        report = fixture("source")
        payload = {"modules": {"server_survey": "", "mount_review": "", "probes": "", "discovery": "", "inventory": "def collect(*args, **kwargs):\n    return " + repr(report)}, "inspect_docker": False}
        for version, expected in (((3, 8, 10), 0), ((3, 7, 17), 3)):
            process = subprocess.run([sys.executable, "-I", "-B", "-c", "import sys; sys.version_info = " + repr(version) + "\n" + BOOTSTRAP], input=json.dumps(payload), capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, expected, process.stderr)
            if expected == 0:
                self.assertEqual(json.loads(process.stdout), report)
            else:
                self.assertEqual(process.stdout, "")
    def test_strict_ssh_and_in_memory_payload(self):
        with patch("migratarr.remote.subprocess.run", return_value=response()) as run:
            report = collect_remote("source.example", "root", port=2222,
                                    identity_file="/private/key", known_hosts="/private/known hosts",
                                    sudo=True, inspect_docker=True)
        self.assertEqual(report["role"], "source")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["ssh", "-F", "/dev/null", "-T"])
        for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "UpdateHostKeys=no",
                       'UserKnownHostsFile="/private/known hosts"', "GlobalKnownHostsFile=/dev/null",
                       "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
                       "PermitLocalCommand=no", "ClearAllForwardings=yes", "ForwardAgent=no"):
            self.assertIn(option, argv)
        self.assertEqual(argv[-7:-1], ["-p", "2222", "-l", "root", "--", "source.example"])
        self.assertEqual(shlex.split(argv[-1])[:7], ["sudo", "-n", "--", "python3", "-I", "-B", "-c"])
        self.assertEqual(shlex.split(argv[-1])[-1], BOOTSTRAP)
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(set(payload["modules"]), {"server_survey", "mount_review", "probes", "discovery", "inventory"})
        self.assertTrue(payload["inspect_docker"])
        self.assertEqual(run.call_args.kwargs["timeout"], 180)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_valid_ipv6_and_default_known_hosts(self):
        with patch("migratarr.remote.subprocess.run", return_value=response()) as run:
            collect_remote("2001:db8::1", "admin")
        argv = run.call_args.args[0]
        self.assertEqual(argv[-2], "2001:db8::1")
        self.assertIn('UserKnownHostsFile="' + str(Path.home() / ".ssh" / "known_hosts") + '"', argv)

    def test_password_auth_delegates_only_to_openssh_terminal_prompt(self):
        with patch("migratarr.remote._has_terminal", return_value=True), patch("migratarr.remote.subprocess.run", return_value=response()) as run, patch.dict(
            "os.environ", {"SSH_ASKPASS": "/unwanted/helper", "SSH_ASKPASS_REQUIRE": "force"}
        ):
            collect_remote("192.0.2.101", "operator", password_auth=True)
        argv = run.call_args.args[0]
        for option in ("BatchMode=no", "PreferredAuthentications=keyboard-interactive,password",
                       "PubkeyAuthentication=no", "NumberOfPasswordPrompts=1",
                       "PasswordAuthentication=yes", "KbdInteractiveAuthentication=yes",
                       "StrictHostKeyChecking=yes", "UpdateHostKeys=no"):
            self.assertIn(option, argv)
        self.assertNotIn("BatchMode=yes", argv)
        self.assertEqual(run.call_args.kwargs["env"]["SSH_ASKPASS_REQUIRE"], "never")
        self.assertNotIn("SSH_ASKPASS", run.call_args.kwargs["env"])
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(set(payload), {"modules", "inspect_docker"})

    def test_password_authentication_failure_has_mode_specific_guidance(self):
        with patch("migratarr.remote._has_terminal", return_value=True), patch("migratarr.remote.subprocess.run", return_value=response(returncode=255, stderr="Permission denied secret-token")):
            with self.assertRaises(SourceConnectionError) as error:
                collect_remote("source", "ray", password_auth=True)
        self.assertIn("password authentication failed", str(error.exception))
        self.assertNotIn("secret-token", str(error.exception))

    def test_password_mode_requires_private_terminal_before_connecting(self):
        with patch("migratarr.remote._has_terminal", return_value=False), patch("migratarr.remote.subprocess.run") as run:
            with self.assertRaisesRegex(SourceConnectionError, "requires a local terminal"):
                collect_remote("source", "ray", password_auth=True)
        run.assert_not_called()

    def test_untrusted_arguments_rejected_before_ssh(self):
        for kwargs in ({"host": "-oProxyCommand=bad"}, {"host": "a;touch /tmp/bad"},
                       {"host": "root@host"}, {"host": "host\n"}, {"host": "::%eth0"},
                       {"host": "."}, {"user": "root;bad"}, {"user": "-root"},
                       {"port": 0}, {"port": 65536}, {"port": True},
                       {"known_hosts": "/private/key\nBadConfig=yes"}, {"identity_file": "/key\x00"},
                       {"timeout": float("inf")}, {"timeout": 0}, {"inspect_docker": "yes"},
                       {"password_auth": "secret"}):
            parameters = {"host": "source.example", "user": "root", **kwargs}
            with self.subTest(kwargs=kwargs), patch("migratarr.remote.subprocess.run") as run:
                with self.assertRaises(ValueError):
                    collect_remote(**parameters)
                run.assert_not_called()

    def test_incomplete_inventory_retained(self):
        report = fixture("source")
        report["errors"] = [{"probe": "cron", "error": "PermissionError"}]
        with patch("migratarr.remote.subprocess.run", return_value=response(report, returncode=2)):
            result = collect_remote("source", "admin")
        self.assertEqual(result["errors"], report["errors"])

    def test_failures_are_classified_without_echoing_remote_output(self):
        for result, expected in (
            (response(returncode=255, stderr="Host key verification failed. secret-token\x1b"), "host key verification failed"),
            (response(returncode=255, stderr="Permission denied (publickey). secret-token"), "authentication failed"),
            (response(returncode=1, stderr="sudo: a password is required secret-token"), "privilege elevation failed"),
            (response(returncode=3, stderr="secret-token"), "Python 3.8"),
            (response(returncode=127, stderr="python3: not found secret-token"), "could not start the collector"),
            (response(returncode=255, stderr="connection refused secret-token"), "source collection failed"),
        ):
            with self.subTest(expected=expected), patch("migratarr.remote.subprocess.run", return_value=result):
                with self.assertRaises(SourceConnectionError) as error:
                    collect_remote("source", "root")
                self.assertIn(expected, str(error.exception))
                self.assertNotIn("secret-token", str(error.exception))
                self.assertNotIn("\x1b", str(error.exception))

    def test_invalid_source_reports_are_rejected(self):
        for stdout in ("not JSON secret-token", "[]", json.dumps(fixture("destination")),
                       json.dumps({"schema_version": 1, "role": "source"})):
            with self.subTest(stdout=stdout), patch("migratarr.remote.subprocess.run", return_value=response(stdout=stdout)):
                with self.assertRaisesRegex(SourceConnectionError, "invalid inventory evidence"):
                    collect_remote("source", "root")

    def test_timeouts_and_missing_ssh_are_sanitized(self):
        for failure in (subprocess.TimeoutExpired("ssh", 180, output="secret-token"),
                        FileNotFoundError("secret-token")):
            with self.subTest(failure=type(failure)), patch("migratarr.remote.subprocess.run", side_effect=failure):
                with self.assertRaises(SourceConnectionError) as error:
                    collect_remote("source", "root")
                self.assertNotIn("secret-token", str(error.exception))

    def test_bootstrap_uses_stub_modules_without_writes_or_host_inventory(self):
        report = fixture("source")
        payload = {"modules": {
            "server_survey": "",
            "mount_review": "",
            "probes": "",
            "discovery": "value = 'loaded-first'",
            "inventory": "from .discovery import value\n"
                         "def collect(role, inspect_docker=False):\n"
                         "    assert value == 'loaded-first' and role == 'source' and inspect_docker\n"
                         "    return " + repr(report) + "\n",
        }, "inspect_docker": True}
        process = subprocess.run([sys.executable, "-I", "-B", "-c", BOOTSTRAP],
                                 input=json.dumps(payload), text=True, capture_output=True,
                                 timeout=10, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), report)


if __name__ == "__main__":
    unittest.main()
