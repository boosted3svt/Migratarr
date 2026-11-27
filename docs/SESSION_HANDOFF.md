# Resume here — 2026-11-27 development checkpoint

Entry point: [START_HERE.md](../START_HERE.md). Reinstall/recreation instructions:
[FRESH_MACHINE.md](FRESH_MACHINE.md). Public Git contains the development context;
private settings, keys, VM images and raw evidence require a separate private
backup or regeneration. Their absence does not block continued VM development.

Working branch: `wip/os-interruption-recovery`.
Last stable main commit when this checkpoint began:
`a651139492a9902d21e81a65a3037f0ae73b3012`.

The user asked to save the repository and continue tomorrow. No VM rehearsal
for the changes on this branch has run yet. Do not describe this milestone as
completed or merge it into main until the focused interruption rehearsal passes.
No migration VM processes were running when this checkpoint was saved.

## Goal and operating preferences

- Deliver a standalone application: clone on a fresh Ubuntu destination, run
  `./install.sh`, supply connection details/decisions to the program, and
  eventually perform the complete migration with no LLM runtime dependency.
- The user explicitly chose whole-OS copying after discussing alternatives.
  The destination boots the source OS version; destination partitions,
  filesystems and UUIDs are retained. Do not redesign this as package-only
  migration while retaining the destination Ubuntu release.
- Survey/migrate the entire server. Application names are not an inclusion list.
- Work in meaningful milestones with focused tests and one combined VM rehearsal
  where appropriate. Avoid repeatedly rerunning unrelated VM matrices.
- Real-machine access is read-only. Real migration, shutdown, reboot, cutover
  and physical boot installation remain unauthorized/unqualified. VM writes are
  authorized only in disposable test guests.
- The temporary `/mnt/local-root` source mount is excluded when present.
- Source Python 3.8 must remain supported; do not upgrade the source interpreter.
- Plex and Tautulli were historical examples; the user says they are no longer
  used on the source. Discover all containers/services without that assumption.
- Destination application/package/cron inventory is deliberately secondary:
  its bootstrap installation is replaced. Storage, firmware, boot, hardware,
  capacity, protected recovery paths and access/network continuity matter.

## Historical real-machine context (not current qualification)

Earlier source evidence: Ubuntu 20.04.6, x86_64, ext3, approximately 244 GiB
allocated, many containers and BIOS GRUB packages. Firmware evidence was
`bios_or_unavailable`; do not treat that as definitive proof of BIOS boot.
Earlier destination evidence: Ubuntu 24.04.5, ext4 with UEFI, one approximately
512 GB NVMe, 15.3 GiB RAM and approximately 427 GiB available on root then.
These snapshots are stale, especially after reinstalling. Resurvey before
planning any real operation. The ext3/possible-BIOS source to UEFI destination
case has not been qualified by the ext4/UEFI cloud-image rehearsal.

Connection details were deliberately not published. The operator can restore
private profiles/keys or enter details again through setup; independent source
host-key verification is required when trust is re-established. No real-source
credentials are needed to continue the disposable VM work.

## Last completed milestone (on main)

`0bce553093449f592bdd5d7c5fbe499c895cd972` introduced actual whole-OS replacement
in guarded VMs. An Ubuntu 20.04 source replaced an Ubuntu 24.04 destination's
ordinary files, retaining destination storage and UEFI binaries. The imported
system booted source kernel `5.4.0-216-generic` and Python 3.8. Account/group and
complete package-database hashes, ACLs, xattrs, capabilities, hard links, sparse
data and a SQLite-backed systemd service passed checks. GPT, EFI bytes and
root/boot/EFI identities were retained.

Evidence on this development machine (private, excluded from Git):
`reports/os-replace-vm-kmwdf46c/resume-result.json`.
Read `WHOLE_OS_LAB.md` for the supported layout and qualification limits.
The original run exposed a missing RAM `chroot` tool and a boot-directory rename
durability issue. Existing root data was verified rather than recopied; only
boot files needed repair. Source expectations must come from the offline frozen
view, not metadata belonging to temporary mounts in the running source.

The later stable main commit `a651139` improved installer directory errors.
The user's installation now works. Only group-write permission on the user's
owned `.local` and `.local/bin` directories was removed to satisfy the installer.
Both the repository launcher (`./migratarr run`) and installed executable work
as entry points; pulling Git updates does not automatically rebuild the installed
executable.

Normal `setup`/`run` remains assessment-only and may exit 2 with `BLOCKED`;
this is expected while physical migration is disabled. Password authentication
is handled by SSH, not saved in the profile. Optional sudo currently requires
pre-existing noninteractive authorization. There is no production migration
command to recommend to the operator at this checkpoint.

## Authentication issue intentionally deferred

The user reported a password-authenticated assessment ending with:
`Source privilege elevation failed. The optional sudo flow requires existing
noninteractive sudo authorization.`

This points to the optional sudo path, not necessarily failed SSH password
authentication. The user explicitly requested deferring this issue. Do not
change their saved profile, sudoers, SSH authorization or source configuration
while continuing the VM milestone. Private connection information and keys stay
in ignored local settings; do not publish them or request passwords in chat.

## Current unfinished milestone: whole-OS interruption recovery

The intended deliverable is resuming cleanup and copying after destination
power loss, rather than only recovering after copying has finished.

Code added/changed on this branch:

- `src/migratarr/os_cleanup.py`: exact ordered cleanup batches; FD-based parent
  identity checks; pending regular-file hashes and symlink targets; idempotent
  replay of missing approved leaves; validation of completed/missing paths,
  ownership and hard-link counts; refusal of new paths and replaced ancestors.
- `src/migratarr/os_execute.py`: schema-2 `os-transfer.json`; durable batch intent
  before unlinking; synced batch completion; separate cleanup/root/boot/reconcile
  stages; interruption handlers; source revalidation; checksummed copy repair;
  initial/resume entry points and protected checkpoint reserve.
- `src/migratarr/os_boot.py`: atomic system-file and GRUB-config installation;
  same-filesystem staging under protected job/boot namespaces; direct
  `mkinitramfs` output staged on boot; temporary build files in RAM via a bound
  `/run`; initramfs SHA1 cache updated in the imported userspace's format.
- `os_replace.py`: delegates new execution to `os_execute`, boot setup to
  `os_boot`; retains the existing legacy reconciliation-only entry point;
  optional VM bandwidth limit; scope-level sync replaces per-file `--fsync` in
  the whole-OS copier. This performance/durability change needs the VM power-cut
  test. The older bounded fixture transfer still has its own existing behavior.
- CLI: `recovery os-resume-vm --vm-token NONCE --preview-sha256 DIGEST` plus
  `--bwlimit` for whole-OS start/resume. Physical/nonce/RAM/UUID guards stay active.
- `tests/test_os_cleanup.py`: partial batch replay, replacement after unlink,
  hard-link content tampering despite restored mtime, replaced parents/new
  paths, and invalid stage/completion checkpoint combinations.

Validation at this save point: all **183** tests pass on Python 3.12 and isolated
Python 3.8.10; `git diff --check` passes. **The new execution and boot-reconciliation
path has not been run in VMs.** Tests passing is not VM qualification.

## Next concrete work

1. Review schema-2 resume/legacy reconciliation behavior and atomic boot staging.
   Preserve the rule that an already ready-for-boot checkpoint must not reset
   its reconciled initramfs back to source bytes. Keep unexpected destination
   paths, replaced parents and changed immutable sources as blockers.
2. Add one combined `scripts/test_os_interrupt_vm.py` rehearsal. It has not
   been written yet. Prefer reusing the already prepared frozen source snapshot
   read-only, with a fresh destination disk from the verified Ubuntu 24.04 image.
3. Cut destination power after a real unlink inside a pending cleanup batch
   but before the completed-prefix checkpoint. Resume via the new CLI and prove
   it completes exactly the remaining approved removals.
4. During the same run, observe real partial bytes in root `os-scratch`, cut
   destination power again, and resume without an artificial rate limit. Verify
   stale temporary-file handling and that source/boot/recovery protections hold.
5. Finish normal boot reconciliation and boot the imported Ubuntu 20.04 system.
   Reuse the existing independent metadata, account/package, service, UUID,
   GPT/EFI and source-immutability assertions. Do not rerun unrelated failure
   matrices unless a new failure requires them.
6. Document the result and remaining limits, then publish/merge only after the
   milestone actually passes. Main should remain the last qualified checkpoint
   until then. Do not publish private artifacts.

The developer harness in `test_os_replace_vm.py` already handles initial
destination bootstrap/job creation, RAM handover, preview and post-copy boot.
It currently calls the normal whole-OS replacement directly; introduce a small
execution hook if useful for the combined fault harness. `RecoveryLab.start`
currently opens a regular VM disk read-write; explicitly implement read-only
source attachment if reusing the frozen original without making a disk copy.

## Available local lab assets

- `.migratarr/vm-cache/focal-server-cloudimg-amd64.img`
  SHA256 `18f2977d77dfea1b74aee14533bd21c34f789139e949c57023b7364894b7e5e9`.
- `.migratarr/vm-cache/noble-server-cloudimg-amd64.img`
  SHA256 `04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94`.
- `.migratarr/lab-assets/vmlinuz-6.8.0-151-generic` and matching installed modules.
- The passed whole-OS lab above contains prepared source/destination images,
  generated fixture keys and source RAM bundle. Preserve those artifacts; their
  hashes/nonces and SSH keys are local fixtures, not hardcoded application inputs.
- QEMU uses unprivileged TCG; KVM is unavailable to this user. Host boot/disks
  must not be modified to accelerate tests.

Quick tests:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

The existing isolated Python 3.8 interpreter is under
`/tmp/migratarr-python38-CRnVyB/root/usr/bin/python3.8`, with `PYTHONHOME` set to
its corresponding `root/usr`. Recreate it if that temporary directory is absent;
do not change the real server's Python installation.

Git publication uses the connected GitHub tools for `boosted3svt/Migratarr`.
Runtime profiles, inventories, reports, SSH keys, receipts and VM images are
excluded from publication. This checkpoint branch is work in progress, not a
new production release.

## Remaining roadmap after this checkpoint

1. Prove whole-OS interruption recovery using the focused combined VM rehearsal
   above; record fresh evidence before merging.
2. Implement general writer discovery, source quiescence and application/data
   consistency across the entire server, including container storage/databases.
3. Qualify additional storage/firmware combinations, particularly the historical
   ext3 and possible BIOS-to-UEFI case, capacity constraints and exclusions.
4. Complete identity/network handover, operator-reviewed cutover and source
   rollback; retain source recoverability.
5. Qualify physical RAM recovery, hardware/driver adaptation and boot recovery,
   then integrate a guided end-to-end workflow with explicit supported cases.

This is an order of work, not a promise that physical migration is safe after
one more test. `migration_ready` and `production_qualified` remain false until
their actual supported-case requirements are implemented and proven. All future
planning/execution decisions must be implemented in the standalone program.
