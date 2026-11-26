"""Exact-preview cleanup in a bounded root-policy VM sandbox only."""
import json
import hashlib
import os
from pathlib import Path
import pkgutil
import re
import shlex
import stat
import subprocess

from .path_policy import classify_path, cleanup_candidates, evidence_hash, logical_path, rules, scan_paths
from .recovery import _directory, _fingerprint, _json, _locked, _now, _read, _write_json
from .transfer import SOURCE, TARGET_BASE, fixture_context, source_guard, ssh_arguments

CASE = "policy-case"


def mounted_paths(root):
    paths = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        path = line.split(" - ", 1)[0].split()[4]
        if path == str(root) or path.startswith(str(root) + "/"):
            if "\\" in path:
                raise ValueError("encoded fixture mount paths are unsupported")
            paths.append(path)
    return sorted(set(paths))


def snapshot(token, job, source):
    # Substitute only the guard nonce; paths and inspection code remain fixed.
    target = _directory(Path(TARGET_BASE) / job["job_id"] / "data" / CASE)
    if target.stat().st_dev != os.stat("/target").st_dev:
        raise ValueError("cleanup sandbox is not on the planned target filesystem")
    local_mounts = mounted_paths(target)
    if str(target) in local_mounts:
        raise ValueError("cleanup sandbox root must not be a mount")
    code = pkgutil.get_data("migratarr", "path_policy.py").decode() + "\n" + pkgutil.get_data("migratarr", "transfer_state.py").decode() + '''
case=root/'policy-case'
s=case.lstat()
assert case.resolve()==case and stat.S_ISDIR(s.st_mode) and s.st_uid==0 and s.st_mode & 0o077==0
selected=[path for path in mounts if path==str(case) or path.startswith(str(case)+'/')]
assert str(case) not in selected and not any('\\\\' in path for path in selected)
print(json.dumps({'entries':scan_paths(str(case),selected),
 'fingerprint':tree_fingerprint(str(case),selected),
 'boundaries':[path[len(str(case)):] for path in selected]}))
'''
    host, ssh = ssh_arguments(source)
    result = subprocess.run(ssh + ["--", host, "python3 -I -B -c " + shlex.quote(source_guard(token, inspection=code))],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("source policy sandbox verification failed")
    remote = json.loads(result.stdout)
    boundaries = sorted(set(remote["boundaries"] + [path[len(str(target)):] for path in local_mounts]))
    policy = rules([{"source_target": "/", "destination_target": "/", "destination_uuid": job["target_uuid"],
                     "mount_boundaries": boundaries}], boundaries)
    destination = scan_paths(str(target), local_mounts)
    choices = cleanup_candidates(remote["entries"], destination, policy)
    blocked = bool(choices["source_collisions"] or choices["type_conflicts"] or choices["hardlink_conflicts"])
    return {"schema_version": 1, "scope": "vm_root_policy_sandbox", "job_id": job["job_id"],
            "migration_ready": False, "status": "blocked" if blocked else "preview_ready",
            "source_fingerprint": remote["fingerprint"], "destination_fingerprint": evidence_hash(destination),
            "path_policy": policy, "destination_mounts": local_mounts, **choices}


def preview_cleanup(token):
    job_path, job, source = fixture_context(token)
    with _locked(job_path):
        report = snapshot(token, job, source)
        path = job_path / "cleanup-preview.json"
        _write_json(path, report, replace=path.exists())
        return {**report, "preview_sha256": _fingerprint(_read(path))}


def remove_candidate(root, candidate):
    logical_path(candidate["path"])
    if candidate["path"] == "/":
        raise ValueError("cleanup cannot remove the sandbox root")
    """Walk parents through directory FDs; never dereference a symlink."""
    relative = candidate["path"].lstrip("/")
    if not relative or any(part in ("", ".", "..") for part in relative.split("/")):
        raise ValueError("invalid cleanup candidate")
    descriptor = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        device = os.fstat(descriptor).st_dev
        parts = relative.split("/")
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
            if os.fstat(descriptor).st_dev != device:
                raise ValueError("cleanup parent crosses a filesystem")
        info = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        expected = candidate["evidence"]
        if (info.st_dev, info.st_ino, info.st_mode) != (expected["device"], expected["inode"], expected["mode"]):
            raise ValueError("cleanup candidate identity changed")
        if stat.S_ISDIR(info.st_mode):
            os.rmdir(parts[-1], dir_fd=descriptor)
        else:
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink) != (expected["size"], expected["mtime_ns"], expected["ctime_ns"], expected["links"]):
                raise ValueError("cleanup file changed")
            if stat.S_ISREG(info.st_mode):
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
                with os.fdopen(fd, "rb") as handle:
                    opened = os.fstat(handle.fileno())
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise ValueError("cleanup file identity changed")
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != expected.get("sha256"):
                    raise ValueError("cleanup file contents changed")
            elif stat.S_ISLNK(info.st_mode) and os.readlink(parts[-1], dir_fd=descriptor) != expected.get("link"):
                raise ValueError("cleanup symlink changed")
            os.unlink(parts[-1], dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def apply_cleanup(token, preview_sha256):
    job_path, job, source = fixture_context(token)
    if not isinstance(preview_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", preview_sha256):
        raise ValueError("an exact cleanup preview digest is required")
    with _locked(job_path):
        path = job_path / "cleanup-preview.json"
        if _fingerprint(_read(path)) != preview_sha256:
            raise ValueError("cleanup preview digest differs")
        saved = _json(path)
        fresh = snapshot(token, job, source)
        if saved != fresh or saved["status"] != "preview_ready":
            raise ValueError("cleanup preview is stale or blocked; generate a fresh preview")
        target = _directory(Path(TARGET_BASE) / job["job_id"] / "data" / CASE)
        journal = job_path / "cleanup.json"
        report = {"schema_version": 1, "scope": saved["scope"], "job_id": job["job_id"],
                  "status": "applying", "migration_ready": False, "preview_sha256": preview_sha256,
                  "removed": [], "updated_at": _now()}
        _write_json(journal, report, replace=journal.exists())
        try:
            for candidate in saved["candidates"]:
                if classify_path(candidate["path"], saved["path_policy"]) != "copy_candidate":
                    raise ValueError("cleanup candidate is protected")
                if mounted_paths(target) != saved["destination_mounts"]:
                    raise ValueError("cleanup mount topology changed")
                remove_candidate(target, candidate)
                report["removed"].append(candidate["path"])
                _write_json(journal, report, replace=True)
            report["status"] = "complete"
        except (OSError, ValueError):
            report["status"] = "interrupted"
            _write_json(journal, report, replace=True)
            raise
        report["updated_at"] = _now()
        _write_json(journal, report, replace=True)
        return report
