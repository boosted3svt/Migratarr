"""Classify mount metadata for review without walking or changing filesystems.

Treatments are assessment labels, not an executable copy/exclusion map. In
particular, image and layered mounts still require backing-data investigation.
"""


REMOTE = {"nfs", "nfs4", "cifs", "smb3", "sshfs", "fuse.sshfs", "9p", "ceph", "glusterfs"}
PSEUDO = {"proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "securityfs", "debugfs", "tracefs", "pstore", "efivarfs", "mqueue", "hugetlbfs", "configfs", "fusectl", "autofs", "binfmt_misc", "rpc_pipefs", "nsfs", "bpf", "fuse.gvfsd-fuse", "fuse.portal"}
LOCAL = {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "f2fs", "zfs"}
IMAGE = {"squashfs"}
LAYERED = {"overlay", "overlayfs"}

# Reinterpret these previously unknown types when reading existing reports.
# Do not reinterpret every explicitly unknown filesystem: an existing report may
# deliberately mark a normally local filesystem as unresolved.
NEWLY_KNOWN = {"bpf", "nsfs", "fuse.gvfsd-fuse", "fuse.portal"} | IMAGE | LAYERED


def classify_filesystem(filesystem):
    for kinds, label in ((REMOTE, "remote"), (PSEUDO, "pseudo"),
                         (LOCAL, "local"), (IMAGE, "image"), (LAYERED, "layered")):
        if filesystem in kinds:
            return label
    return "unknown"


def review_mounts(mounts, role):
    """Return role-specific review rows, preserving input ordering and evidence.

    A repeated local device/root pair represents another view of the same
    filesystem subtree. Prefer the actual root mount as its canonical view,
    otherwise the shortest mountpoint then lexical order. Different roots on a
    shared device can be subvolumes or binds and remain distinct review items.
    """
    if role not in ("source", "destination"):
        raise ValueError("mount review role must be source or destination")
    rows = []
    for mount in mounts:
        filesystem = mount["filesystem"]
        kind = mount.get("kind", classify_filesystem(filesystem))
        if kind == "unknown" and filesystem in NEWLY_KNOWN:
            kind = classify_filesystem(filesystem)
        rows.append({key: mount[key] for key in ("target", "filesystem", "device", "root")})
        rows[-1]["kind"] = kind

    grouped = {}
    local_devices = {}
    for row in rows:
        if row["kind"] == "local":
            key = (row["device"], row["root"])
            grouped.setdefault(key, []).append(row["target"])
            local_devices[row["device"]] = local_devices.get(row["device"], 0) + 1
    canonical = {key: min(targets, key=lambda target: (target != "/", len(target), target))
                 for key, targets in grouped.items()}

    for row in rows:
        kind = row["kind"]
        if kind == "local":
            key = (row["device"], row["root"])
            if role == "destination":
                treatment = "destination_target_review"
            elif row["target"] != canonical[key]:
                treatment = "duplicate_view_review"
                row["duplicate_of"] = canonical[key]
            elif row["root"] != "/" or local_devices[row["device"]] > 1:
                treatment = "local_mapping_review"
            else:
                treatment = "local_candidate_review"
        elif kind == "remote":
            treatment = "remote_excluded"
        elif kind == "pseudo":
            treatment = "runtime_excluded"
        elif kind in ("image", "layered"):
            treatment = "bootstrap_mount" if role == "destination" else kind + "_backing_review"
        else:
            treatment = "unknown_review"
        row["treatment"] = treatment
    return rows
