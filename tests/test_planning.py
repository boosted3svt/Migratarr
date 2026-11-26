import copy
import unittest

from migratarr.planning import RECOVERY_RESERVE_BYTES, build_plan, format_plan


GIB = 1024 ** 3


def mount(target="/", device="8:1", root="/", filesystem="ext4", kind="local"):
    return {"target": target, "device": device, "root": root, "filesystem": filesystem,
            "kind": kind, "shared_device": False, "subtree_or_bind_candidate": root != "/"}


def inventory(role):
    return {"schema_version": 1, "role": role,
            "system": {"os": {"ID": "ubuntu", "PRETTY_NAME": "Ubuntu fixture"},
                       "architecture": "x86_64", "userspace_bits": 64, "boot_mode": "uefi"},
            "mounts": [mount()], "space": [{"target": "/", "used_bytes": 100 * GIB,
                                            "total_bytes": 200 * GIB, "available_bytes": 150 * GIB}],
            "block_devices": {"blockdevices": [{"name": "sda", "type": "disk", "children": [
                {"name": "sda1", "type": "part", "uuid": role + "-uuid", "partuuid": role + "-part", "mountpoints": ["/"]}]}]},
            "errors": [], "limitations": []}


class PlanningTests(unittest.TestCase):
    def test_root_mapping_capacity_and_uuid_preserve_input(self):
        source, destination = inventory("source"), inventory("destination")
        source["mounts"][0]["filesystem"] = "ext3"
        before = copy.deepcopy((source, destination))
        result = build_plan(source, destination)
        self.assertEqual((source, destination), before)
        self.assertFalse(result["migration_ready"])
        self.assertEqual(result["status"], "draft_blocked")
        self.assertEqual(result["filesystem_map"][0]["source"]["identifiers"]["uuid"], "source-uuid")
        self.assertEqual(result["filesystem_map"][0]["destination"]["identifiers"]["uuid"], "destination-uuid")
        capacity = result["capacity"]["filesystems"][0]
        self.assertEqual(capacity["budget_bytes"], 120 * GIB + RECOVERY_RESERVE_BYTES)
        self.assertEqual(result["capacity"]["status"], "preliminary_sufficient")

    def test_missing_used_does_not_infer_from_available_or_total(self):
        source = inventory("source")
        del source["space"][0]["used_bytes"]
        result = build_plan(source, inventory("destination"))
        self.assertEqual(result["capacity"]["status"], "unknown")
        self.assertIsNone(result["capacity"]["filesystems"][0]["budget_bytes"])

    def test_insufficient_destination_budget(self):
        destination = inventory("destination")
        destination["space"][0]["available_bytes"] = 110 * GIB
        result = build_plan(inventory("source"), destination)
        self.assertEqual(result["capacity"]["status"], "insufficient")
        self.assertIn("capacity_insufficient", {b["code"] for b in result["blockers"]})

    def test_duplicate_view_not_silently_selected_then_explicit_exclusion(self):
        source = inventory("source")
        source["mounts"].append(mount("/mnt/local-root"))
        result = build_plan(source, inventory("destination"))
        self.assertEqual(result["capacity"]["status"], "unknown")
        self.assertTrue(all(row["mapping_status"] == "manual_review" for row in result["filesystem_map"]))
        excluded = build_plan(source, inventory("destination"), ["/mnt/local-root"])
        self.assertEqual(len(excluded["filesystem_map"]), 1)
        self.assertEqual(excluded["capacity"]["status"], "preliminary_sufficient")

    def test_shared_subvolumes_not_added_or_mapped_by_guess(self):
        source = inventory("source")
        source["mounts"].append(mount("/home", root="/home-subvolume"))
        source["space"].append({"target": "/home", "used_bytes": 100 * GIB})
        result = build_plan(source, inventory("destination"))
        self.assertEqual(result["capacity"]["status"], "unknown")
        self.assertIsNone(result["filesystem_map"][1]["destination"])
        self.assertTrue(all(row["budget_bytes"] is None for row in result["capacity"]["filesystems"]))

    def test_separate_destination_efi_is_retained_for_boot_adaptation(self):
        destination = inventory("destination")
        destination["mounts"].append(mount("/boot/efi", device="8:2", filesystem="vfat"))
        result = build_plan(inventory("source"), destination)
        self.assertEqual(len(result["filesystem_map"]), 1)
        self.assertEqual(result["destination_adaptations"][0]["target"], "/boot/efi")
        self.assertNotIn("unpaired_destination_filesystem", {b["code"] for b in result["blockers"]})

    def test_unknown_or_multiple_destination_disks_block_without_counting_loops(self):
        for topology, code in ((None, "destination_disk_topology_unknown"), ({"blockdevices": []}, "destination_disk_topology_unknown"),
                               ({"blockdevices": [{"type": "disk", "name": "a"}, {"type": "disk", "name": "b"}, {"type": "loop", "name": "loop0"}]}, "destination_disk_count")):
            destination = inventory("destination")
            destination["block_devices"] = topology
            result = build_plan(inventory("source"), destination)
            self.assertIn(code, {b["code"] for b in result["blockers"]})

    def test_bios_or_unavailable_does_not_claim_definite_bios(self):
        source = inventory("source")
        source["system"]["boot_mode"] = "bios_or_unavailable"
        source["server_survey"] = {"fstab": [{"filesystem": "swap", "source_reference": "UUID=old-swap"}]}
        source["discovery"] = {"packages": [{"name": "grub-pc", "version": "2.04"}]}
        result = build_plan(source, inventory("destination"))
        self.assertEqual(result["boot_reconciliation"]["source_signal"], "bios_or_unavailable")
        self.assertEqual(result["boot_reconciliation"]["source_swap_references"], ["UUID=old-swap"])
        self.assertIn("firmware_mode_unresolved", {b["code"] for b in result["blockers"]})
        self.assertIn("source_uefi_bootloader_not_recorded", {b["code"] for b in result["blockers"]})
        self.assertEqual(result["boot_reconciliation"]["source_boot_packages"], [{"name": "grub-pc", "version": "2.04"}])

    def test_subtree_destination_capacity_remains_unknown(self):
        destination = inventory("destination")
        destination["mounts"][0]["root"] = "/subvolume"
        result = build_plan(inventory("source"), destination)
        self.assertEqual(result["capacity"]["status"], "unknown")
        self.assertIsNone(result["capacity"]["filesystems"][0]["budget_bytes"])

    def test_render_escapes_terminal_controls_and_never_builds_commands(self):
        source = inventory("source")
        source["system"]["os"]["PRETTY_NAME"] = "Ubuntu\x1b[31m"
        result = build_plan(source, inventory("destination"))
        text = format_plan(result)
        self.assertNotIn("\x1b", text)
        self.assertIn("\\u001b", text)
        self.assertIn("draft", text.lower())
        self.assertNotIn("rsync ", text)

    def test_unclassified_source_mount_prevents_aggregate_capacity_claim(self):
        source = inventory("source")
        source["mounts"].append(mount("/mnt/unknown", device="0:44", filesystem="fuse.custom", kind="unknown"))
        result = build_plan(source, inventory("destination"))
        self.assertEqual(result["capacity"]["status"], "unknown")
        self.assertIn("source_unknown_filesystems", {row["code"] for row in result["blockers"]})


if __name__ == "__main__":
    unittest.main()
