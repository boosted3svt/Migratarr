# Whole-system path policy and exact-preview sandbox cleanup

Every generated whole-server draft plan now contains `path_policy`. It describes
candidate local filesystem scopes, mount boundaries, protected paths, files that
need reconciliation, and cleanup requirements. It is review evidence, not an
executable whole-root migration plan. `migration_ready` remains false.

Ordinary local state remains in scope, including manually installed software,
container backing directories, home dotfiles, `/tmp` and `/var/tmp` when they are
local disk contents. There is no application selection list. Remote, runtime,
image/layered mounted views and explicit source exclusions retain their
mountpoint directories while excluding mounted contents. A separately mapped
local filesystem is its own scope; binds/subvolumes or ambiguous mappings are
not silently copied as part of root. Unknown mappings stay unresolved.

Recovery jobs/credentials and RAM assets use protected namespaces under
`/var/lib/migratarr` and `/boot/migratarr`. The active GRUB and EFI trees are
retained until boot reconciliation. Source presence in reserved recovery
namespaces is a collision requiring resolution, rather than permission to
overwrite that source data. `/etc/fstab`, encryption/resume, networking,
machine identity and SSH configuration require separate reconciliation. Parent
directories containing protected paths cannot be selected for recursive cleanup.

The future whole-root engine must use per-file temporary headroom on the same
destination filesystem; the policy explicitly disallows whole-tree delayed
updates or a second staged installation. The bounded transfer fixture now uses
per-file temporary writes and an initial conservative headroom check. A full-server capacity
and cleanup schedule is not qualified by this policy alone.

## Disposable VM test

Run with the verified cloud image and kernel/module inputs described in the
[transfer lab](TRANSFER_LAB.md):

```sh
python3 scripts/test_root_policy_vm.py \
  --image /private/cache/noble-server-cloudimg-amd64.img \
  --sha256 VERIFIED_IMAGE_SHA256 \
  --kernel /private/cache/vmlinuz-KERNEL_VERSION \
  --kernel-version KERNEL_VERSION
```

The harness first copies a root-shaped fixture into the existing bounded
transfer directory on the destination VM disk. It adds dummy recovery/GRUB/EFI,
network and identity files plus obsolete bootstrap files. A source nested tmpfs
and destination same-device bind mount exercise mount exclusions. These files
are inside `policy-case`, not the actual guest OS root.

The same standalone application exposes:

```sh
python3 /opt/migratarr.pyz recovery cleanup-preview-vm --vm-token VM_TEST_NONCE
python3 /opt/migratarr.pyz recovery cleanup-apply-vm \
  --vm-token VM_TEST_NONCE --preview-sha256 EXACT_PREVIEW_SHA256
```

Both commands require the existing disposable-QEMU, RAM-root and target-device
guards. Source evidence is collected over pinned SSH without installing a
collector. The cleanup target is fixed at
`/target/var/lib/migratarr/transfer-tests/JOB_UUID/data/policy-case`; no CLI option
can redirect it to `/target` or a physical filesystem root.

Preview records exact destination-only candidates and source/destination
evidence, including regular-file content hashes. The sandbox census is bounded
to 4,096 entries and 256 MiB. It does not delete anything. Reserved source namespace collisions,
type conflicts and candidate files with multiple hard links block application.
The latter avoids changing an unrelated retained file's link metadata.

Application requires the exact saved preview digest and reconstructs a fresh
preview before the first deletion. Source changes, destination changes and mount
changes invalidate it. Each deletion uses directory descriptors with no symlink
following, rechecks the selected leaf's identity, and syncs its parent. Directories
are removed only with `rmdir` after selected children, never recursive `rmtree`.
Protected, reconciled and mounted paths remain outside the candidate set.

Regular-file contents and symlink targets are checked again at the selected
leaf before unlinking. This also rejects same-size edits with preserved mtime.

A private atomic `cleanup.json` records completed removals. If interrupted,
generate a new preview; an old digest is not permission to resume arbitrary
deletion. The manifest digest identifies an exact review result, not a security
boundary against a privileged operator who can rewrite all job evidence.

The test rejects a wrong digest, reserved source collision, changed source,
changed destination, parent symlink redirect and linked cleanup candidate. It
then checks exact preview/application agreement, protected sentinels and retained
application/home/tmp data, and repeats protection checks after RAM restart and
disk Ubuntu boot. GPT/EFI/root UUID checks remain enabled.

Private previews and results are under `reports/root-policy-vm-*`. This does not
qualify full-root deletion, concurrent-writer cleanup, source rollback/cutover,
boot reconciliation or the actual Ubuntu 20.04/ext3 migration case. Physical
migration remains disabled.

The complete test passed on 2026-11-26. Local evidence is
`reports/root-policy-vm-re766raw/result.json`, with the exact reviewed preview
and eight-entry `cleanup-result.json` alongside it. It retained 25 protected,
reconciled or containing paths and passed all refusal, restart, disk-boot and
GPT/EFI/root UUID assertions. The 160 automated tests passed under Python 3.12
and Python 3.8.10. The offline real-server review generated from earlier snapshots
is `reports/root-policy-review-xx8ynpga/plan.txt`; its migration status stays blocked.
