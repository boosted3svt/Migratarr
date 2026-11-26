"""Bounded fixture stability evidence; not a whole-server snapshot mechanism.

This module is also streamed to the disposable source's in-memory Python.
"""
import hashlib
import json
import os
import stat


def tree_fingerprint(root, excluded_mounts=(), max_entries=4096, max_bytes=256 * 1024 * 1024):
    root = os.path.abspath(root)
    excluded = set(excluded_mounts)
    rows = []
    bytes_read = 0

    def visit(path, relative):
        nonlocal bytes_read
        if len(rows) >= max_entries:
            raise ValueError("fixture stability census exceeds its entry limit")
        if path in excluded:
            rows.append((relative, "excluded_mount"))
            return
        info = os.lstat(path)
        row = [relative, info.st_dev, info.st_ino, info.st_mode, info.st_uid,
               info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink]
        if stat.S_ISLNK(info.st_mode):
            row.append(os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            bytes_read += info.st_size
            if bytes_read > max_bytes:
                raise ValueError("fixture stability census exceeds its byte limit")
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            row.append(digest.hexdigest())
        rows.append(row)
        if stat.S_ISDIR(info.st_mode):
            for name in sorted(os.listdir(path)):
                visit(os.path.join(path, name), name if relative == "." else relative + "/" + name)

    visit(root, ".")
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def largest_fixture_file(root, excluded_mounts=(), max_entries=4096):
    """Fixture-only file-list evidence; no source contents are read here."""
    excluded = set(excluded_mounts)
    largest, count = 0, 0

    def visit(path):
        nonlocal largest, count
        count += 1
        if count > max_entries:
            raise ValueError("fixture file list exceeds its entry limit")
        if path in excluded:
            return
        info = os.lstat(path)
        if stat.S_ISREG(info.st_mode):
            largest = max(largest, info.st_size)
        elif stat.S_ISDIR(info.st_mode):
            for name in os.listdir(path):
                visit(os.path.join(path, name))

    visit(str(root))
    return largest
