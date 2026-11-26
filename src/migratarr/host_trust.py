"""Attended SSH host-key enrollment; no passwords or source changes."""
import os
from pathlib import Path
import stat
import subprocess


def ensure_host_trust(source):
    """Let OpenSSH ask for independent fingerprint verification on first use."""
    path = Path(source["known_hosts"])
    lookup = source["host"] if source["port"] == 22 else "[{}]:{}".format(source["host"], source["port"])

    def present():
        if not path.is_file():
            return False
        result = subprocess.run(["ssh-keygen", "-F", lookup, "-f", str(path)],
                                capture_output=True, text=True, timeout=10)
        return result.returncode == 0

    if present():
        return
    if path.is_symlink() or path.parent.resolve() != path.parent:
        raise ValueError("new SSH trust must use a file without symlink parents")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError("SSH trust file must be owned and not writable by others")
    else:
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    print("First source connection: compare the SSH fingerprint with the source console before answering yes.", flush=True)
    quoted = '"' + str(path).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'
    argv = ["ssh", "-F", "/dev/null", "-T", "-p", str(source["port"]), "-l", source["user"],
            "-o", "StrictHostKeyChecking=ask", "-o", "HashKnownHosts=yes",
            "-o", "PreferredAuthentications=none", "-o", "PubkeyAuthentication=no",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "UserKnownHostsFile=" + quoted, "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "NumberOfPasswordPrompts=0", "-o", "ConnectTimeout=10",
            "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no", "-o", "ControlPath=none",
            "--", source["host"], "true"]
    subprocess.run(argv, check=False, timeout=180)
    if not present():
        raise ValueError("source host key was not accepted; assessment has not started")
    print("Source host key verified. Continuing to source authentication.", flush=True)
