"""Construct a self-contained VM rehearsal image carrying the standalone CLI."""

import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig

from .ramdisk import copy_binary, copy_modules, pack_initramfs
from .recovery import _directory, _fingerprint, _write, _write_json


def extra_network_commands(networks, primary_interface="eth0"):
    """Validate optional directly connected IPv4 interfaces before shell use."""
    commands = []
    interfaces = {primary_interface}
    for network in networks:
        if not isinstance(network, str) or network.count("=") != 1:
            raise ValueError("extra network must be INTERFACE=IPv4/CIDR")
        interface, address = network.split("=", 1)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", interface) or interface in interfaces:
            raise ValueError("extra network interfaces must be valid and unique")
        address = str(ipaddress.IPv4Interface(address))
        interfaces.add(interface)
        commands.extend(["ip link set " + interface + " up",
                         "ip addr add " + address + " dev " + interface])
    return commands


def build_vm_bundle(output, kernel, kernel_version, application, client_public_key, token,
                    interface="eth0", address="10.0.2.15/24", gateway="10.0.2.2", extra_networks=()):
    if not re.fullmatch(r"[a-f0-9]{32,64}", token):
        raise ValueError("invalid VM rehearsal token")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", kernel_version) or not re.fullmatch(r"[A-Za-z0-9_-]+", interface):
        raise ValueError("invalid VM kernel or interface")
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?\n?", client_public_key):
        raise ValueError("one Ed25519 management key is required")
    address = str(ipaddress.IPv4Interface(address))
    gateway = str(ipaddress.IPv4Address(gateway))
    extra_commands = extra_network_commands(extra_networks, interface)
    for path in (kernel, application):
        if Path(path).is_symlink() or not Path(path).is_file():
            raise ValueError("VM bundle inputs must be regular files")
    output = Path(output).absolute()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    _directory(output)
    tree = output / "tree"
    tree.mkdir(mode=0o700)
    for path in ("bin", "sbin", "usr/bin", "usr/sbin", "etc/dropbear", "root/.ssh", "proc", "sys", "dev", "target", "run/migratarr", "tmp", "opt"):
        (tree / path).mkdir(parents=True, exist_ok=True)
    (tree / "run/migratarr").chmod(0o700)
    busybox = shutil.which("busybox")
    if not busybox:
        raise ValueError("VM bundle requires BusyBox")
    copy_binary(tree, busybox)
    copied_busybox = tree / busybox.lstrip("/")
    if copied_busybox != tree / "bin/busybox":
        shutil.copy2(copied_busybox, tree / "bin/busybox")
    for applet in ("sh", "mount", "umount", "mkdir", "cat", "sleep", "sync", "insmod", "ip", "reboot", "poweroff", "awk"):
        (tree / "bin" / applet).symlink_to("busybox")
    for binary in ("dropbear", "blkid", "grub-editenv", "ssh", "rsync"):
        executable = shutil.which(binary)
        if not executable:
            raise ValueError("VM bundle requires " + binary)
        copy_binary(tree, executable)
        desired = tree / ("usr/sbin/dropbear" if binary == "dropbear" else "sbin/blkid" if binary == "blkid" else "usr/bin/" + binary)
        actual = tree / executable.lstrip("/")
        if desired != actual:
            shutil.copy2(actual, desired)
    copy_binary(tree, sys.executable)
    copied_python = tree / str(Path(sys.executable)).lstrip("/")
    if copied_python != tree / "usr/bin/python3":
        shutil.copy2(copied_python, tree / "usr/bin/python3")
    library = Path(sysconfig.get_path("stdlib"))
    shutil.copytree(library, tree / str(library).lstrip("/"),
                    ignore=shutil.ignore_patterns("__pycache__", "site-packages", "dist-packages", "test", "tests", "idlelib", "tkinter", "ensurepip"))
    # Some extension modules have dependencies not used by the Python binary.
    for extension in library.glob("lib-dynload/*.so"):
        copy_binary(tree, str(extension))
    shutil.copyfile(application, tree / "opt/migratarr.pyz")
    key = output / "ssh_host_key"
    subprocess.run(["dropbearkey", "-t", "ed25519", "-f", str(key)], capture_output=True, check=True, timeout=20)
    public = subprocess.run(["dropbearkey", "-y", "-f", str(key)], capture_output=True, text=True, check=True, timeout=20).stdout
    public = next(line for line in public.splitlines() if line.startswith("ssh-ed25519 "))
    shutil.copy2(key, tree / "etc/dropbear/host_key")
    _write(tree / "root/.ssh/authorized_keys", client_public_key.strip().encode() + b"\n")
    (tree / "root").chmod(0o700)
    (tree / "root/.ssh").chmod(0o700)
    for name, data in (("passwd", b"root:x:0:0:root:/root:/bin/sh\n"), ("shadow", b"root:*:19000:0:99999:7:::\n"), ("group", b"root:x:0:\n"), ("nsswitch.conf", b"passwd: files\ngroup: files\nshadow: files\n")):
        _write(tree / "etc" / name, data)
    _write_json(tree / "etc/migratarr-recovery-test.json", {"purpose": "migratarr_disposable_vm", "token": token})
    modules = copy_modules(tree, kernel_version)
    script = ["#!/bin/sh", "export PATH=/bin:/sbin:/usr/bin:/usr/sbin", "mount -t devtmpfs devtmpfs /dev",
              "exec </dev/console >/dev/console 2>&1", "mount -t proc proc /proc", "mount -t sysfs sysfs /sys"]
    script.extend("insmod " + path for path in modules)
    script.extend(["job=", "target_uuid=", "boot_uuid=", "for arg in $(cat /proc/cmdline); do",
                   "case $arg in migratarr.job=*) job=${arg#migratarr.job=};; migratarr.target_uuid=*) target_uuid=${arg#migratarr.target_uuid=};; migratarr.boot_uuid=*) boot_uuid=${arg#migratarr.boot_uuid=};; esac", "done",
                   "case $job:$target_uuid in *[!a-f0-9:-]*|:*) exec sh;; esac",
                   "device=$(/sbin/blkid -U \"$target_uuid\")", "mount -t ext4 \"$device\" /target || exec sh",
                   "if test -n \"$boot_uuid\" && test \"$boot_uuid\" != \"$target_uuid\"; then mount -t ext4 \"$(/sbin/blkid -U \"$boot_uuid\")\" /target/boot || exec sh; fi",
                   "mkdir -p /run/migratarr /dev/pts", "mount -t devpts devpts /dev/pts",
                   "umask 077", "printf '{\"job_id\":\"%s\",\"target_uuid\":\"%s\",\"target_device\":\"%s\"}\\n' \"$job\" \"$target_uuid\" \"$device\" > /run/migratarr/recovery-context.json",
                   "/usr/bin/python3 -I -S -B /opt/migratarr.pyz recovery resume --job /target/var/lib/migratarr/jobs/$job --vm-token " + token + " > /run/migratarr/resume.log 2>&1",
                   "if test $? = 0; then echo rescue > /run/migratarr-lab-mode; else cat /run/migratarr/resume.log; echo rescue_failed > /run/migratarr-lab-mode; fi",
                   "ip link set lo up", "ip link set " + interface + " up", "ip addr add " + address + " dev " + interface,
                   "ip route add default via " + gateway])
    script.extend(extra_commands)
    script.extend([
                   "/usr/sbin/dropbear -F -E -s -g -r /etc/dropbear/host_key -p 22 &",
                   "echo MIGRATARR_NATIVE_RECOVERY_READY", "while :; do sleep 1; done"])
    _write(tree / "init", ("\n".join(script) + "\n").encode())
    (tree / "init").chmod(0o755)
    _write(output / "kernel", Path(kernel).read_bytes())
    pack_initramfs(tree, output / "initrd.img")
    (output / "initrd.img").chmod(0o600)
    manifest = {"purpose": "recovery_vm_rehearsal", "kernel_version": kernel_version,
                "host_public_key": public,
                "sha256": {name: _fingerprint((output / name).read_bytes()) for name in ("kernel", "initrd.img")}}
    _write_json(output / "manifest.json", manifest)
    return {"output_directory": str(output), **manifest}
