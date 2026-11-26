#!/usr/bin/env python3
"""Two disposable Ubuntu VMs: read-only source collection from destination RAM.

The guests share a loopback-only QEMU socket link, without a host bridge or LAN
access. Each has one disk. All keys are generated fixtures.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile

from test_recovery_vm import RecoveryLab, ROOT, OVMF_VARS, command, private_write
from test_ubuntu_recovery_vm import UbuntuRecoveryLab, digest, disk_invariants, inject_seed


def socket_port():
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def peer_network(mode, port):
    return ["-netdev", "socket,id=sourcepeer," + mode + "=127.0.0.1:" + str(port),
            "-device", "e1000,netdev=sourcepeer,mac=52:54:00:12:34:" + ("57" if mode == "listen" else "58")]


class SourceConnectivityLab(UbuntuRecoveryLab):
    def prepare_source(self, source, token):
        """Optional fixture-transfer extension; connectivity tests are read-only."""

    def checked_inventory(self, label):
        result = self.ssh("set +e\npython3 -I -B /opt/migratarr.pyz recovery check-source > /run/migratarr/check-output.json\ncode=$?\nset -e\ntest $code = 0 -o $code = 2\ncat /run/migratarr/check-output.json")
        report = json.loads(result.stdout)
        inventory = report["inventory"]
        addresses = [address.get("local") for interface in inventory["network"]
                     for address in interface.get("addr_info", [])]
        if (report["migration_ready"] is not False or inventory["role"] != "source"
                or "192.0.2.10" not in addresses or inventory["system"]["kernel"] != self.source_kernel):
            raise RuntimeError("RAM source check did not identify the disposable source VM")
        private_write(self.work / (label + ".json"), json.dumps(report, indent=2) + "\n")
        self.record(label, status=report["status"], probe_errors=len(inventory["errors"]))

    def rejected_check(self, label, diagnostic):
        result = self.ssh("set +e\npython3 -I -B /opt/migratarr.pyz recovery check-source > /run/migratarr/rejected-output.json 2>/run/migratarr/rejected-error.txt\ncode=$?\nset -e\ntest $code = 1\ncat /run/migratarr/source-check.json")
        report = json.loads(result.stdout)
        if (report["status"] != "source_unreachable" or report["migration_ready"] is not False
                or "inventory" in report or diagnostic not in report["error"]):
            raise RuntimeError("source connection failure was not recorded safely")
        self.record(label)

    def verify_source_connection(self, after_restart=False):
        if after_restart:
            # Test again with the target unavailable after a cold recovery boot.
            self.ssh("umount /target/boot\numount /target")
            self.checked_inventory("source_inventory_from_ram_after_power_loss")
            self.ssh("mount -t ext4 /dev/vda1 /target\nmount -t ext4 /dev/vda16 /target/boot")
            return
        self.checked_inventory("source_inventory_from_ram_with_target_unmounted")
        original = self.ssh("cat /run/migratarr/auth/known_hosts").stdout
        wrong = "192.0.2.10 " + (self.work / "client-key.pub").read_text().strip() + "\n"
        self.ssh("printf %s " + shlex.quote(wrong) + " > /run/migratarr/auth/known_hosts")
        try:
            self.rejected_check("changed_source_host_key_rejected", "host key verification failed")
        finally:
            self.ssh("printf %s " + shlex.quote(original) + " > /run/migratarr/auth/known_hosts")
        before = self.ssh("cat /run/migratarr/status.json").stdout
        self.ssh("ip link set eth1 down")
        try:
            self.rejected_check("source_link_failure_remains_blocked", "SSH source collection failed")
        finally:
            self.ssh("ip link set eth1 up")
        if self.ssh("cat /run/migratarr/status.json").stdout != before:
            raise RuntimeError("failed source checks changed the recovery checkpoint")
        self.checked_inventory("source_connection_restored_without_reboot")


def main(argv=None, lab_class=SourceConnectivityLab, report_prefix="source-connectivity-", scope="source_connectivity_from_ram"):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("image", "sha256", "kernel", "kernel-version"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        parser.error("run as an unprivileged developer")
    image = Path(args.image).absolute()
    if image.is_symlink() or not image.is_file() or digest(image) != args.sha256:
        parser.error("image must be a regular cloud qcow2 matching its verified SHA256")
    parent = ROOT / "reports"
    parent.mkdir(mode=0o700, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=report_prefix, dir=parent))
    source_work = work / "source"
    source_work.mkdir(mode=0o700)
    source = RecoveryLab(source_work, 300)
    source.memory_mb = 1536
    source.ssh_timeout = 180
    destination = lab_class(work, 300)
    destination.memory_mb = 2048
    destination.ssh_timeout = 240
    port = socket_port()
    source.extra_qemu_args = peer_network("listen", port)
    destination.extra_qemu_args = peer_network("connect", port)
    token = secrets.token_hex(16)
    result = {"schema_version": 1, "status": "failed", "production_qualified": False,
              "scope": scope, "image_sha256": args.sha256,
              "events": destination.events, "source_events": source.events,
              "limitations": ["No source OS copy, imported OS boot or physical migration is qualified.",
                              "Static IPv4, Ubuntu 24.04 and generated keys only; real server credentials are not used."]}
    try:
        print("Source connectivity VMs: " + str(work), flush=True)
        for directory in (work, source_work):
            command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", directory / "client-key"])
            shutil.copyfile(OVMF_VARS, directory / "OVMF_VARS.fd")
        command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", work / "source-client-key"])
        command([sys.executable, ROOT / "scripts/build.py"])
        application = ROOT / "dist/migratarr.pyz"
        source_disk = source_work / "source.img"
        command(["qemu-img", "convert", "-f", "qcow2", "-O", "raw", image, source_disk])
        public = inject_seed(source_disk, source_work, secrets.token_hex(16), application,
                             extra_address="192.0.2.10/24", hostname="migratarr-source-test",
                             authorized_keys=[(work / "source-client-key.pub").read_text().strip()])
        private_write(source_work / "known_hosts", "[127.0.0.1]:{} {}\n".format(source.port, public))
        source.start(source_disk)
        command([sys.executable, application, "recovery", "build-vm-image", "--output", work / "bundle", "--kernel", args.kernel,
                 "--kernel-version", args.kernel_version, "--application", application,
                 "--management-public-key", work / "client-key.pub", "--vm-token", token,
                 "--extra-network", "eth1=192.0.2.20/24"])
        disk = work / "destination.img"
        command(["qemu-img", "convert", "-f", "qcow2", "-O", "raw", image, disk])
        bootstrap = inject_seed(disk, work, token, application, extra_address="192.0.2.20/24", extra_mac="52:54:00:12:34:58",
                                source_auth=((work / "source-client-key").read_bytes(), ("192.0.2.10 " + public + "\n").encode()))
        manifest = json.loads((work / "bundle/manifest.json").read_text())
        private_write(work / "known_hosts", "\n".join("[127.0.0.1]:{} ".format(destination.port) + key for key in (bootstrap, manifest["host_public_key"])) + "\n")
        before = disk_invariants(disk)
        private_write(work / "invariants-before.json", json.dumps(before, indent=2) + "\n")
        source.wait_guest("bootstrap")
        destination.source_lab = source
        destination.vm_token = token
        destination.prepare_source(source, token)
        destination.source_kernel = source.ssh("uname -r").stdout.strip()
        destination.source_profile = {"mode": "ssh", "host": "192.0.2.10", "user": "root", "authentication": "key",
                                      "identity_file": "/opt/lab-source-key", "known_hosts": "/opt/lab-known-hosts", "inspect_docker": False}
        destination.run_workflow(disk, token)
        after = disk_invariants(disk)
        private_write(work / "invariants-after.json", json.dumps(after, indent=2) + "\n")
        if before != after:
            raise RuntimeError("destination GPT, EFI bytes or root UUID changed")
        destination.record("destination_layout_efi_and_root_uuid_unchanged")
        result["status"] = "passed"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
        print("Source connectivity test failed: " + str(exc), file=sys.stderr, flush=True)
    finally:
        for lab in (destination, source):
            lab.stop()
            for handle in lab.log_handles:
                handle.close()
        private_write(work / "result.json", json.dumps(result, indent=2) + "\n")
        print("Evidence: " + str(work / "result.json"), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
