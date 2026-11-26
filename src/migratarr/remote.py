"""Collect source metadata through verified SSH without installing source files."""

import ipaddress
import json
import math
import os
import pkgutil
from pathlib import Path
import re
import shlex
import subprocess
import textwrap

from .assessment import validate


class SourceConnectionError(ValueError):
    """A sanitized connection/collector failure suitable for displaying to a user."""


BOOTSTRAP = textwrap.dedent("""\
    import json, sys, types
    sys.dont_write_bytecode = True
    if sys.version_info < (3, 8):
        sys.exit(3)
    payload = json.load(sys.stdin)
    package = types.ModuleType('migratarr')
    package.__path__ = []
    sys.modules['migratarr'] = package
    for name in ('probes', 'mount_review', 'server_survey', 'discovery', 'inventory'):
        fullname = 'migratarr.' + name
        module = types.ModuleType(fullname)
        module.__package__ = 'migratarr'
        sys.modules[fullname] = module
        exec(compile(payload['modules'][name], '<migratarr:' + name + '>', 'exec'), module.__dict__)
    report = sys.modules['migratarr.inventory'].collect('source', inspect_docker=payload['inspect_docker'])
    print(json.dumps(report, sort_keys=True))
    sys.exit(2 if report['errors'] else 0)
""")


def _host(value):
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError("source host must be a DNS name, IPv4 address or IPv6 address")
    if ":" in value:
        try:
            # Scoped IPv6 addresses introduce extra interface semantics; leave
            # those outside the first supported connector.
            if "%" in value:
                raise ValueError
            ipaddress.IPv6Address(value)
        except ValueError:
            raise ValueError("source host is not a supported IPv6 address") from None
    elif not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                 for label in value.rstrip(".").split(".")):
        raise ValueError("source host must be a DNS name or IP address, without a username or port")
    return value


def _path(value):
    try:
        result = str(Path(value).expanduser().absolute())
    except (TypeError, ValueError):
        raise ValueError("SSH file paths must be local filesystem paths") from None
    if any(ord(char) < 32 or ord(char) == 127 for char in result):
        raise ValueError("SSH file paths must not contain control characters")
    return result


def _payload(inspect_docker):
    modules = {}
    for name in ("probes", "mount_review", "server_survey", "discovery", "inventory"):
        data = pkgutil.get_data("migratarr", name + ".py")
        if data is None:
            raise ValueError("Collector modules are missing; use a complete Migratarr distribution")
        modules[name] = data.decode("utf-8")
    return json.dumps({"modules": modules, "inspect_docker": inspect_docker})


def _has_terminal():
    try:
        descriptor = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        os.close(descriptor)
        return True
    except OSError:
        return False


def _failure(returncode, stderr, password_auth=False):
    # Match diagnostics only to choose a fixed explanation. Never display
    # arbitrary remote output, which may contain tokens or terminal controls.
    diagnostic = (stderr or "").lower()
    if "host key verification failed" in diagnostic or "remote host identification has changed" in diagnostic:
        return "SSH host key verification failed. Verify the source fingerprint through a trusted channel and configure known_hosts before retrying."
    if "permission denied" in diagnostic or "authentication failed" in diagnostic:
        if password_auth:
            return "SSH password authentication failed. Check the source account and enter its password in the SSH terminal prompt."
        return "SSH authentication failed. Use an authorized key or SSH agent, or explicitly enable terminal password authentication."
    if "sudo:" in diagnostic:
        return "Source privilege elevation failed. The optional sudo flow requires existing noninteractive sudo authorization."
    if returncode == 3:
        return "Source Python 3.8 or later is required."
    if returncode == 127:
        return "The source could not start the collector. Check that python3 and any requested sudo command are available."
    return "SSH source collection failed. Check connectivity, the verified host key, key authentication and source Python availability."


def collect_remote(host, user, *, port=22, identity_file=None, known_hosts=None,
                   sudo=False, inspect_docker=False, timeout=180, password_auth=False):
    """Return a validated source inventory, retaining incomplete probe evidence.

    Authentication defaults to key/agent, with strict pre-established host trust.
    Optional password mode lets OpenSSH prompt through the controlling terminal;
    this function never collects or accepts a password. The caller must ensure
    an interactive controlling terminal is available for that mode.
    The source needs Python 3.8+ and a POSIX login shell. Collector modules are
    streamed to an in-memory runtime; no package or temporary files are created.
    SSH/server audit logs and ordinary metadata access may still be recorded.
    """
    host = _host(host)
    if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*\$?", user):
        raise ValueError("source user must be a simple Unix account name")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("source SSH port must be between 1 and 65535")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("source timeout must be a positive finite number")
    if any(type(option) is not bool for option in (sudo, inspect_docker, password_auth)):
        raise ValueError("source collector options must be boolean")
    if password_auth and not _has_terminal():
        raise SourceConnectionError("Password authentication requires a local terminal. Run this command in your terminal so SSH can prompt privately; do not send the password in chat.")
    hosts_path = _path(known_hosts if known_hosts is not None else Path.home() / ".ssh" / "known_hosts")
    # ssh -o values use ssh_config tokenization, even when subprocess receives
    # an argument array. Quote paths there as well as quoting the remote command.
    quoted_hosts = '"' + hosts_path.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'
    options = [
        "BatchMode=no" if password_auth else "BatchMode=yes",
        "StrictHostKeyChecking=yes", "UpdateHostKeys=no",
        "UserKnownHostsFile=" + quoted_hosts, "GlobalKnownHostsFile=/dev/null",
        "ConnectTimeout=10", "ConnectionAttempts=1", "ServerAliveInterval=15",
        "ServerAliveCountMax=2", "ClearAllForwardings=yes", "ForwardAgent=no",
        "ForwardX11=no", "PermitLocalCommand=no", "ControlMaster=no",
        "ControlPath=none", "RequestTTY=no",
        "PasswordAuthentication=yes" if password_auth else "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=yes" if password_auth else "KbdInteractiveAuthentication=no",
        "PubkeyAuthentication=no" if password_auth else "PubkeyAuthentication=yes",
        "NumberOfPasswordPrompts=1" if password_auth else "NumberOfPasswordPrompts=0",
        "LogLevel=ERROR", "AddKeysToAgent=no",
        "CanonicalizeHostname=no", "CheckHostIP=no",
    ]
    if password_auth:
        options.append("PreferredAuthentications=keyboard-interactive,password")
    argv = ["ssh", "-F", "/dev/null", "-T"]
    for option in options:
        argv.extend(["-o", option])
    if identity_file is not None:
        argv.extend(["-i", _path(identity_file), "-o", "IdentitiesOnly=yes"])
    remote_command = ("sudo -n -- " if sudo else "") + "python3 -I -B -c " + shlex.quote(BOOTSTRAP)
    argv.extend(["-p", str(port), "-l", user, "--", host, remote_command])
    environment = {**os.environ, "LC_ALL": "C", "SSH_ASKPASS_REQUIRE": "never"}
    environment.pop("SSH_ASKPASS", None)
    try:
        result = subprocess.run(argv, input=_payload(inspect_docker), capture_output=True,
                                text=True, timeout=timeout, check=False,
                                env=environment)
    except subprocess.TimeoutExpired:
        raise SourceConnectionError("SSH source collection timed out; no migration actions were requested.") from None
    except OSError:
        raise SourceConnectionError("Could not start the local SSH client. Check that OpenSSH is installed and executable.") from None
    except UnicodeError:
        raise SourceConnectionError("SSH source collection returned invalid text output.") from None
    if result.returncode not in (0, 2):
        raise SourceConnectionError(_failure(result.returncode, result.stderr, password_auth))
    try:
        report = json.loads(result.stdout)
        validate(report, "source")
    except (ValueError, TypeError, KeyError, RecursionError):
        raise SourceConnectionError("SSH source collection returned invalid inventory evidence. No migration readiness can be inferred.") from None
    return report
