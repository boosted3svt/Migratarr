"""Compile the root policy for the fixed VM fixture; no physical-root copying."""
from .path_policy import classify_path, logical_path


def copy_filters(policy, destination_mounts=()):
    """Anchor rules inside policy-case; protect mounted roots as well as data."""
    def literal(path):
        path = logical_path(path)
        if path == "/":
            raise ValueError("sandbox root cannot be excluded as a nested boundary")
        for character in ("\\", "*", "?", "[", "]"):
            path = path.replace(character, "\\" + character)
        return "/policy-case" + path

    protected = {row["path"] for key in ("protected_paths", "reconcile_paths") for row in policy[key]}
    mounted = set(destination_mounts)
    boundaries = set(policy["excluded_paths"])
    for scope in policy["copy_scopes"]:
        boundaries.update(scope["mount_boundaries"])
    return sorted({literal(path) for path in protected | mounted} |
                  {literal(path) + "/*" for path in boundaries - protected - mounted})


def validate_copy_paths(source, destination, policy):
    """Reject redirected ancestors and links sharing managed/protected state."""
    for entries in (source, destination):
        for path, row in entries.items():
            decision = classify_path(path, policy)
            if decision in ("container", "mountpoint") and row["kind"] != "directory":
                raise ValueError("policy ancestor or mountpoint must be a directory: " + path)
            if decision == "copy_candidate" and row["kind"] == "special":
                raise ValueError("special files are unsupported by the policy-copy fixture")
    groups = {}
    for path, row in destination.items():
        if row["kind"] != "directory" and classify_path(path, policy) == "copy_candidate":
            groups.setdefault((row["device"], row["inode"]), []).append(row)
    for group in groups.values():
        if any(row["links"] != len(group) for row in group):
            raise ValueError("managed destination file has links outside the copy scope")
