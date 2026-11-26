#!/usr/bin/env python3
"""Test git-clone installation in a new disposable Ubuntu destination VM.

A code-only Git bundle supplies the repository to the network-isolated guest.
No host credentials, reports or real-server connections enter the VM.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile

from test_recovery_vm import RecoveryLab, ROOT, OVMF_VARS, command, private_write
from test_ubuntu_recovery_vm import digest, inject_seed


def code_files():
    files = [ROOT / name for name in ("README.md", ".gitignore", "pyproject.toml", "migratarr", "install.sh")]
    for pattern in ("src/migratarr/*.py", "scripts/*.py", "tests/*.py", "docs/*.md"):
        files.extend(ROOT.glob(pattern))
    for path in sorted(files):
        if path.is_symlink() or not path.is_file():
            raise ValueError("snapshot must contain regular code/documentation files only")
        yield path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args(argv)
    image = Path(args.image).absolute()
    if os.geteuid() == 0 or image.is_symlink() or not image.is_file() or digest(image) != args.sha256:
        parser.error("run unprivileged with a regular cloud image matching its verified SHA256")
    parent = ROOT / "reports"
    parent.mkdir(mode=0o700, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="install-vm-", dir=parent))
    lab = RecoveryLab(work, 300)
    lab.memory_mb = 1536
    lab.ssh_timeout = 180
    report = {"status": "failed", "scope": "fresh_ubuntu_installation_only",
              "production_qualified": False, "events": lab.events}
    print("Installer VM: " + str(work), flush=True)
    try:
        snapshot = work / "snapshot"
        snapshot.mkdir(mode=0o700)
        for path in code_files():
            target = snapshot / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        command(["git", "init", "--initial-branch=main", snapshot])
        command(["git", "-C", snapshot, "add", "."])
        command(["git", "-C", snapshot, "-c", "user.name=VM Test", "-c", "user.email=vm-test@invalid",
                 "commit", "-m", "Code-only installer test snapshot"])
        bundle = work / "code.bundle"
        command(["git", "-C", snapshot, "bundle", "create", bundle, "main", "HEAD"])
        command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", work / "client-key"])
        shutil.copyfile(OVMF_VARS, work / "OVMF_VARS.fd")
        command(["python3", ROOT / "scripts/build.py"])
        disk = work / "destination.img"
        command(["qemu-img", "convert", "-f", "qcow2", "-O", "raw", image, disk])
        public = inject_seed(disk, work, secrets.token_hex(16), ROOT / "dist/migratarr.pyz")
        private_write(work / "known_hosts", "[127.0.0.1]:{} {}\n".format(lab.port, public))
        lab.start(disk)
        lab.wait_guest("bootstrap")
        command(["scp", "-F", "/dev/null", "-i", work / "client-key", "-P", str(lab.port),
                 "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                 "-o", "StrictHostKeyChecking=yes", "-o", "GlobalKnownHostsFile=/dev/null",
                 "-o", "UserKnownHostsFile=" + str(work / "known_hosts"),
                 bundle, "root@127.0.0.1:/opt/code.bundle"])
        result = lab.ssh('''
git clone /opt/code.bundle '/opt/Migratarr checkout'
cd '/opt/Migratarr checkout'
./install.sh --no-start --no-system-packages
mkdir -p /root/.config/migratarr
printf 'preserve this profile\n' > /root/.config/migratarr/installation-test.txt
./install.sh --no-start --no-system-packages
mv '/opt/Migratarr checkout' /opt/checkout-removed
cd /tmp
env -u PYTHONPATH /root/.local/bin/migratarr --help
python3 - <<'PY'
import hashlib, json
from pathlib import Path
target = Path('/root/.local/bin/migratarr')
receipt = json.loads(Path('/root/.local/share/migratarr/install.json').read_text())
assert hashlib.sha256(target.read_bytes()).hexdigest() == receipt['sha256']
assert Path('/root/.config/migratarr/installation-test.txt').read_text() == 'preserve this profile\\n'
PY
/root/.local/bin/migratarr inventory --help
/root/.local/bin/migratarr inventory --role destination > /tmp/installed-inventory.json
python3 - <<'PY'
import json
from pathlib import Path
inventory = json.loads(Path('/tmp/installed-inventory.json').read_text())
assert inventory['role'] == 'destination'
assert inventory['system']['architecture'] == 'x86_64'
PY
''')
        private_write(work / "installation-output.txt", result.stdout + result.stderr)
        lab.record("git_clone_install_update_and_run_without_checkout_passed")
        lab.record("existing_profile_preserved_and_installed_checksum_verified")
        report["status"] = "passed"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
        print("Installer VM failed: " + str(exc), flush=True)
    finally:
        lab.stop()
        private_write(work / "result.json", json.dumps(report, indent=2) + "\n")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
