"""Bounded VM fixture transfer only. Whole-installation replacement is disabled."""
import ipaddress
import errno
import json
import os
import pkgutil
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import uuid

from .profiles import load_profile
from .recovery import _directory, _json, _locked, _now, _read, _write, _write_json, inspect_job
from .recovery_vm import guard_vm
from .space_budget import temporary_budget


SOURCE = "/var/lib/migratarr/transfer-fixture"
SOURCE_MARKER = "/var/lib/migratarr/transfer-fixture-marker.json"
TARGET_BASE = "/target/var/lib/migratarr/transfer-tests"


def mount_exclusions(mounts):
    """Exclude contents of nested mounts, including binds on the same device."""
    result = []
    for mount in mounts:
        if mount.startswith(SOURCE + "/"):
            relative = mount[len(SOURCE) + 1:]
            if not re.fullmatch(r"[A-Za-z0-9_./-]+", relative) or any(part in ("", ".", "..") for part in relative.split("/")):
                raise ValueError("unsupported nested fixture mount path")
            result.append("/" + relative + "/*")
        elif mount == SOURCE:
            raise ValueError("fixture root must not itself be a mount")
    return sorted(set(result))


def source_guard(token, mounts=None, inspection=None):
    """Code streamed to the fixture VM before inspection and before rsync."""
    return '''import json,os,pathlib,stat,sys
assert os.geteuid()==0
assert pathlib.Path('/sys/class/dmi/id/sys_vendor').read_text().strip().lower() in ('qemu','bochs')
p=pathlib.Path(MARKER)
s=p.lstat()
assert stat.S_ISREG(s.st_mode) and s.st_uid==0 and s.st_mode & 0o077 == 0
assert p.resolve()==p
assert json.loads(p.read_text())=={'purpose':'migratarr_transfer_fixture','token':TOKEN}
root=pathlib.Path(SOURCE)
s=root.lstat()
assert stat.S_ISDIR(s.st_mode) and s.st_uid==0 and s.st_mode & 0o077 == 0 and root.resolve()==root
mounts=[]
for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines():
    target=line.split(' - ',1)[0].split()[4]
    if target==SOURCE or target.startswith(SOURCE+'/'): mounts.append(target)
mounts=sorted(set(mounts))
EXPECTED
'''.replace("MARKER", repr(SOURCE_MARKER)).replace("TOKEN", repr(token)).replace("SOURCE", repr(SOURCE)).replace(
        "EXPECTED", inspection if inspection is not None else "assert mounts==" + repr(mounts) + "\nos.execvp('rsync',['rsync']+sys.argv[1:])" if mounts is not None else
        pkgutil.get_data("migratarr", "transfer_state.py").decode("utf-8") +
        "\nprint(json.dumps({'mounts':mounts,'fingerprint':tree_fingerprint(str(root),mounts),'largest_file_bytes':largest_fixture_file(str(root),mounts)}))")


def save_transfer_checkpoint(path, report, reserve):
    """Release preallocated space if an atomic checkpoint runs out of blocks."""
    try:
        _write_json(path, report, replace=path.exists())
    except OSError as exc:
        if exc.errno not in (errno.ENOSPC, errno.EDQUOT):
            raise
        if reserve.exists():
            _read(reserve, 4 * 1024 * 1024)
            reserve.unlink()
        report["checkpoint_reserve_released"] = True
        _write_json(path, report, replace=path.exists())


def ssh_arguments(source):
    host = str(ipaddress.IPv4Address(source["host"]))
    hosts = source["known_hosts"].replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return host, ["ssh", "-F", "/dev/null", "-T", "-i", source["identity_file"], "-p", str(source["port"]), "-l", source["user"],
                  "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                  "-o", 'UserKnownHostsFile="' + hosts + '"', "-o", "GlobalKnownHostsFile=/dev/null",
                  "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                  "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", "-o", "PermitLocalCommand=no",
                  "-o", "ControlPath=none", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]


def rsync_arguments(source, destination, token, mounts, bandwidth=0, policy_exclusions=()):
    if type(bandwidth) is not int or not 0 <= bandwidth <= 1048576:
        raise ValueError("invalid fixture bandwidth limit")
    host, ssh = ssh_arguments(source)
    remote = "python3 -I -B -c " + shlex.quote(source_guard(token, mounts))
    argv = ["rsync", "--archive", "--hard-links", "--acls", "--xattrs", "--numeric-ids", "--one-file-system",
            "--checksum", "--sparse", "--fsync", "--partial-dir=.migratarr-partial",
            "--timeout=30", "--protect-args", "--temp-dir=" + str(Path(destination).parent / "scratch"), "--rsync-path=" + remote,
            "-e", " ".join(shlex.quote(value) for value in ssh)]
    for pattern in mount_exclusions(mounts) + list(policy_exclusions):
        argv.extend(["--exclude", pattern])
    argv.extend(["--exclude", "/.migratarr-partial/***"])
    if bandwidth:
        argv.append("--bwlimit=" + str(bandwidth))
    argv.extend(["--", host + ":" + SOURCE + "/", str(destination) + "/"])
    return argv


def fixture_context(token):
    """All physical-host, RAM-root and backing-disk checks precede writes."""
    guard_vm(token)
    mounts = [line.split() for line in Path("/proc/mounts").read_text().splitlines()]
    roots = [row for row in mounts if len(row) >= 3 and row[1] == "/"]
    if len(roots) != 1 or roots[0][2] not in ("rootfs", "ramfs", "tmpfs"):
        raise ValueError("fixture transfer must run from a RAM root")
    context = _json("/run/migratarr/recovery-context.json")
    job_id = str(uuid.UUID(context["job_id"]))
    target_uuid = str(uuid.UUID(context["target_uuid"]))
    device = context["target_device"]
    if not isinstance(device, str) or not re.fullmatch(r"/dev/[A-Za-z0-9_/-]+", device) or ".." in device:
        raise ValueError("invalid fixture target device")
    target_mounts = [row for row in mounts if len(row) >= 3 and row[1] == "/target"]
    if len(target_mounts) != 1 or target_mounts[0][2] != "ext4":
        raise ValueError("fixture target must be a mounted ext4 filesystem")
    block = os.stat(device)
    if not stat.S_ISBLK(block.st_mode) or os.stat("/target").st_dev != block.st_rdev:
        raise ValueError("fixture target is not backed by the planned block device")
    actual = subprocess.run(["blkid", "-s", "UUID", "-o", "value", device], capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    if actual != target_uuid:
        raise ValueError("fixture target UUID differs")
    job_path = Path("/target/var/lib/migratarr/jobs") / job_id
    job = inspect_job(job_path)
    status = _json("/run/migratarr/status.json")
    if job["target_uuid"] != target_uuid or status["job_id"] != job_id or job["phase"] != "rescue_ready" or status.get("migration_ready") is not False:
        raise ValueError("fixture transfer requires the resumed job for this target")
    runtime = _directory("/run/migratarr")
    _read(runtime / "profile.json")
    source = load_profile(runtime / "profile.json")["source"]
    if source["mode"] != "ssh" or source["authentication"] != "key" or source["user"] != "root" or source["sudo"]:
        raise ValueError("fixture transfer requires explicit root SSH key authentication without sudo")
    for field, name in (("identity_file", "source_key"), ("known_hosts", "known_hosts")):
        if source[field] != str(runtime / "auth" / name):
            raise ValueError("fixture transfer requires credentials restored into RAM")
        _read(source[field], 1024 * 1024)
    return job_path, job, source


class TransferInterrupted(Exception):
    pass


def clean_scratch(path):
    """Discard only regular orphan files in the job's private rsync scratch."""
    path = _directory(path)
    items = list(path.iterdir())
    if len(items) > 4096:
        raise ValueError("fixture scratch exceeds its file limit")
    for item in items:
        info = item.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("unexpected object in fixture scratch")
    for item in items:
        item.unlink()
    return len(items)


def failure_reason(code, diagnostic):
    text = diagnostic.lower()
    if "no space left on device" in text or "disk quota exceeded" in text:
        return "space_exhausted"
    if code == 24:
        return "source_changed"
    if code in (10, 12, 30, 35, 255) or any(word in text for word in ("broken pipe", "connection reset", "connection timed out")):
        return "connection_lost"
    return "transfer_error"


def transfer_fixture(token, bandwidth=0, root_policy=False):
    if type(root_policy) is not bool:
        raise ValueError("invalid policy fixture selection")
    job_path, job, source = fixture_context(token)
    host, ssh = ssh_arguments(source)
    probe = subprocess.run(ssh + ["--", host, "python3 -I -B -c " + shlex.quote(source_guard(token))], capture_output=True, text=True, timeout=30)
    if probe.returncode:
        raise ValueError("source fixture verification or SSH trust failed")
    evidence = json.loads(probe.stdout)
    mounts = evidence["mounts"]
    mount_exclusions(mounts)
    with _locked(job_path):
        parent = Path(TARGET_BASE)
        parent.mkdir(mode=0o700, exist_ok=True)
        _directory(parent)
        run = parent / job["job_id"]
        run.mkdir(mode=0o700, exist_ok=True)
        _directory(run)
        data = run / "data"
        data.mkdir(mode=0o700, exist_ok=True)
        _directory(data)
        scratch = run / "scratch"
        scratch.mkdir(mode=0o700, exist_ok=True)
        _directory(scratch)
        # Nested destination mounts could redirect writes away from the target.
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            mount = line.split(" - ", 1)[0].split()[4]
            if mount == str(run) or mount.startswith(str(run) + "/"):
                if not root_policy or not mount.startswith(str(data / "policy-case") + "/"):
                    raise ValueError("nested fixture destination mounts are unsupported")
        policy = None
        if root_policy:
            from .cleanup import snapshot
            policy = snapshot(token, job, source, for_copy=True)
            if policy["status"] != "preview_ready":
                raise ValueError("policy copy is blocked by source collisions or destination conflicts")
        argv = rsync_arguments(source, data, token, mounts, bandwidth,
                               policy["copy_exclusions"] if policy else ())
        checkpoint = job_path / ("policy-transfer.json" if root_policy else "transfer.json")
        previous = _json(checkpoint) if checkpoint.exists() else None
        if previous is not None and (not isinstance(previous, dict) or previous.get("schema_version") != 1 or previous.get("job_id") != job["job_id"] or previous.get("migration_ready") is not False or type(previous.get("attempt")) is not int or previous["attempt"] < 1 or previous.get("phase") not in ("copying", "interrupted", "failed", "complete")):
            raise ValueError("invalid fixture transfer checkpoint")
        attempt = previous["attempt"] + 1 if previous else 1
        report = {"schema_version": 1, "job_id": job["job_id"], "migration_ready": False,
                  "scope": "vm_fixture_only", "phase": "copying", "attempt": attempt,
                  "previous_phase": previous["phase"] if previous else None,
                  "destination": str(data), "exclusions": mount_exclusions(mounts), "updated_at": _now(),
                  "source_fingerprint_before": evidence["fingerprint"], "checkpoint_reserve_released": False}
        if policy:
            report.update(scope="vm_root_policy_fixture_only", path_policy=policy["path_policy"],
                          policy_exclusions=policy["copy_exclusions"])
        report["discarded_temporary_files"] = clean_scratch(scratch)
        reserve = job_path / ".transfer-checkpoint-reserve"
        if reserve.exists():
            _read(reserve, 4 * 1024 * 1024)
        else:
            _write(reserve, b"\0" * (2 * 1024 * 1024))
        save_transfer_checkpoint(checkpoint, report, reserve)
        capacity = os.statvfs(data)
        report["temporary_space"] = temporary_budget(capacity.f_bavail * capacity.f_frsize,
                                                     evidence["largest_file_bytes"], 2 * 1024 * 1024)
        if report["temporary_space"]["status"] != "sufficient":
            report.update(phase="failed", failure_reason="insufficient_temporary_space", returncode=None)
            save_transfer_checkpoint(checkpoint, report, reserve)
            return report
        save_transfer_checkpoint(checkpoint, report, reserve)
        process = None
        handlers = {}
        log = Path("/run/migratarr/transfer-stderr.log")

        def interrupted(signum, frame):
            raise TransferInterrupted()

        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                handlers[signum] = signal.signal(signum, interrupted)
            _write(log, b"", replace=log.exists())
            descriptor = os.open(str(log), os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "ab") as errors:
                process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=errors, start_new_session=True)
            code = process.wait()
            report.update(phase="complete" if code == 0 else "failed", returncode=code)
            if code == 0:
                clean_scratch(scratch)
            if code:
                diagnostic = _read(log).decode("utf-8", "replace")
                report["failure_reason"] = failure_reason(code, diagnostic)
                if report["failure_reason"] == "space_exhausted" and reserve.exists():
                    reserve.unlink()
                    report["checkpoint_reserve_released"] = True
            else:
                verified = subprocess.run(ssh + ["--", host, "python3 -I -B -c " + shlex.quote(source_guard(token))],
                                          capture_output=True, text=True, timeout=30)
                if verified.returncode:
                    report.update(phase="failed", failure_reason="source_verification_failed")
                else:
                    after = json.loads(verified.stdout)
                    report["source_fingerprint_after"] = after["fingerprint"]
                    if after != evidence:
                        report.update(phase="failed", failure_reason="source_changed")
        except TransferInterrupted:
            report.update(phase="interrupted" if process else "failed", returncode=None)
        except OSError as exc:
            report.update(phase="failed", failure_reason="local_io_error", failure_errno=exc.errno)
        except (subprocess.SubprocessError, ValueError, KeyError):
            report.update(phase="failed", failure_reason="source_verification_failed")
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        subprocess.run(["sync"], check=True, timeout=30)
        if log.exists():
            try:
                saved_log = job_path / "transfer-stderr.log"
                _write(saved_log, _read(log), replace=saved_log.exists())
                report["diagnostic_log_persisted"] = True
            except OSError as exc:
                if exc.errno not in (errno.ENOSPC, errno.EDQUOT):
                    raise
                report["diagnostic_log_persisted"] = False
        report["updated_at"] = _now()
        save_transfer_checkpoint(checkpoint, report, reserve)
        return report
