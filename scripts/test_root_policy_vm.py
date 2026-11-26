#!/usr/bin/env python3
"""Test whole-root path decisions inside a bounded sandbox on the VM disk."""
import json
import shlex

from test_recovery_vm import ROOT, private_write
from test_source_connectivity_vm import SourceConnectivityLab, main
from test_transfer_vm import TransferLab


class PolicyLab(TransferLab):
    def policy_command(self, action):
        code = (ROOT / "scripts/vm_policy_fixture.py").read_text()
        return "python3 -I -B -c " + shlex.quote(code) + " " + action + " " + self.vm_token

    def prepare_source(self, source, token):
        super().prepare_source(source, token)
        source.ssh(self.policy_command("source-setup"))
        self.expected = json.loads(source.ssh(self.fixture_command("snapshot /var/lib/migratarr/transfer-fixture")).stdout)

    def preview(self, label):
        result = self.ssh("set +e\npython3 -I -B /opt/migratarr.pyz recovery cleanup-preview-vm --vm-token " + self.vm_token +
                          " > /run/migratarr/policy-preview-output.json\ncode=$?\nset -e\ntest $code = 0 -o $code = 2\ncat /run/migratarr/policy-preview-output.json")
        report = json.loads(result.stdout)
        private_write(self.work / (label + ".json"), json.dumps(report, indent=2) + "\n")
        return report

    def rejected_apply(self, digest, label):
        result = self.ssh("set +e\npython3 -I -B /opt/migratarr.pyz recovery cleanup-apply-vm --vm-token " + self.vm_token +
                          " --preview-sha256 " + digest + " >/run/migratarr/rejected-cleanup.json 2>/run/migratarr/rejected-cleanup.log\ncode=$?\nset -e\ntest $code = 1")
        self.record(label)

    def verify_source_connection(self, after_restart=False):
        SourceConnectivityLab.verify_source_connection(self, after_restart)
        if after_restart:
            self.ssh(self.policy_command("target-mount"))
            self.ssh(self.policy_command("verify"))
            self.record("cleanup_and_protection_survived_ram_restart")
            return
        self.ssh("mount -t ext4 /dev/vda1 /target\nmount -t ext4 /dev/vda16 /target/boot")
        copied = json.loads(self.run_cli("recovery transfer-fixture --vm-token " + self.vm_token))
        if copied["phase"] != "complete":
            raise RuntimeError("initial policy sandbox transfer failed")
        self.verify_metadata("initial_root_shaped_fixture_copy_matches")
        self.source_lab.ssh(self.policy_command("source-mount"))
        self.ssh(self.policy_command("target-seed"))
        self.ssh(self.policy_command("target-mount"))
        preview = self.preview("initial-cleanup-preview")
        if preview["status"] != "preview_ready" or not preview["candidates"]:
            raise RuntimeError("expected an exact cleanup preview")
        names = [row["path"] for row in preview["candidates"]]
        if "/old-stack/old.log" not in names or "/home/user/.old-bootstrap" not in names:
            raise RuntimeError("ordinary bootstrap files were not previewed")
        self.rejected_apply("0" * 64, "wrong_preview_digest_rejected")
        self.source_lab.ssh(self.policy_command("source-collision"))
        blocked = self.preview("reserved-source-collision-preview")
        if blocked["status"] != "blocked" or not blocked["source_collisions"]:
            raise RuntimeError("source recovery namespace collision was not blocked")
        self.rejected_apply(blocked["preview_sha256"], "source_reserved_namespace_collision_rejected")
        self.source_lab.ssh(self.policy_command("source-clear-collision"))
        preview = self.preview("source-staleness-preview")
        self.source_lab.ssh(self.policy_command("source-change"))
        self.rejected_apply(preview["preview_sha256"], "source_changed_since_preview_rejected")
        self.source_lab.ssh(self.policy_command("source-restore"))
        preview = self.preview("destination-staleness-preview")
        self.ssh(self.policy_command("target-change"))
        self.rejected_apply(preview["preview_sha256"], "destination_changed_since_preview_rejected")
        preview = self.preview("symlink-staleness-preview")
        self.ssh(self.policy_command("target-swap-link"))
        self.rejected_apply(preview["preview_sha256"], "symlink_parent_redirect_rejected")
        self.ssh("test -f " + self.data + "/policy-case/old-stack-saved/old.log")
        self.ssh(self.policy_command("target-restore-link"))
        self.ssh(self.policy_command("target-hardlink"))
        blocked = self.preview("linked-target-preview")
        if blocked["status"] != "blocked" or not blocked["hardlink_conflicts"]:
            raise RuntimeError("external hardlink cleanup was not blocked")
        self.rejected_apply(blocked["preview_sha256"], "hardlinked_cleanup_candidate_rejected")
        self.ssh(self.policy_command("target-unlink"))
        preview = self.preview("reviewed-cleanup-preview")
        expected = {row["path"] for row in preview["candidates"]}
        report = json.loads(self.run_cli("recovery cleanup-apply-vm --vm-token " + self.vm_token + " --preview-sha256 " + preview["preview_sha256"]))
        if report["status"] != "complete" or set(report["removed"]) != expected or report["migration_ready"] is not False:
            raise RuntimeError("cleanup did not match its exact preview")
        private_write(self.work / "cleanup-result.json", json.dumps(report, indent=2) + "\n")
        self.ssh(self.policy_command("verify"))
        self.record("exact_preview_cleanup_completed", removed=len(report["removed"]))
        self.record("recovery_efi_network_identity_and_mount_data_retained")

    def verify_disk_boot(self):
        self.ssh(self.policy_command("verify-disk"))
        self.record("root_policy_protection_persisted_after_disk_boot")


if __name__ == "__main__":
    raise SystemExit(main(lab_class=PolicyLab, report_prefix="root-policy-vm-", scope="bounded_whole_root_policy_and_cleanup"))
