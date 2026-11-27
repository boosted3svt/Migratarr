# Migratarr — whole-server migration project handoff

Date: 2026-11-26

For current development state and tomorrow's next steps, read
[SESSION_HANDOFF.md](SESSION_HANDOFF.md) first. This file is historical requirements.

Historical requirements handoff. Implementation has since progressed; see the
[current roadmap](ROADMAP.md) and [installation instructions](INSTALLATION.md)
for current capabilities. The original proposed design below is not a statement
that migration execution is implemented or authorized.

## Start here

The operator wants a NEW, SEPARATE project named **Migratarr**. Do not add this
functionality to Installarr. This file captures the migration discussion so work
can continue in a new Codex chat without the rest of that conversation.

**Status: requirements and proposed design only.** No migration engine, rescue
environment, boot changes or source-server connection has been implemented.
This handoff request authorizes creating this document, not performing a
migration, stopping services, rebooting or modifying disks.

This document originated in the Installarr checkout for convenient handoff.
Migratarr now has its own checkout and repository; do not modify or publish
Installarr as part of creating Migratarr.

## Intended outcome

Migrate the ENTIRE installation from an existing Linux server to new hardware:

- Installed OS, kernel files, application binaries and package-manager records.
- System configuration, services, users/groups and numeric ownership identities.
- Home directories, local files, databases, credentials and application settings.
- Docker configuration, images/container state where portable, named volumes and
  local bind-mounted application data.
- Plex server identity, authorization, libraries, metadata, watched status and
  playback positions; Tautulli history, metrics and configuration.
- Other locally installed applications, not only a prescribed media stack.

The goal is the same logical server and application configuration on new
hardware, with the necessary hardware/storage/network adaptations. This is not
just copying media folders, reinstalling selected packages or rebuilding apps
from installer defaults. Do not impose Installarr's standard UID/GID, quality
profiles, application versions or directory layout on the source installation.

Hardware differences and filesystem changes mean this is not a bit-for-bit clone.

## Operator-confirmed requirements

1. **Same CPU architecture.** Architecture conversion is not planned. Verify the
   actual architecture/bitness matches rather than treating all x86 variants as
   interchangeable.
2. **One destination disk.** There will not be another disk or a second location
   containing a complete staged copy of the source installation.
3. **Retain the destination filesystems and partition layout.** The operator can
   first install Ubuntu using the desired filesystem/layout. Migratarr must not
   format or repartition that disk as part of this agreed design.
4. **Start on the destination.** The operator launches Migratarr on the new
   server's initially installed Ubuntu system, on the same disk being migrated.
5. **Source access over the network.** Collect source IP/hostname, username and
   authentication privately. The operator originally described a root account
   and password. Root SSH or an appropriately privileged administrator/sudo flow
   can supply access; determine the supported authentication mechanism before
   implementation. Do not require secrets in chat or command-line arguments.
6. **Stop source application writers.** Docker containers and databases will be
   stopped for the consistent final copy. Discover non-container databases,
   scheduled jobs and other writers rather than assuming Docker covers all of
   them. Coordinate stopping them explicitly at migration time.
7. **Preserve Plex identity.** The existing claimed logical Plex server must
   continue working after migration, including clients and watch/progress data.
   Retain its full state and authorization; do not obtain a fresh claim or create
   a replacement Plex identity as the normal migration path.
8. **Preserve Tautulli history and metrics.** Retain its complete configuration
   and database along with its own instance identity and Plex association.
9. **Inherit the original network address at cutover.** The operator is willing to
   change router DHCP reservations externally. MAC cloning is optional if the
   NIC/driver supports it, not a mandatory substitute for router changes.
10. **The two physical machines may remain present/powered on afterward.** This
    does not authorize simultaneous use of the same IP/MAC on one network, or
    two active Plex instances with the same Plex identity. Plan a controlled
    handover. The source can remain available at another management address
    with migrated services disabled, or be kept offline for rollback.
11. **Exclude remote mounted data and recursive duplication.** Preserve mount
    configuration/mountpoint directories, but do not pull NAS/share contents into
    the local migration. Never dereference symlinks into recursive copies.

The initial Ubuntu installation is the bootstrap system. The source OS and
package versions replace its contents; retaining the destination filesystem
does NOT mean retaining the destination Ubuntu release. A source ext3 installation
can be copied at file level onto the destination's already-created ext4 filesystem.

## Proposed single-disk execution model

This is an agreed direction, not an implemented or validated mechanism.

### Phase 1: preparation under the destination's existing Ubuntu

- Privately collect/verify source SSH access, including host-key verification.
- Inventory source/destination OS, boot mode, filesystems, storage layout,
  hardware, networking, applications, Docker storage and required data sizes.
- Produce a concrete source-to-destination filesystem map and adaptation plan.
- Verify destination capacity, filesystem features, boot compatibility and RAM.
- Install a self-contained temporary migration boot environment on the existing
  disk and create a protected, durable job/checkpoint directory on that disk.
- Verify the recovery environment contains all required drivers, network tools,
  SSH/transfer tools, credentials handling and status/reconnection mechanisms.
- Prepare its boot entry and startup job before any destructive file replacement.

### Phase 2: boot the migration environment into RAM

**The original script process WILL stop on reboot.** A new migration process must
start automatically in the RAM-based environment, load the saved job and continue
the workflow. A checkpoint file by itself does not arrange this continuation.

The SSH connection will drop. The RAM environment needs its own working network
configuration, SSH access and progress/error reporting so the operator can
reconnect. Use a temporary destination address until the final identity handover.

The running migration environment must be self-contained in RAM, independent of
the destination root filesystem being replaced. Only tools/runtime state need to
fit in RAM; source data streams directly to disk, not through a full RAM image.

### Phase 3: copy directly onto the existing destination filesystems

- Mount destination filesystems as migration targets, not as the copier's active
  OS root. Do not overwrite a normally running Ubuntu root tree in place.
- Use file-level transfer over SSH, such as appropriately configured rsync.
  `dd`/raw imaging would preserve the source filesystem and is not this design.
- Preserve numeric UID/GID, permissions, timestamps, symlinks, hard links, ACLs,
  extended attributes/capabilities, sparse files and required special files where
  the destination supports them. Account for tool/version limitations.
- Preserve symlink OBJECTS without following them. Omitting all symlinks would
  break a Linux installation. Never use a dereferencing mode as the default.
- Explicitly enumerate allowed local source filesystems. Required separately
  mounted local `/boot`, EFI or `/home` content must be handled deliberately;
  excluding all mounts blindly would leave an incomplete installation.
- Explicitly exclude remote mounts, bind-mount duplication and runtime pseudo
  filesystems such as `/proc`, `/sys`, `/run` and dynamically generated `/dev`.
  A one-filesystem flag alone does not exclude same-filesystem bind mounts.
- Protect migration checkpoints, rescue files and their boot path from transfer
  overwrites/deletions. An unrestricted root-level `rsync --delete` is insufficient.
- A preliminary copy can reduce downtime, but the final pass must occur with
  source application writers stopped. Do not claim a globally atomic snapshot of
  a running OS merely because its Docker containers were stopped.

Retaining the destination filesystem allows persistent checkpoints on that same
disk. If formatting is introduced later, this assumption no longer holds and the
recovery design must change. RAM alone cannot retain checkpoints through power loss.

### Phase 4: reconcile the copied OS for the destination

- Retain destination filesystem UUIDs/PARTUUIDs; update copied references to
  point to them. The term is **UUID**, not user UID.
- Update `/etc/fstab`, including filesystem types and compatible mount options.
- Inspect boot root references, `/etc/crypttab`, swap/resume settings, systemd
  units, scripts and disk-by-id/serial-dependent configuration.
- Rebuild initramfs and install/configure the appropriate bootloader for the
  destination; handle BIOS/UEFI, EFI partition contents and Secure Boot needs.
- Reconcile NIC names, permanent-MAC matches, addresses, DHCP identifiers,
  routes, DNS, bridges/bonds/VLANs, firewall bindings and VPN configuration.
- Check GPU/transcoding drivers, PCI/USB device mappings and custom udev rules.
- Assess hardware-bound licenses, TPM-backed secrets/encryption and firmware
  settings; some cannot be migrated by copying disk files.
- Keep application identities and databases intact. Decide OS machine-id and SSH
  host-key policy deliberately, accounting for source coexistence and replacement
  semantics; do not blindly regenerate every identity.
- Reconcile Docker backing-filesystem/storage-driver compatibility. Do not assume
  copying `/var/lib/docker` works across arbitrary drivers/filesystems. Any
  fallback export/recreation must preserve the required state or report a
  limitation before migration starts, not silently discard it.
- If the source was managed by Installarr, inspect its saved filesystem checks,
  generated service definitions and immutable checkpoint fingerprints. They may
  refer to old source devices/filesystem types. Reconcile these without invoking
  fresh-stack configuration that overwrites imported app settings.

### Phase 5: cutover, verification and recovery cleanup

- Keep imported services from starting prematurely during preparation.
- Complete transfer verification and hardware/boot reconciliation.
- Have the source relinquish its inherited network address before the destination
  adopts it. Never activate the cloned Plex identity on both machines together.
- Reboot into the migrated OS, reconnect and verify boot, mounts, ownership,
  networking, application authentication, databases, Plex identity and Tautulli
  history/metrics against the recorded source inventory.
- Retain protected recovery files/checkpoints until validation succeeds, then
  clean up temporary migration assets and temporary authentication material.
- Keep the source data intact for rollback. Do not treat a checkpoint as proof
  that a partially overwritten destination will always remain bootable after an
  interruption; validate recovery from actual failure points.

## Implementation priorities and open decisions

1. Create the independent Migratarr project/repository; keep Installarr unchanged.
2. Implement a READ-ONLY migration assessment and machine-readable inventory.
3. Define the first supported matrix: source Ubuntu releases, boot modes,
   filesystems, destination layouts and Docker storage implementations.
4. Prove a single-disk RAM boot/SSH/resume workflow in disposable VMs before
   deploying a copier to a physical system. Source and destination can be two
   VMs; the destination must still have just one disk to reflect the requirement.
5. Specify protected checkpoint/recovery paths, transfer exclusion rules,
   interrupted-job semantics and boot fallback behavior.
6. Define secure SSH authentication persistence across the RAM boot and cleanup.
7. Implement transfer, reconciliation, cutover and evidence-based verification.

The exact rescue-image technology, boot-entry mechanism, supported layouts,
machine-id/SSH-host-key policy and failure recovery implementation remain open.
An automatic RAM rescue boot is a substantial feature, not an existing property
of Installarr or a normal shell script. Fail unsupported cases during assessment.

Do not start disk changes or source-server operations merely from this handoff.
The new chat should first inspect the agreed requirements and build the project
and read-only assessment. Obtain the actual source/destination details privately
when they become necessary, and present a concrete reviewed migration plan before
any destination OS replacement or source-service/network cutover.

## Primary references consulted during the design discussion

- [Linux initramfs / RAM root filesystem](https://docs.kernel.org/filesystems/ramfs-rootfs-initramfs.html)
- [Rsync options and preservation/mount-boundary behavior](https://download.samba.org/pub/rsync/rsync.html)
- [fstab identifiers and filesystem configuration](https://man7.org/linux/man-pages/man5/fstab.5.html)
- [Netplan interface matching and configured MAC addresses](https://netplan.readthedocs.io/en/stable/netplan-yaml/)
- [Docker storage driver compatibility](https://docs.docker.com/engine/storage/drivers/select-storage-driver/)
- [Systemd cloned-system machine-id considerations](https://www.freedesktop.org/software/systemd/man/247/machine-id.html)

These explain building blocks, not proof that the proposed Migratarr workflow has
been implemented or tested. Recheck version-specific behavior during development.
