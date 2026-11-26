# Transfer failure recovery in disposable VMs

This extends [the bounded transfer lab](TRANSFER_LAB.md) with a disconnected
source link, source mutation during copying and actual ENOSPC on the destination
VM's disk. Physical migration and whole-system replacement remain disabled.

Run with the same verified Ubuntu cloud image and kernel/module inputs:

```sh
python3 scripts/test_transfer_failures_vm.py \
  --image /private/cache/noble-server-cloudimg-amd64.img \
  --sha256 VERIFIED_IMAGE_SHA256 \
  --kernel /private/cache/vmlinuz-KERNEL_VERSION \
  --kernel-version KERNEL_VERSION
```

The harness uses the existing two-VM isolated socket network and generated keys.
All fault injection occurs inside marked disposable guests. It first establishes
the ordinary interruption/retry and metadata-preservation baseline, then:

1. Adds a new source fixture file, starts a throttled copy and disconnects the
   destination's source-facing NIC after observing partial data. It requires a
   failed checkpoint, restores the link, retries and compares metadata.
2. Starts another copy and modifies a hard-linked source file while retaining
   its size and modification time. A before/after fixture fingerprint must
   reject completion; a stable retry must match the changed source.
3. Starts another copy and allocates the destination's remaining free blocks to
   a temporary sibling file, including blocks normally reserved for root. It
   requires a space-exhaustion checkpoint and released checkpoint reserve,
   removes the filler and verifies a successful retry.

It then repeats the forced destination-termination test, corrupt-image fallback,
metadata checks after disk Ubuntu boots, and unchanged GPT/EFI/root-UUID checks.
Private results and manifests are under `reports/transfer-failures-*`.

## Application behavior

Fixture transfers use per-file temporary writes rather than whole-tree delayed
updates. Before rsync starts, a bounded, metadata-only source walk finds the
largest regular file in the fixture, excluding mounted contents and symlink
targets. Available target space must cover that file's logical size plus a
2 MiB recovery headroom allowance, in addition to the allocated checkpoint
reserve. Existing target file blocks are not credited as immediately reusable.
Insufficient evidence or headroom blocks the transfer without starting rsync.
The checkpoint records the budget and `insufficient_temporary_space` when blocked.

This is an initial fixture check, not continuous enforcement or a qualified
whole-server capacity schedule. Concurrent allocation can still cause ENOSPC;
the failure/retry path below remains necessary. Sparse files are budgeted
conservatively by logical size. Reviewed space reclamation and capacity checks
throughout a whole-OS transfer remain future work.

Before and after a successful rsync, the fixture guard computes a bounded source
fingerprint from file contents, paths, inode/device identifiers, numeric owners,
mode, size, link targets, link counts, ctime and mtime. It excludes nested mounted
contents and does not follow symlinks. The development limit is 4,096 entries
and 256 MiB of logical fixture contents. Reads alone do not change the result.
Unexpected source changes or failed post-copy verification leave the transfer
failed; rsync exit code zero alone cannot mark it complete.

Live diagnostics are written privately in RAM so a full target cannot prevent
error collection. A 2 MiB checkpoint reserve is allocated before copying. On a
space-exhaustion result it is released before saving the failed checkpoint;
atomic checkpoint writes also release it and retry on ENOSPC/EDQUOT. Logs are
copied to the job when possible, and the report records whether that succeeded.
The reserve is recreated on a subsequent retry. Every result retains
`migration_ready: false`; no failure authorizes cutover or deletes recovery data.

Rsync writes temporary files to a private sibling `scratch` directory on the
target disk, outside the copied data tree. After abrupt termination, a retry
discards regular orphan scratch files before copying again. Cleanup validates
the whole scratch directory before deleting anything and refuses symlinks,
directories and files with multiple hard links. It does not search the data
tree for filenames that merely look temporary. The report records how many
orphan files were discarded. Source files beginning with a dot remain ordinary
data; whole-system reserved-path collisions still need a separate policy.

## Limits

These are bounded fixture failure cases. Comparing two fingerprints is not an
atomic source snapshot or a substitute for coordinating all writers during a
real migration. Concurrent changes that undo themselves and mutations during
the fingerprint walk require further consistency design. The reserve does not
solve a target that is already full before job preparation, hardware I/O errors,
filesystem corruption or exhaustion of every recovery path. Inode exhaustion,
quota enforcement, persistent connection loss and a comprehensive capacity
policy remain unqualified. No source services are stopped by this lab.

## Observed qualification

The per-file temporary-write version passed the complete failure/recovery lab on
2026-11-26. Evidence is `reports/transfer-failures-r88edxnj/result.json`.
Network loss, same-size source mutation, ENOSPC, forced power loss, retry,
metadata persistence after disk boot and corrupt-rescue fallback all passed.
GPT, EFI bytes and root UUID remained unchanged. Budget records are included
in the transfer checkpoints. The insufficient-headroom preflight is also
covered by an automated check that requires no rsync process to start.
All 171 automated tests passed under Python 3.8.10 and Python 3.12.

Earlier baseline:

The complete run passed on 2026-11-26. Local evidence is
`reports/transfer-failures-68c5ryw1/result.json`. The three private
`*-checkpoint.json` files record link loss (rsync 255), source change despite
rsync zero, and space exhaustion (rsync 11 with reserve released). Each was
followed by a verified successful retry. The filler allocated 710,946,816 bytes
before normal file allocation reached ENOSPC; ext4 still reported internal
reserved free blocks, so this is not a claim of zero filesystem free blocks.

An offline read of the stopped guest disk saved
`final-transfer-checkpoint.json`: attempt 10 completed after an in-progress
attempt and discarded one orphan scratch file. All 17 final entries matched
after disk Ubuntu boot, and the protected sibling, GPT/EFI/root UUID checks
passed. The 146 automated tests passed under Python 3.12 and Python 3.8.10.
