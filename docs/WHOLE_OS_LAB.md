# Whole-OS replacement in isolated VMs

This experimental rehearsal replaces an Ubuntu 24.04 destination installation
with the complete Ubuntu 20.04 source installation. It does not upgrade the
source OS. Physical migration remains disabled and every report retains
`migration_ready: false` / `production_qualified: false`.

The test uses two newly generated, nonce-marked QEMU guests with a private socket
network and no LAN access. Each has one disk. The source first boots its real
Ubuntu 20.04 userspace/kernel, with a custom account, a systemd service, SQLite
application state and the existing ownership/ACL/xattr/capability/link/sparse
fixture. It is then powered off and exposed from RAM as a read-only filesystem.
No application can write to it during copying. Its disk hash must remain
unchanged throughout transfer.

The Ubuntu 24.04 destination boots its existing single disk into the established
RAM recovery environment. `os-preview-vm` scans source/destination metadata and
saves a compressed exact preview under the protected job directory. It excludes
runtime views and protected boot/recovery, management and identity namespaces.
The preview identifies destination-only entries and type conflicts without
staging a second installation. Reserved source namespace collisions, unexpected
mounts, external hard links and changed previews block execution.

`os-replace-vm` requires the exact preview digest and revalidates both trees
before the first deletion. Cleanup walks directory descriptors without following
symlinks and removes directories only after their selected children. Copying
uses numeric IDs, ACLs, xattrs, hard links, checksums, sparse files and per-file
temporary writes. Root and separate boot scopes have scratch space on their
own filesystems. A largest-file headroom check precedes each scope, with free
space monitored during copying; the transfer stops before spending the protected
64 MiB headroom allowance. This is not comprehensive quota/inode qualification.

The application rewrites fstab using the retained destination root/boot/EFI
UUIDs, disables swap resume and cloud-init reconfiguration, rebuilds the source
kernel's initramfs in the imported userspace, and writes a UUID-based GRUB menu.
The destination UEFI binaries remain intact. A failed imported-OS boot selects
RAM recovery on the next attempt. Recovery does not restore the old Ubuntu 24.04
installation: its ordinary files have been replaced. The source remains the
rollback copy.

Management networking, SSH access and the destination machine-id are retained
for this isolated test. Production network/identity handover is not qualified.
The imported OS must boot its source kernel and userspace, match source account
and package-database hashes, preserve the full metadata fixture and start its
custom service reading the original SQLite state. A destination-only marker
must be absent. GPT, EFI contents and root UUID must match their pre-transfer
values, and boot/EFI UUIDs are recorded in the reconciliation result.

Run with verified cloud images and an installed recovery kernel/module set:

```sh
python3 scripts/test_os_replace_vm.py \
  --source-image /private/cache/focal-server-cloudimg-amd64.img \
  --source-sha256 VERIFIED_SOURCE_SHA256 \
  --destination-image /private/cache/noble-server-cloudimg-amd64.img \
  --destination-sha256 VERIFIED_DESTINATION_SHA256 \
  --kernel /private/cache/vmlinuz-KERNEL_VERSION \
  --kernel-version KERNEL_VERSION
```

Canonical supplies [Focal cloud-image checksums](https://cloud-images.ubuntu.com/focal/current/SHA256SUMS).
The initial source image used here is SHA256
`18f2977d77dfea1b74aee14533bd21c34f789139e949c57023b7364894b7e5e9`.
Images, credentials, metadata manifests and reports stay private under ignored
directories; none are part of the published application.

## Observed cross-release result

The rehearsal passed on 2026-11-27. Local evidence is
`reports/os-replace-vm-kmwdf46c/resume-result.json`, with the original attempt,
exact preview summary, reconciliation result and both metadata views alongside.
It removed 57,836 previewed destination entries and copied the complete source
installation. The destination then booted Ubuntu 20.04, kernel
`5.4.0-216-generic`, and Python 3.8. Source passwd/group/package-database hashes,
ACLs, xattrs, capabilities, hard links, sparse data and SQLite-backed service
state passed independent checks. Destination GPT, EFI bytes and root UUID were
unchanged; the live root/boot/EFI UUIDs matched the reconciliation plan. The
source metadata matched its original frozen census and the source disk hash
remained unchanged during resumed verification. The boot filesystem UUID was
also compared with the original verified destination image. All 178 automated
tests passed under Python 3.8.10 and Python 3.12.

The initial run exposed a missing RAM-image `chroot` command and a missing final
kernel directory entry after an abrupt stop. RAM images now include `chroot`
(with a BusyBox fallback for older images), and each copy scope syncs directory
changes before completion. The existing root payload was verified without
recopying it; only the boot scope needed repair before initramfs rebuilding.
Metadata expectations now come from the frozen source view, so temporary
source mountpoint metadata is not confused with underlying disk metadata.

After a completed copy, boot reconciliation can be retried with the same exact
preview digest using `recovery os-reconcile-vm`. It verifies job/target identity,
the immutable source and the copied root payload before touching boot setup.
Root payload differences block the retry; boot payload differences can be
repaired from the frozen source without deleting protected boot/recovery data.
This is not arbitrary interrupted whole-root cleanup resume.

The saved developer lab can be continued without creating new images:

```sh
python3 scripts/resume_os_replace_vm.py \
  --work /absolute/path/to/Migratarr/reports/os-replace-vm-EXAMPLE \
  --destination-image /private/cache/noble-server-cloudimg-amd64.img
```

Use `--verify-boot-only` when a ready-for-boot reconciliation result is already
saved. This checks the imported OS and data without repeating copy/reconciliation.

The first implemented case is x86_64, UEFI without Secure Boot, an ext4 source
root containing `/boot`, and an ext4 destination root with a separate ext4 boot
and FAT EFI partition. Other mounts, BIOS conversion, ext3, encryption, RAID/LVM,
physical hardware, comprehensive application qualification and interrupted
whole-OS retry remain separate gates. A failed replacement stays in recovery
with diagnostics and a failed checkpoint; its old preview cannot authorize
arbitrary cleanup after partial changes. Do not infer general migration support
from this single VM pair.
