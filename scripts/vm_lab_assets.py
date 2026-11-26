"""Construct synthetic guest trees for the disposable RAM-recovery VM lab.

This helper reads host executables and kernel modules, and writes only into a
new caller-selected directory. It does not install a recovery environment on a
real server and is not a production Ubuntu bootstrap image.
"""

import gzip
import lzma
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import uuid


import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from migratarr.ramdisk import copy_binary as _copy_binary, copy_modules as _modules, pack_initramfs


def _run(argv):
    return subprocess.run([str(value) for value in argv], check=True, capture_output=True, timeout=60)


BUSYBOX_APPLETS = (
    "sh", "mount", "umount", "mkdir", "mknod", "cat", "cp", "mv", "rm",
    "sleep", "sync", "reboot", "poweroff", "insmod", "ip", "grep", "sed",
    "awk", "test", "ls", "readlink", "switch_root", "chmod", "printf",
    "uname", "head", "wc", "stat", "sha256sum", "cut", "tr", "sort", "find",
)
ROOT_OWNED_PATHS = (
    "/", "/root", "/root/.ssh", "/root/.ssh/authorized_keys", "/etc/passwd",
    "/etc/shadow", "/etc/dropbear", "/etc/dropbear/dropbear_ed25519_host_key",
)




def _write(root, relative, content, mode=0o644):
    path = root / relative.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)








NETWORK_AND_SSH = """\
/bin/busybox ip link set lo up
/bin/busybox ip link set eth0 up
/bin/busybox ip addr add 10.0.2.15/24 dev eth0
/bin/busybox ip route add default via 10.0.2.2
mkdir -p /dev/pts /run
mount -t devpts devpts /dev/pts
/usr/sbin/dropbear -F -E -s -g -r /etc/dropbear/dropbear_ed25519_host_key -p 22 &
"""


def _common_tree(root, client_public_key, host_key):
    for directory in ("bin", "sbin", "usr/sbin", "etc/dropbear", "proc", "sys", "dev", "run", "tmp", "root/.ssh", "target"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    busybox = shutil.which("busybox")
    dropbear = shutil.which("dropbear")
    if not busybox or not dropbear:
        raise ValueError("busybox-static and dropbear-bin are required for the VM lab")
    # Static busybox avoids a hidden dependency on the disk while in rescue RAM.
    target = root / "bin/busybox"
    shutil.copy2(busybox, str(target))
    dynamic = subprocess.run(["ldd", busybox], capture_output=True, text=True, timeout=20, check=False)
    if dynamic.returncode == 0:
        raise ValueError("the VM lab requires static busybox")
    for applet in BUSYBOX_APPLETS:
        (root / "bin" / applet).symlink_to("busybox")
    _copy_binary(root, dropbear)
    # The guest scripts use this stable path independent of host package layout.
    actual_dropbear = root / str(Path(dropbear)).lstrip("/")
    wanted_dropbear = root / "usr/sbin/dropbear"
    if actual_dropbear != wanted_dropbear:
        shutil.copy2(str(actual_dropbear), str(wanted_dropbear))
    _write(root, "etc/passwd", "root:x:0:0:lab root:/root:/bin/sh\n")
    _write(root, "etc/shadow", "root:*:19000:0:99999:7:::\n", 0o600)
    _write(root, "etc/group", "root:x:0:\n")
    _write(root, "etc/nsswitch.conf", "passwd: files\ngroup: files\nshadow: files\n")
    _write(root, "root/.ssh/authorized_keys", client_public_key.strip() + "\n", 0o600)
    (root / "root").chmod(0o700)
    (root / "root/.ssh").chmod(0o700)
    shutil.copy2(str(host_key), str(root / "etc/dropbear/dropbear_ed25519_host_key"))


def _initramfs_init(module_paths, rescue):
    lines = ["#!/bin/sh", "export PATH=/bin:/usr/sbin:/sbin", "mount -t devtmpfs devtmpfs /dev",
             "exec </dev/console >/dev/console 2>&1", "mount -t proc proc /proc", "mount -t sysfs sysfs /sys"]
    lines += ["insmod " + path for path in module_paths]
    lines += ["mkdir -p /target", "mount -t ext4 /dev/vda2 /target || exec sh"]
    if not rescue:
        lines += ["mount --move /dev /target/dev", "mount --move /proc /target/proc", "mount --move /sys /target/sys",
                  "exec switch_root /target /sbin/init"]
    else:
        lines += ["echo rescue > /run/migratarr-lab-mode", "job=/target/var/lib/migratarr/jobs/lab-job",
                  "test -s $job/job-id && test -s $job/phase || exec sh", "count=0", "test ! -f $job/resume-count || count=$(cat $job/resume-count)",
                  "case $count in ''|*[!0-9]*) exec sh;; esac", "count=$((count + 1))", "echo $count > $job/resume-count.tmp", "mv $job/resume-count.tmp $job/resume-count",
                  "if test \"$(cat $job/phase)\" = prepared; then", "echo rescue_ready > $job/phase.tmp", "mv $job/phase.tmp $job/phase", "fi", "sync", NETWORK_AND_SSH,
                  "echo MIGRATARR_LAB_RESCUE_READY", "while :; do sleep 1; done"]
    return "\n".join(lines) + "\n"


LAB_COMMAND = """\
#!/bin/sh
set -eu
export PATH=/bin:/usr/sbin:/sbin
mode=$(cat /run/migratarr-lab-mode)
prefix=
test "$mode" != rescue || prefix=/target
job=$prefix/var/lib/migratarr/jobs/lab-job
case "${1:-status}" in
  status)
    printf 'mode=%s\\n' "$mode"
    for name in job-id phase resume-count; do
      if test -f "$job/$name"; then printf '%s=' "$name"; cat "$job/$name"; fi
    done
    ;;
  prepare)
    test "$mode" = bootstrap
    mkdir -p /boot/migratarr "$job"
    cp /usr/share/migratarr-lab/rescue.img /boot/migratarr/rescue.img
    if test ! -f "$job/job-id"; then cp /etc/migratarr-lab-job-id "$job/job-id"; fi
    echo prepared > "$job/phase.tmp"
    mv "$job/phase.tmp" "$job/phase"
    echo armed > /boot/migratarr/armed
    sync
    ;;
  checkpoint)
    case "${2:-}" in ''|*[!a-zA-Z0-9_-]*) exit 2;; esac
    echo "$2" > "$job/phase.tmp"
    mv "$job/phase.tmp" "$job/phase"
    sync
    ;;
  unmount-target) test "$mode" = rescue; umount /target;;
  mount-target) test "$mode" = rescue; mount -t ext4 /dev/vda2 /target;;
  hide-rescue) mv "$prefix/boot/migratarr/rescue.img" "$prefix/boot/migratarr/rescue.img.disabled"; sync;;
  disarm) rm -f "$prefix/boot/migratarr/armed"; sync;;
  reboot) sync; reboot -f;;
  *) exit 2;;
esac
"""


def build_trees(directory, kernel_path, kernel_version, client_public_key):
    """Create new synthetic guest trees and root-owned RAM archives."""
    directory = Path(directory).absolute()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    if not isinstance(client_public_key, str) or not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?\n?", client_public_key):
        raise ValueError("the lab requires one generated Ed25519 client public key")
    kernel_path = Path(kernel_path)
    if not kernel_path.is_file() or kernel_path.is_symlink():
        raise ValueError("provide a readable regular kernel file copied into the lab workspace")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", kernel_version):
        raise ValueError("invalid kernel version")
    kernel = directory / "vmlinuz"
    shutil.copyfile(str(kernel_path), str(kernel))
    host_key = directory / "dropbear_ed25519_host_key"
    _run(["dropbearkey", "-t", "ed25519", "-f", host_key])
    host_public = _run(["dropbearkey", "-y", "-f", host_key]).stdout.decode()
    host_public = next(line for line in host_public.splitlines() if line.startswith("ssh-ed25519 "))
    trees = {name: directory / name for name in ("bootstrap-tree", "rescue-tree", "disk-tree")}
    for root in trees.values():
        root.mkdir()
        _common_tree(root, client_public_key, host_key)
        _write(root, "usr/sbin/migratarr-lab", LAB_COMMAND, 0o755)
    module_paths = _modules(trees["bootstrap-tree"], kernel_version)
    shutil.copytree(str(trees["bootstrap-tree"] / "lib/migratarr-lab-modules"),
                    str(trees["rescue-tree"] / "lib/migratarr-lab-modules"))
    _write(trees["bootstrap-tree"], "init", _initramfs_init(module_paths, False), 0o755)
    _write(trees["rescue-tree"], "init", _initramfs_init(module_paths, True), 0o755)
    _write(trees["disk-tree"], "sbin/init", "#!/bin/sh\nexport PATH=/bin:/usr/sbin:/sbin\necho bootstrap > /run/migratarr-lab-mode\n" +
           NETWORK_AND_SSH + "\necho MIGRATARR_LAB_BOOTSTRAP_READY\nwhile :; do sleep 1; done\n", 0o755)
    job_id = str(uuid.uuid4())
    _write(trees["disk-tree"], "etc/migratarr-lab-job-id", job_id + "\n", 0o600)
    (trees["disk-tree"] / "boot").mkdir()
    shutil.copyfile(str(kernel), str(trees["disk-tree"] / "boot/vmlinuz"))
    _write(trees["disk-tree"], "boot/migratarr-lab-marker", "synthetic single-disk VM only\n")
    bootstrap = directory / "bootstrap.img"
    rescue = directory / "rescue.img"
    pack_initramfs(trees["bootstrap-tree"], bootstrap)
    pack_initramfs(trees["rescue-tree"], rescue)
    shutil.copyfile(str(bootstrap), str(trees["disk-tree"] / "boot/bootstrap.img"))
    seed = trees["disk-tree"] / "usr/share/migratarr-lab/rescue.img"
    seed.parent.mkdir(parents=True)
    shutil.copyfile(str(rescue), str(seed))
    return {"bootstrap_tree": trees["bootstrap-tree"], "rescue_tree": trees["rescue-tree"],
            "disk_tree": trees["disk-tree"], "bootstrap_initramfs": bootstrap,
            "rescue_initramfs": rescue, "kernel": kernel, "host_public_key": host_public,
            "job_id": job_id, "root_owned_paths": ROOT_OWNED_PATHS}
