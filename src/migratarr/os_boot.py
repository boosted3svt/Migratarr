"""Restartable boot reconciliation for the guarded single-disk VM case."""
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess

from .recovery import _directory


def generated_paths(kernels):
    return ['/etc/cloud/cloud-init.disabled', '/etc/systemd/system/migratarr-os-boot-success.service',
            '/etc/systemd/system/multi-user.target.wants/migratarr-os-boot-success.service'] + [
                '/var/lib/initramfs-tools/' + kernel for kernel in kernels]


def install_staged(stage, target, mode=0o644):
    stage, target = Path(stage), Path(target)
    if target.parent.resolve() != target.parent or stage.parent.resolve() != stage.parent:
        raise ValueError('boot reconciliation parent is a symlink')
    if stage.stat().st_dev != target.parent.stat().st_dev:
        raise ValueError('boot reconciliation staging must use the same filesystem')
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ValueError('boot reconciliation target is not an owned regular file')
    fd = os.open(str(stage), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ValueError('invalid staged boot file')
        os.fchmod(fd, mode); os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(stage), str(target))
    for directory in {target.parent, stage.parent}:
        fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)


def atomic_write(stage_dir, target, contents, mode=0o644):
    staged = Path(stage_dir) / ('write-' + secrets.token_hex(8))
    fd = os.open(str(staged), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(contents.encode()); handle.flush(); os.fsync(handle.fileno())
        install_staged(staged, target, mode)
    finally:
        if staged.exists(): staged.unlink()


def atomic_link(stage_dir, target, value):
    target = Path(target)
    if target.parent.resolve() != target.parent:
        raise ValueError('boot unit parent is a symlink')
    if (target.exists() or target.is_symlink()) and (not target.is_symlink() or os.readlink(target) != value):
        raise ValueError('boot unit link differs from the planned link')
    staged = Path(stage_dir) / ('link-' + secrets.token_hex(8))
    os.symlink(value, staged)
    try:
        os.replace(str(staged), str(target))
        fd = os.open(str(target.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if staged.is_symlink(): staged.unlink()


def reconcile_boot(job, kernels, journal):
    from .os_replace import grub_config
    if not kernels or any(not re.fullmatch(r'[A-Za-z0-9_.+-]+', k) for k in kernels):
        raise ValueError('invalid source kernel list')
    boot_uuid = subprocess.check_output(['blkid', '-s', 'UUID', '-o', 'value', '/dev/vda16'], text=True).strip()
    if boot_uuid != job['assets']['boot_uuid']:
        raise ValueError('destination boot UUID differs from the prepared recovery job')
    efi_uuid = subprocess.check_output(['blkid', '-s', 'UUID', '-o', 'value', '/dev/vda15'], text=True).strip()
    stage = Path('/target/var/lib/migratarr/jobs') / job['job_id'] / 'reconcile-staging'
    boot_stage = Path('/target/boot/migratarr') / job['job_id'] / 'reconcile-staging'
    for directory in (stage, boot_stage):
        directory.mkdir(mode=0o700, exist_ok=True); _directory(directory)
    fstab = 'UUID={} / ext4 defaults 0 1\nUUID={} /boot ext4 defaults 0 2\nUUID={} /boot/efi vfat umask=0077 0 1\n'.format(job['target_uuid'], boot_uuid, efi_uuid)
    atomic_write(stage, '/target/etc/fstab', fstab)
    atomic_write(stage, '/target/etc/crypttab', '')
    resume = Path('/target/etc/initramfs-tools/conf.d/resume')
    resume.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(stage, resume, 'RESUME=none\n')
    atomic_write(stage, '/target/etc/cloud/cloud-init.disabled', '')
    unit = Path('/target/etc/systemd/system/migratarr-os-boot-success.service')
    atomic_write(stage, unit, '[Unit]\nAfter=network.target\n[Service]\nType=oneshot\nExecStart=/usr/bin/grub-editenv /boot/grub/grubenv unset migratarr_os_attempted\n[Install]\nWantedBy=multi-user.target\n')
    wants = unit.parent / 'multi-user.target.wants'; wants.mkdir(exist_ok=True)
    atomic_link(stage, wants / unit.name, '../' + unit.name)
    kernel = sorted(kernels, key=lambda v: [int(p) if p.isdigit() else p for p in re.split(r'(\d+)', v)])[-1]
    output = boot_stage / 'initrd-staged'
    if output.exists():
        info = output.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
            raise ValueError('unexpected initramfs staging file')
        output.unlink()
    temporary = Path('/run/migratarr/initramfs-work')
    temporary.mkdir(mode=0o700, exist_ok=True); _directory(temporary)
    mounted = []
    try:
        for name in ('dev', 'proc', 'sys', 'run'):
            subprocess.run(['mount', '--bind', '/' + name, '/target/' + name], check=True, timeout=30)
            mounted.append('/target/' + name)
        chroot = ['chroot'] if shutil.which('chroot') else ['/bin/busybox', 'chroot']
        with Path(journal).open('ab') as log:
            subprocess.run(chroot + ['/target', '/usr/bin/env', 'TMPDIR=/run/migratarr/initramfs-work',
                           '/usr/sbin/mkinitramfs', '-o', str(output)[len('/target'):], kernel],
                           check=True, stdout=log, stderr=log, timeout=900)
    finally:
        for mount in reversed(mounted): subprocess.run(['umount', mount], check=True, timeout=30)
    digest = hashlib.sha1(output.read_bytes()).hexdigest()
    install_staged(output, Path('/target/boot/initrd.img-' + kernel), 0o600)
    cache = Path('/target/var/lib/initramfs-tools'); cache.mkdir(exist_ok=True)
    atomic_write(stage, cache / kernel, digest + '  /boot/initrd.img-' + kernel + '\n')
    atomic_write(boot_stage, '/target/boot/grub/grub.cfg', grub_config(kernel, job['target_uuid'], boot_uuid, job['job_id']))
    subprocess.run(['grub-editenv', '/target/boot/grub/grubenv', 'unset', 'migratarr_os_attempted', 'migratarr_job', 'migratarr_attempted'], check=True, timeout=30)
    subprocess.run(['sync'], check=True, timeout=60)
    return {'kernel': kernel, 'boot_uuid': boot_uuid, 'efi_uuid': efi_uuid,
            'retained': ['UEFI binaries', 'management networking', 'SSH access', 'destination machine-id'],
            'adapted': ['fstab', 'resume', 'initramfs', 'GRUB menu', 'cloud-init disabled']}
