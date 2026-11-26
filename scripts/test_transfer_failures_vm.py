#!/usr/bin/env python3
"""Real network-loss, source-change and ENOSPC tests between disposable VMs."""
import json
import shlex
import time

from test_recovery_vm import ROOT, private_write
from test_source_connectivity_vm import main
from test_transfer_vm import TransferLab


class FailureLab(TransferLab):
    def fault_command(self, action):
        script = (ROOT / "scripts/vm_transfer_faults.py").read_text()
        return "python3 -I -B -c " + shlex.quote(script) + " " + action + " " + self.vm_token

    def wait_failure(self, reason):
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            state = json.loads(self.ssh("cat " + self.job + "/transfer.json").stdout)
            if state["phase"] in ("failed", "complete", "interrupted"):
                private_write(self.work / (reason + "-checkpoint.json"), json.dumps(state, indent=2) + "\n")
                if state["phase"] != "failed" or state.get("failure_reason") != reason or state["migration_ready"] is not False:
                    raise RuntimeError("unexpected failure outcome: " + repr(state))
                return state
            time.sleep(1)
        raise RuntimeError("copy did not finish with the injected failure")

    def retry_and_verify(self, label):
        report = json.loads(self.run_cli("recovery transfer-fixture --vm-token " + self.vm_token))
        if report["phase"] != "complete" or report["previous_phase"] != "failed":
            raise RuntimeError("failed attempt did not recover")
        self.expected = json.loads(self.source_lab.ssh(self.fixture_command("snapshot /var/lib/migratarr/transfer-fixture")).stdout)
        self.verify_metadata(label)

    def exercise_failures(self):
        for action, reason in (("network", "connection_lost"), ("mutation", "source_changed"), ("space", "space_exhausted")):
            self.source_lab.ssh(self.fault_command("add-file") + " failure-" + action + ".bin")
            self.launch_interrupted_transfer()
            if action == "network":
                self.ssh("ip link set eth1 down")
                try:
                    self.wait_failure(reason)
                finally:
                    self.ssh("ip link set eth1 up")
            elif action == "mutation":
                self.source_lab.ssh(self.fault_command("mutate"))
                self.wait_failure(reason)
            else:
                try:
                    filled = json.loads(self.ssh(self.fault_command("fill-target")).stdout)
                    self.record("target_disk_exhausted", **filled)
                    state = self.wait_failure(reason)
                    if state["checkpoint_reserve_released"] is not True:
                        raise RuntimeError("disk-full outcome did not release its checkpoint reserve")
                finally:
                    self.ssh(self.fault_command("remove-filler"))
            self.record(reason + "_recorded_without_migration_approval")
            self.retry_and_verify("metadata_preserved_after_" + action + "_recovery")


if __name__ == "__main__":
    raise SystemExit(main(lab_class=FailureLab, report_prefix="transfer-failures-", scope="vm_transfer_failure_recovery"))
