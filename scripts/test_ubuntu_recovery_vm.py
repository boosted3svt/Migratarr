#!/usr/bin/env python3
"""Full Ubuntu guest test of the packaged recovery-rehearsal workflow.

Only new regular VM image files are modified. No real-server SSH or host boot
configuration is used. The image must match an explicitly supplied SHA256.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid

from test_recovery_vm import RecoveryLab, ROOT, OVMF_VARS, command, private_write


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for data in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(data)
    return result.hexdigest()


def partitions(disk):
    with disk.open("rb") as handle:
        handle.seek(512)
        header = handle.read(512)
        if header[:8] != b"EFI PART":
            raise ValueError("expected a GPT cloud image")
        lba, count, size = struct.unpack_from("<QII", header, 72)
        if count > 256 or size != 128:
            raise ValueError("unsupported image partition table")
        handle.seek(lba * 512)
        table = handle.read(count * size)
    result = []
    for index in range(count):
        row = table[index * size:(index + 1) * size]
        if row[:16] == b"\0" * 16:
            continue
        first, last = struct.unpack_from("<QQ", row, 32)
        result.append({"number": index + 1, "type": str(uuid.UUID(bytes_le=row[:16])),
                       "first": first, "bytes": (last - first + 1) * 512})
    return result


def sparse_region(source, destination, start, length, destination_start=0, create=True):
    mode = "xb" if create else "r+b"
    with source.open("rb") as src, destination.open(mode) as dst:
        if create:
            dst.truncate(length)
        src.seek(start)
        position = 0
        while position < length:
            data = src.read(min(1024 * 1024, length - position))
            if not data:
                raise ValueError("truncated VM image")
            # For an existing destination, zero blocks must be written as well.
            if not create or data.strip(b"\0"):
                dst.seek(destination_start + position)
                dst.write(data)
            position += len(data)
        dst.flush()
        os.fsync(dst.fileno())


def disk_invariants(disk):
    """Fingerprint layout, EFI contents and root identity, not mutable OS data."""
    rows = partitions(disk)
    with disk.open("rb") as handle:
        def region(start, length):
            handle.seek(start)
            result = hashlib.sha256()
            while length:
                block = handle.read(min(length, 1024 * 1024))
                if not block:
                    raise ValueError("truncated invariant region")
                result.update(block)
                length -= len(block)
            return result.hexdigest()

        result = {"gpt_primary": region(0, 34 * 512),
                  "gpt_backup": region(disk.stat().st_size - 33 * 512, 33 * 512)}
        for row in rows:
            if row["type"] == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b":
                result["efi_partition_" + str(row["number"])] = region(row["first"] * 512, row["bytes"])
            if row["number"] == 1:
                handle.seek(row["first"] * 512 + 1024 + 104)
                result["root_uuid"] = str(uuid.UUID(bytes=handle.read(16)))
        return result


def inject_seed(disk, work, token, application, extra_address=None, source_auth=None,
                authorized_keys=(), hostname="migratarr-recovery-test", extra_mac="52:54:00:12:34:57"):
    rows = partitions(disk)
    roots = [row for row in rows if row["number"] == 1 and row["type"] == "0fc63daf-8483-4772-8e79-3d69d8477de4"]
    if len(roots) != 1:
        raise ValueError("unsupported cloud image root layout")
    root = roots[0]
    part = work / "root-seed.img"
    sparse_region(disk, part, root["first"] * 512, root["bytes"])
    command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", work / "bootstrap-host-key"])
    public = (work / "client-key.pub").read_text().strip()
    user = {"users": [{"name": "root", "lock_passwd": True, "ssh_authorized_keys": [public] + list(authorized_keys)}],
            "disable_root": False, "ssh_pwauth": False, "ssh_deletekeys": False,
            "ssh_genkeytypes": [], "growpart": {"mode": "off"}, "resize_rootfs": False,
            "bootcmd": [["sh", "-c", "echo bootstrap > /run/migratarr-lab-mode"]]}
    seed = work / "seed"
    seed.mkdir(mode=0o700)
    network = {"version": 2, "ethernets": {"lab": {"match": {"macaddress": "52:54:00:12:34:56"}, "set-name": "eth0", "addresses": ["10.0.2.15/24"], "routes": [{"to": "default", "via": "10.0.2.2"}]}}}
    if extra_address:
        network["ethernets"]["source-link"] = {"match": {"macaddress": extra_mac}, "set-name": "eth1", "addresses": [extra_address]}
    files = {
        "/var/lib/cloud/seed/nocloud/user-data": ("#cloud-config\n" + json.dumps(user)).encode(),
        "/var/lib/cloud/seed/nocloud/meta-data": json.dumps({"instance-id": "migratarr-" + token, "local-hostname": hostname}).encode(),
        "/var/lib/cloud/seed/nocloud/network-config": json.dumps(network).encode(),
        "/etc/cloud/cloud.cfg.d/99-migratarr-test.cfg": b"datasource_list: [NoCloud]\n",
        "/etc/migratarr-recovery-test.json": json.dumps({"purpose": "migratarr_disposable_vm", "token": token}).encode(),
        "/etc/ssh/ssh_host_ed25519_key": (work / "bootstrap-host-key").read_bytes(),
        "/etc/ssh/ssh_host_ed25519_key.pub": (work / "bootstrap-host-key.pub").read_bytes(),
        "/opt/migratarr.pyz": application.read_bytes(),
        "/opt/lab-source-key": source_auth[0] if source_auth else (work / "client-key").read_bytes(),
        "/opt/lab-known-hosts": source_auth[1] if source_auth else b"source.invalid " + public.encode() + b"\n",
    }
    directories = set()
    for target in files:
        parent = Path(target).parent
        while str(parent) != "/":
            directories.add(str(parent)); parent = parent.parent
    commands = ["mkdir " + directory for directory in sorted(directories, key=lambda value: (value.count('/'), value))]
    for index, (target, data) in enumerate(files.items()):
        source = seed / str(index)
        source.write_bytes(data)
        commands.extend(["rm " + target, "write " + str(source) + " " + target,
                         "set_inode_field " + target + " uid 0", "set_inode_field " + target + " gid 0",
                         "set_inode_field " + target + " mode 0100600"])
    script = work / "seed-debugfs.txt"
    private_write(script, "\n".join(commands) + "\n")
    command(["debugfs", "-w", "-f", script, part])
    sparse_region(part, disk, 0, root["bytes"], root["first"] * 512, create=False)
    return (work / "bootstrap-host-key.pub").read_text().strip()


class UbuntuRecoveryLab(RecoveryLab):
    def run_cli(self, arguments):
        return self.ssh("python3 -I -B /opt/migratarr.pyz " + arguments).stdout

    def verify_source_connection(self, after_restart=False):
        """Optional two-VM extension; the original rehearsal uses fixture auth."""

    def verify_disk_boot(self):
        """Optional assertion of fixture data after returning to disk Ubuntu."""

    def run_workflow(self, disk, token):
        self.start(disk)
        first = self.wait_guest("bootstrap")
        self.record("ubuntu_bootstrap_started", os_release=self.ssh("cat /etc/os-release").stdout)
        # All staging state/decisions use the same CLI shipped to operators.
        self.ssh("umask 077\npython3 -I -B /opt/migratarr.pyz inventory --role destination > /opt/destination.json")
        generate = """import json,copy
p='/opt/destination.json'
d=json.load(open(p));s=copy.deepcopy(d);s['role']='source';s['errors']=[]
json.dump(s,open('/opt/source-fixture.json','w'))
json.dump({'schema_version':1,'source':{'mode':'ssh','host':'source.invalid','user':'lab','authentication':'key','identity_file':'/opt/lab-source-key','known_hosts':'/opt/lab-known-hosts','inspect_docker':False},'destination':{'mode':'local'},'output_dir':'/opt/reports'},open('/opt/profile.json','w'))
"""
        import shlex
        self.ssh("umask 077\npython3 -c " + shlex.quote(generate))
        source_profile = getattr(self, "source_profile", None)
        if source_profile:
            self.ssh("umask 077\npython3 -c " + shlex.quote("import json;p='/opt/profile.json';d=json.load(open(p));d['source']=" + repr(source_profile) + ";json.dump(d,open(p,'w'))"))
            self.ssh("umask 077\nset +e\npython3 -I -B /opt/migratarr.pyz source-ssh --host " + shlex.quote(source_profile["host"]) + " --user " + shlex.quote(source_profile["user"]) + " --identity-file /opt/lab-source-key --known-hosts /opt/lab-known-hosts > /opt/source-fixture.json\ncode=$?\nset -e\ntest $code = 0 -o $code = 2")
            self.record("source_inventory_collected_before_reboot")
        self.ssh("umask 077\nset +e\npython3 -I -B /opt/migratarr.pyz plan --source /opt/source-fixture.json --destination /opt/destination.json > /opt/plan.json\ncode=$?\nset -e\ntest $code = 2")
        created = json.loads(self.run_cli("recovery create --config /opt/profile.json --plan /opt/plan.json --state-dir /var/lib/migratarr/jobs"))
        job = created["job_directory"]
        self.job_id = created["job_id"]
        bundle = self.work / "bundle"
        self.ssh("mkdir -p /opt/recovery-bundle\nchmod 700 /opt/recovery-bundle")
        for name in ("kernel", "initrd.img"):
            self.upload(bundle / name, "/opt/recovery-bundle/" + name)
        self.run_cli("recovery prepare-vm --job " + job + " --bundle /opt/recovery-bundle --vm-token " + token)
        self.record("standalone_cli_prepared_recovery", job_id=created["job_id"])
        self.ssh("python3 -I -B /opt/migratarr.pyz recovery boot-vm --job " + job + " --vm-token " + token, required=False)
        rescue = self.wait_guest("rescue", first["boot_id"])
        if rescue["root_filesystem"] not in ("rootfs", "ramfs", "tmpfs"):
            raise RuntimeError("native recovery did not boot into RAM")
        status = json.loads(self.ssh("cat /run/migratarr/status.json").stdout)
        if status["job_id"] != created["job_id"] or status["resume_count"] != 1:
            raise RuntimeError("standalone CLI did not automatically resume its saved job")
        self.ssh("test -s /run/migratarr/auth/source_key\nif awk '$2 == \"/target/boot\" {found=1} END {exit !found}' /proc/mounts; then umount /target/boot; fi\numount /target")
        self.ssh("test -s /run/migratarr/auth/source_key\npython3 -I -B /opt/migratarr.pyz --help")
        self.record("standalone_runtime_and_auth_survive_unmounted_target")
        self.verify_source_connection()
        self.stop(abrupt=True)
        self.start(disk)
        self.wait_guest("rescue", rescue["boot_id"])
        second = json.loads(self.ssh("cat /run/migratarr/status.json").stdout)
        if second["job_id"] != status["job_id"] or second["resume_count"] != 2:
            raise RuntimeError("standalone job did not resume after interrupted recovery")
        self.record("standalone_job_resumed_after_power_loss", resume_count=2)
        self.verify_source_connection(after_restart=True)
        # Deliberately corrupt the disposable guest's rescue image and force
        # another boot. GRUB's attempt marker must return to Ubuntu on failure.
        self.ssh("printf bad-image > /target/boot/migratarr/" + created["job_id"] + "/initrd.img\nsync")
        self.stop(abrupt=True)
        self.start(disk)
        self.wait_guest("bootstrap")
        self.record("corrupt_rescue_image_falls_back_to_ubuntu")
        self.verify_disk_boot()
        # A forced poweroff leaves FAT's mounted/dirty flag set even when no
        # EFI file changed. Unmount the ESP cleanly before bytewise comparison.
        self.ssh("sync\numount /boot/efi\npoweroff -f", required=False)
        self.process.wait(timeout=30)
        self.stop()

    def upload(self, source, destination):
        # Binary data passes only over authenticated SSH to the loopback guest.
        argv = ["ssh", "-F", "/dev/null", "-T", "-i", str(self.work / "client-key"), "-p", str(self.port),
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "UserKnownHostsFile=" + str(self.work / "known_hosts"), "-o", "GlobalKnownHostsFile=/dev/null",
                "root@127.0.0.1", "umask 077; cat > " + destination]
        with source.open("rb") as handle:
            result = subprocess.run(argv, stdin=handle, capture_output=True, timeout=120)
        if result.returncode:
            raise RuntimeError("could not upload the rehearsal asset to the guest")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--kernel", required=True)
    parser.add_argument("--kernel-version", required=True)
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        parser.error("run this developer test as an unprivileged user")
    image = Path(args.image).absolute()
    if not image.is_file() or image.is_symlink() or digest(image) != args.sha256:
        parser.error("image must be a regular cloud image matching its explicit SHA256")
    parent = ROOT / "reports";parent.mkdir(mode=0o700, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="ubuntu-recovery-", dir=parent))
    lab = UbuntuRecoveryLab(work, 240);lab.memory_mb = 2048;lab.ssh_timeout = 120
    token = secrets.token_hex(16)
    result = {"schema_version": 1, "status": "failed", "production_qualified": False,
              "scope": "ubuntu_standalone_recovery_rehearsal", "events": lab.events,
              "limitations": ["No source OS copy or imported OS boot is tested.", "Host-derived VM rescue kernel/tools do not qualify physical hardware or other releases."]}
    try:
        print("Ubuntu recovery VM: " + str(work), flush=True)
        command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", work / "client-key"])
        command([sys.executable, ROOT / "scripts/build.py"])
        application = ROOT / "dist/migratarr.pyz"
        command([sys.executable, application, "recovery", "build-vm-image", "--output", work / "bundle", "--kernel", args.kernel,
                 "--kernel-version", args.kernel_version, "--application", application,
                 "--management-public-key", work / "client-key.pub", "--vm-token", token])
        native = json.loads((work / "bundle/manifest.json").read_text())
        disk = work / "destination.img"
        command(["qemu-img", "convert", "-f", "qcow2", "-O", "raw", image, disk])
        bootstrap_public = inject_seed(disk, work, token, application)
        before = disk_invariants(disk)
        private_write(work / "invariants-before.json", json.dumps(before, indent=2) + "\n")
        private_write(work / "known_hosts", "\n".join("[127.0.0.1]:{} ".format(lab.port) + key for key in (bootstrap_public, native["host_public_key"])) + "\n")
        shutil.copyfile(OVMF_VARS, work / "OVMF_VARS.fd")
        lab.run_workflow(disk, token)
        after = disk_invariants(disk)
        private_write(work / "invariants-after.json", json.dumps(after, indent=2) + "\n")
        if after != before:
            raise RuntimeError("GPT, EFI contents or root filesystem UUID changed")
        lab.record("disk_layout_efi_and_root_uuid_unchanged", invariants=before)
        result["status"] = "passed"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
        print("Ubuntu recovery test failed: " + str(exc), file=sys.stderr, flush=True)
    finally:
        lab.stop()
        for handle in lab.log_handles:handle.close()
        private_write(work / "result.json", json.dumps(result, indent=2) + "\n")
        print("Evidence: " + str(work / "result.json"), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
