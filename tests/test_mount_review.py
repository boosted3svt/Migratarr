import copy
import unittest

from migratarr.mount_review import classify_filesystem, review_mounts


def mount(target, filesystem="ext3", device="8:3", root="/", kind=None):
    return {"target": target, "filesystem": filesystem, "device": device,
            "root": root, "kind": kind or classify_filesystem(filesystem)}


class MountReviewTests(unittest.TestCase):
    def test_classification_distinguishes_data_and_generated_views(self):
        for fs, expected in (("nfs", "remote"), ("ext3", "local"),
                             ("squashfs", "image"), ("overlay", "layered"),
                             ("overlayfs", "layered"), ("bpf", "pseudo"),
                             ("nsfs", "pseudo"), ("fuse.gvfsd-fuse", "pseudo"),
                             ("fuse.portal", "pseudo"), ("fuse.custom", "unknown")):
            self.assertEqual(classify_filesystem(fs), expected)

    def test_root_duplicate_is_recognized_regardless_of_order(self):
        for mounts in ([mount("/mnt/local-root"), mount("/")],
                       [mount("/"), mount("/mnt/local-root")]):
            original = copy.deepcopy(mounts)
            rows = {row["target"]: row for row in review_mounts(mounts, "source")}
            self.assertEqual(rows["/mnt/local-root"]["treatment"], "duplicate_view_review")
            self.assertEqual(rows["/mnt/local-root"]["duplicate_of"], "/")
            self.assertEqual(rows["/"]["treatment"], "local_mapping_review")
            self.assertEqual(mounts, original)

    def test_shared_device_different_roots_are_not_duplicates(self):
        rows = review_mounts([mount("/", "btrfs", root="/@"),
                              mount("/home", "btrfs", root="/@home")], "source")
        self.assertTrue(all(row["treatment"] == "local_mapping_review" for row in rows))
        self.assertTrue(all("duplicate_of" not in row for row in rows))

    def test_nonroot_duplicate_uses_shortest_then_lexical_target(self):
        rows = review_mounts([mount("/ccc"), mount("/bbb"), mount("/long")], "source")
        self.assertEqual(rows[0]["duplicate_of"], "/bbb")
        self.assertEqual(rows[1]["treatment"], "local_mapping_review")

    def test_saved_unknown_generated_types_can_be_reviewed(self):
        rows = review_mounts([mount("/snap/a/1", "squashfs", kind="unknown"),
                              mount("/merged", "overlay", kind="unknown"),
                              mount("/sys/fs/bpf", "bpf", kind="unknown")], "source")
        self.assertEqual([row["kind"] for row in rows], ["image", "layered", "pseudo"])
        self.assertEqual([row["treatment"] for row in rows],
                         ["image_backing_review", "layered_backing_review", "runtime_excluded"])

    def test_destination_generated_mounts_are_bootstrap_evidence(self):
        rows = review_mounts([mount("/", "ext4"), mount("/snap/a/1", "squashfs"),
                              mount("/merged", "overlay")], "destination")
        self.assertEqual([row["treatment"] for row in rows],
                         ["destination_target_review", "bootstrap_mount", "bootstrap_mount"])

    def test_unknown_filesystems_fail_closed_and_explicit_unknown_is_retained(self):
        rows = review_mounts([mount("/custom", "fuse.custom"),
                              mount("/", "ext4", kind="unknown")], "source")
        self.assertTrue(all(row["kind"] == "unknown" and row["treatment"] == "unknown_review" for row in rows))

    def test_overlay_root_never_becomes_local_root(self):
        row = review_mounts([mount("/", "overlay", kind="unknown")], "source")[0]
        self.assertEqual(row["kind"], "layered")
        self.assertEqual(row["treatment"], "layered_backing_review")

    def test_remote_and_pseudo_treatments_and_role_validation(self):
        rows = review_mounts([mount("/nas", "nfs"), mount("/proc", "proc")], "source")
        self.assertEqual([row["treatment"] for row in rows], ["remote_excluded", "runtime_excluded"])
        with self.assertRaises(ValueError):
            review_mounts([], "invalid")


if __name__ == "__main__":
    unittest.main()
