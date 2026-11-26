# Single-disk RAM recovery development lab

The VM is a disposable test machine for the RAM-boot mechanism, not the proposed
migration environment for a physical destination. Inside the VM, a disk-rooted
bootstrap reboots into an initramfs rescue system. That system must keep running
with its target filesystem unmounted. The physical implementation would reboot
the destination itself into RAM, replacing its running bootstrap OS.

This first test uses a synthetic BusyBox Linux guest, UEFI without Secure Boot,
one GPT disk image with EFI and ext4 partitions, and ephemeral key-only SSH.
It does not perform a migration or connect to the real source server.

The next rehearsal uses a full Ubuntu bootstrap and the packaged application;
see [saved recovery jobs and the Ubuntu lab](RECOVERY_JOBS.md). The limits below
describe this original synthetic mechanism test.

## Running the lab

Check local developer tools:

```sh
python3 scripts/test_recovery_vm.py --check
```

On Ubuntu, the principal packages are `qemu-system-x86`, `ovmf`, `mtools`,
`busybox-static`, `dropbear-bin`, `e2fsprogs`, `kmod`, `zstd` and `openssh-client`.
Installing these is a local development-machine setup step. The harness runs
unprivileged using software CPU emulation, without access to `/dev/kvm`.

Provide a readable regular copy of a Linux kernel and its matching installed
module version. If `/boot` kernels are unreadable, use the normal administrator
flow to copy the selected kernel into a private development cache. The host need
not boot that kernel. Do not run the harness as root.

```sh
python3 scripts/test_recovery_vm.py \
  --kernel /private/cache/vmlinuz-KERNEL_VERSION \
  --kernel-version KERNEL_VERSION
```

Each run creates a private `reports/recovery-vm-*` directory. Optional
`--work-dir` must name a new directory. Image construction uses new regular files,
without host block-device access, host filesystem mounts, `grub-install` or host
EFI writes. Guest SSH is forwarded to a random localhost port; restricted QEMU
networking prevents guest access to the real source/network. Guest keys are
generated and pinned locally. No real source credentials enter the guest.

## Assertions and evidence

The test boots from the single virtual disk, prepares a durable job and rescue
image on that disk, and reboots through GRUB into RAM. It reconnects over SSH,
unmounts the target, opens another SSH session, and remounts the target. After
committing a checkpoint, it kills QEMU and restarts the same disk, checking the
job identity, checkpoint and increased resume counter. Finally it hides the
rescue image inside the guest and checks fallback to the disk bootstrap.

After guest shutdown, the test checks unchanged GPT metadata, EFI partition
contents and root filesystem UUID. `result.json` records each phase and final
status. Serial/QEMU logs remain alongside it; failed runs retain evidence and
terminate their QEMU process. Normal unit tests do not launch VMs.

## Limits

Passing is a narrow mechanism proof, not production migration readiness. Power
loss occurs after a committed checkpoint, not during OS replacement. Fallback
covers a missing rescue image, not a corrupt kernel, broken firmware or network
driver. There is no full Ubuntu guest stack, source VM or copy engine. The actual
source release/filesystem and BIOS-to-UEFI adaptation remain unqualified.

Next cases need disposable Ubuntu source/destination VMs, partial-target-write
and lost-connectivity recovery, boot-attempt fallback, metadata preservation and
the supported hardware/storage matrix. No lab result enables physical migration.

Building blocks: [Linux initramfs](https://docs.kernel.org/filesystems/ramfs-rootfs-initramfs.html)
and [QEMU invocation](https://www.qemu.org/docs/master/system/invocation.html).
Observed lab results are local test evidence rather than claims from these references.
