#!/usr/bin/env python3
"""Finish boot reconciliation in saved disposable whole-OS lab images."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time

from test_recovery_vm import ROOT, RecoveryLab, command, private_write
from test_ubuntu_recovery_vm import UbuntuRecoveryLab, digest, disk_invariants
from test_source_connectivity_vm import peer_network, socket_port
from test_os_replace_vm import fixture_command, wait_imported, boot_filesystem_uuid


def write_report(path, value):
    # Reports stay in the owned private lab folder; never touch published code.
    path.write_text(json.dumps(value, indent=2) + '\n'); path.chmod(0o600)


def saved_lab(cls, directory):
    lab = cls(directory, 420)
    lab.memory_mb = 2560
    lab.ssh_timeout = 1800
    lab.port = int(re.search(r'\[127\.0\.0\.1\]:(\d+)', (directory / 'known_hosts').read_text()).group(1))
    lab.launches = max(int(p.stem.split('-')[1]) for p in directory.glob('serial-*.log'))
    return lab


def disk_boot_or_recovery(lab):
    deadline = time.monotonic() + lab.boot_timeout
    code = "import json,pathlib; p=pathlib.Path('/etc/os-release'); fields=dict(line.split('=',1) for line in p.read_text().splitlines() if '=' in line) if p.exists() else {}; m=pathlib.Path('/run/migratarr-lab-mode'); print(json.dumps([fields.get('VERSION_ID','').strip(chr(34)),m.read_text().strip() if m.exists() else '']))"
    while time.monotonic() < deadline:
        response = lab.ssh('python3 -I -B -c ' + shlex.quote(code), required=False)
        if response.returncode == 0:
            release, mode = json.loads(response.stdout)
            if release == '20.04': return 'imported'
            if mode == 'rescue': return 'rescue'
            if mode == 'rescue_failed': raise RuntimeError('saved RAM recovery could not resume')
        time.sleep(2)
    raise RuntimeError('saved guest did not reach imported OS or RAM recovery')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True)
    parser.add_argument('--destination-image', required=True)
    parser.add_argument('--verify-boot-only', action='store_true', help='Verify a saved ready-for-boot result without repeating copy/reconciliation')
    args = parser.parse_args(argv)
    work = Path(args.work).absolute()
    if os.geteuid() == 0 or work.resolve() != work or work.parent != ROOT / 'reports':
        parser.error('run unprivileged with an existing private lab under reports')
    info = work.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077: parser.error('lab directory must be private and owned')
    previous = json.loads((work / 'result.json').read_text())
    if previous['scope'] != 'whole_os_ubuntu_20_04_to_24_04_vm_only': parser.error('not a whole-OS lab')
    image = Path(args.destination_image)
    if image.is_symlink() or digest(image) != previous['destination_image_sha256']: parser.error('destination image checksum differs')
    for p in (work / 'destination.img', work / 'source/source.img'):
        s = p.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_nlink != 1:
            parser.error('VM disks must be owned regular files without links')
    source = saved_lab(RecoveryLab, work / 'source')
    destination = saved_lab(UbuntuRecoveryLab, work)
    port = socket_port()
    source.extra_qemu_args = peer_network('listen', port) + ['-kernel', str(work / 'source/bundle/kernel'),
        '-initrd', str(work / 'source/bundle/source-initrd.img'), '-append', 'console=ttyS0,115200 rdinit=/init net.ifnames=0']
    destination.extra_qemu_args = peer_network('connect', port)
    token = json.loads((work / 'bundle/tree/etc/migratarr-recovery-test.json').read_text())['token']
    preview = json.loads((work / 'os-preview-summary.json').read_text())
    report = {'status': 'failed', 'production_qualified': False, 'scope': previous['scope'],
              'continued_from': 'completed_copy_failed_boot_reconciliation', 'events': destination.events,
              'source_events': source.events, 'original_attempt': previous}
    print('Resuming saved whole-OS lab: ' + str(work), flush=True)
    try:
        before_image = work / 'baseline-invariants.img'
        if not before_image.exists(): command(['qemu-img', 'convert', '-f', 'qcow2', '-O', 'raw', image, before_image])
        if before_image.is_symlink() or not before_image.is_file(): raise ValueError('invalid baseline image')
        before = disk_invariants(before_image)
        original_boot_uuid = boot_filesystem_uuid(before_image)
        source_before = digest(work / 'source/source.img')
        source.start(work / 'source/source.img'); source.wait_guest('source_readonly')
        expected_hashes = json.loads(source.ssh("python3 -I -B -c \"import json,hashlib,pathlib;print(json.dumps([hashlib.sha256(pathlib.Path('/source'+p).read_bytes()).hexdigest() for p in ('/etc/passwd','/etc/group','/var/lib/dpkg/status')]))\"").stdout)
        expected = json.loads(source.ssh(fixture_command('snapshot /source/srv/migratarr-os-fixture')).stdout)
        write_report(work / 'frozen-source-metadata.json', expected)
        destination.start(work / 'destination.img')
        if args.verify_boot_only:
            result = json.loads((work / 'os-transfer-result.json').read_text())
            if result['phase'] != 'ready_for_vm_boot' or result.get('payload_verified') is not True:
                raise RuntimeError('saved transfer has not verified its copied payload')
            if disk_boot_or_recovery(destination) == 'rescue':
                destination.ssh('grub-editenv /target/boot/grub/grubenv unset migratarr_os_attempted\nsync\numount /target/boot\numount /target\npoweroff -f', required=False)
                destination.process.wait(timeout=60); destination.stop(); destination.start(work / 'destination.img')
            wait_imported(destination)
        else:
            destination.wait_guest('rescue')
            command([sys.executable, ROOT / 'scripts/build.py'])
            destination.upload(ROOT / 'dist/migratarr.pyz', '/opt/migratarr.pyz')
            check = destination.ssh("/bin/busybox mkdir -p /target/run/sshd\n/bin/busybox chroot /target /usr/sbin/sshd -t", required=False)
            private_write(work / ('ssh-config-check-' + str(destination.launches) + '.txt'), check.stdout + check.stderr)
            if check.returncode: raise RuntimeError('imported SSH configuration requires reconciliation: ' + check.stderr)
            result = json.loads(destination.run_cli('recovery os-reconcile-vm --vm-token ' + token + ' --preview-sha256 ' + preview['preview_sha256']))
            write_report(work / 'os-transfer-result.json', result)
            destination.record('copied_payload_verified_and_boot_reconciled', **result['boot'])
            destination.ssh('sync\numount /target/boot\numount /target\npoweroff -f', required=False)
            destination.process.wait(timeout=60); destination.stop()
            destination.start(work / 'destination.img'); wait_imported(destination)
        actual = json.loads(destination.ssh(fixture_command('snapshot /srv/migratarr-os-fixture')).stdout)
        write_report(work / 'imported-metadata.json', actual)
        if actual != expected:
            different = [key for key in set(actual['entries']) | set(expected['entries']) if actual['entries'].get(key) != expected['entries'].get(key)]
            raise RuntimeError('application metadata differs: ' + repr(different))
        hashes = json.loads(destination.ssh("python3 -I -B -c \"import json,hashlib,pathlib;print(json.dumps([hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() for p in ('/etc/passwd','/etc/group','/var/lib/dpkg/status')]))\"").stdout)
        if hashes != expected_hashes: raise RuntimeError('source account/package database differs')
        proof = json.loads(destination.ssh('cat /var/lib/migratarr-witness/boot-proof.json').stdout)
        destination.ssh('systemctl is-active migratarr-witness.service\ntest ! -e /usr/local/share/migratarr-bootstrap-only')
        kernel = destination.ssh('uname -r').stdout.strip()
        expected_kernel = next(e['kernel'] for e in previous['source_events'] if e['event'] == 'older_source_booted_with_application_state')
        if kernel != expected_kernel or proof != {'release': '20.04', 'python': [3, 8], 'state': 'source application state'}:
            raise RuntimeError('imported kernel or application service proof differs')
        uuids = destination.ssh("findmnt -n -o UUID /\nfindmnt -n -o UUID /boot\nfindmnt -n -o UUID /boot/efi").stdout.splitlines()
        if result['boot']['boot_uuid'] != original_boot_uuid: raise RuntimeError('original destination boot UUID changed')
        if uuids != [before['root_uuid'], result['boot']['boot_uuid'], result['boot']['efi_uuid']]: raise RuntimeError('filesystem identities differ')
        destination.record('older_os_kernel_accounts_packages_metadata_and_application_verified', kernel=kernel, **proof)
        destination.ssh('sync\numount /boot/efi\nsystemctl poweroff', required=False)
        destination.process.wait(timeout=120); destination.stop()
        after = disk_invariants(work / 'destination.img')
        if before != after: raise RuntimeError('GPT, EFI bytes or root UUID changed')
        source.stop()
        if digest(work / 'source/source.img') != source_before: raise RuntimeError('read-only source disk changed')
        destination.record('destination_disk_identities_and_source_snapshot_unchanged')
        report.update(status='passed', kernel=kernel, boot=result['boot'], invariants=after,
                      source_disk_sha256=source_before, source_metadata_and_copied_payload_verified=True)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        report['error'] = str(exc); print('Saved OS lab failed: ' + str(exc), flush=True)
    finally:
        destination.stop(); source.stop()
        write_report(work / 'resume-result.json', report)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())
