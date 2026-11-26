"""Fixture creation and independent metadata snapshots, executed only in VMs."""
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess

SOURCE = Path("/var/lib/migratarr/transfer-fixture")
EXCLUDED = {"remote", "bind-duplicate", ".migratarr-partial"}


def setup(token):
    if os.geteuid() != 0 or Path("/sys/class/dmi/id/sys_vendor").read_text().strip().lower() not in ("qemu", "bochs"):
        raise ValueError("fixture setup requires a disposable root QEMU guest")
    SOURCE.parent.mkdir(mode=0o700, exist_ok=True)
    SOURCE.mkdir(mode=0o700)
    marker = SOURCE.parent / "transfer-fixture-marker.json"
    marker.write_text(json.dumps({"purpose": "migratarr_transfer_fixture", "token": token}))
    marker.chmod(0o600)
    (SOURCE / "owned.txt").write_bytes(b"Numeric IDs must survive without account creation.\n")
    os.chown(SOURCE / "owned.txt", 23101, 23102)
    (SOURCE / "owned.txt").chmod(0o640)
    os.setxattr(SOURCE / "owned.txt", "user.migratarr", b"binary\x00metadata\xff")
    os.link(SOURCE / "owned.txt", SOURCE / "hardlink.txt")
    (SOURCE / "link-outside").symlink_to("/opt/migratarr-outside-fixture.txt")
    os.lchown(SOURCE / "link-outside", 23103, 23104)
    Path("/opt/migratarr-outside-fixture.txt").write_text("Do not follow this symlink.\n")
    directory = SOURCE / "acl-directory"
    directory.mkdir(mode=0o2750)
    os.chown(directory, 23101, 23102)
    acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in
        ((1, 7, 0xffffffff), (2, 5, 24680), (4, 5, 0xffffffff), (16, 5, 0xffffffff), (32, 0, 0xffffffff)))
    os.setxattr(directory, "system.posix_acl_access", acl)
    os.setxattr(directory, "system.posix_acl_default", acl)
    (directory / "nested.txt").write_text("Default and access ACL fixture.\n")
    cap = SOURCE / "capability.bin"
    cap.write_bytes(b"Capability metadata fixture; never executed.\n")
    cap.chmod(0o750)
    os.setxattr(cap, "security.capability", struct.pack("<IIIII", 0x02000001, 1 << 10, 0, 0, 0))
    with (SOURCE / "sparse.bin").open("wb") as handle:
        handle.write(b"start")
        handle.seek(16 * 1024 * 1024)
        handle.write(b"end")
    with (SOURCE / "large.bin").open("wb") as handle:
        for _ in range(8):
            handle.write(os.urandom(1024 * 1024))
    for name in ("remote", "shared", "bind-duplicate"):
        (SOURCE / name).mkdir()
    (SOURCE / "shared/keep.txt").write_text("Include the original tree exactly once.\n")
    subprocess.run(["mount", "-t", "tmpfs", "-o", "size=1m", "tmpfs", str(SOURCE / "remote")], check=True)
    (SOURCE / "remote/excluded.txt").write_text("Nested mount contents must stay out.\n")
    subprocess.run(["mount", "--bind", str(SOURCE / "shared"), str(SOURCE / "bind-duplicate")], check=True)
    for path in [SOURCE] + list(SOURCE.rglob("*")):
        os.utime(path, ns=(1700000000123456789, 1700000000123456789), follow_symlinks=False)


def add_restart_file():
    (SOURCE / "acl-directory").chmod(0o2750)
    with (SOURCE / "restart.bin").open("xb") as handle:
        for _ in range(16):
            handle.write(os.urandom(1024 * 1024))


def snapshot(root):
    root = Path(root)
    source_snapshot = str(root) == str(SOURCE)
    rows, links = {}, {}

    def visit(path, relative):
        item = path.lstat()
        kind = "symlink" if stat.S_ISLNK(item.st_mode) else "directory" if stat.S_ISDIR(item.st_mode) else "file"
        row = {"kind": kind, "uid": item.st_uid, "gid": item.st_gid, "mode": stat.S_IMODE(item.st_mode),
               "mtime_ns": item.st_mtime_ns,
               "xattrs": {name: os.getxattr(path, name, follow_symlinks=False).hex()
                          for name in sorted(os.listxattr(path, follow_symlinks=False))}}
        if kind == "file":
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            row.update(size=item.st_size, sha256=digest.hexdigest())
            if item.st_size > 1024 * 1024:
                row["sparse"] = item.st_blocks * 512 < item.st_size // 8
            links.setdefault((item.st_dev, item.st_ino), []).append(relative)
        elif kind == "symlink":
            row["link"] = os.readlink(path)
        rows[relative] = row
        if kind == "directory" and (not source_snapshot or relative == "." or relative.split("/")[0] not in EXCLUDED):
            for child in sorted(path.iterdir()):
                if child.name == ".migratarr-partial":
                    continue
                visit(child, child.name if relative == "." else relative + "/" + child.name)

    visit(root, ".")
    return {"entries": rows, "hardlinks": sorted(sorted(group) for group in links.values() if len(group) > 1)}


if __name__ == "__main__":
    import sys
    if sys.argv[1] == "setup":
        setup(sys.argv[2])
    elif sys.argv[1] == "add-restart":
        add_restart_file()
    elif sys.argv[1] == "snapshot":
        print(json.dumps(snapshot(sys.argv[2]), sort_keys=True))
