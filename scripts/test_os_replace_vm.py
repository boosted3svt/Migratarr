#!/usr/bin/env python3
"""Whole Ubuntu 20.04 -> 24.04 VM disk replacement from destination RAM.

The imported OS remains Ubuntu 20.04. Both images must match supplied hashes.
All writes occur in generated, isolated QEMU guests; no real-server auth is used.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from test_recovery_vm import ROOT, OVMF_VARS, RecoveryLab, command, private_write
from test_ubuntu_recovery_vm import UbuntuRecoveryLab, digest, inject_seed, disk_invariants, partitions
from test_source_connectivity_vm import peer_network, socket_port


def fixture_code():
    return (ROOT / 'scripts/vm_transfer_fixture.py').read_text().replace('/var/lib/migratarr/transfer-fixture', '/srv/migratarr-os-fixture')


def boot_filesystem_uuid(disk):
    boot = next(row for row in partitions(disk) if row['number'] == 16)
    with disk.open('rb') as handle:
        handle.seek(boot['first'] * 512 + 1024 + 104)
        return str(uuid.UUID(bytes=handle.read(16)))


def fixture_command(action):
    return 'python3 -I -B -c ' + shlex.quote(fixture_code()) + ' ' + action


def wait_imported(destination):
    """Cloud-init is disabled after import; inspect the booted OS directly."""
    deadline = time.monotonic() + destination.boot_timeout
    probe = "python3 -I -B -c " + shlex.quote("import json,pathlib;d=dict(line.split('=',1) for line in pathlib.Path('/etc/os-release').read_text().splitlines() if '=' in line);print(json.dumps([d.get('VERSION_ID','').strip(chr(34)),pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),next(line.split()[2] for line in pathlib.Path('/proc/mounts').read_text().splitlines() if line.split()[1]=='/')]))")
    while time.monotonic() < deadline:
        result = destination.ssh(probe, required=False)
        if result.returncode == 0:
            release, boot_id, filesystem = json.loads(result.stdout)
            if release == '20.04' and filesystem == 'ext4':
                destination.record('imported_os_ssh_ready', boot_id=boot_id, release=release, filesystem=filesystem)
                return
        time.sleep(2)
    raise RuntimeError('imported Ubuntu 20.04 did not become reachable')


WITNESS = '''#!/usr/bin/python3
import json,sqlite3,sys
from pathlib import Path
fields={k:v.strip('"') for k,v in (line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)}
db=sqlite3.connect('file:/srv/migratarr-os-fixture/state.db?mode=ro',uri=True)
result={'release':fields['VERSION_ID'],'python':list(sys.version_info[:2]),'state':db.execute('select value from state').fetchone()[0]}
Path('/var/lib/migratarr-witness/boot-proof.json').write_text(json.dumps(result))
'''


def prepare_source(source, token):
    source.ssh('cloud-init status --wait')
    setup = '''import json,os,sqlite3,subprocess
from pathlib import Path
marker=Path('/etc/migratarr-recovery-test.json')
assert json.loads(marker.read_text())['purpose']=='migratarr_disposable_vm'
marker.unlink()
out=Path('/etc/migratarr-os-source-test.json')
out.write_text(json.dumps({'purpose':'migratarr_os_source_vm','token':TOKEN}));out.chmod(0o600)
subprocess.run(['groupadd','--gid','23102','migration-fixture'],check=True)
subprocess.run(['useradd','--uid','23101','--gid','23102','--no-create-home','--home-dir','/srv/migratarr-os-fixture','--shell','/usr/sbin/nologin','migration-fixture'],check=True)
db=sqlite3.connect('/srv/migratarr-os-fixture/state.db')
db.execute('create table state(value text)');db.execute("insert into state values ('source application state')");db.commit();db.close()
os.chown('/srv/migratarr-os-fixture/state.db',23101,23102)
Path('/var/lib/migratarr-witness').mkdir()
script=Path('/usr/local/bin/migratarr-witness');script.write_text(WITNESS);script.chmod(0o755)
unit=Path('/etc/systemd/system/migratarr-witness.service')
unit.write_text('[Unit]\\nAfter=network.target\\n[Service]\\nType=oneshot\\nRemainAfterExit=yes\\nExecStart=/usr/local/bin/migratarr-witness\\n[Install]\\nWantedBy=multi-user.target\\n')
subprocess.run(['systemctl','enable','--now','migratarr-witness.service'],check=True)
'''.replace('TOKEN', repr(token)).replace('WITNESS', repr(WITNESS))
    source.ssh(fixture_command('setup ' + token))
    source.ssh('python3 -I -B -c ' + shlex.quote(setup))
    expected = json.loads(source.ssh(fixture_command('snapshot /srv/migratarr-os-fixture')).stdout)
    inventory = source.ssh('python3 -I -B /opt/migratarr.pyz inventory --role source').stdout
    identity = source.ssh("sha256sum /etc/passwd /etc/group /var/lib/dpkg/status").stdout
    kernel = source.ssh('uname -r').stdout.strip()
    proof = json.loads(source.ssh('cat /var/lib/migratarr-witness/boot-proof.json').stdout)
    if proof['release'] != '20.04' or proof['python'] != [3, 8]:
        raise RuntimeError('source VM did not boot actual Ubuntu 20.04 userspace')
    source.record('older_source_booted_with_application_state', kernel=kernel, **proof)
    return expected, inventory, identity, kernel


def source_ram_bundle(work, application, args, token, own_key, peer_key):
    command([sys.executable, application, 'recovery', 'build-vm-image', '--output', work / 'bundle',
             '--kernel', args.kernel, '--kernel-version', args.kernel_version,
             '--application', application, '--management-public-key', own_key, '--vm-token', token])
    tree = work / 'bundle/tree'
    (tree / 'root/.ssh/authorized_keys').write_text(own_key.read_text().strip() + '\n' + peer_key.read_text().strip() + '\n')
    prefix = (tree / 'init').read_text().split('job=', 1)[0]
    (tree / 'source').mkdir()
    script = prefix + '''mkdir -p /source /dev/pts
mount -t devpts devpts /dev/pts
mount -t ext4 -o ro,noload /dev/vda1 /source || exec sh
echo source_readonly > /run/migratarr-lab-mode
ip link set lo up
ip link set eth0 up
ip addr add 10.0.2.15/24 dev eth0
ip route add default via 10.0.2.2
ip link set eth1 up
ip addr add 192.0.2.10/24 dev eth1
/usr/sbin/dropbear -F -E -s -g -r /etc/dropbear/host_key -p 22 &
while :; do sleep 1; done
'''
    (tree / 'init').write_text(script)
    sys.path.insert(0, str(ROOT / 'src'))
    from migratarr.ramdisk import pack_initramfs
    pack_initramfs(tree, work / 'bundle/source-initrd.img')
    return json.loads((work / 'bundle/manifest.json').read_text())['host_public_key']


def destination_workflow(destination, disk, args, token, source_token, inventory):
    destination.start(disk); first = destination.wait_guest('bootstrap')
    destination.ssh("umask 077\nprintf 'destination bootstrap only' > /usr/local/share/migratarr-bootstrap-only\npython3 -I -B /opt/migratarr.pyz inventory --role destination > /opt/destination.json")
    private_write(destination.work / 'source-inventory.json', inventory)
    destination.upload(destination.work / 'source-inventory.json', '/opt/source.json')
    profile = {'schema_version': 1, 'source': {'mode': 'ssh', 'host': '192.0.2.10', 'user': 'root', 'authentication': 'key',
                'identity_file': '/opt/lab-source-key', 'known_hosts': '/opt/lab-known-hosts', 'inspect_docker': False},
               'destination': {'mode': 'local'}, 'output_dir': '/opt/reports'}
    destination.ssh('umask 077\nprintf %s ' + shlex.quote(json.dumps(profile)) + ' > /opt/profile.json')
    destination.ssh('set +e\npython3 -I -B /opt/migratarr.pyz plan --source /opt/source.json --destination /opt/destination.json > /opt/plan.json\ncode=$?\nset -e\ntest $code = 2')
    job = json.loads(destination.run_cli('recovery create --config /opt/profile.json --plan /opt/plan.json --state-dir /var/lib/migratarr/jobs'))
    private_write(destination.work / 'recovery-job.json', json.dumps(job, indent=2) + '\n')
    for name in ('kernel', 'initrd.img'):
        destination.ssh('mkdir -p /opt/recovery-bundle\nchmod 700 /opt/recovery-bundle')
        destination.upload(destination.work / 'bundle' / name, '/opt/recovery-bundle/' + name)
    destination.run_cli('recovery prepare-vm --job ' + job['job_directory'] + ' --bundle /opt/recovery-bundle --vm-token ' + token)
    destination.ssh('python3 -I -B /opt/migratarr.pyz recovery boot-vm --job ' + job['job_directory'] + ' --vm-token ' + token, required=False)
    destination.wait_guest('rescue', first['boot_id'])
    preview = json.loads(destination.run_cli('recovery os-preview-vm --vm-token ' + token + ' --source-token ' + source_token))
    private_write(destination.work / 'os-preview-summary.json', json.dumps(preview, indent=2) + '\n')
    destination.record('whole_os_preview_ready', **{k:v for k,v in preview.items() if k.endswith('entries')})
    result = json.loads(destination.run_cli('recovery os-replace-vm --vm-token ' + token + ' --preview-sha256 ' + preview['preview_sha256']))
    private_write(destination.work / 'os-transfer-result.json', json.dumps(result, indent=2) + '\n')
    if result['phase'] != 'ready_for_vm_boot': raise RuntimeError('OS replacement is not ready for boot')
    destination.record('whole_os_replaced_and_boot_reconciled', **result['boot'])
    destination.ssh('sync\numount /target/boot\numount /target\npoweroff -f', required=False)
    destination.process.wait(timeout=60); destination.stop()
    destination.start(disk)
    wait_imported(destination)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-image', 'source-sha256', 'destination-image', 'destination-sha256', 'kernel', 'kernel-version'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    if os.geteuid() == 0: parser.error('run as an unprivileged developer')
    for image, sha in ((args.source_image, args.source_sha256), (args.destination_image, args.destination_sha256)):
        p=Path(image)
        if p.is_symlink() or not p.is_file() or digest(p) != sha: parser.error('image checksum mismatch')
    work = Path(tempfile.mkdtemp(prefix='os-replace-vm-', dir=ROOT / 'reports'))
    source_work = work / 'source'; source_work.mkdir(mode=0o700)
    source = RecoveryLab(source_work, 360); source.memory_mb=2048; source.ssh_timeout=300
    destination = UbuntuRecoveryLab(work, 360); destination.memory_mb=2560; destination.ssh_timeout=1800
    port=socket_port(); source.extra_qemu_args=peer_network('listen',port); destination.extra_qemu_args=peer_network('connect',port)
    token, source_token = secrets.token_hex(16), secrets.token_hex(16)
    report={'status':'failed','scope':'whole_os_ubuntu_20_04_to_24_04_vm_only','production_qualified':False,
            'events':destination.events,'source_events':source.events,
            'source_image_sha256':args.source_sha256,'destination_image_sha256':args.destination_sha256}
    print('Whole OS VMs: ' + str(work), flush=True)
    try:
        for directory in (work,source_work):
            command(['ssh-keygen','-q','-t','ed25519','-N','','-f',directory/'client-key'])
            shutil.copyfile(OVMF_VARS,directory/'OVMF_VARS.fd')
        command(['ssh-keygen','-q','-t','ed25519','-N','','-f',work/'source-client-key'])
        command([sys.executable,ROOT/'scripts/build.py']); application=ROOT/'dist/migratarr.pyz'
        srcdisk=source_work/'source.img'; command(['qemu-img','convert','-f','qcow2','-O','raw',args.source_image,srcdisk])
        public=inject_seed(srcdisk,source_work,source_token,application,extra_address='192.0.2.10/24',authorized_keys=[(work/'client-key.pub').read_text().strip()],hostname='migratarr-older-source')
        ram_public=source_ram_bundle(source_work,application,args,source_token,source_work/'client-key.pub',work/'source-client-key.pub')
        private_write(source_work/'known_hosts','\n'.join('[127.0.0.1]:{} {}'.format(source.port,k) for k in (public,ram_public))+'\n')
        source.start(srcdisk); source.wait_guest('bootstrap')
        expected,inventory,identity,kernel=prepare_source(source,source_token)
        private_write(work/'source-runtime-metadata.json',json.dumps(expected,indent=2)+'\n')
        source.ssh('sync\nsystemctl poweroff',required=False); source.process.wait(timeout=120); source.stop()
        source_before=digest(srcdisk)
        private_write(work/'source-disk-before.sha256',source_before+'\n')
        source.extra_qemu_args += ['-kernel',str(source_work/'bundle/kernel'),'-initrd',str(source_work/'bundle/source-initrd.img'),'-append','console=ttyS0,115200 rdinit=/init net.ifnames=0']
        source.start(srcdisk); source.wait_guest('source_readonly')
        expected = json.loads(source.ssh(fixture_command('snapshot /source/srv/migratarr-os-fixture')).stdout)
        private_write(work/'source-metadata.json',json.dumps(expected,indent=2)+'\n')
        command([sys.executable,application,'recovery','build-vm-image','--output',work/'bundle','--kernel',args.kernel,'--kernel-version',args.kernel_version,'--application',application,'--management-public-key',work/'client-key.pub','--vm-token',token,'--extra-network','eth1=192.0.2.20/24'])
        disk=work/'destination.img'; command(['qemu-img','convert','-f','qcow2','-O','raw',args.destination_image,disk])
        bootstrap=inject_seed(disk,work,token,application,extra_address='192.0.2.20/24',extra_mac='52:54:00:12:34:58',source_auth=((work/'source-client-key').read_bytes(),('192.0.2.10 '+ram_public+'\n').encode()))
        native=json.loads((work/'bundle/manifest.json').read_text())
        private_write(work/'known_hosts','\n'.join('[127.0.0.1]:{} {}'.format(destination.port,k) for k in (bootstrap,native['host_public_key']))+'\n')
        before=disk_invariants(disk)
        original_boot_uuid=boot_filesystem_uuid(disk)
        result=destination_workflow(destination,disk,args,token,source_token,inventory)
        if result['boot']['boot_uuid'] != original_boot_uuid: raise RuntimeError('destination boot UUID changed')
        actual=json.loads(destination.ssh(fixture_command('snapshot /srv/migratarr-os-fixture')).stdout)
        if actual != expected: raise RuntimeError('imported application metadata differs')
        if destination.ssh('sha256sum /etc/passwd /etc/group /var/lib/dpkg/status').stdout != identity:
            raise RuntimeError('source accounts or package database differs after import')
        proof=json.loads(destination.ssh('cat /var/lib/migratarr-witness/boot-proof.json').stdout)
        if proof['release']!='20.04' or proof['python']!=[3,8] or proof['state']!='source application state':
            raise RuntimeError('imported OS/application service proof differs')
        destination.ssh('systemctl is-active migratarr-witness.service\ntest ! -e /usr/local/share/migratarr-bootstrap-only\ntest -s /var/lib/migratarr/jobs/*/os-transfer.json')
        actual_kernel=destination.ssh('uname -r').stdout.strip()
        if actual_kernel != kernel: raise RuntimeError('destination did not boot the source kernel')
        destination.record('imported_older_os_kernel_accounts_packages_and_application_verified',kernel=actual_kernel,**proof)
        destination.ssh('sync\numount /boot/efi\npoweroff -f',required=False); destination.process.wait(timeout=60); destination.stop()
        after=disk_invariants(disk)
        if after!=before: raise RuntimeError('destination GPT, EFI bytes or root UUID changed')
        source.stop()
        if digest(srcdisk)!=source_before: raise RuntimeError('read-only source disk was modified')
        destination.record('destination_layout_efi_root_uuid_and_source_disk_unchanged')
        report.update(status='passed',source_kernel=kernel,imported_kernel=actual_kernel,boot=result['boot'],invariants=after)
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        report['error']=str(exc); print('Whole OS rehearsal failed: '+str(exc),flush=True)
    finally:
        destination.stop(); source.stop()
        private_write(work/'result.json',json.dumps(report,indent=2)+'\n')
    print('Evidence: '+str(work/'result.json'),flush=True)
    return 0 if report['status']=='passed' else 1


if __name__=='__main__': raise SystemExit(main())
