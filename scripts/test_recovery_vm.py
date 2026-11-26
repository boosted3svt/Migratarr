#!/usr/bin/env python3
"""Exercise a RAM rescue handover using one disposable QEMU disk.

This is a developer integration test, not a production rescue installer. The
guest is a minimal synthetic Linux system, not a qualified Ubuntu migration.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time

from vm_lab_assets import build_trees
from vm_lab_disk import build_disk, check_disk_layout


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("qemu-system-x86_64", "mke2fs", "debugfs", "mformat", "mmd", "mcopy",
         "grub-mkstandalone", "modprobe", "zstd", "dropbear", "dropbearkey", "ssh", "ssh-keygen")
OVMF_CODE = Path("/usr/share/OVMF/OVMF_CODE_4M.fd")
OVMF_VARS = Path("/usr/share/OVMF/OVMF_VARS_4M.fd")


def command(argv, **kwargs):
    return subprocess.run([str(value) for value in argv], capture_output=True,
                          text=True, check=True, timeout=120, **kwargs)


def prerequisites():
    return {"tools": {name: shutil.which(name) for name in TOOLS},
            "uefi_code": OVMF_CODE.is_file(), "uefi_vars": OVMF_VARS.is_file(),
            "runs_unprivileged": os.geteuid() != 0}


def private_write(path, text):
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


class RecoveryLab:
    def __init__(self, work, boot_timeout):
        self.work = work
        self.boot_timeout = boot_timeout
        self.memory_mb = 512
        self.ssh_timeout = 10
        self.extra_qemu_args = []
        self.process = None
        self.launches = 0
        self.log_handles = []
        self.events = []
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]

    def record(self, event, **details):
        row = {"event": event, "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **details}
        self.events.append(row)
        print(event, flush=True)

    def start(self, disk):
        if self.process is not None:
            raise RuntimeError("a VM is already running")
        self.launches += 1
        serial = self.work / ("serial-{}.log".format(self.launches))
        stderr = (self.work / ("qemu-{}.log".format(self.launches))).open("wb")
        self.log_handles.append(stderr)
        argv = ["qemu-system-x86_64", "-nodefaults", "-machine", "q35,accel=tcg", "-cpu", "max",
                "-m", str(self.memory_mb), "-smp", "2", "-display", "none", "-monitor", "none",
                "-serial", "file:" + str(serial),
                "-drive", "if=pflash,format=raw,readonly=on,file=" + str(OVMF_CODE),
                "-drive", "if=pflash,format=raw,file=" + str(self.work / "OVMF_VARS.fd"),
                "-drive", "if=none,id=osdisk,format=raw,file=" + str(disk),
                "-device", "virtio-blk-pci,drive=osdisk",
                "-netdev", "user,id=labnet,restrict=on,hostfwd=tcp:127.0.0.1:{}-:22".format(self.port),
                "-device", "e1000,netdev=labnet",
                "-object", "rng-random,id=labrng,filename=/dev/urandom",
                "-device", "virtio-rng-pci,rng=labrng"]
        argv.extend(self.extra_qemu_args)
        self.process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stderr, stderr=stderr)
        self.record("vm_started", launch=self.launches, data_disks=1, acceleration="tcg", ssh_address="127.0.0.1")

    def stop(self, abrupt=False):
        if self.process is None:
            return
        if self.process.poll() is None:
            if abrupt:
                self.process.kill()
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None

    def ssh(self, script, required=True):
        argv = ["ssh", "-F", "/dev/null", "-T", "-i", str(self.work / "client-key"),
                "-p", str(self.port), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
                "-o", "UserKnownHostsFile=" + str(self.work / "known_hosts"),
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=2",
                "-o", "ConnectionAttempts=1", "-o", "LogLevel=ERROR",
                "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ControlPath=none",
                "-l", "root", "--", "127.0.0.1", script]
        argv[-1] = "set -eu\n" + script
        try:
            result = subprocess.run(argv, text=True, capture_output=True, timeout=self.ssh_timeout if required else 10)
        except subprocess.TimeoutExpired:
            if required:
                raise
            # A forced reboot can leave the forwarded TCP stream waiting until
            # the host timeout. The next boot is independently verified below.
            return subprocess.CompletedProcess(argv, 255, "", "guest SSH stream closed or timed out during transition")
        if required and result.returncode:
            raise RuntimeError("guest SSH action failed: " + result.stderr.strip())
        return result

    def wait_guest(self, mode, previous_boot=None):
        deadline = time.monotonic() + self.boot_timeout
        probe = "cat /run/migratarr-lab-mode /proc/sys/kernel/random/boot_id\nawk '$2 == \"/\" {print $3}' /proc/mounts"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("QEMU exited; inspect its saved log")
            try:
                result = self.ssh(probe, required=False)
                fields = result.stdout.splitlines()
                if result.returncode == 0 and fields and fields[0] == "rescue_failed":
                    diagnostic = self.ssh("cat /run/migratarr/resume.log", required=False).stdout
                    private_write(self.work / "recovery-startup-failure.log", diagnostic)
                    raise RuntimeError("RAM recovery startup failed; inspect recovery-startup-failure.log")
                if result.returncode == 0 and len(fields) == 3 and fields[0] == mode and fields[1] != previous_boot:
                    self.record(mode + "_ssh_ready", boot_id=fields[1], root_filesystem=fields[2])
                    return {"mode": fields[0], "boot_id": fields[1], "root_filesystem": fields[2]}
            except subprocess.TimeoutExpired:
                pass
            time.sleep(1)
        raise RuntimeError("guest did not reach {} over SSH; inspect serial logs".format(mode))

    def exercise(self, info):
        self.start(info["disk"])
        initial = self.wait_guest("bootstrap")
        if initial["root_filesystem"] != "ext4":
            raise RuntimeError("bootstrap is not running from the disposable ext4 root")
        self.ssh("/usr/sbin/migratarr-lab prepare")
        job = self.ssh("cat /var/lib/migratarr/jobs/lab-job/job-id").stdout.strip()
        if not job:
            raise RuntimeError("no durable job identifier was written")
        self.record("job_prepared_on_single_disk", job_id=job)
        self.ssh("sync\nreboot -f", required=False)
        rescue = self.wait_guest("rescue", initial["boot_id"])
        if rescue["root_filesystem"] not in ("rootfs", "ramfs", "tmpfs"):
            raise RuntimeError("rescue is not independent of the disk root")
        self.ssh("test -b /dev/vda2\ntest ! -e /sys/block/vdb\numount /target")
        # This new SSH process must start after the target filesystem is unmounted.
        self.ssh("test ! -e /target/var/lib/migratarr/jobs/lab-job/job-id\ntest -f /run/migratarr-lab-mode")
        self.record("rescue_ssh_survives_unmounted_target")
        self.ssh("mount -t ext4 /dev/vda2 /target")
        self.ssh("/usr/sbin/migratarr-lab checkpoint lab_checkpoint_1")
        first = self.ssh("cat /target/var/lib/migratarr/jobs/lab-job/job-id /target/var/lib/migratarr/jobs/lab-job/resume-count /target/var/lib/migratarr/jobs/lab-job/phase").stdout.splitlines()
        if len(first) != 3 or first[0] != job or first[2] != "lab_checkpoint_1":
            raise RuntimeError("checkpoint could not be read back")
        self.ssh("sync")
        self.stop(abrupt=True)
        self.record("power_loss_after_committed_checkpoint")
        self.start(info["disk"])
        restarted = self.wait_guest("rescue", rescue["boot_id"])
        if restarted["root_filesystem"] not in ("rootfs", "ramfs", "tmpfs"):
            raise RuntimeError("resumed environment is not RAM-rooted")
        second = self.ssh("cat /target/var/lib/migratarr/jobs/lab-job/job-id /target/var/lib/migratarr/jobs/lab-job/resume-count /target/var/lib/migratarr/jobs/lab-job/phase").stdout.splitlines()
        if len(second) != 3 or second[0] != job or int(second[1]) <= int(first[1]) or second[2] != first[2]:
            raise RuntimeError("durable checkpoint did not survive the restart")
        self.record("checkpoint_resumed_after_power_loss", resume_count=int(second[1]))
        # Fault injection is confined to this synthetic guest's rescue asset.
        self.ssh("mv /target/boot/migratarr/rescue.img /target/boot/migratarr/rescue.img.disabled\nsync\nreboot -f", required=False)
        fallback = self.wait_guest("bootstrap", restarted["boot_id"])
        if fallback["root_filesystem"] != "ext4":
            raise RuntimeError("missing-image fallback failed to return to disk bootstrap")
        self.record("missing_rescue_image_falls_back_to_bootstrap")
        self.ssh("sync\npoweroff -f", required=False)
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            raise RuntimeError("guest did not power off cleanly")
        self.stop()
        verification = check_disk_layout(info)
        if not verification["passed"]:
            raise RuntimeError("virtual partition table, EFI contents or root UUID changed unexpectedly")
        self.record("disk_layout_verified", **verification)
        return verification


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Print local prerequisites only")
    parser.add_argument("--kernel", help="Readable Linux bzImage; never a host block device")
    parser.add_argument("--kernel-version", help="Matching installed /lib/modules version")
    parser.add_argument("--work-dir", help="New private lab directory; refuses existing paths")
    parser.add_argument("--boot-timeout", type=int, default=180)
    args = parser.parse_args(argv)
    checks = prerequisites()
    if args.check:
        print(json.dumps(checks, indent=2))
        return 0 if all(checks["tools"].values()) and checks["uefi_code"] and checks["uefi_vars"] and checks["runs_unprivileged"] else 1
    if not all(checks["tools"].values()) or not checks["uefi_code"] or not checks["uefi_vars"] or not checks["runs_unprivileged"]:
        parser.error("run unprivileged with the prerequisites listed by --check")
    if not args.kernel or not args.kernel_version or not re.fullmatch(r"[A-Za-z0-9_.+-]+", args.kernel_version):
        parser.error("provide --kernel and its matching --kernel-version")
    kernel = Path(args.kernel).resolve()
    if not kernel.is_file() or not os.access(str(kernel), os.R_OK):
        parser.error("--kernel must be a readable regular file; copy a kernel to a private lab cache if needed")
    if args.boot_timeout < 10:
        parser.error("--boot-timeout must be at least 10 seconds")
    if args.work_dir:
        work = Path(args.work_dir).absolute()
        if work.resolve() != work:
            parser.error("lab output directory must not use symlink parents")
        work.mkdir(mode=0o700)
    else:
        parent = ROOT / "reports"
        parent.mkdir(mode=0o700, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="recovery-vm-", dir=parent))
    if any(char in str(work) for char in (",", "\n", "\r")):
        parser.error("lab paths cannot contain commas or control characters")
    lab = RecoveryLab(work, args.boot_timeout)
    result = {"schema_version": 1, "status": "failed", "production_qualified": False,
              "scope": "synthetic_one_disk_uefi_boot_ram_ssh_checkpoint_smoke_test", "events": lab.events,
              "limitations": ["Not a stock Ubuntu installation or a whole-server migration test.",
                              "Power loss is injected after a committed checkpoint, not during a data transfer.",
                              "Fallback covers a missing rescue image, not every kernel/firmware failure.",
                              "No source VM, production copier or physical boot configuration is exercised."]}
    try:
        print("Private VM lab: " + str(work), flush=True)
        command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", work / "client-key"])
        assets = build_trees(work / "assets", kernel, args.kernel_version, (work / "client-key.pub").read_text())
        private_write(work / "known_hosts", "[127.0.0.1]:{} ".format(lab.port) + assets["host_public_key"].strip() + "\n")
        tree = Path(assets["disk_tree"])
        (tree / "boot").mkdir(exist_ok=True)
        (tree / "usr/share/migratarr-lab").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(assets["kernel"], tree / "boot/vmlinuz")
        shutil.copyfile(assets["bootstrap_initramfs"], tree / "boot/bootstrap.img")
        shutil.copyfile(assets["rescue_initramfs"], tree / "usr/share/migratarr-lab/rescue.img")
        disk_work = work / "disk"
        disk_work.mkdir(mode=0o700)
        info = build_disk(disk_work, tree, assets["root_owned_paths"])
        shutil.copyfile(OVMF_VARS, work / "OVMF_VARS.fd")
        verification = lab.exercise(info)
        result.update(status="passed", disk_verification=verification)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
        print("VM lab failed: " + str(exc), file=sys.stderr, flush=True)
    finally:
        lab.stop()
        for handle in lab.log_handles:
            handle.close()
        private_write(work / "result.json", json.dumps(result, indent=2) + "\n")
        print("Evidence: " + str(work / "result.json"), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
