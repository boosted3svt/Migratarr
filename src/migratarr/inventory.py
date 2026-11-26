"""Collect bounded, read-only Linux metadata without traversing data mounts."""

import datetime
import json
import os
import platform
import re
import struct
import subprocess

from .discovery import discover
from .probes import block_devices, system_units
from .mount_review import REMOTE, PSEUDO, LOCAL, classify_filesystem
from .server_survey import survey


def unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def parse_mountinfo(data):
    mounts = []
    for line in data.splitlines():
        before, after = line.split(" - ", 1)
        fields, tail = before.split(), after.split()
        if len(fields) < 6 or len(tail) < 3:
            raise ValueError("invalid mountinfo record")
        fs = tail[0]
        kind = classify_filesystem(fs)
        mounts.append({"mount_id": int(fields[0]), "parent_id": int(fields[1]),
                       "device": fields[2], "root": unescape(fields[3]),
                       "target": unescape(fields[4]), "options": fields[5].split(","),
                       "filesystem": fs, "kind": kind,
                       # Sources can contain credentials for some mount helpers.
                       # Device IDs and lsblk provide correlation without recording them.
                       "subtree_or_bind_candidate": fields[3] != "/"})
    counts = {}
    for mount in mounts:
        counts[mount["device"]] = counts.get(mount["device"], 0) + 1
    for mount in mounts:
        mount["shared_device"] = counts[mount["device"]] > 1
    return mounts


def collect(role, inspect_docker=False):
    if role not in {"source", "destination"}:
        raise ValueError("role must be source or destination")
    if role == "destination" and inspect_docker:
        raise ValueError("--inspect-docker applies only to --role source; destination applications will be replaced")
    errors = []

    def read(path):
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            errors.append({"probe": path, "error": type(exc).__name__})
            return ""

    def command(name, argv):
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=20,
                                    check=False, env={**os.environ, "LC_ALL": "C"})
            if result.returncode:
                errors.append({"probe": name, "error": "command failed", "returncode": result.returncode})
                return None
            return json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            errors.append({"probe": name, "error": "unavailable or invalid output"})
            return None

    release = {}
    for line in read("/etc/os-release").splitlines():
        key, sep, value = line.partition("=")
        if sep and key in {"ID", "VERSION_ID", "PRETTY_NAME"}:
            release[key] = value.strip('"')
    try:
        mounts = parse_mountinfo(read("/proc/self/mountinfo"))
    except (ValueError, IndexError):
        mounts = []
        errors.append({"probe": "mountinfo", "error": "invalid output"})
    blocks = block_devices(errors)
    network = command("network", ["ip", "-json", "address", "show"])
    # Avoid process arguments, environment variables, configuration and database
    # contents: all commonly expose credentials.
    services = None
    if role == "source":
        services = system_units("service", errors)
    space = []
    # statvfs only known local filesystems; never walk files or remote mounts.
    for mount in mounts:
        if mount["kind"] != "local":
            continue
        try:
            stat = os.statvfs(mount["target"])
            space.append({"target": mount["target"], "total_bytes": stat.f_blocks * stat.f_frsize,
                          "free_bytes": stat.f_bfree * stat.f_frsize,
                          "used_bytes": (stat.f_blocks - stat.f_bfree) * stat.f_frsize,
                          "available_bytes": stat.f_bavail * stat.f_frsize,
                          "total_inodes": stat.f_files, "available_inodes": stat.f_favail,
                          "device": mount["device"],
                          "shared_device": mount["shared_device"]})
        except OSError as exc:
            errors.append({"probe": "space", "target": mount["target"], "error": type(exc).__name__})
    mem = read("/proc/meminfo")
    match = re.search(r"^MemTotal:\s+(\d+) kB$", mem, re.M)
    discovery = discover(inspect_docker=inspect_docker) if role == "source" else None
    server_survey = survey() if role == "source" else None
    if discovery is not None:
        errors.extend(discovery["errors"])
    if server_survey is not None:
        errors.extend(server_survey["errors"])
    return {"schema_version": 1, "role": role,
            "collected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "system": {"os": release, "architecture": platform.machine(),
                       "userspace_bits": struct.calcsize("P") * 8,
                       "kernel": platform.release(), "boot_mode": "uefi" if os.path.isdir("/sys/firmware/efi") else "bios_or_unavailable",
                       "python_version": platform.python_version(),
                       "memory_bytes": int(match[1]) * 1024 if match else None,
                       "effective_uid": os.geteuid()},
            "mounts": mounts, "block_devices": blocks, "network": network,
            "services": services, "space": space, "errors": errors, "discovery": discovery,
            "server_survey": server_survey,
            "collection_scope": "source_system_and_applications" if role == "source" else "destination_infrastructure",
            "limitations": ["Current mount and network namespaces only; container/chroot detection is not conclusive.",
                            "No data-size scan, complete application/database inventory or boot-chain verification.",
                            "Kernel architecture and collector bitness do not prove all installed binary architectures.",
                            "No credentials, mount sources, application configuration or database contents collected."] +
                           (discovery["limitations"] if discovery is not None else [
                               "Destination packages, services, schedulers and Docker are intentionally not inventoried; bootstrap applications will be replaced.",
                               "Destination preparation conflicts and recovery requirements still need a focused check before migration."]) +
                           (server_survey["limitations"] if server_survey is not None else [])}
