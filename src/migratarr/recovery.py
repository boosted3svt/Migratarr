"""Durable recovery-rehearsal jobs. No copying or migration execution."""

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import uuid

from .profiles import load_profile
from .remote import collect_remote, SourceConnectionError


MAX_JSON = 8 * 1024 * 1024
VM_MARKER = Path("/etc/migratarr-recovery-test.json")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _directory(path):
    path = Path(path).absolute()
    item = path.lstat()
    if path.resolve() != path or not stat.S_ISDIR(item.st_mode) or item.st_uid != os.geteuid() or item.st_mode & 0o077:
        raise ValueError("recovery directories must be private, owned directories without symlink parents")
    return path


def _read(path, maximum=MAX_JSON):
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        item = os.fstat(handle.fileno())
        if not stat.S_ISREG(item.st_mode) or item.st_uid != os.geteuid() or item.st_mode & 0o077:
            raise ValueError("recovery files must be private regular files owned by the current user")
        data = handle.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("recovery file exceeds its size limit")
        return data


def _json(path):
    try:
        return json.loads(_read(path).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("invalid recovery JSON evidence") from None


def _sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, data, replace=False):
    path = Path(path)
    if not replace and (path.exists() or path.is_symlink()):
        raise FileExistsError("recovery output already exists")
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            _read(path)  # Refuse replacing a link, public file or another owner's file.
            os.replace(str(temporary), str(path))
        else:
            os.link(str(temporary), str(path), follow_symlinks=False)
            temporary.unlink()
        _sync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json(path, data, replace=False):
    _write(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode(), replace)


def _fingerprint(data):
    return hashlib.sha256(data).hexdigest()


def _check_document(document):
    if not isinstance(document, dict) or type(document.get("schema_version")) is not int or document.get("schema_version") != 1 or document.get("purpose") != "recovery_rehearsal":
        raise ValueError("unsupported recovery job")
    try:
        if str(uuid.UUID(document["job_id"])) != document["job_id"]:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("invalid recovery job identifier") from None
    if document.get("phase") not in ("created", "prepared", "rescue_ready", "recovery_checked"):
        raise ValueError("invalid recovery job phase")
    if type(document.get("resume_count")) is not int or document["resume_count"] < 0:
        raise ValueError("invalid recovery resume count")
    if document.get("migration_ready") is not False:
        raise ValueError("recovery rehearsal cannot authorize migration")
    try:
        if str(uuid.UUID(document["target_uuid"])) != document["target_uuid"]:
            raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ValueError("invalid recovery target UUID") from None
    evidence, auth = document.get("evidence_sha256"), document.get("auth_sha256")
    if not isinstance(evidence, dict) or set(evidence) != {"profile.json", "plan.json"} or not isinstance(auth, dict) or set(auth) - {"source_key", "known_hosts"}:
        raise ValueError("invalid recovery evidence manifest")
    if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in list(evidence.values()) + list(auth.values())):
        raise ValueError("invalid recovery evidence digest")
    if set(auth) not in (set(), {"source_key", "known_hosts"}):
        raise ValueError("incomplete recovery authentication evidence")
    if document["phase"] != "created":
        assets = document.get("assets")
        if not isinstance(assets, dict) or not {"kernel", "initrd.img"} <= set(assets) or set(assets) - {"kernel", "initrd.img", "boot_uuid"}:
            raise ValueError("invalid prepared recovery assets")
        if any(not isinstance(assets[name], str) or not re.fullmatch(r"[a-f0-9]{64}", assets[name]) for name in ("kernel", "initrd.img")):
            raise ValueError("invalid recovery boot digest")


def inspect_job(path):
    path = _directory(path)
    document = _json(path / "job.json")
    _check_document(document)
    if path.name != document["job_id"]:
        raise ValueError("recovery job path and identity differ")
    for name in ("profile.json", "plan.json"):
        if _fingerprint(_read(path / name)) != document["evidence_sha256"].get(name):
            raise ValueError("saved recovery evidence changed")
    for name, digest in document.get("auth_sha256", {}).items():
        _directory(path / "auth")
        if name not in ("source_key", "known_hosts") or _fingerprint(_read(path / "auth" / name, 1024 * 1024)) != digest:
            raise ValueError("saved recovery authentication changed")
    return document


@contextlib.contextmanager
def _locked(path):
    path = _directory(path)
    fd = os.open(str(path / ".lock"), os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        item = os.fstat(fd)
        if not stat.S_ISREG(item.st_mode) or item.st_uid != os.geteuid() or item.st_mode & 0o077:
            raise ValueError("unsafe recovery lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        os.close(fd)


def create_job(profile_path, plan_path, state_directory):
    profile = load_profile(profile_path)
    # A plan is evidence for rehearsal only; no value can enable a copier here.
    with open(plan_path, "rb") as handle:
        raw = handle.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise ValueError("plan exceeds its size limit")
    plan = json.loads(raw)
    if not isinstance(plan, dict) or plan.get("schema_version") != 1 or plan.get("scope") != "entire_local_installation":
        raise ValueError("expected a whole-server draft plan")
    target_roots = [row.get("destination") for row in plan.get("filesystem_map", []) if isinstance(row, dict) and isinstance(row.get("destination"), dict) and row["destination"].get("target") == "/"]
    if len(target_roots) != 1:
        raise ValueError("a single destination root mapping is required for recovery")
    target_uuid = (target_roots[0].get("identifiers") or {}).get("uuid")
    try:
        target_uuid = str(uuid.UUID(target_uuid))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("recovery needs a resolved destination filesystem UUID") from None
    source = profile["source"]
    auth = {}
    if source["mode"] == "ssh":
        if source["authentication"] != "key" or not source.get("identity_file"):
            raise ValueError("recovery requires an explicit unencrypted key; passwords and agent sessions cannot survive reboot")
        with open(source["known_hosts"], "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("known-host evidence must be a regular file")
            hosts = handle.read(1024 * 1024 + 1)
        auth = {"source_key": _read(source["identity_file"], 1024 * 1024), "known_hosts": hosts}
        if len(auth["known_hosts"]) > 1024 * 1024:
            raise ValueError("known-host evidence exceeds its size limit")
        result = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", source["identity_file"]], stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        if result.returncode:
            raise ValueError("recovery key must be readable without a passphrase prompt")
        source["identity_file"] = "auth/source_key"
        source["known_hosts"] = "auth/known_hosts"
    parent = Path(state_directory).absolute()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _directory(parent)
    job_id = str(uuid.uuid4())
    job = parent / job_id
    job.mkdir(mode=0o700)
    _write(job / ".lock", b"")
    _write_json(job / "profile.json", profile)
    _write_json(job / "plan.json", plan)
    if auth:
        (job / "auth").mkdir(mode=0o700)
        for name, data in auth.items():
            _write(job / "auth" / name, data)
    document = {"schema_version": 1, "purpose": "recovery_rehearsal", "job_id": job_id,
                "target_uuid": target_uuid, "phase": "created", "resume_count": 0,
                "migration_ready": False, "created_at": _now(), "updated_at": _now(),
                "evidence_sha256": {name: _fingerprint(_read(job / name)) for name in ("profile.json", "plan.json")},
                "auth_sha256": {name: _fingerprint(data) for name, data in auth.items()},
                "events": [{"event": "created", "time": _now()}]}
    _write_json(job / "job.json", document)
    _sync_dir(parent)
    return {"job_directory": str(job), **document}


def mark_prepared(job_path, assets):
    with _locked(job_path) as path:
        job = inspect_job(path)
        if job["phase"] != "created":
            raise ValueError("only a new job can be prepared")
        job.update(phase="prepared", updated_at=_now(), assets=assets)
        _check_document(job)
        job["events"].append({"event": "prepared", "time": _now()})
        _write_json(path / "job.json", job, replace=True)
        return job


def resume_job(job_path, runtime_directory, target_uuid):
    """Resume a saved rehearsal, with runtime provenance enforced by the caller."""
    runtime = Path(runtime_directory).absolute()
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    _directory(runtime)
    with _locked(job_path) as path:
        job = inspect_job(path)
        if job["phase"] not in ("prepared", "rescue_ready", "recovery_checked") or target_uuid != job["target_uuid"]:
            raise ValueError("recovery job is not prepared for this target filesystem")
        auth_runtime = runtime / "auth"
        if job["auth_sha256"]:
            auth_runtime.mkdir(mode=0o700, exist_ok=True)
            _directory(auth_runtime)
            for name in job["auth_sha256"]:
                destination = auth_runtime / name
                data = _read(path / "auth" / name, 1024 * 1024)
                if destination.exists():
                    if _read(destination) != data:
                        raise ValueError("runtime authentication differs from the saved job")
                else:
                    _write(destination, data)
        profile = _json(path / "profile.json")
        if profile["source"]["mode"] == "ssh":
            profile["source"]["identity_file"] = str(auth_runtime / "source_key")
            profile["source"]["known_hosts"] = str(auth_runtime / "known_hosts")
        profile_path = runtime / "profile.json"
        _write_json(profile_path, profile, replace=profile_path.exists())
        job.update(phase="rescue_ready" if job["phase"] == "prepared" else job["phase"],
                   resume_count=job["resume_count"] + 1, updated_at=_now())
        job["events"].append({"event": "resumed", "time": _now()})
        _write_json(path / "job.json", job, replace=True)
        _write_json(runtime / "status.json", {"job_id": job["job_id"], "phase": job["phase"],
                    "resume_count": job["resume_count"], "migration_ready": False}, replace=(runtime / "status.json").exists())
        return job


def check_source(runtime_directory):
    """Read-only connectivity/inventory check using restored runtime credentials.

    This works while target filesystems are unmounted. It never changes the
    durable job phase or authorizes transfer, even when the source is reachable.
    """
    runtime = _directory(runtime_directory)
    status = _json(runtime / "status.json")
    if (not isinstance(status, dict) or status.get("migration_ready") is not False
            or status.get("phase") not in ("rescue_ready", "recovery_checked")
            or type(status.get("resume_count")) is not int or status["resume_count"] < 1):
        raise ValueError("source check requires a resumed recovery runtime")
    try:
        job_id = str(uuid.UUID(status["job_id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ValueError("invalid runtime job identity") from None
    _read(runtime / "profile.json")
    profile = load_profile(runtime / "profile.json")
    source = profile["source"]
    if source["mode"] != "ssh" or source["authentication"] != "key":
        raise ValueError("source check requires explicit restored SSH key authentication")
    auth = _directory(runtime / "auth")
    for field, name in (("identity_file", "source_key"), ("known_hosts", "known_hosts")):
        if source.get(field) != str(auth / name):
            raise ValueError("source check credentials must belong to the resumed runtime")
        _read(auth / name, 1024 * 1024)
    report_path = runtime / "source-check.json"
    report = {"schema_version": 1, "job_id": job_id, "migration_ready": False,
              "checked_at": _now(), "status": "checking_source"}
    _write_json(report_path, report, replace=report_path.exists())
    try:
        inventory = collect_remote(source["host"], source["user"], port=source["port"],
                                   identity_file=source["identity_file"], known_hosts=source["known_hosts"],
                                   sudo=source["sudo"], inspect_docker=source["inspect_docker"])
    except SourceConnectionError as exc:
        report.update(status="source_unreachable", error=str(exc))
        _write_json(report_path, report, replace=True)
        raise
    report.update(status="source_incomplete" if inventory["errors"] else "source_reachable",
                  inventory=inventory)
    _write_json(report_path, report, replace=True)
    return report
