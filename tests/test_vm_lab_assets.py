import gzip
import importlib.util
from pathlib import Path
import stat
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "vm_lab_assets", Path(__file__).resolve().parents[1] / "scripts/vm_lab_assets.py")
ASSETS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ASSETS)


def records(path):
    content = gzip.decompress(path.read_bytes())
    offset, result = 0, {}
    while True:
        header = content[offset:offset + 110]
        if header[:6] != b"070701":
            raise ValueError("invalid cpio test archive")
        fields = [int(header[index:index + 8], 16) for index in range(6, 110, 8)]
        offset += 110
        name = content[offset:offset + fields[11] - 1].decode()
        offset = (offset + fields[11] + 3) & ~3
        data = content[offset:offset + fields[6]]
        offset = (offset + fields[6] + 3) & ~3
        if name == "TRAILER!!!":
            return result
        result[name] = {"mode": fields[1], "uid": fields[2], "gid": fields[3], "data": data}


class VmLabAssetTests(unittest.TestCase):
    def test_cpio_is_root_owned_and_preserves_symlink_objects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tree"
            root.mkdir()
            (root / "bin").mkdir()
            (root / "bin/program").write_bytes(b"program bytes")
            (root / "bin/program").chmod(0o755)
            outside = Path(temporary) / "outside"
            outside.write_bytes(b"outside secret is not copied")
            (root / "link").symlink_to(outside)
            output = Path(temporary) / "initramfs.img"
            ASSETS.pack_initramfs(root, output)
            rows = records(output)
            self.assertEqual(rows["bin/program"]["data"], b"program bytes")
            self.assertTrue(stat.S_ISREG(rows["bin/program"]["mode"]))
            self.assertTrue(stat.S_ISLNK(rows["link"]["mode"]))
            self.assertEqual(rows["link"]["data"], str(outside).encode())
            self.assertNotIn(b"outside secret is not copied", gzip.decompress(output.read_bytes()))
            self.assertTrue(all(row["uid"] == 0 and row["gid"] == 0 for row in rows.values()))

    def test_ram_init_retains_its_root_and_requires_durable_job(self):
        rescue = ASSETS._initramfs_init(["/lib/example.ko"], True)
        bootstrap = ASSETS._initramfs_init(["/lib/example.ko"], False)
        self.assertNotIn("switch_root", rescue)
        self.assertIn("mount -t ext4 /dev/vda2 /target", rescue)
        self.assertIn("test -s $job/job-id && test -s $job/phase || exec sh", rescue)
        self.assertIn('if test "$(cat $job/phase)" = prepared; then', rescue)
        self.assertIn("mv $job/resume-count.tmp $job/resume-count", rescue)
        self.assertIn("exec switch_root /target /sbin/init", bootstrap)
        self.assertIn("mount --move /dev /target/dev", bootstrap)

    def test_guest_preparation_has_only_synthetic_guest_paths(self):
        command = ASSETS.LAB_COMMAND
        self.assertIn("test \"$mode\" = bootstrap", command)
        self.assertIn("/boot/migratarr/armed", command)
        self.assertIn("/var/lib/migratarr/jobs/lab-job", command)
        self.assertIn("/dev/vda2", command)
        self.assertNotIn("/dev/sda", command)
        self.assertNotIn("192.168.", command)


if __name__ == "__main__":
    unittest.main()
