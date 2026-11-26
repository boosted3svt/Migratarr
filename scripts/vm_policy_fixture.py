"""Create and inspect a root-shaped fixture only inside disposable QEMU guests."""
import json
import os
from pathlib import Path
import subprocess
import sys

SOURCE = Path("/var/lib/migratarr/transfer-fixture/policy-case")
PROTECTED = {
    "var/lib/migratarr/jobs/keep": "protected job",
    "boot/migratarr/rescue.img": "protected recovery",
    "boot/grub/grub.cfg": "protected active boot",
    "boot/efi/EFI/ubuntu/loader.efi": "protected EFI",
    "etc/fstab": "bootstrap fstab pending reconciliation",
    "etc/netplan/00.yaml": "temporary management network",
    "etc/machine-id": "bootstrap identity",
    "etc/ssh/config": "bootstrap SSH identity",
}
EXTRAS = ("etc/obsolete.conf", "old-stack/old.log", "old-stack/sub/old.cache",
          "home/user/.old-bootstrap", "tmp/bootstrap-only")


def source_guard(token):
    if os.geteuid() != 0 or Path("/sys/class/dmi/id/sys_vendor").read_text().strip().lower() not in ("qemu", "bochs"):
        raise ValueError("QEMU fixture required")
    marker = json.loads(Path("/var/lib/migratarr/transfer-fixture-marker.json").read_text())
    if marker != {"purpose": "migratarr_transfer_fixture", "token": token}:
        raise ValueError("fixture marker differs")


def target(token, disk_boot=False):
    sys.path.insert(0, "/opt/migratarr.pyz")
    from migratarr.recovery import _directory
    if disk_boot:
        from migratarr.recovery_vm import guard_vm
        guard_vm(token)
        jobs = list(Path("/var/lib/migratarr/jobs").iterdir())
        if len(jobs) != 1:
            raise ValueError("expected one test job")
        run = Path("/var/lib/migratarr/transfer-tests") / jobs[0].name
    else:
        from migratarr.transfer import fixture_context, TARGET_BASE
        _, job, _ = fixture_context(token)
        run = Path(TARGET_BASE) / job["job_id"]
    return _directory(run / "data/policy-case"), _directory(run)


def write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


if __name__ == "__main__":
    action, token = sys.argv[1:3]
    if action.startswith("source-"):
        source_guard(token)
        if action == "source-setup":
            SOURCE.mkdir(mode=0o700)
            for name in ("boot", "mnt/share", "etc/ssh", "home/user", "var/lib/app", "tmp"):
                (SOURCE / name).mkdir(parents=True, exist_ok=True)
            write(SOURCE, "etc/config.ini", "source managed config")
            write(SOURCE, "etc/fstab", "source fstab for reconciliation")
            write(SOURCE, "etc/ssh/source_config", "source SSH config")
            write(SOURCE, "home/user/keep.txt", "source user data")
            write(SOURCE, "var/lib/app/database", "source application state")
            write(SOURCE, "tmp/local-data", "local tmp data is not silently discarded")
        elif action == "source-mount":
            subprocess.run(["mount", "-t", "tmpfs", "-o", "size=1m", "tmpfs", str(SOURCE / "mnt/share")], check=True)
            write(SOURCE, "mnt/share/remote-ignored.txt", "do not copy this mounted view")
        elif action == "source-collision":
            write(SOURCE, "var/lib/migratarr/jobs/collision", "source-owned namespace")
        elif action == "source-clear-collision":
            (SOURCE / "var/lib/migratarr/jobs/collision").unlink()
            (SOURCE / "var/lib/migratarr/jobs").rmdir()
            (SOURCE / "var/lib/migratarr").rmdir()
        elif action == "source-change":
            write(SOURCE, "home/user/keep.txt", "changed since preview")
        elif action == "source-restore":
            write(SOURCE, "home/user/keep.txt", "source user data")
    else:
        case, run = target(token, action == "verify-disk")
        if action == "target-seed":
            for name, value in PROTECTED.items():
                write(case, name, value)
            for name in EXTRAS:
                write(case, name, "destination bootstrap-only data")
            write(run, "outside-protected/keep", "outside cleanup root")
            (case / "old-link").symlink_to(run / "outside-protected")
        elif action == "target-mount":
            (run / "mount-fixture").mkdir(mode=0o700, exist_ok=True)
            write(run, "mount-fixture/keep-on-disk", "bound mount data")
            subprocess.run(["mount", "--bind", str(run / "mount-fixture"), str(case / "mnt/share")], check=True)
        elif action == "target-change":
            write(case, "etc/obsolete.conf", "changed since preview")
        elif action == "target-swap-link":
            (case / "old-stack").rename(case / "old-stack-saved")
            (case / "old-stack").symlink_to(run / "outside-protected")
        elif action == "target-restore-link":
            (case / "old-stack").unlink()
            (case / "old-stack-saved").rename(case / "old-stack")
        elif action == "target-hardlink":
            os.link(run / "outside-protected/keep", case / "linked-extra")
        elif action == "target-unlink":
            (case / "linked-extra").unlink()
        elif action in ("verify", "verify-disk"):
            for name, value in PROTECTED.items():
                assert (case / name).read_text() == value, name
            for name in EXTRAS:
                assert not (case / name).exists(), name
            assert not (case / "old-stack").exists()
            assert not (case / "old-link").is_symlink()
            assert (run / "outside-protected/keep").read_text() == "outside cleanup root"
            assert (run / "mount-fixture/keep-on-disk").read_text() == "bound mount data"
            assert (case / "mnt/share").is_dir()
            assert not (case / "mnt/share/remote-ignored.txt").exists()
            assert (case / "home/user/keep.txt").read_text() == "source user data"
            assert (case / "var/lib/app/database").read_text() == "source application state"
            assert (case / "tmp/local-data").read_text() == "local tmp data is not silently discarded"
            print(json.dumps({"protected_files": len(PROTECTED), "managed_data_retained": True}))
    os.sync()
