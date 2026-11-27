"""Bounded metadata census for offline VM filesystems; no content staging."""
import os
import stat


def census(root, opaque, max_entries=250000):
    root = os.path.abspath(root)
    entries = {}
    def visit(path, name):
        if name in opaque:
            return
        if len(entries) >= max_entries:
            raise ValueError("OS census exceeds its entry limit")
        s = os.lstat(path)
        kind = "d" if stat.S_ISDIR(s.st_mode) else "f" if stat.S_ISREG(s.st_mode) else "l" if stat.S_ISLNK(s.st_mode) else "x"
        row = [kind, s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid,
               s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink, s.st_blocks * 512]
        if kind == "l":
            row.append(os.readlink(path))
        entries[name] = row
        if kind == "d":
            for child in sorted(os.listdir(path)):
                visit(os.path.join(path, child), (name.rstrip("/") + "/" + child))
    visit(root, "/")
    return entries


def removal_paths(source, destination, guards):
    """Exact obsolete/type-conflict entries; preserve guarded ancestors."""
    conflicts = {p for p in source.keys() & destination.keys() if source[p][0] != destination[p][0]}
    result = []
    for path, row in destination.items():
        conflicting = any(path == p or path.startswith(p + "/") for p in conflicts)
        if path in source and not conflicting:
            continue
        contains_guard = path == "/" or any(g.startswith(path + "/") or g == path for g in guards)
        if contains_guard:
            if conflicting:
                raise ValueError("type conflict contains protected paths: " + path)
            continue
        result.append(path)
    return sorted(result, key=lambda p: (-p.count("/"), p))
