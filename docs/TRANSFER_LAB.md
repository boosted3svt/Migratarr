# Guarded VM fixture transfer

This milestone copies a bounded fixture from a disposable source VM onto the
destination VM's existing disk while Migratarr runs in RAM. It does not replace
an operating system, stop source services, format/repartition, or enable physical
migration. Every transfer report retains `migration_ready: false`.

## Run the two-VM test

Use the developer tools, verified Ubuntu 24.04 AMD64 cloud image and readable
kernel/module pair described in [the source connectivity lab](SOURCE_CONNECTIVITY_LAB.md):

```sh
python3 scripts/test_transfer_vm.py \
  --image /private/cache/noble-server-cloudimg-amd64.img \
  --sha256 VERIFIED_IMAGE_SHA256 \
  --kernel /private/cache/vmlinuz-KERNEL_VERSION \
  --kernel-version KERNEL_VERSION
```

The unprivileged harness creates two new regular disk images, generated keys and
an isolated socket network. Each guest has one data disk. There is no host
bridge, real source connection or physical boot change. The disposable source
fixture is created deliberately by this test, not by the source inventory tool.
The source guest needs rsync; the RAM bundle carries its own rsync and libraries.

Within the marked destination guest, the standalone command is:

```sh
python3 /opt/migratarr.pyz recovery transfer-fixture --vm-token VM_TEST_NONCE
```

It requires a RAM root, the resumed job and the planned ext4 UUID. It checks
that `/target` actually belongs to that block device, rather than accepting an
unmounted directory in RAM. Source credentials must be the explicit key and
pinned trust file restored into RAM. Both source and destination must pass
disposable-QEMU marker checks; source fixture approval uses the destination's
test nonce. No password or agent session is required.

The source is fixed at `/var/lib/migratarr/transfer-fixture`. The destination is
fixed at `/target/var/lib/migratarr/transfer-tests/JOB_UUID/data`. Neither path is
an operator-selectable root-copy target. Private canonical directories prevent
redirecting the target through symlinks, and nested destination mounts are
rejected. Existing recovery state lives outside this tree.

## Preservation and interruption checks

The copy uses rsync over verified SSH with numeric IDs, hard links, ACLs, xattrs,
symlinks, checksums, sparse files and synced file writes. It does not use deletion,
symlink dereferencing or in-place replacement. Nested source mount contents are
explicitly excluded, including bind mounts on the same device; their mountpoint
directories remain in the copy. The source mount list is checked again when the
remote rsync process starts. That is a fixture safety check, not a general
solution for live mount/writer races.

The fixture includes ownership IDs without requiring account creation, access
and default ACLs, a binary user xattr, a Linux file capability, a hard-link pair,
an external symlink, a sparse file, a nested tmpfs and a duplicate bind mount.
Independent snapshots compare contents, numeric owners/groups, modes, nanosecond
modification times, link targets, hard-link groups, xattr/ACL/capability bytes and
sparse allocation. Destination snapshots include any accidentally copied nested
mount contents, so exclusions cannot pass merely by hiding those files from
verification.

The harness observes a partial copy, sends SIGTERM and checks the durable
interrupted checkpoint, then retries to completion. It adds another source file
and kills the entire destination VM during the next copy. After RAM recovery
loads the saved job, another invocation detects the earlier in-progress attempt
and retries safely. Final snapshots are checked in RAM and again after booting
the destination's original disk Ubuntu. A protected recovery sibling must survive,
and GPT metadata, EFI contents and root UUID must remain unchanged.

Transfer checkpoints live beside the recovery job as private atomic
`transfer.json` files. They record attempts and `copying`, `interrupted`, `failed`
or `complete` phases. Raw rsync errors remain in private `transfer-stderr.log`.
An unsuccessful copy returns exit code `2`; a guard/input failure returns `1`.
The development-only `--bwlimit` option makes interruption tests reproducible.

## Remaining gates

This is a fixture copy, not whole-system migration qualification. Live writers,
whole-root exclusions and protected paths, destination cleanup/deletion, all special file types,
boot/fstab/initramfs reconciliation and imported OS boot still need work.
The [failure-recovery lab](TRANSFER_FAILURES_LAB.md) extends the fixture with
in-flight connectivity loss, source-change detection and destination ENOSPC.
Delayed updates and partial files consume additional destination space; this
fixture's budget is not a full-server capacity policy. The actual Ubuntu
20.04/ext3 source and BIOS/UEFI adaptation remain unqualified.

Private manifests, serial logs and `result.json` remain under
`reports/transfer-vm-*`. Normal unit tests do not launch VMs.

The full test passed on 2026-11-26. Local evidence is
`reports/transfer-vm-e52vylt3/result.json`. The initial source's 13 entries matched
after SIGTERM/retry; the updated source's 14 entries matched after forced VM
termination/retry and after booting disk Ubuntu. The protected sibling and
GPT/EFI/root UUID assertions also passed. The 137 automated tests passed under
Python 3.12 and Python 3.8.10. These results qualify this fixture case only.
