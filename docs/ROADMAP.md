# Implementation and qualification gates

Development checkpoint: the `wip/os-interruption-recovery` branch contains
unfinished schema-2 cleanup/copy resume and atomic boot reconciliation. Read
[the current session handoff](SESSION_HANDOFF.md) before continuing. Unit tests
pass; the combined interruption VM rehearsal is still pending. This does not
change the completed milestones or production qualification below.

The deliverable is a standalone script/application, with no LLM runtime or
operator-chat dependency. Runtime discovery, unsupported-case decisions, planning,
checkpoints and eventual execution must be implemented as deterministic program
behavior. Development investigations inform reusable code and fixtures rather
than becoming manual steps in each migration. Current entry points provide
guided profile setup, repeatable runs and a code-only single-file zipapp build;
execution implementation and production recovery qualification remain outstanding.

The fresh-destination entry point is now `git clone` followed by `./install.sh`.
It builds and installs a code-only executable, checks system prerequisites,
preserves profiles on update, and launches guided assessment in a terminal.
Interactive first-use SSH trust uses an independent fingerprint check through
OpenSSH. See [installation](INSTALLATION.md). Physical migration stays disabled.

The assessment covers the whole server. Inventory and preservation requirements
apply to the entire local installation and all software/state; no application's
name determines inclusion or priority. Container evidence supports filesystem
dependency and writer discovery. References to particular applications in the
original handoff are examples of state that must survive when present, not a
selective application migration design or evidence of current use.

## 0. Guided assessment (implemented)

Local JSON inventory and offline comparison, versioned schema, probe errors,
architecture/bitness comparison, mount classification and explicit blockers.
Unknown evidence fails closed. This is preliminary discovery, not a complete
application inventory or a validated migration plan. A terminal wizard saves
private reports and optionally compares an existing source inventory offline.

With both inventories, the wizard now saves a blocked review plan (`plan.json`
and `plan.txt`); the `plan` command also works entirely offline. It maps
unambiguous matching local mount targets, retains destination filesystem
identifiers and EFI layout, records boot/swap reconciliation requirements and
checks aggregate source allocation against per-filesystem destination capacity.
The initial budget adds a 20% allowance and a proposed 2 GiB root recovery
reserve. Unknown mappings/shared capacity remain unresolved, and a positive
capacity result is not a migration qualification. No per-file census is required
for this preliminary storage check.

Destination collection covers infrastructure needed for migration: bootstrap
OS/tooling, architecture, RAM, boot signal, storage, capacity and networking.
Destination application/package/cron/Docker enumeration is intentionally skipped
because that installation will be replaced. Focused preparation conflict checks
are still needed before boot or transfer work.

Source-only discovery adds dpkg package metadata and architectures, loaded
systemd services/timers, installed service/timer unit files and cron names and
presence. Optional Docker inspection filters local-daemon storage and container
metadata. None of this establishes complete writer or application state discovery.

## 1. Complete read-only evidence

Read-only SSH source inventory is implemented using strict verified host keys,
existing keys/agents or explicit terminal password authentication. Collector code
runs in memory without installing remote files. Tests cover the transport and
runtime with mocked SSH and local stub modules; actual source validation remains
dependent on available credentials and evidence. Passwords are not collected by
Python or stored. Optional elevation requires preexisting noninteractive sudo.
VM recovery jobs now preserve explicit key authentication in protected job files
and restore it into RAM. A source-VM connection after reboot still needs testing;
assessment access does not establish production reboot connectivity.

The source collector supports Python 3.8+. Compatibility is checked under an
isolated Python 3.8.10 runtime and the destination's Python 3.12. The actual
in-memory payload was also executed under Python 3.8.10 with mocked environmental
probes. Older systemctl text and lsblk MOUNTPOINT outputs are covered by fixtures;
this does not qualify any Ubuntu release for migration execution.

Operator clarification: the survey and eventual migration cover the entire
server. Application examples in the handoff do not define a selection list or
current usage. Container metadata supports whole-server storage and writer
analysis; there is no plan to migrate containers as individually chosen apps.
Local account/group records, subordinate ID ranges, sanitized fstab entries,
swap/boot references and system-file metadata are collected as server evidence.
External identity providers, inaccessible writers, data sizes, manual installs
and a complete privileged filesystem/configuration review remain unresolved.

Assessment now recognizes runtime, image and layered filesystem mounts, supports
explicit operator-requested source mount exclusions, and records per-mount review
treatments. Source image backing data and Docker layer/volume preservation remain
unqualified. Existing source/destination JSON can be reanalyzed without source
access; original failed probes are retained rather than treated as resolved.

Complete discovery of source application/system writers and local state locations,
and Docker storage and volume/bind dependencies. Extend the existing package and
scheduler evidence to cover manual installs and user services. Inspect metadata
without dumping secrets. Inventory encryption,
swap, boot chain, NIC topology and hardware-bound state. Measure local data sizes
only with explicit mount boundaries and bind/subvolume exclusions. Produce a
reviewable source-to-destination map; never sum repeated filesystem capacity.

## 2. First qualification target (proposal, not a support claim)

Start disposable VM work with Ubuntu 24.04 LTS on both ends, x86_64 with 64-bit
userspace, UEFI without Secure Boot, one destination disk, ext4 root and an
existing FAT EFI system partition. Start without Docker; qualify Docker overlay2
on ext4 as a separate gate. Releases, BIOS, Secure Boot, other filesystems,
encryption/LVM/RAID, separate local filesystems and alternate Docker drivers
remain unqualified until covered by reproducible tests. Source and destination
release equality is a test simplification; the eventual migrated OS is the
source OS, not the destination bootstrap Ubuntu installation.

The actual source assessment is Ubuntu 20.04.6 with ext3 root and BIOS GRUB
packages, targeting the existing Ubuntu 24.04 bootstrap's ext4/UEFI layout. A
separate disposable-VM case must cover that source release/filesystem and boot
adaptation before it can be qualified; the simpler first target above does not
qualify the actual machines. Local QEMU tools are installed and the synthetic
RAM-recovery lab below has passed; full Ubuntu migration remains unqualified.

## 3. Single-disk RAM boot proof

The initial synthetic UEFI/BusyBox mechanism test is implemented in
`scripts/test_recovery_vm.py`; see [the lab instructions](RECOVERY_LAB.md).
It has booted from one virtual disk into RAM, retained SSH with the target
unmounted, resumed a committed job after forced QEMU termination, exercised
missing-image fallback and verified unchanged GPT/EFI/root UUID metadata.
This is a limited developer proof, not an automatic production qualification.

The standalone application now implements protected recovery jobs, image building,
VM-only boot preparation and automatic job resume. The full Ubuntu harness is
`scripts/test_ubuntu_recovery_vm.py`; see [recovery jobs](RECOVERY_JOBS.md) for
commands, credential handling, safeguards and test limits. Physical boot
installation and migration execution remain disabled.

On 2026-11-26 the Ubuntu 24.04.5 UEFI guest rehearsal passed using the packaged
application: automatic RAM startup/job resume, application and credential-file
availability with root and separate boot targets unmounted, another resume after
forced QEMU termination, corrupt-initramfs fallback to Ubuntu, and byte-for-byte
unchanged GPT/EFI contents plus unchanged root UUID. Local evidence is
`reports/ubuntu-recovery-x6fz8ion/result.json`. The 124 automated tests also
passed under Python 3.12 and Python 3.8.10.

The two-VM source connectivity gate also passed on 2026-11-26. The RAM image
includes OpenSSH and the standalone `recovery check-source` command uses only
restored runtime credentials. The test collected source inventories with no
probe errors while target root and `/boot` were unmounted, repeated that after
forced destination termination, rejected a wrong host key and a disconnected
source link, and recovered when connectivity returned. Failed checks left the
job checkpoint unchanged. Fallback and GPT/EFI/root-UUID checks still passed.
Evidence: `reports/source-connectivity-0791zysg/result.json`; instructions:
[two-VM source connectivity lab](SOURCE_CONNECTIVITY_LAB.md). All 131 automated
tests passed under Python 3.12 and Python 3.8.10.

The bounded transfer prototype passed on 2026-11-26. The standalone VM-only
`recovery transfer-fixture` command copied onto the destination disk from RAM,
preserved numeric ownership, modes, ACLs, xattrs, a capability, links, timestamps
and sparse-file behavior, and excluded nested mount and bind contents. A
SIGTERM interruption saved a retryable checkpoint; another copy was interrupted
by forced destination termination and successfully retried after RAM reboot.
Independent manifests still matched after returning to disk Ubuntu. Protected
recovery data and GPT/EFI/root UUID checks passed. Evidence is
`reports/transfer-vm-e52vylt3/result.json`; see [the transfer lab](TRANSFER_LAB.md).
All 137 automated tests passed under Python 3.12 and Python 3.8.10.

The failure-recovery extension passed on 2026-11-26: in-flight link loss,
same-size/same-mtime source mutation and actual destination ENOSPC each produced
a failed checkpoint, followed by a successful retry with matching metadata.
Source mutation was rejected despite rsync returning zero. A 2 MiB reserve
preserved checkpoint capacity during disk exhaustion. Private scratch storage
fixed orphan temporary files after abrupt termination; offline inspection of
the final checkpoint confirmed one orphan discarded and attempt 10 complete.
All 17 final entries matched after disk Ubuntu boot; protected recovery data
and GPT/EFI/root UUID checks passed. Evidence:
`reports/transfer-failures-68c5ryw1/result.json` and
`reports/transfer-failures-68c5ryw1/final-transfer-checkpoint.json`.
See [the failure lab](TRANSFER_FAILURES_LAB.md). All 146 automated tests passed
under Python 3.12 and Python 3.8.10.

Whole-system path-policy drafting and bounded exact-preview cleanup passed on
2026-11-26. Every generated draft plan now describes candidate filesystem scopes,
protected recovery/boot paths, reconciliation paths and mount exclusions. The
standalone VM sandbox refused wrong digests, reserved source collisions, stale
source/destination evidence, symlink redirects and linked cleanup candidates.
It removed exactly eight previewed bootstrap entries, retained protected and
managed data, and repeated protection checks after RAM restart and disk Ubuntu
boot. GPT/EFI/root UUID checks passed. Evidence is
`reports/root-policy-vm-re766raw/result.json`; see
[the path-policy instructions](ROOT_PATH_POLICY.md). All 160 automated tests
passed under Python 3.12 and Python 3.8.10. An offline review using the existing
real-server snapshots is `reports/root-policy-review-xx8ynpga/plan.txt`; it does
not establish fresh evidence or authorize execution.

Fixture copying now uses per-file temporary writes and an initial headroom
check, with a conservative largest-file allowance and protected recovery space.
Insufficient space records a failed checkpoint before launching rsync. This
does not yet enforce headroom continuously or schedule whole-OS cleanup.
The updated complete failure/recovery VM run passed with unchanged GPT, EFI
bytes and root UUID (`reports/transfer-failures-r88edxnj/result.json`). Fresh
Ubuntu clone/install/update and destination inventory passed separately
(`reports/install-vm-gwmw20j1/result.json`). All 171 automated tests passed under
Python 3.8.10 and Python 3.12.

The next gates are wiring the policy into a whole-OS VM transfer, implementing
the continuous temporary-space budget and reviewed cleanup schedule, and
boot/fstab/initramfs reconciliation. The bounded
fixture does not qualify full-root transfer or deletion. Full writer discovery and
the actual Ubuntu 20.04/ext3 boot adaptation remain outstanding; this rehearsal
does not qualify copying or physical installation.

The intermediate policy-copy integration now applies the root rules during
bounded fixture transfer, then uses the same policy for exact-preview cleanup.
It protects boot/recovery and reconciliation paths, excludes mounted data,
rejects redirected ancestors and external hard links, and records separate
policy-transfer checkpoints. A repeat-copy rehearsal exercises preservation
after cleanup. This closes the gap between policy drafting and fixture copying;
full-root replacement, continuous headroom enforcement and imported-OS boot
remain subsequent gates.
The combined integration rehearsal passed (`reports/policy-copy-vm-xlyapz4b/result.json`):
copy, eight exact-preview removals, repeat copy, RAM restart and disk boot, with
unchanged GPT/EFI/root UUID. All 173 automated tests passed under Python 3.8.10
and Python 3.12.

Use disposable source and destination VMs; the destination must have only one
disk. Demonstrate automatic process startup after reboot, network reconnection,
durable job loading, RAM/driver availability, log access and a tested fallback.
Choose the rescue-image and boot-entry mechanisms based on that evidence.
No physical disk replacement can precede this proof.

The VM installer uses protected job path `/var/lib/migratarr/jobs/<job-id>` and
rescue path `/boot/migratarr`. Assessment does not create these paths.
Detect source collisions before production adoption. Jobs use atomic, versioned
checkpoints; credentials are separate private files, outside checkpoint JSON.
A new process resumes after reboot. Define interrupted-transfer semantics and
extend rescue fallback coverage before implementing file deletion.

## 4. Transfer and reconciliation

The first whole-OS implementation is the [cross-release VM lab](WHOLE_OS_LAB.md):
an offline, read-only Ubuntu 20.04 source replaces an Ubuntu 24.04 destination's
ordinary files from RAM. It uses an exact metadata preview, guarded obsolete-file
cleanup, per-filesystem temporary writes, headroom monitoring and destination-UUID
fstab/initramfs/GRUB reconciliation. UEFI binaries, management access and recovery
assets are retained. This is an experimental single-layout implementation;
whole-OS interruption/retry and production cutover remain separate gates.

The cross-release whole-OS rehearsal passed on 2026-11-27. The Ubuntu 24.04
destination was replaced with the Ubuntu 20.04 source and booted source kernel
5.4.0-216-generic / Python 3.8. Complete account and package-database hashes,
the metadata fixture and the SQLite-backed systemd service passed checks.
GPT, EFI bytes and filesystem identities were retained. Evidence is
`reports/os-replace-vm-kmwdf46c/resume-result.json`. A missing rescue command and
copy-boundary durability issue were fixed; reconciliation-only retry verified
the existing root payload and repaired boot files without repeating root copy.
This qualifies the named VM happy path and recovery continuation, not physical
migration, ext3/BIOS adaptation or arbitrary whole-OS interruption recovery.

Review an explicit local filesystem allowlist and remote/pseudo exclusions.
Preserve symlink objects, numeric ownership, permissions, hard links, ACLs,
xattrs and capabilities; retain mountpoint directories. A one-filesystem flag
does not exclude bind mounts on the same filesystem. Protect the live job,
rescue image and destination mount infrastructure from source overwrite/deletion.
Never format/repartition. No second full staged copy may be required.

Discover and explicitly coordinate all writers before final copy. Keep imported
services inactive until cutover. Preserve complete server/application identities,
configuration and databases. Check Docker portability before copying state and report unsupported
cases rather than losing state. Reconcile fstab, crypttab, resume, initramfs,
bootloader, NIC/device bindings, GPU drivers and Installarr device fingerprints
without rerunning fresh-stack setup. Decide machine-id/SSH-host-key policy.

## 5. Reviewed cutover and verification

Present a concrete plan for the actual machines before OS replacement or writer
shutdown. Prove source address relinquishment before destination address use,
and prevent simultaneous conflicting server/application identities. Verify boot,
mounts, ownership, application identities, databases and persisted application state.
Retain source rollback with migrated services disabled or source offline.
Clean up recovery material and credentials only after verification.

Upstream references in the handoff describe building blocks. Recheck official
version-specific documentation as each later mechanism is implemented.
