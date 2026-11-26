"""Read-only metadata probes compatible with older Ubuntu command versions."""

import json
import os
import re
import subprocess


def _run(argv, probe, errors):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=20,
                              check=False, env={**os.environ, "LC_ALL": "C",
                                                "SYSTEMD_COLORS": "0"})
    except subprocess.TimeoutExpired:
        errors.append({"probe": probe, "error": "command timed out"})
    except (OSError, UnicodeError):
        errors.append({"probe": probe, "error": "command unavailable or invalid text output"})
    return None


def _error(probe, errors, message="invalid command output"):
    errors.append({"probe": probe, "error": message})


def _unit(value, kinds):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_:@.\\-]+", value)) and any(
        value.endswith("." + kind) for kind in kinds)


def _token(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]+", value))


def _legacy_systemd_table(text, header, kinds):
    """Recognize old systemctl tables without accepting arbitrary invalid JSON.

    Some versions accept --output=json for list commands but still emit their
    usual table and exit successfully. Retry those with explicit text flags.
    A complete header or a valid first metadata row identifies that case; the
    retried output still has to pass the normal record validation below.
    """
    if not isinstance(text, str):
        return False
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if fields[:len(header)] == list(header):
            return True
        if fields and fields[0] == "●":
            fields = fields[1:]
        metadata_fields = 4 if header[0] == "UNIT" and header[1] == "LOAD" else 2
        return len(fields) >= metadata_fields and _unit(fields[0], kinds) and all(
            _token(field) for field in fields[1:metadata_fields])
    return False


def _normalize_blocks(value, legacy):
    if not isinstance(value, dict) or not isinstance(value.get("blockdevices"), list):
        raise ValueError("invalid block device list")

    def normalize(node):
        if not isinstance(node, dict) or not isinstance(node.get("name"), str) or not isinstance(node.get("type"), str):
            raise ValueError("invalid block device")
        size = node.get("size")
        if isinstance(size, str) and size.isdecimal():
            size = int(size)
        if type(size) is not int or size < 0:
            raise ValueError("invalid block capacity")
        result = {"name": node["name"], "type": node["type"], "size": size}
        for field in ("fstype", "uuid", "partuuid", "pkname"):
            item = node.get(field)
            if item is not None and not isinstance(item, str):
                raise ValueError("invalid block metadata")
            result[field] = item
        if legacy:
            if "mountpoint" not in node:
                raise ValueError("missing mountpoint")
            item = node["mountpoint"]
            if item is not None and not isinstance(item, str):
                raise ValueError("invalid mountpoint")
            result["mountpoints"] = [] if item is None else [item]
        else:
            if "mountpoints" not in node:
                raise ValueError("missing mountpoints")
            items = node["mountpoints"]
            if items is None:
                items = []
            if not isinstance(items, list) or any(item is not None and not isinstance(item, str) for item in items):
                raise ValueError("invalid mountpoints")
            result["mountpoints"] = [item for item in items if item is not None]
        if "children" in node:
            if not isinstance(node["children"], list):
                raise ValueError("invalid block children")
            result["children"] = [normalize(child) for child in node["children"]]
        return result

    return {"blockdevices": [normalize(node) for node in value["blockdevices"]]}


def block_devices(errors):
    """Return lsblk JSON with mountpoints normalized, or None with an error."""
    probe = "lsblk"
    prefix = ["lsblk", "--json", "--bytes", "--output"]
    columns = "NAME,TYPE,SIZE,FSTYPE,UUID,PARTUUID,MOUNTPOINTS,PKNAME"
    process = _run(prefix + [columns], probe, errors)
    if process is None:
        return None
    legacy = process.returncode != 0
    if legacy:
        process = _run(prefix + [columns.replace("MOUNTPOINTS", "MOUNTPOINT")], probe, errors)
        if process is None:
            return None
    if process.returncode:
        _error(probe, errors, "command failed")
        return None
    try:
        return _normalize_blocks(json.loads(process.stdout), legacy)
    except (ValueError, TypeError, RecursionError):
        _error(probe, errors)
        return None


def system_units(kind, errors):
    """Return sanitized loaded service/timer records, preferring JSON output."""
    if kind not in ("service", "timer"):
        raise ValueError("unit kind must be service or timer")
    probe = "services" if kind == "service" else "timers"
    prefix = ["systemctl", "list-units", "--type=" + kind, "--all", "--no-pager"]
    process = _run(prefix + ["--output=json"], probe, errors)
    if process is None:
        return None
    legacy = process.returncode != 0 or _legacy_systemd_table(
        process.stdout, ("UNIT", "LOAD", "ACTIVE", "SUB"), (kind,))
    if legacy:
        process = _run(prefix + ["--plain", "--no-legend"], probe, errors)
        if process is None:
            return None
    if process.returncode:
        _error(probe, errors, "command failed")
        return None
    try:
        if legacy:
            records = []
            for line in process.stdout.splitlines():
                if not line.strip():
                    continue
                fields = line.split()
                if fields and fields[0] == "●":
                    fields = fields[1:]
                if len(fields) < 4:
                    raise ValueError("incomplete system unit")
                records.append(dict(zip(("unit", "load", "active", "sub"), fields[:4])))
        else:
            records = json.loads(process.stdout)
        if not isinstance(records, list):
            raise ValueError("expected system unit list")
        result = []
        for record in records:
            if not isinstance(record, dict) or not _unit(record.get("unit"), (kind,)) or not all(
                _token(record.get(field)) for field in ("load", "active", "sub")):
                raise ValueError("invalid system unit")
            result.append({field: record[field] for field in ("unit", "load", "active", "sub")})
        return result
    except (ValueError, TypeError):
        _error(probe, errors)
        return None


def unit_files(errors):
    """Return installed service/timer names and states without descriptions."""
    probe = "unit_files"
    prefix = ["systemctl", "list-unit-files", "--type=service,timer", "--no-pager"]
    process = _run(prefix + ["--output=json"], probe, errors)
    if process is None:
        return None
    legacy = process.returncode != 0 or _legacy_systemd_table(
        process.stdout, ("UNIT", "FILE", "STATE"), ("service", "timer"))
    if legacy:
        process = _run(prefix + ["--no-legend", "--plain"], probe, errors)
        if process is None:
            return None
    if process.returncode:
        _error(probe, errors, "command failed")
        return None
    try:
        if legacy:
            records = []
            for line in process.stdout.splitlines():
                if not line.strip():
                    continue
                fields = line.split()
                if len(fields) not in (2, 3):
                    raise ValueError("invalid installed unit record")
                records.append({"unit_file": fields[0], "state": fields[1]})
        else:
            records = json.loads(process.stdout)
        if not isinstance(records, list):
            raise ValueError("expected installed unit list")
        result = []
        for record in records:
            if not isinstance(record, dict) or not _unit(record.get("unit_file"), ("service", "timer")) or not _token(record.get("state")):
                raise ValueError("invalid installed unit")
            result.append({"unit_file": record["unit_file"], "state": record["state"]})
        return result
    except (ValueError, TypeError):
        _error(probe, errors)
        return None
