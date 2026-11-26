"""Private, secret-free profiles for repeatable standalone assessments."""

import json
import os
from pathlib import Path
import re

from .assessment import normalize_ignored_mounts
from .remote import _host


MAX_PROFILE_BYTES = 1024 * 1024


def _keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError("profile has missing or unsupported fields; credentials must not be stored in profiles")


def _path(value, base):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("profile paths must be nonempty local paths without control characters")
    try:
        path = Path(value).expanduser()
    except (TypeError, ValueError, RuntimeError):
        raise ValueError("profile paths must be valid local filesystem paths") from None
    if not path.is_absolute():
        path = base / path
    return os.path.abspath(str(path))


def _normalize(path, data):
    _keys(data, ("schema_version", "source", "destination", "output_dir", "ignored_source_mounts"),
          ("schema_version", "source"))
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("profile requires schema version 1")
    base = Path(_path(str(path), Path.cwd())).parent
    source = data["source"]
    if not isinstance(source, dict):
        raise ValueError("profile source must be an object")
    mode = source.get("mode")
    if mode == "inventory":
        _keys(source, ("mode", "path"), ("mode", "path"))
        normalized_source = {"mode": "inventory", "path": _path(source["path"], base)}
    elif mode == "ssh":
        _keys(source, ("mode", "host", "user", "port", "authentication", "identity_file", "known_hosts", "sudo", "inspect_docker"),
              ("mode", "host", "user"))
        host = _host(source["host"])
        user = source["user"]
        if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*\$?", user):
            raise ValueError("profile source requires a simple Unix account name")
        port = source.get("port", 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("profile SSH port must be an integer from 1 to 65535")
        authentication = source.get("authentication", "key")
        if authentication not in ("key", "password"):
            raise ValueError("profile authentication must be key or password")
        for field in ("sudo", "inspect_docker"):
            if type(source.get(field, False if field == "sudo" else True)) is not bool:
                raise ValueError("profile SSH feature options must be boolean")
        identity = source.get("identity_file")
        if authentication == "password" and identity is not None:
            raise ValueError("password authentication profiles cannot specify an identity file")
        known_hosts = source.get("known_hosts")
        if known_hosts is None:
            known_hosts = str(Path.home() / ".ssh" / "known_hosts")
        normalized_source = {"mode": "ssh", "host": host, "user": user, "port": port,
                             "authentication": authentication,
                             "identity_file": None if identity is None else _path(identity, base),
                             "known_hosts": _path(known_hosts, base),
                             "sudo": source.get("sudo", False), "inspect_docker": source.get("inspect_docker", True)}
    else:
        raise ValueError("profile source mode must be ssh or inventory")
    destination = data.get("destination", {"mode": "local"})
    if not isinstance(destination, dict):
        raise ValueError("profile destination must be an object")
    if destination.get("mode") == "local":
        _keys(destination, ("mode",), ("mode",))
        normalized_destination = {"mode": "local"}
    elif destination.get("mode") == "inventory":
        _keys(destination, ("mode", "path"), ("mode", "path"))
        normalized_destination = {"mode": "inventory", "path": _path(destination["path"], base)}
    else:
        raise ValueError("profile destination mode must be local or inventory")
    ignored = data.get("ignored_source_mounts", [])
    if not isinstance(ignored, list):
        raise ValueError("profile ignored source mounts must be a list")
    return {"schema_version": 1, "source": normalized_source, "destination": normalized_destination,
            "output_dir": _path(data.get("output_dir", "reports"), base),
            "ignored_source_mounts": normalize_ignored_mounts(ignored)}


def load_profile(path):
    """Read and validate settings without opening identity files or connecting."""
    path = Path(_path(str(path), Path.cwd()))
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_PROFILE_BYTES + 1)
        if len(raw) > MAX_PROFILE_BYTES:
            raise ValueError("profile exceeds the size limit")
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("profile contains invalid JSON") from None
    return _normalize(path, data)


def save_profile(path, data):
    """Create a private profile; never overwrite a file or follow an existing link."""
    path = Path(_path(str(path), Path.cwd()))
    normalized = _normalize(path, data)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(normalized, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return normalized


def setup_profile(path, input_fn=None):
    """Prompt for connection settings, never passwords, then save a new profile."""
    if input_fn is None:
        input_fn = input

    def answer(prompt, default=""):
        value = input_fn(prompt).strip()
        return value or default

    def yes_no(prompt, default):
        value = answer(prompt, "yes" if default else "no").lower()
        if value not in ("y", "yes", "n", "no"):
            raise ValueError("setup yes/no answers must be yes or no")
        return value in ("y", "yes")

    host = answer("Source hostname or IP: ")
    user = answer("Source SSH username: ")
    try:
        port = int(answer("Source SSH port [22]: ", "22"))
    except ValueError:
        raise ValueError("setup SSH port must be an integer") from None
    authentication = answer("Authentication method [key/password, default key]: ", "key").lower()
    if authentication not in ("key", "password"):
        raise ValueError("setup authentication must be key or password")
    identity = None
    if authentication == "key":
        identity = answer("Local identity-file path (Enter for SSH agent/default keys): ") or None
    known_hosts = answer("SSH host-trust file [~/.ssh/known_hosts; first connection asks you to verify]: ", "~/.ssh/known_hosts")
    sudo = yes_no("Use existing noninteractive sudo authorization? [y/N]: ", False)
    docker = yes_no("Include source Docker metadata? [Y/n]: ", True)
    ignored = answer("Ignored source mount paths, comma separated (Enter for none): ")
    output = answer("Report parent directory [reports, relative to profile]: ", "reports")
    return save_profile(path, {"schema_version": 1,
        "source": {"mode": "ssh", "host": host, "user": user, "port": port,
                   "authentication": authentication, "identity_file": identity, "known_hosts": known_hosts,
                   "sudo": sudo, "inspect_docker": docker},
        "destination": {"mode": "local"}, "output_dir": output,
        "ignored_source_mounts": [item.strip() for item in ignored.split(",") if item.strip()]})
