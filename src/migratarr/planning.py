"""Pure whole-server migration planning; no executable migration operations."""

import json

from .assessment import normalize_ignored_mounts, validate
from .mount_review import review_mounts
from .path_policy import build_path_policy

RECOVERY_RESERVE_BYTES = 2 * 1024 ** 3


def _integer(value):
    return type(value) is int and value >= 0


def _identifiers(inventory, target):
    blocks = inventory.get("block_devices")
    if not isinstance(blocks, dict):
        return {"status": "unknown", "uuid": None, "partuuid": None}
    matches = []

    def visit(nodes):
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if target in (node.get("mountpoints") or []):
                matches.append(node)
            visit(node.get("children"))

    visit(blocks.get("blockdevices"))
    if len(matches) != 1:
        return {"status": "unknown" if not matches else "ambiguous", "uuid": None, "partuuid": None}
    node = matches[0]
    return {"status": "resolved" if node.get("uuid") else "unknown",
            "uuid": node.get("uuid"), "partuuid": node.get("partuuid")}


def _space(inventory, target):
    rows = [row for row in inventory["space"] if isinstance(row, dict) and row.get("target") == target]
    return rows[0] if len(rows) == 1 else {}


def _endpoint(inventory, row):
    result = {key: row[key] for key in ("target", "filesystem", "device", "root")}
    result["identifiers"] = _identifiers(inventory, row["target"])
    return result


def build_plan(source, destination, ignored_source_mounts=()):
    """Build a blocked review draft from inventory snapshots without file scans."""
    validate(source, "source")
    validate(destination, "destination")
    ignored = normalize_ignored_mounts(ignored_source_mounts)
    source_rows = review_mounts([row for row in source["mounts"] if not any(
        row["target"] == path or row["target"].startswith(path + "/") for path in ignored)], "source")
    destination_rows = review_mounts(destination["mounts"], "destination")
    source_local = [row for row in source_rows if row["kind"] == "local"]
    destination_local = [row for row in destination_rows if row["kind"] == "local"]
    blockers = []

    def block(code, message):
        if code not in {row["code"] for row in blockers}:
            blockers.append({"code": code, "message": message})

    block("execution_unqualified", "Migration execution, RAM boot/resume and recovery remain unimplemented or unqualified.")
    block("writer_and_privilege_review", "Review all writers, privileged copying, metadata preservation and controlled network handover.")
    block("boot_storage_review", "Validate bootloader/initramfs, hardware adaptations and complete storage-engine portability.")
    if source["errors"] or destination["errors"]:
        block("incomplete_evidence", "Inventory probe failures remain unresolved; inspect both snapshots.")
    blocks = destination.get("block_devices")
    topology = blocks.get("blockdevices") if isinstance(blocks, dict) else None
    disks = [node["name"] for node in topology if isinstance(node, dict) and node.get("type") == "disk" and isinstance(node.get("name"), str)] if isinstance(topology, list) else []
    disk_count = len(disks) if disks else None
    if disk_count is None:
        block("destination_disk_topology_unknown", "A single destination disk has not been established from block-device evidence.")
    elif disk_count != 1:
        block("destination_disk_count", "The agreed design requires one destination disk; multiple physical disks were reported.")
    if not any(row["target"] == "/" for row in source_local) or not any(row["target"] == "/" for row in destination_local):
        block("root_mapping_missing", "Both machines need an identifiable local root filesystem.")
    if (source["system"]["architecture"], source["system"]["userspace_bits"]) != (destination["system"]["architecture"], destination["system"]["userspace_bits"]):
        block("architecture_mismatch", "Source and destination architecture/bitness differ.")
    for role, rows in (("source", source_rows), ("destination", destination_rows)):
        if any(row["kind"] == "unknown" for row in rows):
            block(role + "_unknown_filesystems", "Unclassified filesystem mounts require review.")
    if any(row["kind"] in ("image", "layered") for row in source_rows):
        block("backing_data_review", "Preserve local backing data for image/layered mounts without traversing their mounted views; portability is unresolved.")
    source_boot, destination_boot = source["system"].get("boot_mode"), destination["system"].get("boot_mode")
    discovery = source.get("discovery") or {}
    packages = discovery.get("packages") if isinstance(discovery, dict) else None
    boot_packages = [{"name": row["name"], "version": row.get("version")} for row in packages
                     if isinstance(row, dict) and isinstance(row.get("name"), str) and
                     row["name"].startswith(("grub", "shim", "linux-image", "initramfs-tools"))] if isinstance(packages, list) else None
    if source_boot not in ("bios", "uefi") or destination_boot not in ("bios", "uefi"):
        block("firmware_mode_unresolved", "Missing EFI evidence does not establish BIOS mode; confirm actual firmware and boot requirements.")
    elif source_boot != destination_boot:
        block("firmware_mode_change", "Source and destination firmware modes differ; boot adaptation requires validation.")
    if destination_boot == "uefi" and boot_packages is not None and not any(row["name"].startswith("grub-efi") for row in boot_packages):
        block("source_uefi_bootloader_not_recorded", "A source-compatible UEFI bootloader is not recorded in installed dpkg metadata; validate how the copied source OS will boot on the retained EFI layout.")

    mapping, capacity = [], []
    for row in source_local:
        candidates = [target for target in destination_local if target["target"] == row["target"]]
        target = candidates[0] if len(candidates) == 1 else None
        manual = row["treatment"] in ("local_mapping_review", "duplicate_view_review")
        if manual:
            block("source_mount_mapping_required", "Shared devices, subvolumes or duplicate views need an explicit map; none are silently selected for copying.")
        if target is None:
            block("unpaired_source_filesystem", "A source local mount has no unique destination at the same mount target; explicit mapping is required.")
        entry = {"source": _endpoint(source, row), "destination": _endpoint(destination, target) if target else None,
                 "mapping_status": "manual_review" if manual or target is None else "candidate",
                 "action": "review_only"}
        mapping.append(entry)
        source_space = _space(source, row["target"])
        target_space = _space(destination, target["target"]) if target else {}
        used, available = source_space.get("used_bytes"), target_space.get("available_bytes")
        source_shared = sum(other["device"] == row["device"] for other in source_local) > 1
        destination_shared = target is not None and sum(other["device"] == target["device"] for other in destination_local) > 1
        known = target is not None and target["root"] == "/" and not manual and not source_shared and not destination_shared and _integer(used) and _integer(available)
        if known and _integer(source_space.get("total_bytes")) and used > source_space["total_bytes"]:
            known = False
        margin = (used + 4) // 5 if known else None
        reserve = RECOVERY_RESERVE_BYTES if row["target"] == "/" else 0
        required = used + margin + reserve if known else None
        state = "unknown" if not known else "preliminary_sufficient" if available >= required else "insufficient"
        capacity.append({"source_target": row["target"], "destination_target": target["target"] if target else None,
                         "status": state, "source_used_bytes": used if _integer(used) else None,
                         "destination_available_bytes": available if _integer(available) else None,
                         "margin_percent": 20, "margin_bytes": margin, "recovery_reserve_bytes": reserve,
                         "budget_bytes": required, "remaining_after_budget_bytes": available - required if known else None})
        if state == "unknown":
            block("capacity_unresolved", "Unambiguous per-filesystem used/free evidence is missing; shared storage is not summed.")
        elif state == "insufficient":
            block("capacity_insufficient", "A destination filesystem lacks the conservative space budget.")
        if target is not None and target["root"] != "/":
            entry["mapping_status"] = "manual_review"
            capacity[-1]["status"] = "unknown"
            block("destination_subtree_mapping_required", "A destination target is a subtree/subvolume and needs explicit storage mapping.")
    mapped_targets = {entry["destination"]["target"] for entry in mapping if entry["destination"]}
    adaptations = []
    for row in destination_local:
        if row["target"] not in mapped_targets:
            if row["target"] == "/boot/efi" and row["filesystem"] == "vfat":
                adaptations.append({"target": row["target"], "purpose": "existing_efi_boot_adaptation", "action": "retain_filesystem_and_validate_bootloader"})
            else:
                block("unpaired_destination_filesystem", "An extra destination local filesystem needs review rather than an invented source mapping.")
    states = [row["status"] for row in capacity]
    unresolved_source = any(row["kind"] == "unknown" for row in source_rows) or not any(row["target"] == "/" for row in source_local)
    overall = "insufficient" if "insufficient" in states else "unknown" if unresolved_source or not states or "unknown" in states else "preliminary_sufficient"
    return {"schema_version": 1, "status": "draft_blocked", "migration_ready": False,
            "scope": "entire_local_installation", "source_os_to_import": source["system"]["os"],
            "destination_bootstrap_os": destination["system"]["os"],
            "destination_layout_policy": "retain_existing_partitions_filesystems_and_identifiers",
            "destination_disk_count": disk_count, "destination_disks": disks,
            "boot_reconciliation": {"source_signal": source_boot, "destination_signal": destination_boot,
                "source_boot_packages": boot_packages,
                "source_swap_references": [row.get("source_reference") for row in ((source.get("server_survey") or {}).get("fstab") or []) if isinstance(row, dict) and row.get("filesystem") == "swap"],
                "requirements": ["Retain destination EFI partition and identifiers; never blindly replace its contents with source boot files.",
                                 "Reconcile copied root/fstab references with destination UUIDs, filesystem types and mount options.",
                                 "Reconcile all source swap UUIDs and resume references against the existing destination layout.",
                                 "Validate source-OS-compatible UEFI/BIOS bootloader, initramfs, drivers and Secure Boot requirements before boot."]},
            "ignored_source_mounts": ignored, "filesystem_map": mapping,
            "path_policy": build_path_policy(mapping, source_rows, destination_rows, ignored),
            "destination_adaptations": adaptations, "capacity": {"status": overall, "filesystems": capacity,
                "limitations": ["Snapshot filesystem allocation is not transferable file-byte proof and includes filesystem metadata.",
                                "Sparse files, hard links, filesystem feature differences and ongoing writes require validation.",
                                "Capacity is checked per filesystem, never by adding shared-device/subvolume usage.",
                                "No recursive scan or per-file size/ownership audit is performed; the margin is a planning allowance, not a guarantee."]},
            "preservation_requirements": ["numeric_uid_gid", "permissions", "symlink_objects", "hard_links", "acls", "xattrs", "capabilities", "sparse_file_semantics"],
            "mount_boundary_policy": "Do not traverse remote/runtime/image/layered views; preserve local backing data and mount configuration after review.",
            "blockers": blockers}


def format_plan(plan):
    """Render an escaped, readable review without shell commands."""
    def safe(value):
        return json.dumps(str(value), ensure_ascii=True)[1:-1]

    lines = ["Migratarr whole-server plan", "Status: DRAFT BLOCKED; no migration execution is authorized or implemented.",
             "Scope: complete source OS and local state; destination partitions/filesystems are retained.",
             "Source OS to import: " + safe(plan["source_os_to_import"].get("PRETTY_NAME", plan["source_os_to_import"].get("ID", "unknown"))),
             "Destination physical disks reported: " + safe(plan["destination_disk_count"]), "", "Filesystem mapping candidates:"]
    for row in plan["filesystem_map"]:
        source, target = row["source"], row["destination"]
        destination = safe(target["target"]) + " [" + safe(target["filesystem"]) + "]" if target else "UNMAPPED"
        lines.append("  " + safe(source["target"]) + " [" + safe(source["filesystem"]) + "] -> " + destination + " (" + safe(row["mapping_status"]) + ")")
    for row in plan["destination_adaptations"]:
        lines.append("  Retain " + safe(row["target"]) + " for boot adaptation; no source-data copy is inferred.")
    lines.extend(["", "Boot and swap reconciliation:"])
    for requirement in plan["boot_reconciliation"]["requirements"]:
        lines.append("  " + safe(requirement))
    lines.extend(["", "Aggregate storage capacity by filesystem: " + safe(plan["capacity"]["status"])])
    for row in plan["capacity"]["filesystems"]:
        def gib(value):
            return "unknown" if value is None else "{:.2f} GiB".format(value / 1024 ** 3)
        lines.append("  " + safe(row["source_target"]) + ": source allocated " + gib(row["source_used_bytes"]) +
                     "; budget " + gib(row["budget_bytes"]) + "; destination available " + gib(row["destination_available_bytes"]) + "; " + safe(row["status"]))
    lines.append("  Budget: allocated bytes + 20% allowance + 2 GiB recovery reserve on root. This is preliminary snapshot evidence, not a copy-size guarantee.")
    for path in plan["ignored_source_mounts"]:
        lines.append("  Operator source exclusion: " + safe(path))
    policy = plan.get("path_policy")
    if policy:
        lines.extend(["", "Whole-system path policy (review only):"])
        for key, label in (("protected_paths", "Protect"), ("reconcile_paths", "Reconcile before import")):
            for rule in policy[key]:
                lines.append("  " + label + ": " + safe(rule["path"]))
        for path in policy["excluded_paths"]:
            lines.append("  Retain mountpoint, exclude contents: " + safe(path))
        lines.append("  Cleanup requires an exact fresh preview; whole-root deletion remains disabled.")
    lines.extend(["", "Preserve: " + ", ".join(plan["preservation_requirements"]), "", "Unresolved checks:"])
    for blocker in plan["blockers"]:
        lines.append("  " + safe(blocker["code"]) + ": " + safe(blocker["message"]))
    return "\n".join(lines) + "\n"
