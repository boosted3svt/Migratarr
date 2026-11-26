# Destination installation

On an Ubuntu destination with Git available:

```sh
git clone https://github.com/boosted3svt/Migratarr.git
cd Migratarr
./install.sh
```

Run as your normal user. The installer requests sudo only if system packages
are missing. Python 3.8+ is required; an existing older interpreter is not
upgraded automatically. There are no pip dependencies. The source only needs
its existing compatible Python and SSH service for assessment; destination
installation does not install anything on the source.

The installer builds a code-only executable at `~/.local/bin/migratarr` and
records its checksum at `~/.local/share/migratarr/install.json`. Interactive
installation launches guided setup and read-only assessment. A blocked
assessment is expected: physical migration execution is not implemented.
Running assessment does not install recovery boot entries or start copying.

If `~/.local/bin` is not in your shell's PATH, run `~/.local/bin/migratarr`
directly. It works independently of the checkout. Profiles default to
`~/.config/migratarr/config.json`; passwords are never saved. On the first
interactive source connection, compare the displayed SSH fingerprint with
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` run on the source console,
then accept it only if they match. Existing mismatched keys are refused.
Noninteractive assessment requires preverified host trust and unattended auth.

To install without starting assessment:

```sh
./install.sh --no-start
```

To prohibit apt and sudo calls, require existing tools:

```sh
./install.sh --no-start --no-system-packages
```

Use `--prefix /owned/path` to install under another prefix. Installation
directories must be owned by the invoking user, not writable by others, and
have no symlink parents. Updates use the same installer after `git pull`.
They preserve profiles and reports. An unrelated, modified or symlinked command
is refused; the installer checks its receipt before replacing it.

Developer VM rehearsals have separate dependencies and use only disposable
guests; they are not required for normal assessment. The installer VM test is:

```sh
python3 scripts/test_install_vm.py --image /path/to/ubuntu-cloud.img --sha256 VERIFIED_SHA256
```

It clones a code-only Git bundle into a new Ubuntu guest with external networking
disabled, installs and updates the command, checks profile preservation and
runs destination inventory through the installed executable after moving the
checkout away. This verifies
the clone/install workflow without placing real credentials in the guest.

The Ubuntu 24.04.5 VM run passed on 2026-11-26; local evidence is
`reports/install-vm-gwmw20j1/result.json`. All 171 automated tests also passed
under Python 3.8.10 and Python 3.12. This qualifies the installation and
assessment entry point, not migration execution.
