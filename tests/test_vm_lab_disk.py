import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zlib


SPEC = importlib.util.spec_from_file_location("vm_lab_disk", Path(__file__).resolve().parents[1] / "scripts/vm_lab_disk.py")
disk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(disk)


class LabDiskTests(unittest.TestCase):
    def test_gpt_headers_tables_and_partition_boundaries(self):
        layout = disk.make_gpt()
        primary, backup = layout["primary"], layout["backup"]
        self.assertEqual(primary[510:512], b"\x55\xaa")
        self.assertEqual(primary[450], 0xee)
        self.assertEqual(len(primary), 34 * 512)
        self.assertEqual(len(backup), 33 * 512)
        for sector in (primary[512:1024], backup[-512:]):
            self.assertEqual(sector[:8], b"EFI PART")
            header = bytearray(sector[:92])
            checksum = struct.unpack_from("<I", header, 16)[0]
            struct.pack_into("<I", header, 16, 0)
            self.assertEqual(zlib.crc32(header) & 0xffffffff, checksum)
        table = primary[1024:]
        self.assertEqual(table, backup[:-512])
        self.assertEqual(zlib.crc32(table) & 0xffffffff, struct.unpack_from("<I", primary, 512 + 88)[0])
        self.assertEqual(uuid.UUID(bytes_le=table[:16]), disk.ESP_TYPE)
        self.assertEqual(uuid.UUID(bytes_le=table[128:144]), disk.ROOT_TYPE)
        self.assertEqual(struct.unpack_from("<QQ", table, 32), (layout["esp_start_lba"], layout["esp_start_lba"] + layout["esp_sectors"] - 1))
        self.assertEqual(struct.unpack_from("<QQ", table, 128 + 32), (layout["root_start_lba"], layout["root_start_lba"] + layout["root_sectors"] - 1))
        self.assertLessEqual(layout["root_start_lba"] + layout["root_sectors"], layout["disk_bytes"] // 512 - 33)

    def test_layout_check_ignores_root_data_but_detects_partition_and_efi_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "test.img"
            layout = disk.make_gpt(256 * 1024 ** 2)
            disk._new_file(image, layout["disk_bytes"])
            root_uuid = uuid.uuid4()
            with image.open("r+b") as handle:
                handle.write(layout["primary"])
                handle.seek(layout["disk_bytes"] - len(layout["backup"]))
                handle.write(layout["backup"])
                handle.seek(layout["root_start_lba"] * 512 + 1024 + 104)
                handle.write(root_uuid.bytes)
            info = {key: value for key, value in layout.items() if key not in ("primary", "backup")}
            info["disk"] = str(image)
            info.update(disk.inspect_disk_layout(info))
            with image.open("r+b") as handle:
                handle.seek(layout["root_start_lba"] * 512 + 8192)
                handle.write(b"checkpoint contents may change")
            self.assertTrue(disk.check_disk_layout(info)["passed"])
            with image.open("r+b") as handle:
                handle.seek(layout["esp_start_lba"] * 512 + 1024)
                handle.write(b"unexpected EFI change")
            result = disk.check_disk_layout(info)
            self.assertFalse(result["passed"])
            self.assertTrue(result["partition_table_unchanged"])
            self.assertFalse(result["esp_unchanged"])

    def test_existing_output_or_symlink_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "tree"
            staging.mkdir()
            original = root / "keep"
            original.write_text("original")
            (root / "root.img").symlink_to(original)
            with patch.object(disk, "_run") as run:
                with self.assertRaises(FileExistsError):
                    disk.build_disk(root, staging, [])
            run.assert_not_called()
            self.assertEqual(original.read_text(), "original")
            self.assertFalse((root / "destination.img").exists())

    def test_public_or_symlink_work_directory_and_guest_command_injection_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "tree"
            staging.mkdir()
            link = root / "link"
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                disk.build_disk(link, staging, [])
            with patch.object(disk, "_run") as run:
                for guest in ("/root;bad", "/root/../outside", "relative"):
                    with self.assertRaises(ValueError):
                        disk.build_disk(root, staging, [guest])
                root.chmod(0o755)
                with self.assertRaises(ValueError):
                    disk.build_disk(root, staging, [])
            run.assert_not_called()
            self.assertFalse((root / "destination.img").exists())


if __name__ == "__main__":
    unittest.main()
