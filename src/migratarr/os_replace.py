"""Whole-OS replacement ONLY in nonce-marked, offline disposable QEMU guests.

First case: Ubuntu 20.04 ext4 root -> Ubuntu 24.04 ext4 root + separate boot,
retaining UEFI binaries, management access and destination filesystem identities.
"""
import hashlib
import json
import os
from pathlib import Path
import pkgutil
import re
import shlex
import signal
import shutil
import stat
import subprocess
import time
import zlib

from .os_census import census, removal_paths
from .path_policy import PROTECTED, RECONCILE, RUNTIME, COLLISION_PATHS
from .recovery import _directory, _json, _locked, _read, _write, _write_json, _fingerprint
from .transfer import fixture_context, ssh_arguments, clean_scratch

SOURCE_MARKER = "/etc/migratarr-os-source-test.json"
OPAQUE = sorted(set(PROTECTED) | set(RECONCILE) | set(RUNTIME) | {SOURCE_MARKER, "/lost+found"})
HEADROOM = 64 * 1024 * 1024


def release(path):
    fields = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value.strip('"')
    return [fields.get("ID"), fields.get("VERSION_ID")]


def source_code(token, scope=None):
    if not re.fullmatch(r"[a-f0-9]{32,64}", token):
        raise ValueError("invalid source VM nonce")
    code = '''import os,json,pathlib,stat,sys,subprocess
assert os.geteuid()==0
assert pathlib.Path('/sys/class/dmi/id/sys_vendor').read_text().strip().lower() in ('qemu','bochs')
root=pathlib.Path('/source')
assert root.resolve()==root
marker=root/'etc/migratarr-os-source-test.json'
s=marker.lstat()
assert stat.S_ISREG(s.st_mode) and s.st_uid==0 and s.st_mode & 0o077==0
assert json.loads(marker.read_text())=={'purpose':'migratarr_os_source_vm','token':TOKEN}
mounts=[line.split() for line in pathlib.Path('/proc/mounts').read_text().splitlines()]
selected=[r for r in mounts if r[1]=='/source' or r[1].startswith('/source/')]
assert len(selected)==1 and selected[0][1:3]==['/source','ext4'] and 'ro' in selected[0][3].split(',')
assert selected[0][0]=='/dev/vda1' and root.stat().st_dev==os.stat('/dev/vda1').st_rdev
assert not any((root/p.lstrip('/')).exists() or (root/p.lstrip('/')).is_symlink() for p in COLLISIONS)
'''.replace("TOKEN", repr(token)).replace("COLLISIONS", repr(COLLISION_PATHS))
    if scope:
        if scope not in ("root", "boot"):
            raise ValueError("invalid OS copy scope")
        # With --protect-args rsync transmits the fixed scope path over its
        # protocol, rather than necessarily placing it in the SSH argv.
        return code + "\nassert '--server' in sys.argv and '--sender' in sys.argv\nos.execvp('rsync',['rsync']+sys.argv[1:])\n"
    code += pkgutil.get_data("migratarr", "os_census.py").decode()
    code += '''
fields={}
for line in (root/'etc/os-release').read_text().splitlines():
 if '=' in line:
  k,v=line.split('=',1);fields[k]=v.strip('"')
assert [fields.get('ID'),fields.get('VERSION_ID')]==['ubuntu','20.04']
for line in (root/'etc/fstab').read_text().splitlines():
 if line.strip() and not line.lstrip().startswith('#'):
  assert line.split()[1] in ('/','/boot/efi'), 'unsupported source filesystem map'
kernels=sorted(p.name[8:] for p in (root/'boot').glob('vmlinuz-*') if p.is_file())
assert kernels and all(__import__('re').fullmatch(r'[A-Za-z0-9_.+-]+',k) for k in kernels)
print(json.dumps({'release':['ubuntu','20.04'],'entries':census(str(root),OPAQUE),'kernels':kernels},separators=(',',':')))
'''.replace("OPAQUE", repr(OPAQUE))
    return code


def context(token):
    path, job, source = fixture_context(token)
    mounts = [line.split() for line in Path("/proc/mounts").read_text().splitlines()]
    selected = [row for row in mounts if row[1] == "/target" or row[1].startswith("/target/")]
    if sorted((r[0], r[1], r[2]) for r in selected) != [('/dev/vda1', '/target', 'ext4'), ('/dev/vda16', '/target/boot', 'ext4')]:
        raise ValueError("OS rehearsal requires exactly the planned root and separate boot mounts")
    disks = [p.name for p in Path('/sys/block').iterdir() if not p.name.startswith(('loop', 'ram'))]
    if disks != ["vda"]:
        raise ValueError("OS rehearsal requires exactly one destination disk")
    if os.stat('/target/boot').st_dev != os.stat('/dev/vda16').st_rdev:
        raise ValueError("boot mount is not backed by the expected device")
    return path, job, source


def remote_evidence(source, nonce):
    host, ssh = ssh_arguments(source)
    result = subprocess.run(ssh + ['--', host, 'python3 -I -B -c ' + shlex.quote(source_code(nonce))],
                            capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise ValueError("read-only source OS guard failed: " + result.stderr[-1000:])
    return json.loads(result.stdout)


def snapshot(job, source, nonce):
    remote = remote_evidence(source, nonce)
    destination = census('/target', OPAQUE)
    if any(stat.S_ISBLK(row[3]) or stat.S_ISCHR(row[3]) for row in remote['entries'].values()):
        raise ValueError("source device nodes outside runtime scopes are unsupported")
    for name in ('/boot', '/var', '/var/lib', '/etc'):
        if any(entries.get(name, [None])[0] != 'd' for entries in (remote['entries'], destination)):
            raise ValueError("protected ancestors must be directories")
    counts = {}
    for row in destination.values():
        if row[0] != 'd':
            counts[(row[1], row[2])] = counts.get((row[1], row[2]), 0) + 1
    if any(row[0] != 'd' and row[9] != counts[(row[1], row[2])] for row in destination.values()):
        raise ValueError("destination links cross a protected namespace")
    return {'schema_version': 1, 'scope': 'offline_qemu_whole_os_only', 'migration_ready': False,
            'job_id': job['job_id'], 'target_uuid': job['target_uuid'], 'source_nonce': nonce,
            'source': remote, 'destination': destination,
            'remove': removal_paths(remote['entries'], destination, OPAQUE)}


def preview_os(token, source_token):
    path, job, source = context(token)
    with _locked(path):
        if release('/target/etc/os-release') != ['ubuntu', '24.04']:
            raise ValueError("first OS replacement case requires an Ubuntu 24.04 destination")
        report = snapshot(job, source, source_token)
        data = zlib.compress(json.dumps(report, sort_keys=True, separators=(',', ':')).encode())
        target = path / 'os-preview.z'
        _write(target, data, replace=target.exists())
        return {'status': 'preview_ready', 'scope': report['scope'], 'migration_ready': False,
                'source_release': report['source']['release'], 'destination_release': ['ubuntu', '24.04'],
                'source_entries': len(report['source']['entries']), 'destination_entries': len(report['destination']),
                'cleanup_entries': len(report['remove']), 'preview_sha256': _fingerprint(data)}


def remove_entry(path, row):
    # Descriptor traversal prevents redirecting the exact reviewed leaf.
    if not path.startswith('/') or path == '/' or any(p in ('', '.', '..') for p in path[1:].split('/')):
        raise ValueError("invalid OS cleanup path")
    fd = os.open('/target', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = path[1:].split('/')
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        if [info.st_dev, info.st_ino, info.st_mode] != row[1:4]:
            raise ValueError("OS cleanup identity changed")
        if row[0] == 'd':
            os.rmdir(parts[-1], dir_fd=fd)
        else:
            if info.st_size != row[6] or info.st_mtime_ns != row[7]:
                raise ValueError("OS cleanup file changed")
            os.unlink(parts[-1], dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)


def scope_filters(scope):
    paths = list(OPAQUE)
    if scope == 'root':
        return [p + ('/*' if p in RUNTIME else '') for p in paths] + ['/boot/*']
    return [p[len('/boot'):] for p in paths if p.startswith('/boot/')]


def copy_arguments(source, nonce, scope, scratch):
    host, ssh = ssh_arguments(source)
    remote = 'python3 -I -B -c ' + shlex.quote(source_code(nonce, scope))
    relative = '' if scope == 'root' else 'boot/'
    args = ['rsync', '--archive', '--hard-links', '--acls', '--xattrs', '--numeric-ids', '--one-file-system',
            '--checksum', '--sparse', '--fsync', '--timeout=60', '--protect-args',
            '--temp-dir=' + str(scratch), '--rsync-path=' + remote, '-e', shlex.join(ssh)]
    for pattern in scope_filters(scope):
        args.extend(['--exclude', pattern])
    args.extend(['--', host + ':/source/' + relative, '/target/' + relative])
    return args


class PayloadMismatch(ValueError):
    pass


def verify_scope(source, nonce, scope, scratch):
    args = copy_arguments(source, nonce, scope, scratch)
    result = subprocess.run(args[:1] + ['--dry-run', '--itemize-changes'] + args[1:],
                            capture_output=True, text=True, timeout=1200)
    if result.returncode:
        raise ValueError('OS payload verification failed: ' + result.stderr[-1000:])
    differences = []
    for line in result.stdout.splitlines():
        # Reconciliation creates entries under existing directories; their
        # mtimes may change, but file contents/ownership/modes must still match.
        if len(line) >= 11 and line[1] == 'd' and all(c == '.' or i == 4 for i, c in enumerate(line[:11]) if i not in (0, 1)):
            continue
        differences.append(line)
    if differences:
        raise PayloadMismatch('copied OS differs from the frozen source: ' + repr(differences[:8]))


def copy_scope(source, nonce, scope, scratch, log):
    args = copy_arguments(source, nonce, scope, scratch)
    with log.open('ab') as errors:
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=errors, start_new_session=True)
    try:
        while process.poll() is None:
            if any(os.statvfs(root).f_bavail * os.statvfs(root).f_frsize < HEADROOM for root in ('/target', '/target/boot')):
                raise ValueError("OS copy stopped to preserve recovery headroom")
            time.sleep(0.25)
        if process.returncode:
            raise ValueError("OS rsync failed; inspect the RAM diagnostic log")
        # Flush the directory changes after rsync's final file rename, too.
        subprocess.run(['sync'], check=True, timeout=60)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=10)


def grub_config(kernel, root_uuid, boot_uuid, job_id):
    import uuid
    root_uuid, boot_uuid, job_id = (str(uuid.UUID(v)) for v in (root_uuid, boot_uuid, job_id))
    if not re.fullmatch(r'[A-Za-z0-9_.+-]+', kernel):
        raise ValueError("invalid source kernel version")
    return '''set timeout=3
set default=0
load_env
if [ "${migratarr_os_attempted}" = "1" ]; then set default=1; fi
menuentry 'Migratarr imported Ubuntu' {
 search --no-floppy --fs-uuid --set=root BOOT
 set migratarr_os_attempted=1
 save_env migratarr_os_attempted
 linux /vmlinuz-KERNEL root=UUID=ROOT ro console=ttyS0,115200 net.ifnames=0 panic=10
 initrd /initrd.img-KERNEL
}
menuentry 'Migratarr RAM recovery' {
 search --no-floppy --fs-uuid --set=root BOOT
 linux /migratarr/JOB/kernel console=ttyS0,115200 migratarr.job=JOB migratarr.target_uuid=ROOT migratarr.boot_uuid=BOOT
 initrd /migratarr/JOB/initrd.img
}
'''.replace('KERNEL', kernel).replace('ROOT', root_uuid).replace('BOOT', boot_uuid).replace('JOB', job_id)


def reconcile(job, kernels, journal):
    boot_uuid = subprocess.check_output(['blkid', '-s', 'UUID', '-o', 'value', '/dev/vda16'], text=True).strip()
    if boot_uuid != job['assets']['boot_uuid']:
        raise ValueError('destination boot UUID differs from the prepared recovery job')
    efi_uuid = subprocess.check_output(['blkid', '-s', 'UUID', '-o', 'value', '/dev/vda15'], text=True).strip()
    fstab = 'UUID={} / ext4 defaults 0 1\nUUID={} /boot ext4 defaults 0 2\nUUID={} /boot/efi vfat umask=0077 0 1\n'.format(job['target_uuid'], boot_uuid, efi_uuid)
    Path('/target/etc/fstab').write_text(fstab)
    Path('/target/etc/crypttab').write_text('')
    resume = Path('/target/etc/initramfs-tools/conf.d/resume')
    resume.parent.mkdir(parents=True, exist_ok=True); resume.write_text('RESUME=none\n')
    Path('/target/etc/cloud/cloud-init.disabled').touch()
    # The imported OS clears the boot-attempt flag only after multi-user startup.
    service = Path('/target/etc/systemd/system/migratarr-os-boot-success.service')
    service.write_text('[Unit]\nAfter=network.target\n[Service]\nType=oneshot\nExecStart=/usr/bin/grub-editenv /boot/grub/grubenv unset migratarr_os_attempted\n[Install]\nWantedBy=multi-user.target\n')
    wants = service.parent / 'multi-user.target.wants'
    wants.mkdir(exist_ok=True)
    link = wants / service.name
    if link.is_symlink(): link.unlink()
    link.symlink_to('../' + service.name)
    kernel = sorted(kernels, key=lambda v: [int(p) if p.isdigit() else p for p in re.split(r'(\d+)', v)])[-1]
    mounted = []
    try:
        for path in ('dev', 'proc', 'sys'):
            subprocess.run(['mount', '--bind', '/' + path, '/target/' + path], check=True, timeout=30)
            mounted.append('/target/' + path)
        with journal.open('ab') as log:
            chroot = ['chroot'] if shutil.which('chroot') else ['/bin/busybox', 'chroot']
            subprocess.run(chroot + ['/target', '/usr/sbin/update-initramfs', '-u', '-k', kernel],
                           check=True, stdout=log, stderr=log, timeout=600)
    finally:
        for mount in reversed(mounted):
            subprocess.run(['umount', mount], check=True, timeout=30)
    config = Path('/target/boot/grub/grub.cfg')
    config.write_text(grub_config(kernel, job['target_uuid'], boot_uuid, job['job_id']))
    subprocess.run(['grub-editenv', '/target/boot/grub/grubenv', 'unset', 'migratarr_os_attempted', 'migratarr_job', 'migratarr_attempted'], check=True, timeout=30)
    return {'kernel': kernel, 'boot_uuid': boot_uuid, 'efi_uuid': efi_uuid,
            'retained': ['UEFI binaries', 'management networking', 'SSH access', 'destination machine-id'],
            'adapted': ['fstab', 'resume', 'initramfs', 'GRUB menu', 'cloud-init disabled']}


def reconcile_os(token, preview_sha256):
    """Verify already copied payloads before retrying boot reconciliation only."""
    path, job, source = context(token)
    with _locked(path):
        data = _read(path / 'os-preview.z', 64 * 1024 * 1024)
        if _fingerprint(data) != preview_sha256:
            raise ValueError('OS preview digest differs')
        decoder = zlib.decompressobj()
        raw = decoder.decompress(data, 128 * 1024 * 1024 + 1)
        if len(raw) > 128 * 1024 * 1024 or not decoder.eof:
            raise ValueError('invalid OS preview')
        saved = json.loads(raw)
        state = _json(path / 'os-transfer.json')
        if (saved['job_id'] != job['job_id'] or saved['target_uuid'] != job['target_uuid']
                or state.get('preview_sha256') != preview_sha256
                or state.get('scope') != 'offline_qemu_whole_os_only'
                or state.get('migration_ready') is not False
                or state.get('removed') != len(saved['remove'])):
            raise ValueError('OS reconciliation does not match the saved job')
        if remote_evidence(source, saved['source_nonce']) != saved['source']:
            raise ValueError('frozen source evidence changed')
        log = Path('/run/migratarr/os-transfer.log')
        if not log.exists(): _write(log, b'')
        try:
            state['phase'] = 'verify_copied_payload'
            _write_json(path / 'os-transfer.json', state, replace=True)
            for scope in ('root', 'boot'):
                scratch = (path if scope == 'root' else Path('/target/boot/migratarr') / job['job_id']) / 'os-scratch'
                _directory(scratch); clean_scratch(scratch)
                try:
                    verify_scope(source, saved['source_nonce'], scope, scratch)
                except PayloadMismatch:
                    if scope != 'boot':
                        raise
                    copy_scope(source, saved['source_nonce'], scope, scratch, log)
                    clean_scratch(scratch)
                    verify_scope(source, saved['source_nonce'], scope, scratch)
                    state['boot_payload_repaired'] = True
            state['phase'] = 'reconcile'
            _write_json(path / 'os-transfer.json', state, replace=True)
            state['boot'] = reconcile(job, saved['source']['kernels'], log)
            state.update(phase='ready_for_vm_boot', payload_verified=True)
            state.pop('error', None)
            _write_json(path / 'os-transfer.json', state, replace=True)
            subprocess.run(['sync'], check=True, timeout=60)
            return state
        except BaseException as exc:
            state.update(phase='failed', error=str(exc))
            _write_json(path / 'os-transfer.json', state, replace=True)
            raise


def replace_os(token, preview_sha256):
    path, job, source = context(token)
    if not re.fullmatch(r'[a-f0-9]{64}', preview_sha256):
        raise ValueError("exact OS preview digest required")
    with _locked(path):
        data = _read(path / 'os-preview.z', 64 * 1024 * 1024)
        if _fingerprint(data) != preview_sha256:
            raise ValueError("OS preview digest differs")
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(data, 128 * 1024 * 1024 + 1)
        if len(decoded) > 128 * 1024 * 1024 or not decoder.eof:
            raise ValueError("invalid or oversized OS preview")
        saved = json.loads(decoded)
        if snapshot(job, source, saved['source_nonce']) != saved:
            raise ValueError("OS preview is stale; no deletion started")
        state = {'phase': 'cleanup', 'migration_ready': False, 'scope': saved['scope'], 'preview_sha256': preview_sha256, 'removed': 0}
        checkpoint = path / 'os-transfer.json'
        log = Path('/run/migratarr/os-transfer.log')
        _write(log, b'', replace=log.exists())
        reserve = path / '.os-checkpoint-reserve'
        _write(reserve, b'\0' * (16 * 1024 * 1024))
        def save():
            try:
                _write_json(checkpoint, state, replace=checkpoint.exists())
            except OSError:
                if reserve.exists(): reserve.unlink()
                _write_json(checkpoint, state, replace=checkpoint.exists())
        try:
            save()
            for name in saved['remove']:
                remove_entry(name, saved['destination'][name]); state['removed'] += 1
                if state['removed'] % 250 == 0: save()
            save()
            for scope in ('root', 'boot'):
                state['phase'] = 'copy_' + scope; save()
                scratch = (path if scope == 'root' else Path('/target/boot/migratarr') / job['job_id']) / 'os-scratch'
                scratch.mkdir(mode=0o700, exist_ok=True); _directory(scratch); clean_scratch(scratch)
                maximum = max((r[6] for p, r in saved['source']['entries'].items() if r[0] == 'f' and p.startswith('/boot/') == (scope == 'boot')), default=0)
                available = os.statvfs(scratch)
                if available.f_bavail * available.f_frsize < maximum + HEADROOM:
                    raise ValueError("insufficient temporary headroom for the largest source file")
                copy_scope(source, saved['source_nonce'], scope, scratch, log); clean_scratch(scratch)
            state['phase'] = 'reconcile'; save()
            state['boot'] = reconcile(job, saved['source']['kernels'], log)
            if release('/target/etc/os-release') != ['ubuntu', '20.04']:
                raise ValueError("copied OS release differs from source")
            state['phase'] = 'ready_for_vm_boot'; save()
            subprocess.run(['sync'], check=True, timeout=60)
            return state
        except BaseException as exc:
            state.update(phase='failed', error=str(exc)); save()
            raise
