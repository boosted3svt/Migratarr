#!/usr/bin/env python3
"""One milestone rehearsal: policy-aware copy, cleanup, repeat and restart."""
import json

from test_root_policy_vm import PolicyLab
from test_source_connectivity_vm import main


class PolicyCopyLab(PolicyLab):
    def verify_source_connection(self, after_restart=False):
        if after_restart:
            self.ssh(self.policy_command("target-mount"))
            self.ssh(self.policy_command("verify"))
            self.record("policy_copy_and_cleanup_survived_ram_restart")
            return
        self.ssh("umask 077\nmount -t ext4 /dev/vda1 /target\nmount -t ext4 /dev/vda16 /target/boot\nmkdir -p " + self.data + "/policy-case/mnt/share\nchmod 700 " + self.data + "/policy-case")
        self.ssh(self.policy_command("target-seed"))
        self.source_lab.ssh(self.policy_command("source-mount"))
        self.ssh(self.policy_command("target-mount"))
        report = json.loads(self.run_cli("recovery transfer-fixture --root-policy --vm-token " + self.vm_token))
        if report["phase"] != "complete":
            raise RuntimeError("policy copy did not complete")
        actual = json.loads(self.ssh(self.fixture_command("snapshot " + self.data)).stdout)
        for key, row in self.expected["entries"].items():
            if not key.startswith("policy-case") and actual["entries"].get(key) != row:
                raise RuntimeError("metadata fixture differs: " + key)
        if actual["hardlinks"] != self.expected["hardlinks"]:
            raise RuntimeError("hard links were not preserved")
        self.ssh("test ! -e " + self.data + "/policy-case/etc/ssh/source_config")
        self.record("policy_copy_preserved_metadata_and_excluded_reconciliation_files")
        preview = self.preview("policy-copy-cleanup-preview")
        cleanup = json.loads(self.run_cli("recovery cleanup-apply-vm --vm-token " + self.vm_token +
                                        " --preview-sha256 " + preview["preview_sha256"]))
        if cleanup["status"] != "complete" or set(cleanup["removed"]) != {row["path"] for row in preview["candidates"]}:
            raise RuntimeError("cleanup did not match preview")
        self.ssh(self.policy_command("verify"))
        repeated = json.loads(self.run_cli("recovery transfer-fixture --root-policy --vm-token " + self.vm_token))
        if repeated["phase"] != "complete" or repeated["attempt"] != report["attempt"] + 1:
            raise RuntimeError("policy copy could not be repeated")
        self.ssh(self.policy_command("verify"))
        self.record("policy_copy_cleanup_and_repeat_retained_protected_and_mounted_data", removed=len(cleanup["removed"]))


if __name__ == "__main__":
    raise SystemExit(main(lab_class=PolicyCopyLab, report_prefix="policy-copy-vm-", scope="bounded_policy_aware_copy_cleanup_and_repeat"))
