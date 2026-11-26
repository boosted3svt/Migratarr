"""Read-only package, scheduler and optional local Docker discovery.

This module records metadata only. It does not read job commands, service
definitions, container environments, application settings or database contents.
"""

import json
import os
import stat
import subprocess

from .probes import system_units, unit_files


CRON_DIRECTORIES = (
    "/etc/cron.d", "/etc/cron.hourly", "/etc/cron.daily",
    "/etc/cron.weekly", "/etc/cron.monthly", "/var/spool/cron/crontabs",
)
DOCKER_SOCKET = "unix:///var/run/docker.sock"
DRIVER_STATUS_KEYS = {
    "Backing Filesystem", "Supports d_type", "Native Overlay Diff",
    "Using metacopy", "userxattr",
}


def _records(value, fields):
    """Reject unexpected shapes rather than presenting malformed data as empty."""
    if not isinstance(value, list):
        raise ValueError("expected record list")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("expected record object")
        if not all(isinstance(item.get(field), str) for field in fields):
            raise ValueError("missing string field")
        result.append({field: item[field] for field in fields})
    return result


def discover(inspect_docker=False):
    """Return metadata and explicit errors; Docker needs a caller's opt-in.

    Docker probes are pinned to the conventional local Unix socket. Environment
    variables and configured contexts cannot silently select a remote daemon.
    Rootless/custom sockets remain outside this initial discovery milestone.
    """
    errors = []

    def error(probe, message, **details):
        errors.append({"probe": probe, "error": message, **details})

    def command(probe, argv, parse):
        try:
            process = subprocess.run(
                argv, capture_output=True, text=True, timeout=20, check=False,
                env={**os.environ, "LC_ALL": "C"},
            )
            if process.returncode:
                error(probe, "command failed", returncode=process.returncode)
                return None
            return parse(process.stdout)
        except subprocess.TimeoutExpired:
            error(probe, "command timed out")
        except OSError as exc:
            error(probe, type(exc).__name__)
        except (ValueError, TypeError, UnicodeError):
            error(probe, "invalid command output")
        return None

    def packages(text):
        result = []
        for line in text.splitlines():
            fields = line.split("\t")
            if len(fields) != 4:
                raise ValueError("invalid package record")
            status, name, architecture, version = fields
            if status != "installed":
                continue
            if not all((name, architecture, version)):
                raise ValueError("incomplete package record")
            result.append({"name": name, "architecture": architecture, "version": version})
        return result

    installed = command("packages", [
        "dpkg-query", "--show",
        "--showformat=${db:Status-Status}\t${Package}\t${Architecture}\t${Version}\n",
    ], packages)
    timers = system_units("timer", errors)
    installed_unit_files = unit_files(errors)

    cron = {}
    for path in CRON_DIRECTORIES:
        try:
            mode = os.lstat(path).st_mode
            if not stat.S_ISDIR(mode):
                error("cron", "directory is a symlink or not a directory", path=path)
                cron[path] = None
                continue
            # No recursion, stat of individual entries or job-file reads.
            with os.scandir(path) as entries:
                cron[path] = {"present": True, "entry_names": sorted(entry.name for entry in entries)}
        except FileNotFoundError:
            cron[path] = {"present": False, "entry_names": []}
        except OSError as exc:
            cron[path] = None
            error("cron", type(exc).__name__, path=path)
    try:
        cron["/etc/crontab"] = {"present": bool(os.lstat("/etc/crontab"))}
    except FileNotFoundError:
        cron["/etc/crontab"] = {"present": False}
    except OSError as exc:
        cron["/etc/crontab"] = None
        error("cron", type(exc).__name__, path="/etc/crontab")

    docker = {"requested": bool(inspect_docker), "endpoint": DOCKER_SOCKET if inspect_docker else None,
              "info": None, "containers": None}
    if inspect_docker:
        def docker_info(text):
            info = json.loads(text)
            if not isinstance(info, dict):
                raise ValueError("expected Docker info object")
            fields = ("ServerVersion", "Driver", "DockerRootDir")
            if not all(isinstance(info.get(field), str) and info[field] for field in fields):
                raise ValueError("incomplete Docker info")
            result = {field: info[field] for field in fields}
            status = info.get("DriverStatus")
            if status is not None:
                if not isinstance(status, list) or any(
                    not isinstance(row, list) or len(row) != 2 or
                    not all(isinstance(value, str) for value in row) for row in status
                ):
                    raise ValueError("invalid Docker driver status")
                result["DriverStatus"] = [row for row in status if row[0] in DRIVER_STATUS_KEYS]
            return result

        docker["info"] = command("docker_info", [
            "docker", "--host", DOCKER_SOCKET, "info", "--format", "{{json .}}",
        ], docker_info)
        docker["containers"] = command("docker_containers", [
            "docker", "--host", DOCKER_SOCKET, "ps", "--all", "--format", "{{json .}}",
        ], lambda text: _records([json.loads(line) for line in text.splitlines() if line.strip()],
                                 ("ID", "Image", "Names", "State")))

    return {
        "packages": installed,
        "package_architectures": None if installed is None else sorted({p["architecture"] for p in installed}),
        "timers": timers, "unit_files": installed_unit_files, "cron": cron, "docker": docker,
        "errors": errors,
        "limitations": [
            "Package metadata covers dpkg only; manual installs, snap and other package managers are unresolved.",
            "Systemd evidence covers the system manager only; user services and schedulers are unresolved.",
            "Cron evidence contains directory names and crontab presence only, not commands or per-job writers.",
            "No complete application-state, writer, identity or database consistency verification.",
            "Docker discovery is opt-in and supports only the conventional local Unix socket; storage portability and volume/bind dependencies remain unverified.",
            "An empty or unavailable probe does not establish that applications or writers are absent.",
        ],
    }
