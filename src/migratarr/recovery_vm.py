"""Recovery rehearsal installer. Physical machines are deliberately unsupported."""

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import uuid

from .recovery import VM_MARKER, _directory, _json, _read, _write, _write_json, _fingerprint, inspect_job, mark_prepared, resume_job


GRUB_SCRIPT = Path("/etc/grub.d/42_migratarr_recovery_test")


def guard_vm(token):
    if os.geteuid() != 0:
        raise ValueError("VM recovery rehearsal requires root inside the disposable guest")
    vendor = Path("/sys/class/dmi/id/sys_vendor").read_text().strip().lower()
    if vendor not in ("qemu", "bochs"):
        raise ValueError("recovery installation is restricted to disposable QEMU guests; physical installation is not implemented")
    marker = _json(VM_MARKER)
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32,64}", token) or marker != {"purpose": "migratarr_disposable_vm", "token": token}:
        raise ValueError("the disposable VM marker does not match")


def _run(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError("VM recovery command failed: " + argv[0])
    return result.stdout.strip()


def _canonical_job(job_path):
    path = _directory(job_path)
    if path.parent != Path("/var/lib/migratarr/jobs"):
        raise ValueError("VM jobs must live under /var/lib/migratarr/jobs")
    return path, inspect_job(path)


def grub_script(job_id, target_uuid, boot_uuid=None):
    job_id, target_uuid = str(uuid.UUID(job_id)), str(uuid.UUID(target_uuid))
    boot_uuid = str(uuid.UUID(boot_uuid)) if boot_uuid else None
    return '''#!/bin/sh
exec cat <<'MIGRATARR_GRUB'
if [ "${migratarr_job}" = "JOB" ]; then
  if [ "${migratarr_attempted}" != "1" ]; then
    set default="migratarr-JOB"
  fi
fi
menuentry 'Migratarr recovery rehearsal' --id migratarr-JOB {
  search --no-floppy --fs-uuid --set=root BOOTUUID
  set migratarr_attempted=1
  save_env migratarr_attempted
  linux BOOTPREFIX/migratarr/JOB/kernel console=ttyS0,115200 panic=5 migratarr.job=JOB migratarr.target_uuid=ROOTUUID migratarr.boot_uuid=BOOTUUID
  initrd BOOTPREFIX/migratarr/JOB/initrd.img
}
MIGRATARR_GRUB
'''.replace("JOB", job_id).replace("ROOTUUID", target_uuid).replace("BOOTUUID", boot_uuid or target_uuid).replace("BOOTPREFIX", "" if boot_uuid else "/boot")


def prepare_vm(job_path, kernel_path, initramfs_path, token):
    guard_vm(token)
    path, job = _canonical_job(job_path)
    if job["phase"] != "created":
        raise ValueError("only a new recovery job can be prepared")
    if not Path("/sys/firmware/efi").is_dir():
        raise ValueError("this rehearsal supports UEFI guests only")
    root_uuid = _run(["findmnt", "-n", "-o", "UUID", "/"])
    root_type = _run(["findmnt", "-n", "-o", "FSTYPE", "/"])
    if root_uuid != job["target_uuid"] or root_type != "ext4":
        raise ValueError("the running VM root does not match the planned ext4 target")
    boot_uuid = _run(["findmnt", "-n", "-o", "UUID", "--target", "/boot"])
    boot_uuid = None if boot_uuid == root_uuid else str(uuid.UUID(boot_uuid))
    if GRUB_SCRIPT.exists() or GRUB_SCRIPT.is_symlink():
        raise ValueError("a recovery boot entry already exists; inspect or disarm that job first")
    boot_parent = Path("/boot/migratarr")
    boot_parent.mkdir(mode=0o700, exist_ok=True)
    _directory(boot_parent)
    boot = boot_parent / job["job_id"]
    boot.mkdir(mode=0o700)
    assets = {}
    for name, source in (("kernel", kernel_path), ("initrd.img", initramfs_path)):
        data = _read(source, 256 * 1024 * 1024)
        if not data:
            raise ValueError("empty recovery boot asset")
        _write(boot / name, data)
        assets[name] = _fingerprint(data)
    # This private file becomes executable only after its contents are durable.
    _write(GRUB_SCRIPT, grub_script(job["job_id"], job["target_uuid"], boot_uuid).encode())
    GRUB_SCRIPT.chmod(0o755)
    _run(["update-grub"])
    assets["boot_uuid"] = boot_uuid
    prepared = mark_prepared(path, assets)
    _run(["grub-editenv", "/boot/grub/grubenv", "set", "migratarr_job=" + job["job_id"], "migratarr_attempted=0"])
    _run(["sync"])
    return prepared


def boot_vm(job_path, token):
    guard_vm(token)
    _, job = _canonical_job(job_path)
    if job["phase"] not in ("prepared", "rescue_ready", "recovery_checked"):
        raise ValueError("recovery job is not prepared")
    _run(["sync"])
    _run(["systemctl", "reboot"])
    return {"status": "reboot_requested", "job_id": job["job_id"]}


def resume_vm(job_path, token):
    guard_vm(token)
    root_fs = None
    for line in Path("/proc/mounts").read_text().splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[1] == "/":
            root_fs = fields[2]
    if root_fs not in ("rootfs", "ramfs", "tmpfs"):
        raise ValueError("recovery resume must run from a RAM root")
    context = _json("/run/migratarr/recovery-context.json")
    try:
        job_id = str(uuid.UUID(context["job_id"]))
        target_uuid = str(uuid.UUID(context["target_uuid"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ValueError("invalid recovery boot context") from None
    expected = Path("/target/var/lib/migratarr/jobs") / job_id
    if Path(job_path).absolute() != expected:
        raise ValueError("boot context and recovery job path differ")
    target_device = context.get("target_device")
    if not isinstance(target_device, str) or not re.fullmatch(r"/dev/[A-Za-z0-9_/-]+", target_device) or ".." in target_device:
        raise ValueError("invalid recovery target device")
    actual = _run(["/sbin/blkid", "-o", "value", "-s", "UUID", target_device])
    if actual != target_uuid:
        raise ValueError("recovery target device UUID differs")
    job = inspect_job(expected)
    if job["target_uuid"] != target_uuid:
        raise ValueError("recovery target and job UUID differ")
    for name in ("kernel", "initrd.img"):
        data = _read(Path("/target/boot/migratarr") / job_id / name, 256 * 1024 * 1024)
        if _fingerprint(data) != job.get("assets", {}).get(name):
            raise ValueError("prepared recovery boot asset changed")
    result = resume_job(expected, "/run/migratarr", target_uuid)
    # Only a fully loaded, verified rehearsal may clear GRUB's failed-boot flag.
    _run(["/usr/bin/grub-editenv", "/target/boot/grub/grubenv", "set", "migratarr_attempted=0"])
    _run(["sync"])
    return result
