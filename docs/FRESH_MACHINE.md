# Recreate the workspace after a reinstall

The public repository preserves code, tests, requirements, development decisions,
qualification summaries and next steps. It does **not** contain passwords,
private SSH keys, live connection profiles, inventories, raw VM images or local
test logs. None of those is required to continue developing the VM milestone:
fresh disposable guests and fixture keys can be generated from verified images.

## Recover the code and context

Follow [START_HERE.md](../START_HERE.md) to clone and select
`wip/os-interruption-recovery`. Read [SESSION_HANDOFF.md](SESSION_HANDOFF.md)
before treating work-in-progress code as qualified. No existing checkout,
installed Migratarr command, old Python process or old chat is required.

For ordinary assessment, the checkout launcher works directly:

```sh
./migratarr --help
./migratarr setup
./migratarr run
```

Python 3.8+ is required. No pip dependencies are needed. The optional installer
builds a separate executable; after pulling updates, rerun the installer to
refresh that installed copy. Repository `./migratarr` uses current source directly.
Do not automatically run a real-machine assessment while pursuing the VM task.

## Developer tools for VM rehearsals

Use an x86_64 Linux development host. The tested environment was Ubuntu 24.04,
with Python 3.12, and unprivileged QEMU TCG rather than KVM. On a fresh Ubuntu
host, the developer tool packages are:

```sh
sudo apt-get update
sudo apt-get install --no-install-recommends \
  git python3 openssh-client rsync qemu-system-x86 qemu-utils ovmf \
  mtools busybox-static dropbear-bin e2fsprogs kmod zstd \
  grub-common grub-efi-amd64-bin
```

This is a development-machine package installation, not a source-server action.
Use the normal administrator approval flow. Do not install or upgrade Python on
the real source. Do not run `grub-install`, repartition a host disk, mount host
block devices or reboot the host to reproduce the lab. Run the harnesses as the
normal unprivileged user.

Check the base prerequisites:

```sh
python3 scripts/test_recovery_vm.py --check
```

The harness currently expects OVMF files at
`/usr/share/OVMF/OVMF_CODE_4M.fd` and `/usr/share/OVMF/OVMF_VARS_4M.fd`. If a new
distro uses different locations, identify equivalent firmware and adapt the
developer harness explicitly; do not silently assume a missing file is valid.

Provide a readable **regular file** containing an x86_64 Linux kernel and its
matching installed module tree under `/lib/modules/KERNEL_VERSION`. The old
host used `6.8.0-151-generic`, but a fresh host does not have to boot or install
that exact version. Prefer its already installed compatible kernel/modules.
`modprobe --show-depends -S KERNEL_VERSION` must resolve the required virtio,
e1000 and ext4 modules, including dependencies. The builder supports built-in
drivers and copies loadable modules from that selected version.

If the kernel in `/boot` needs administrator read access, copy it to a private
development cache with the normal admin flow. Do not change `/boot` permissions.
For example, from the checkout, after selecting a version actually installed:

```sh
mkdir -p .migratarr/lab-assets
chmod 700 .migratarr .migratarr/lab-assets
lab_kernel_version="$(uname -r)"
test -d "/lib/modules/$lab_kernel_version"
sudo install -m 600 -o "$(id -u)" -g "$(id -g)" \
  "/boot/vmlinuz-$lab_kernel_version" \
  ".migratarr/lab-assets/vmlinuz-$lab_kernel_version"
```

In a container or managed environment without matching kernel files/modules,
report that missing prerequisite and supply a verified compatible kernel/module
set. Do not pretend the old `.migratarr` cache exists. A different recovery
kernel is a new lab input and must be recorded/retested, not called identical
to the historical qualification.

## Recover verified cloud images

Create private `.migratarr/vm-cache` and `reports` directories. Historical images:

| Role | File | SHA256 |
|---|---|---|
| Source | `focal-server-cloudimg-amd64.img` | `18f2977d77dfea1b74aee14533bd21c34f789139e949c57023b7364894b7e5e9` |
| Destination | `noble-server-cloudimg-amd64.img` | `04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94` |

Canonical publishes images and checksum manifests under:

- [Ubuntu 20.04 Focal](https://cloud-images.ubuntu.com/focal/)
- [Ubuntu 24.04 Noble](https://cloud-images.ubuntu.com/noble/)

The `current/` URLs move. Check the downloaded bytes against the publisher's
manifest over trusted HTTPS (and its signature when available). Do not substitute
a guessed hash or reuse a historical hash for different bytes. If the historical
image is no longer at `current/`, locate its published dated build; otherwise use
a newly verified image and record that as a new test case. New layouts or
filesystem features may require explicit qualification changes.

`qemu-img info IMAGE` should identify a qcow2 image. The current whole-OS case
requires a source root containing `/boot`, and a destination with ext4 root
partition 1, FAT EFI partition 15 and separate ext4 boot partition 16. The
application's guards must remain in place if an image has a different layout.

Rebuild the application and run the existing whole-OS lab when a new baseline
or new prepared source is needed:

```sh
python3 scripts/build.py
python3 scripts/test_os_replace_vm.py \
  --source-image /private/cache/focal-server-cloudimg-amd64.img \
  --source-sha256 VERIFIED_SOURCE_SHA256 \
  --destination-image /private/cache/noble-server-cloudimg-amd64.img \
  --destination-sha256 VERIFIED_DESTINATION_SHA256 \
  --kernel /private/cache/vmlinuz-KERNEL_VERSION \
  --kernel-version KERNEL_VERSION
```

This generates fixture SSH keys, seeds, profiles, OS/application state, VM disks,
RAM images and evidence itself. It does not need the real source's credentials.
On the checkpoint branch its execution path is work in progress: implement the
combined fault harness described in SESSION_HANDOFF before reporting the new
interruption milestone as passed. Reuse existing prepared lab assets if they
were privately backed up, but do not require them for correctness.

## Python compatibility and evidence

Run the fast suite first:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

The checkpoint passed 183 tests on Python 3.12 and a privately extracted Python
3.8.10 runtime. The old runtime was under `/tmp`, so a reinstall will remove it.
Recreate Python 3.8 in an isolated development environment or extract verified
Focal interpreter/stdlib packages into a private cache and set `PYTHONHOME` to
that cache's `usr` directory. Use `ldd` to identify missing dependencies; do not
replace the host/source interpreter or install old system libraries globally.
An existing container runtime is another isolated option; it is not required
for assessment or an excuse to install Docker on the real source.

Historical test outcomes in the repository are summaries, not bundled raw logs.
If private reports are lost, say so. Recreate new evidence instead of claiming
that deleted original logs were inspected. Never merge based solely on a
historical test count when the new code or runtime has changed.

## Optional private backup before reinstalling

Keep any desired backup on your own private/off-machine storage, outside Git:

- `~/.config/migratarr/`: saved connection profile and possibly default reports.
- Checkout `.migratarr/`: private profiles/keys plus optional downloaded assets.
- Relevant SSH identities and `known_hosts` referenced by those profiles.
- Checkout `reports/`: optional raw evidence and prepared disposable VM disks.

Reports and VM folders can be large and may contain credentials copied into
test jobs. Store them privately. If you do not keep them, code/tests/context
still recover from Git, but profiles, private keys and original test evidence
do not. Fresh fixture keys are generated by the lab; real-source access must be
re-established through the operator's normal SSH setup and independent host-key
verification.

The GitHub connection used by the previous Codex installation may also need
reconnecting. The repository is `boosted3svt/Migratarr`; cloning its public code
does not require recovering a token. Never embed tokens in Git URLs or publish
private settings to make a new session work.
