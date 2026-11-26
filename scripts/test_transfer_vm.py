#!/usr/bin/env python3
"""Copy a metadata fixture between two VMs, from destination RAM onto its disk."""
import json
from pathlib import Path
import shlex
import sys
import time

from test_recovery_vm import ROOT, private_write
from test_source_connectivity_vm import SourceConnectivityLab, main


class TransferLab(SourceConnectivityLab):
    def fixture_command(self, arguments):
        script = (ROOT / "scripts/vm_transfer_fixture.py").read_text()
        return "python3 -I -B -c " + shlex.quote(script) + " " + arguments

    def exercise_failures(self):
        """Optional extension before the baseline interruption tests."""

    def prepare_source(self, source, token):
        source.ssh("command -v rsync\n" + self.fixture_command("setup " + token))
        self.expected = json.loads(source.ssh(self.fixture_command("snapshot /var/lib/migratarr/transfer-fixture")).stdout)
        entries = self.expected["entries"]
        assert entries["owned.txt"]["uid"] == 23101
        assert "system.posix_acl_default" in entries["acl-directory"]["xattrs"]
        assert "security.capability" in entries["capability.bin"]["xattrs"]
        assert self.expected["hardlinks"] == [["hardlink.txt", "owned.txt"]]
        private_write(self.work / "source-manifest-initial.json", json.dumps(self.expected, indent=2) + "\n")
        self.record("source_metadata_fixture_prepared")

    @property
    def job(self):
        return "/target/var/lib/migratarr/jobs/" + self.job_id

    @property
    def data(self):
        return "/target/var/lib/migratarr/transfer-tests/" + self.job_id + "/data"

    def transfer_command(self, limited=False):
        return "python3 -I -B /opt/migratarr.pyz recovery transfer-fixture --vm-token " + self.vm_token + (" --bwlimit 512" if limited else "")

    def launch_interrupted_transfer(self):
        pid = self.ssh("umask 077\n" + self.transfer_command(True) + " > /run/migratarr/transfer-output.json 2>/run/migratarr/transfer-error.log </dev/null &\necho $!").stdout.strip()
        if not pid.isdigit():
            raise RuntimeError("no transfer process started")
        # Observe real bytes written on disk, rather than merely a running PID.
        script = """import json,os,pathlib,sys
job=pathlib.Path(sys.argv[1]);data=pathlib.Path(sys.argv[2])
state=json.loads((job/'transfer.json').read_text())
partial=any(p.is_file() and p.stat().st_size>65536 for p in (data.parent/'scratch').iterdir() if not p.is_symlink())
sys.exit(0 if state['phase']=='copying' and partial else 1)
"""
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            result = self.ssh("python3 -c " + shlex.quote(script) + " " + self.job + " " + self.data, required=False)
            if result.returncode == 0:
                self.ssh("sync")
                return pid
            time.sleep(1)
        diagnostic = self.ssh("cat /run/migratarr/transfer-error.log", required=False).stdout
        raise RuntimeError("transfer did not produce an on-disk partial file: " + diagnostic)

    def verify_metadata(self, label, disk_boot=False):
        root = self.data[len("/target"):] if disk_boot else self.data
        actual = json.loads(self.ssh(self.fixture_command("snapshot " + root)).stdout)
        private_write(self.work / (label + ".json"), json.dumps(actual, indent=2) + "\n")
        if actual != self.expected:
            keys = sorted(set(actual["entries"]) | set(self.expected["entries"]))
            differences = [key for key in keys if actual["entries"].get(key) != self.expected["entries"].get(key)]
            raise RuntimeError("fixture metadata differs: " + repr(differences))
        self.record(label, entries=len(actual["entries"]), hardlink_groups=len(actual["hardlinks"]))

    def verify_source_connection(self, after_restart=False):
        super().verify_source_connection(after_restart)
        if not after_restart:
            self.ssh("mount -t ext4 /dev/vda1 /target\nmount -t ext4 /dev/vda16 /target/boot")
            # This sibling file must survive every attempt and every reboot.
            self.ssh("printf protected > /target/var/lib/migratarr/protected-transfer-sentinel\nsync")
            pid = self.launch_interrupted_transfer()
            self.ssh("kill -TERM " + pid)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                state = json.loads(self.ssh("cat " + self.job + "/transfer.json").stdout)
                if state["phase"] == "interrupted":
                    break
                time.sleep(1)
            else:
                raise RuntimeError("SIGTERM did not save an interrupted checkpoint")
            self.record("interrupted_transfer_checkpoint_saved", attempt=state["attempt"])
            result = json.loads(self.run_cli("recovery transfer-fixture --vm-token " + self.vm_token))
            if result["phase"] != "complete" or result["previous_phase"] != "interrupted":
                raise RuntimeError("interrupted fixture transfer could not be retried")
            self.verify_metadata("fixture_metadata_preserved_after_retry")
            self.exercise_failures()
            self.source_lab.ssh(self.fixture_command("add-restart"))
            self.expected = json.loads(self.source_lab.ssh(self.fixture_command("snapshot /var/lib/migratarr/transfer-fixture")).stdout)
            private_write(self.work / "source-manifest-final.json", json.dumps(self.expected, indent=2) + "\n")
            self.launch_interrupted_transfer()
            self.record("on_disk_partial_observed_before_destination_power_loss")
            # The base harness now kills QEMU while the copy is still running.
        else:
            state = json.loads(self.ssh("cat " + self.job + "/transfer.json").stdout)
            if state["phase"] != "copying":
                raise RuntimeError("power loss did not interrupt an active copy")
            result = json.loads(self.run_cli("recovery transfer-fixture --vm-token " + self.vm_token))
            if result["phase"] != "complete" or result["previous_phase"] != "copying":
                raise RuntimeError("copy did not recover from stale in-progress checkpoint")
            self.verify_metadata("fixture_metadata_preserved_after_power_loss")
            self.ssh("test \"$(cat /target/var/lib/migratarr/protected-transfer-sentinel)\" = protected")
            self.record("protected_recovery_sibling_survived_transfer")

    def verify_disk_boot(self):
        self.verify_metadata("fixture_files_persist_after_booting_disk_ubuntu", disk_boot=True)


if __name__ == "__main__":
    raise SystemExit(main(lab_class=TransferLab, report_prefix="transfer-vm-", scope="bounded_vm_fixture_transfer"))
