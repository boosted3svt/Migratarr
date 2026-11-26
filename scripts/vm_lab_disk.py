"""Build one disposable GPT disk image for the recovery VM lab.

This development helper operates on newly created regular files only. It never
opens host block devices, mounts a filesystem or invokes grub-install.
"""

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import subprocess
import uuid
import zlib


SECTOR_BYTES = 512
DISK_BYTES = 1024 ** 3
ENTRY_COUNT = 128
ENTRY_BYTES = 128
ESP_START_LBA = 2048
ESP_SECTORS = 64 * 1024 ** 2 // SECTOR_BYTES
ROOT_START_LBA = ESP_START_LBA + ESP_SECTORS
ESP_TYPE = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")
ROOT_TYPE = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4")

GRUB_CONFIG = """serial --unit=0 --speed=115200
terminal_input serial
terminal_output serial
set timeout=0
search --label MIGRATARR_LAB --set=root
if [ -f /boot/migratarr/armed ]; then
  if [ -f /boot/migratarr/rescue.img ]; then
    echo Migratarr lab: loading RAM recovery
    linux /boot/vmlinuz console=ttyS0,115200 panic=10 migratarr_mode=rescue
    initrd /boot/migratarr/rescue.img
    boot
  fi
fi
echo Migratarr lab: loading disk bootstrap
linux /boot/vmlinuz console=ttyS0,115200 panic=10 migratarr_mode=bootstrap
initrd /boot/bootstrap.img
boot
"""


def _run(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True,
                          timeout=120, env={**os.environ, "LC_ALL": "C"})


def _private_directory(path):
    path = Path(path).absolute()
    if path.is_symlink():
        raise ValueError("lab work directory must not be a symlink")
    item = path.stat()
    if not stat.S_ISDIR(item.st_mode) or item.st_uid != os.geteuid() or stat.S_IMODE(item.st_mode) & 0o077:
        raise ValueError("lab work directory must be a private directory owned by the current user")
    # No traversal through a symlink to another output location.
    if path.resolve() != path:
        raise ValueError("lab work directory must not have symlink parents")
    return path


def _new_file(path, size=0):
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.ftruncate(descriptor, size)
    finally:
        os.close(descriptor)


def _regular(path):
    item = Path(path).lstat()
    if not stat.S_ISREG(item.st_mode) or item.st_uid != os.geteuid():
        raise ValueError("lab images must be regular files owned by the current user")
    return item


def _header(current, backup, entries_lba, sectors, disk_guid, table_crc):
    header = bytearray(struct.pack("<8sIIIIQQQQ16sQIII", b"EFI PART", 0x10000,
                                   92, 0, 0, current, backup, 34, sectors - 34,
                                   disk_guid.bytes_le, entries_lba, ENTRY_COUNT,
                                   ENTRY_BYTES, table_crc))
    struct.pack_into("<I", header, 16, zlib.crc32(header) & 0xffffffff)
    return bytes(header).ljust(SECTOR_BYTES, b"\0")


def make_gpt(disk_bytes=DISK_BYTES, disk_guid=None, esp_guid=None, root_guid=None):
    """Return GPT metadata bytes and layout, without opening files."""
    if type(disk_bytes) is not int or disk_bytes % 4096 or disk_bytes < 256 * 1024 ** 2:
        raise ValueError("lab disk size must be an aligned regular-image capacity")
    sectors = disk_bytes // SECTOR_BYTES
    root_sectors = ((sectors - 34 - ROOT_START_LBA + 1) // 8) * 8
    disk_guid = disk_guid or uuid.uuid4()
    esp_guid = esp_guid or uuid.uuid4()
    root_guid = root_guid or uuid.uuid4()
    table = bytearray(ENTRY_COUNT * ENTRY_BYTES)
    for index, kind, identity, first, count, name in (
        (0, ESP_TYPE, esp_guid, ESP_START_LBA, ESP_SECTORS, "MIGRATARR_LAB_ESP"),
        (1, ROOT_TYPE, root_guid, ROOT_START_LBA, root_sectors, "MIGRATARR_LAB_ROOT"),
    ):
        table[index * ENTRY_BYTES:(index + 1) * ENTRY_BYTES] = struct.pack(
            "<16s16sQQQ72s", kind.bytes_le, identity.bytes_le, first,
            first + count - 1, 0, name.encode("utf-16le").ljust(72, b"\0"))
    crc = zlib.crc32(table) & 0xffffffff
    mbr = bytearray(SECTOR_BYTES)
    mbr[446:462] = struct.pack("<B3sB3sII", 0, b"\x00\x02\x00", 0xee,
                               b"\xff\xff\xff", 1, min(sectors - 1, 0xffffffff))
    mbr[510:512] = b"\x55\xaa"
    primary = bytes(mbr) + _header(1, sectors - 1, 2, sectors, disk_guid, crc) + bytes(table)
    backup = bytes(table) + _header(sectors - 1, 1, sectors - 33, sectors, disk_guid, crc)
    return {"primary": primary, "backup": backup, "disk_bytes": disk_bytes,
            "esp_start_lba": ESP_START_LBA, "esp_sectors": ESP_SECTORS,
            "root_start_lba": ROOT_START_LBA, "root_sectors": root_sectors,
            "disk_uuid": str(disk_guid), "esp_partuuid": str(esp_guid),
            "root_partuuid": str(root_guid)}


def _copy_partition(source, destination, offset, expected_size):
    item = _regular(source)
    _regular(destination)
    if item.st_size != expected_size:
        raise ValueError("partition image has unexpected capacity")
    with open(source, "rb") as src, open(destination, "r+b") as dst:
        # Skipping zero chunks preserves sparse output without depending on
        # host filesystem SEEK_HOLE behavior. The new image is already zeroed.
        while True:
            block = src.read(1024 * 1024)
            if not block:
                break
            position = src.tell() - len(block)
            if block.strip(b"\0"):
                dst.seek(offset + position)
                dst.write(block)
        dst.flush()
        os.fsync(dst.fileno())


def _digest_region(handle, start, length):
    handle.seek(start)
    result = hashlib.sha256()
    while length:
        data = handle.read(min(length, 1024 * 1024))
        if not data:
            raise ValueError("truncated lab disk image")
        result.update(data)
        length -= len(data)
    return result.hexdigest()


def inspect_disk_layout(info):
    """Read immutable GPT/ESP regions and the ext4 UUID from the raw image."""
    disk = Path(info.get("disk", info.get("disk_image", "")))
    item = _regular(disk)
    if item.st_size != info["disk_bytes"]:
        raise ValueError("lab disk image capacity changed")
    with disk.open("rb") as handle:
        primary = _digest_region(handle, 0, 34 * SECTOR_BYTES)
        backup = _digest_region(handle, item.st_size - 33 * SECTOR_BYTES, 33 * SECTOR_BYTES)
        partition_table = hashlib.sha256((primary + backup).encode("ascii")).hexdigest()
        esp = _digest_region(handle, info["esp_start_lba"] * SECTOR_BYTES,
                             info["esp_sectors"] * SECTOR_BYTES)
        handle.seek(info["root_start_lba"] * SECTOR_BYTES + 1024 + 104)
        root_uuid = str(uuid.UUID(bytes=handle.read(16)))
    return {"partition_table_sha256": partition_table, "esp_sha256": esp,
            "root_uuid": root_uuid}


def check_disk_layout(info):
    current = inspect_disk_layout(info)
    current["partition_table_unchanged"] = current["partition_table_sha256"] == info["partition_table_sha256"]
    current["esp_unchanged"] = current["esp_sha256"] == info["esp_sha256"]
    current["root_uuid_unchanged"] = current["root_uuid"] == info["root_uuid"]
    current["passed"] = all(current[key] for key in ("partition_table_unchanged", "esp_unchanged", "root_uuid_unchanged"))
    return current


def build_disk(work, disk_tree, root_owned_paths):
    """Format and assemble one new disposable regular-file virtual disk."""
    work = _private_directory(work)
    disk_tree = Path(disk_tree).absolute()
    if disk_tree.is_symlink() or not disk_tree.is_dir():
        raise ValueError("disk staging tree must be a real directory")
    layout = make_gpt()
    names = ("root.img", "efi.img", "destination.img", "grub-lab.cfg", "BOOTX64.EFI")
    if any((work / name).exists() or (work / name).is_symlink() for name in names):
        raise FileExistsError("lab image output already exists")
    owned = []
    for path in root_owned_paths:
        if not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]*", path) or ".." in PurePosixPath(path).parts:
            raise ValueError("guest ownership paths must be simple absolute paths")
        owned.append(path)
    root = work / "root.img"
    esp = work / "efi.img"
    disk = work / "destination.img"
    _new_file(root, layout["root_sectors"] * SECTOR_BYTES)
    _new_file(esp, layout["esp_sectors"] * SECTOR_BYTES)
    _new_file(disk, layout["disk_bytes"])
    root_uuid = str(uuid.uuid4())
    _run(["mke2fs", "-q", "-t", "ext4", "-F", "-b", "4096", "-U", root_uuid,
          "-L", "MIGRATARR_LAB", "-E", "lazy_itable_init=0,lazy_journal_init=0,root_owner=0:0",
          "-d", str(disk_tree), str(root)])
    for path in owned:
        for field in ("uid", "gid"):
            _run(["debugfs", "-w", "-R", "set_inode_field " + path + " " + field + " 0", str(root)])
    config = work / "grub-lab.cfg"
    _new_file(config)
    config.write_text(GRUB_CONFIG, encoding="utf-8")
    efi = work / "BOOTX64.EFI"
    _new_file(efi)
    _run(["grub-mkstandalone", "-O", "x86_64-efi", "-o", str(efi),
          "--modules=part_gpt fat ext2 linux search search_label serial terminal test gzio",
          "boot/grub/grub.cfg=" + str(config)])
    _run(["mformat", "-F", "-v", "MIGRATARR", "-i", str(esp), "::"])
    _run(["mmd", "-i", str(esp), "::/EFI", "::/EFI/BOOT"])
    _run(["mcopy", "-i", str(esp), str(efi), "::/EFI/BOOT/BOOTX64.EFI"])
    with disk.open("r+b") as handle:
        handle.write(layout["primary"])
        handle.seek(layout["disk_bytes"] - len(layout["backup"]))
        handle.write(layout["backup"])
    _copy_partition(esp, disk, layout["esp_start_lba"] * SECTOR_BYTES,
                    layout["esp_sectors"] * SECTOR_BYTES)
    _copy_partition(root, disk, layout["root_start_lba"] * SECTOR_BYTES,
                    layout["root_sectors"] * SECTOR_BYTES)
    info = {key: value for key, value in layout.items() if key not in ("primary", "backup")}
    info.update({"disk": str(disk), "disk_image": str(disk), "root_image": str(root),
                 "esp_image": str(esp), "root_uuid": root_uuid})
    info.update(inspect_disk_layout(info))
    if info["root_uuid"] != root_uuid:
        raise ValueError("root filesystem UUID was not preserved during assembly")
    return info
