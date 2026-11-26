"""Fault injection helpers, used only inside marked disposable test guests."""
import errno
import json
import os
from pathlib import Path
import sys

SOURCE = Path("/var/lib/migratarr/transfer-fixture")


def source_guard(token):
    if os.geteuid() != 0 or Path("/sys/class/dmi/id/sys_vendor").read_text().strip().lower() not in ("qemu", "bochs"):
        raise ValueError("fault injection requires a disposable QEMU guest")
    marker = json.loads(Path("/var/lib/migratarr/transfer-fixture-marker.json").read_text())
    if marker != {"purpose": "migratarr_transfer_fixture", "token": token}:
        raise ValueError("fixture marker differs")


def fill_target(token):
    sys.path.insert(0, "/opt/migratarr.pyz")
    from migratarr.transfer import fixture_context, TARGET_BASE
    from migratarr.recovery import _directory
    _, job, _ = fixture_context(token)
    parent = _directory(Path(TARGET_BASE) / job["job_id"])
    path = parent / "space-filler"
    total = 0
    with path.open("xb") as handle:
        path.chmod(0o600)
        for chunk in (16 * 1024 * 1024, 1024 * 1024, 4096):
            while True:
                try:
                    os.posix_fallocate(handle.fileno(), total, chunk)
                    total += chunk
                except OSError as exc:
                    if exc.errno != errno.ENOSPC:
                        raise
                    break
        os.fsync(handle.fileno())
    print(json.dumps({"allocated_bytes": total, "free_blocks": os.statvfs(parent).f_bfree}))


def remove_filler(token):
    sys.path.insert(0, "/opt/migratarr.pyz")
    from migratarr.transfer import fixture_context, TARGET_BASE
    from migratarr.recovery import _directory, _read
    _, job, _ = fixture_context(token)
    path = _directory(Path(TARGET_BASE) / job["job_id"]) / "space-filler"
    if path.is_symlink() or not path.is_file():
        raise ValueError("expected a regular filler file")
    path.unlink()
    os.sync()


if __name__ == "__main__":
    action, token = sys.argv[1:3]
    if action == "fill-target":
        fill_target(token)
    elif action == "remove-filler":
        remove_filler(token)
    else:
        source_guard(token)
        if action == "add-file":
            name = sys.argv[3]
            if name not in ("failure-network.bin", "failure-mutation.bin", "failure-space.bin"):
                raise ValueError("unsupported fixture file")
            with (SOURCE / name).open("xb") as handle:
                for _ in range(8):
                    handle.write(os.urandom(1024 * 1024))
            os.sync()
        elif action == "mutate":
            path = SOURCE / "owned.txt"
            before = path.stat()
            with path.open("r+b") as handle:
                handle.write(b"MUTATED")
                os.fsync(handle.fileno())
            # Deliberately retain size and mtime; ctime must still catch this.
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
