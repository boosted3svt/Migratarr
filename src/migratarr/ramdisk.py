"""Shared RAM filesystem construction for recovery rehearsal and developer tests."""

import gzip
import lzma
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

def _run(argv):
    return subprocess.run([str(value) for value in argv], check=True,
                          capture_output=True, timeout=60)

def copy_binary(root, executable):
    """Copy an executable and its absolute shared-library dependencies."""
    source = Path(executable)
    target = root / str(source).lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source.resolve()), str(target))
    result = subprocess.run(["ldd", str(source)], capture_output=True, text=True,
                            timeout=20, check=False)
    if result.returncode and "not a dynamic executable" not in result.stderr + result.stdout:
        raise ValueError("could not inspect guest executable dependencies")
    for name in re.findall(r"(?:=>\s+)?(/[^\s()]+)", result.stdout):
        library = Path(name)
        destination = root / name.lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(library.resolve()), str(destination))

def copy_modules(tree, kernel_version):
    paths = []
    for module in ("virtio_pci", "virtio_blk", "virtio_rng", "e1000", "ext4"):
        output = _run(["modprobe", "--show-depends", "-S", kernel_version, module]).stdout.decode()
        for line in output.splitlines():
            fields = line.split()
            if fields and fields[0] == "insmod" and fields[1] not in paths:
                paths.append(fields[1])
    names = []
    directory = tree / "lib/migratarr-lab-modules"
    directory.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(paths):
        source = Path(name)
        target = directory / ("{:02d}-".format(index) + source.name.split(".ko")[0] + ".ko")
        if source.suffix == ".zst":
            target.write_bytes(_run(["zstd", "-d", "--stdout", source]).stdout)
        elif source.suffix == ".xz":
            target.write_bytes(lzma.decompress(source.read_bytes()))
        elif source.suffix == ".gz":
            target.write_bytes(gzip.decompress(source.read_bytes()))
        else:
            target.write_bytes(source.read_bytes())
        names.append("/" + str(target.relative_to(tree)))
    return names

def pack_initramfs(tree, output):
    """Write newc cpio with root-owned entries without host root privileges."""
    tree, output = Path(tree), Path(output)
    with output.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as archive:
        inode = 0

        def record(name, mode, data=b"", links=1):
            nonlocal inode
            inode += 1
            encoded = name.encode("utf-8") + b"\0"
            fields = (inode, mode, 0, 0, links, 0, len(data), 0, 0, 0, 0, len(encoded), 0)
            header = b"070701" + b"".join(("{:08x}".format(value)).encode("ascii") for value in fields)
            archive.write(header + encoded)
            archive.write(b"\0" * ((-len(header) - len(encoded)) % 4))
            archive.write(data)
            archive.write(b"\0" * ((-len(data)) % 4))

        for path in sorted(tree.rglob("*"), key=lambda item: (len(item.parts), str(item))):
            info = path.lstat()
            name = str(path.relative_to(tree))
            if stat.S_ISLNK(info.st_mode):
                data = os.readlink(str(path)).encode("utf-8")
            elif stat.S_ISREG(info.st_mode):
                data = path.read_bytes()
            elif stat.S_ISDIR(info.st_mode):
                data = b""
            else:
                raise ValueError("lab archive accepts only directories, files and symlinks")
            record(name, info.st_mode, data, 2 if stat.S_ISDIR(info.st_mode) else 1)
        record("TRAILER!!!", 0)
        archive.write(b"\0" * 512)
