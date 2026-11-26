"""Whole-root path decisions and bounded namespace evidence; no mutations.

This standard-library-only module can also be streamed into a fixture VM.
"""
import hashlib
import json
import os
import stat

PROTECTED = {
    "/var/lib/migratarr": "durable_jobs_credentials_and_transfer_state",
    "/boot/migratarr": "ram_recovery_assets",
    "/boot/grub": "active_boot_chain_until_boot_reconciliation",
    "/boot/efi": "retained_efi_filesystem_until_boot_reconciliation",
    "/etc/grub.d/42_migratarr_recovery_test": "active_vm_recovery_entry",
    "/etc/migratarr-recovery-test.json": "disposable_vm_guard",
}
RECONCILE = {
    "/etc/fstab": "destination_filesystem_identifiers_and_types",
    "/etc/crypttab": "encryption_mapping",
    "/etc/initramfs-tools/conf.d/resume": "swap_resume_mapping",
    "/etc/netplan": "network_hardware_and_controlled_address_handover",
    "/etc/network/interfaces": "network_hardware_and_address_handover",
    "/etc/NetworkManager/system-connections": "network_hardware_and_address_handover",
    "/etc/systemd/network": "network_hardware_and_address_handover",
    "/etc/machine-id": "machine_identity_decision",
    "/etc/ssh": "ssh_identity_and_access_reconciliation",
}
RUNTIME = ("/proc", "/sys", "/dev", "/run")
COLLISION_PATHS = ("/var/lib/migratarr", "/boot/migratarr",
                   "/etc/grub.d/42_migratarr_recovery_test", "/etc/migratarr-recovery-test.json")


def logical_path(path):
    if (not isinstance(path, str) or not path.startswith("/") or "//" in path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or any(part in (".", "..") for part in path.split("/"))):
        raise ValueError("policy paths must be canonical absolute paths")
    return path.rstrip("/") or "/"


def within(path, parent):
    return parent == "/" or path == parent or path.startswith(parent + "/")


def rules(scopes, excluded=()):
    return {"schema_version": 1, "status": "draft_blocked", "migration_ready": False,
            "copy_scopes": scopes,
            "protected_paths": [{"path": key, "reason": value} for key, value in PROTECTED.items()],
            "reconcile_paths": [{"path": key, "reason": value} for key, value in RECONCILE.items()],
            "excluded_paths": sorted(set(logical_path(path) for path in list(RUNTIME) + list(excluded))),
            "deletion_policy": "exact_preview_only_after_fresh_source_and_destination_validation",
            "source_collision_policy": "block_on_reserved_recovery_namespace_presence",
            "temporary_space_policy": {"whole_tree_delayed_updates": False,
                "scratch": "private_on_same_destination_filesystem",
                "budget": "reserve_recovery_space_and_validate_next_file_temporary_headroom",
                "qualified": False}}


def build_path_policy(mapping, source_mounts, destination_mounts, ignored=()):
    scopes, excluded, unresolved = [], list(ignored), []
    mounts = list(source_mounts) + list(destination_mounts)
    for row in mounts:
        target = logical_path(row["target"])
        if target != "/" and row.get("kind") != "local":
            excluded.append(target)
            if row.get("kind") == "unknown":
                unresolved.append(target)
    for entry in mapping:
        source, destination = entry["source"], entry.get("destination")
        target = logical_path(source["target"])
        if target == "/boot/efi":
            continue
        if (entry.get("mapping_status") != "candidate" or not destination
                or source["root"] != "/" or destination["root"] != "/"
                or source["target"] != destination["target"]):
            unresolved.append(target)
            if target != "/":
                excluded.append(target)
            continue
        scopes.append({"source_target": target, "destination_target": target,
            "source_uuid": source["identifiers"].get("uuid"),
            "destination_uuid": destination["identifiers"].get("uuid"),
            "action": "candidate_only", "mount_boundaries": sorted(set(
                logical_path(row["target"]) for row in mounts
                if row["target"] != target and within(logical_path(row["target"]), target)))})
    paired = {scope["destination_target"] for scope in scopes}
    for row in destination_mounts:
        if row["kind"] == "local" and row["target"] not in paired and row["target"] != "/boot/efi":
            unresolved.append(row["target"])
            if row["target"] != "/":
                excluded.append(row["target"])
    result = rules(scopes, excluded)
    result["unresolved_mounts"] = sorted(set(unresolved))
    result["requirements"] = ["Probe source reserved-path collisions before adopting recovery namespaces.",
        "Revalidate live source and destination mount identities before copy or cleanup.",
        "Coordinate every source writer and preserve the source for rollback.",
        "Review cleanup candidates; never use unconditional root-level recursive deletion.",
        "Reconcile boot, storage, networking and identity before booting the imported OS."]
    return result


def classify_path(path, policy):
    path = logical_path(path)
    for action, key in (("protect", "protected_paths"), ("reconcile", "reconcile_paths")):
        for rule in policy[key]:
            if within(path, rule["path"]):
                return action
    for boundary in policy["excluded_paths"]:
        if path == boundary:
            return "mountpoint"
        if within(path, boundary):
            return "exclude"
    scopes = [scope for scope in policy["copy_scopes"] if within(path, scope["destination_target"])]
    if not scopes:
        return "blocked"
    scope = max(scopes, key=lambda row: len(row["destination_target"]))
    for boundary in scope["mount_boundaries"]:
        if path == boundary:
            return "mountpoint"
        if within(path, boundary):
            return "exclude"
    guards = [rule["path"] for key in ("protected_paths", "reconcile_paths") for rule in policy[key]]
    guards.extend(policy["excluded_paths"] + scope["mount_boundaries"])
    if path == scope["destination_target"] or any(within(guard, path) for guard in guards):
        return "container"
    return "copy_candidate"


def scan_paths(root, mounts=(), max_entries=4096, max_bytes=256 * 1024 * 1024):
    """Record bounded metadata/content evidence without following links/mounts."""
    root = os.path.abspath(root)
    mount_set = set(mounts)
    entries = {}
    bytes_read = 0

    def visit(path, logical):
        nonlocal bytes_read
        if len(entries) >= max_entries:
            raise ValueError("namespace preview exceeds its entry limit")
        info = os.lstat(path)
        kind = "directory" if stat.S_ISDIR(info.st_mode) else "symlink" if stat.S_ISLNK(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "special"
        entries[logical] = {"kind": kind, "device": info.st_dev, "inode": info.st_ino,
            "mode": info.st_mode, "uid": info.st_uid, "gid": info.st_gid,
            "size": info.st_size, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
            "links": info.st_nlink}
        if kind == "symlink":
            entries[logical]["link"] = os.readlink(path)
        elif kind == "file" and path not in mount_set:
            bytes_read += info.st_size
            if bytes_read > max_bytes:
                raise ValueError("namespace preview exceeds its byte limit")
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            entries[logical]["sha256"] = digest.hexdigest()
        if kind == "directory" and path not in mount_set:
            for name in sorted(os.listdir(path)):
                visit(os.path.join(path, name), "/" + name if logical == "/" else logical + "/" + name)

    visit(root, "/")
    return entries


def evidence_hash(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cleanup_candidates(source, destination, policy):
    """Preview only: no filesystem operations are performed here."""
    collisions = [path for path in source if any(within(path, reserved) for reserved in COLLISION_PATHS)]
    candidates = [{"path": path, "evidence": row} for path, row in destination.items()
                  if path not in source and classify_path(path, policy) == "copy_candidate"]
    candidates.sort(key=lambda row: (-row["path"].count("/"), row["path"]))
    return {"candidates": candidates, "source_collisions": sorted(collisions),
            "hardlink_conflicts": sorted(row["path"] for row in candidates
                                         if row["evidence"]["kind"] != "directory" and row["evidence"]["links"] > 1),
            "type_conflicts": sorted(path for path in set(source) & set(destination)
                                     if source[path]["kind"] != destination[path]["kind"]),
            "retained": [{"path": path, "decision": classify_path(path, policy)}
                         for path in destination if classify_path(path, policy) != "copy_candidate"]}
