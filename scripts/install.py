#!/usr/bin/env python3
"""Install the standalone destination application without pip or a virtualenv."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

from build import build

TOOLS = {"ssh": "openssh-client", "ssh-keygen": "openssh-client", "rsync": "rsync",
         "lsblk": "util-linux", "findmnt": "util-linux", "ip": "iproute2", "systemctl": "systemd"}


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def owned_directory(path):
    missing = []
    current = path
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o755)
    info = path.lstat()
    if path.resolve() != path or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError("installation directories must be owned, non-writable by others, and free of symlink parents")


def _install_unlocked(prefix):
    prefix = Path(prefix).expanduser().absolute()
    if any(ord(char) < 32 or ord(char) == 127 for char in str(prefix)):
        raise ValueError("invalid installation prefix")
    owned_directory(prefix)
    binary = prefix / "bin"
    state = prefix / "share/migratarr"
    owned_directory(binary)
    owned_directory(prefix / "share")
    owned_directory(state)
    target = binary / "migratarr"
    receipt = state / "install.json"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or target.stat().st_uid != os.geteuid():
            raise ValueError("refusing to replace an unmanaged Migratarr command")
        if receipt.is_symlink() or not receipt.is_file():
            raise ValueError("existing command has no Migratarr installation receipt")
        previous = json.loads(receipt.read_text())
        if previous.get("application") != str(target) or previous.get("sha256") != fingerprint(target):
            raise ValueError("existing command was modified or is not managed by this installer")
    fd, name = tempfile.mkstemp(prefix=".migratarr-", dir=binary)
    os.close(fd)
    staged = Path(name)
    try:
        build(staged)
        subprocess.run([sys.executable, str(staged), "--help"], check=True, capture_output=True, timeout=20)
        staged.chmod(0o755)
        with staged.open("rb") as handle:
            os.fsync(handle.fileno())
        digest = fingerprint(staged)
        previous_bytes = target.read_bytes() if target.exists() else None
        previous_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
        record = {"schema_version": 1, "application": str(target), "sha256": digest,
                  "installed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        fd, name = tempfile.mkstemp(prefix=".install-", dir=state)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(record, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if receipt.is_symlink():
                raise ValueError("refusing a symlink installation receipt")
            os.replace(str(staged), str(target))
            try:
                os.replace(str(temporary), str(receipt))
            except BaseException:
                if previous_bytes is None:
                    target.unlink()
                else:
                    restore_fd, restore_name = tempfile.mkstemp(prefix=".restore-", dir=binary)
                    with os.fdopen(restore_fd, "wb") as restore:
                        restore.write(previous_bytes)
                        restore.flush()
                        os.fsync(restore.fileno())
                    os.chmod(restore_name, previous_mode)
                    os.replace(restore_name, str(target))
                raise
        finally:
            if temporary.exists():
                temporary.unlink()
        return target
    finally:
        if staged.exists():
            staged.unlink()


def install(prefix):
    prefix = Path(os.path.abspath(str(Path(prefix).expanduser())))
    if any(ord(char) < 32 or ord(char) == 127 for char in str(prefix)):
        raise ValueError("invalid installation prefix")
    for directory in (prefix, prefix / "share", prefix / "share/migratarr"):
        owned_directory(directory)
    path = prefix / "share/migratarr/install.lock"
    descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError("unsafe installation lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return _install_unlocked(prefix)
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default=str(Path.home() / ".local"), help="Installation prefix (default: ~/.local)")
    parser.add_argument("--no-start", action="store_true", help="Install without launching guided assessment")
    parser.add_argument("--no-system-packages", action="store_true", help="Require existing tools; never invoke apt or sudo")
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 8):
            raise ValueError("Python 3.8+ is required; the installer does not upgrade an existing interpreter")
        if platform.system() != "Linux":
            raise ValueError("Migratarr requires Linux")
        release = Path("/etc/os-release").read_text()
        if not any(line in ('ID=ubuntu', 'ID="ubuntu"') for line in release.splitlines()):
            raise ValueError("the installer currently supports Ubuntu destinations")
        missing = sorted({package for tool, package in TOOLS.items() if shutil.which(tool) is None})
        if missing:
            if args.no_system_packages:
                raise ValueError("missing destination packages: " + ", ".join(missing))
            privilege = [] if os.geteuid() == 0 else ["sudo"]
            subprocess.run(privilege + ["apt-get", "update"], check=True)
            subprocess.run(privilege + ["apt-get", "install", "-y", "--no-install-recommends", "--no-upgrade"] + missing, check=True)
            absent = [tool for tool in TOOLS if shutil.which(tool) is None]
            if absent:
                raise ValueError("required tools still unavailable: " + ", ".join(absent))
        target = install(args.prefix)
        print("Installed Migratarr: " + str(target), flush=True)
        print("Run: " + shlex.quote(str(target)), flush=True)
        print("Current release: assessment and guarded VM rehearsals; physical migration is not enabled.", flush=True)
        if not args.no_start and sys.stdin.isatty():
            return subprocess.call([sys.executable, str(target)])
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Migratarr installation failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
