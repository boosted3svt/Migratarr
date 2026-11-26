"""Offline comparison. No result from this milestone authorizes migration."""

import posixpath

from .mount_review import review_mounts


def validate(inventory, role):
    if not isinstance(inventory, dict) or type(inventory.get("schema_version")) is not int or inventory["schema_version"] != 1:
        raise ValueError(f"{role}: expected inventory schema version 1")
    if inventory.get("role") != role:
        raise ValueError(f"{role}: inventory role does not match")
    system = inventory.get("system")
    if not isinstance(system, dict) or not isinstance(system.get("os"), dict):
        raise ValueError(f"{role}: missing system/OS record")
    if not isinstance(system.get("architecture"), str) or not system["architecture"] or system.get("userspace_bits") not in (32, 64):
        raise ValueError(f"{role}: invalid architecture/bitness")
    for field in ("mounts", "errors", "limitations", "space"):
        if not isinstance(inventory.get(field), list):
            raise ValueError(f"{role}: invalid {field}")
    for mount in inventory["mounts"]:
        if not isinstance(mount, dict) or not all(key in mount for key in ("target", "kind", "filesystem", "device", "root", "shared_device", "subtree_or_bind_candidate")):
            raise ValueError(f"{role}: incomplete mount record")
        if not all(isinstance(mount[key], str) and mount[key] for key in ("target", "kind", "filesystem", "device", "root")):
            raise ValueError(f"{role}: invalid mount strings")
        # nsfs uses roots such as net:[4026532668], not filesystem paths.
        if not mount["target"].startswith("/") or (mount["kind"] == "local" and not mount["root"].startswith("/")):
            raise ValueError(f"{role}: mount paths must be absolute")
        if mount["kind"] not in {"local", "remote", "pseudo", "image", "layered", "unknown"} or any(type(mount[key]) is not bool for key in ("shared_device", "subtree_or_bind_candidate")):
            raise ValueError(f"{role}: invalid mount classification")


def normalize_ignored_mounts(paths):
    result = []
    for path in paths:
        if not isinstance(path, str) or not path.startswith("/") or any(ord(char) < 32 or ord(char) == 127 for char in path):
            raise ValueError("ignored source mounts must be absolute paths without control characters")
        normalized = posixpath.normpath("/" + path.lstrip("/"))
        if normalized == "/":
            raise ValueError("the source root cannot be ignored")
        if normalized not in result:
            result.append(normalized)
    return result


def assess(source, destination, *, ignored_source_mounts=()):
    validate(source, "source")
    validate(destination, "destination")
    findings = []
    ignored = normalize_ignored_mounts(ignored_source_mounts)
    mount_reviews = {}
    discovery = source.get("discovery") or {}
    if not isinstance(discovery, dict):
        raise ValueError("source: invalid discovery record")
    packages = discovery.get("packages")
    if packages is not None and not isinstance(packages, list):
        raise ValueError("source: invalid package records")

    def add(code, message):
        findings.append({"severity": "blocker", "code": code, "message": message})

    add("executor_unimplemented", "Migration execution and RAM recovery/resume are not implemented or VM validated.")
    add("inventory_incomplete", "Application writers, identities, Docker portability, data sizes, boot chain and hardware adaptations require further inventory.")
    a, b = source["system"], destination["system"]
    if (a["architecture"], a["userspace_bits"]) != (b["architecture"], b["userspace_bits"]):
        add("architecture_mismatch", "Source and destination architecture/collector bitness differ.")
    for role, inv in (("source", source), ("destination", destination)):
        if inv["errors"]:
            details = "; ".join(str(error.get("probe", "unknown")) + ": " + str(error.get("error", "unknown")) +
                                (" at " + str(error["path"]) if "path" in error else "") for error in inv["errors"] if isinstance(error, dict))
            add(f"{role}_probe_errors", "Incomplete probes: " + details)
        if inv["system"]["os"].get("ID") != "ubuntu":
            add(f"{role}_os_unqualified", "Initial qualification target is Ubuntu; no release is execution-qualified yet.")
        mounts = inv["mounts"]
        if role == "source":
            mounts = [m for m in mounts if not any(m["target"] == path or m["target"].startswith(path + "/") for path in ignored)]
        rows = review_mounts(mounts, role)
        mount_reviews[role] = rows
        if not any(m["target"] == "/" and m["kind"] == "local" for m in rows):
            add(f"{role}_root_unqualified", "Root is absent or not a recognized local filesystem.")
        unknown = sorted({m["filesystem"] for m in rows if m["kind"] == "unknown"})
        if unknown:
            add(f"{role}_unknown_mounts", "Unclassified filesystems require investigation: " + ", ".join(unknown))
        if role == "source" and any(m["treatment"] in {"local_mapping_review", "duplicate_view_review"} for m in rows):
            add(f"{role}_mount_mapping", "Repeated devices/subtrees require an explicit bind/subvolume transfer map.")
        if role == "source":
            if any(m["kind"] == "image" for m in rows):
                add("source_image_backing_review", "Mounted filesystem images (including Snap) need backing-file/state preservation; their mounted views must not be counted as independent local data.")
            if any(m["kind"] == "layered" for m in rows):
                add("source_layered_backing_review", "Layered filesystem views need Docker/storage dependency review; do not duplicate mounted container views into the copy.")
    if source["system"].get("boot_mode") != destination["system"].get("boot_mode"):
        add("boot_mode_review", "Source and destination boot signals differ or are unavailable; confirm firmware mode and plan bootloader/initramfs adaptation.")
    add("filesystem_map_required", "Review all local filesystems, remote exclusions, destination disk count and protected recovery paths.")
    add("cutover_plan_required", "Review all application and system writers, server/application state preservation, network handover and source rollback before execution.")
    return {"schema_version": 1, "status": "blocked", "migration_ready": False,
            "findings": findings, "mount_review": mount_reviews,
            "ignored_source_mounts": ignored,
            "migration_scope": "entire_local_installation",
            "source_summary": {"system": source["system"], "probe_errors": source["errors"],
                               "package_count": None if packages is None else len(packages),
                               "service_count": None if source.get("services") is None else len(source["services"]),
                               "active_service_count": None if source.get("services") is None else sum(unit.get("active") == "active" for unit in source["services"]),
                               "timer_count": None if discovery.get("timers") is None else len(discovery["timers"]),
                               "server_survey": source.get("server_survey"),
                               "docker": discovery.get("docker")},
            "support": "Assessment preview only; no migration configuration is qualified."}
