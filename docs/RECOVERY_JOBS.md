# Standalone recovery jobs and Ubuntu VM rehearsal

Recovery is implemented as commands in the same `migratarr.pyz` application that
performs assessment. It does not depend on a chat session or a surviving Python
process. The recovery workflow does not replace an operating system or enable
physical boot installation. The separate [bounded VM transfer](TRANSFER_LAB.md)
command copies only an approved fixture tree; it does not enable whole-system
replacement.

`cleanup-preview-vm` and `cleanup-apply-vm` exercise the
[whole-root path policy](ROOT_PATH_POLICY.md) inside a fixed sandbox on a
disposable guest's target disk. They do not enable cleanup of the actual root.

## Durable jobs

With an existing profile and generated plan, save a private recovery job:

```sh
python3 dist/migratarr.pyz recovery create \
  --config /private/profile.json --plan /private/plan.json \
  --state-dir /private/jobs
python3 dist/migratarr.pyz recovery status --job /private/jobs/JOB_UUID
```

Creating or inspecting a job does not change boot settings. Creation requires
one resolved destination root filesystem UUID. A blocked plan remains blocked;
creating a rehearsal job does not approve its filesystem map or authorize copying.

The job stores versioned state, profile and plan snapshots, checksums and a resume
counter. Updates use an exclusive lock, atomic replacement and filesystem sync.
Inspection rejects changed evidence, symlinked files and public or wrongly owned
job files/directories. Checksums detect changes; they are not signatures against
a privileged attacker who can rewrite the entire job.

An SSH profile must select an explicit unencrypted private key and an existing
verified known-hosts file. Password prompts and agent sessions cannot survive a
reboot. The key and host trust file are copied into private job files; private
key contents never enter JSON output or the distributable application. During
resume they are copied into RAM and a runtime profile points to those copies.
Recovery jobs therefore contain credentials and must be protected like keys.
Automatic credential cleanup is still future work.

Once the job has resumed, collect read-only source evidence using its RAM copies
of the explicit identity and verified known-hosts file:

```sh
python3 /opt/migratarr.pyz recovery check-source
```

`--runtime-dir` defaults to `/run/migratarr`. This command works with target
filesystems unmounted. It saves a private `source-check.json` and prints the same
JSON result, including the source inventory. Exit code `0` means collection
completed its probes; `2` retains explicit probe failures; `1` means invalid
runtime evidence or connection failure. A new failed attempt replaces an old
successful result with `source_unreachable`; no stale inventory is reported as
current. The command never advances the durable job or enables migration.
It requires a resumed runtime, explicit restored key authentication and private
credential files inside that runtime. Password and agent prompts are not used.

## Full Ubuntu rehearsal

Run as an unprivileged developer with the tools listed in
[the original mechanism lab](RECOVERY_LAB.md), plus `qemu-img` and `debugfs`.
Supply a downloaded Ubuntu 24.04 AMD64 cloud qcow2 image, its verified SHA256,
and a readable kernel with matching modules installed on the development host:

```sh
python3 scripts/test_ubuntu_recovery_vm.py \
  --image /private/cache/noble-server-cloudimg-amd64.img \
  --sha256 VERIFIED_IMAGE_SHA256 \
  --kernel /private/cache/vmlinuz-KERNEL_VERSION \
  --kernel-version KERNEL_VERSION
```

The harness verifies the image digest and creates a disposable regular disk file.
It injects its cloud-init seed into that disk, so the guest has one data disk and
no second seed disk. Firmware uses UEFI without Secure Boot. Networking is
restricted to the harness's loopback SSH connection; no real source keys or
addresses enter the guest. A generated fixture profile names `source.invalid`.
The assessment fixture is not evidence of a real source's compatibility.

Inside Ubuntu, the packaged CLI inventories the guest, creates a draft plan and
saves a job under `/var/lib/migratarr/jobs/JOB_UUID`. The harness builds a RAM
image with the same CLI, Python, network/storage drivers and key-only management
SSH. The CLI installs its recovery entry and asks Ubuntu to reboot. The image
starts the CLI automatically, validates the target UUID and boot asset checksums,
loads the job, and restores test credentials into RAM.

The test then checks that the packaged application runs and restored credential
files remain available with the target filesystems unmounted; restarts the disk after forced QEMU
termination; and checks that a corrupt rescue image falls back to Ubuntu.
Each run retains private `reports/ubuntu-recovery-*/result.json` and serial logs.
After the fallback boot, the harness cleanly unmounts the EFI filesystem and
compares GPT metadata, all EFI partition bytes and the root filesystem UUID
against the preboot snapshot. The clean unmount clears FAT's mount-state flag;
it does not hide or normalize changes to boot files. Both invariant snapshots
remain with the report.
Normal unit tests do not launch a VM.

The full rehearsal passed locally on 2026-11-26 with Ubuntu 24.04.5, using rescue
kernel `6.8.0-151-generic`. Evidence is
`reports/ubuntu-recovery-x6fz8ion/result.json`, with matching
`invariants-before.json` and `invariants-after.json` for GPT metadata, EFI
partition bytes and root UUID. The harness cleanly unmounts EFI at the end so
its normal mounted/dirty flag does not distort the bytewise comparison.

## Boot safeguards and limits

`recovery prepare-vm`, `boot-vm` and `resume` require root inside a QEMU/Bochs
guest with a matching private disposable-VM marker and nonce. Root permission
alone is insufficient. These safeguards prevent ordinary physical-host use;
they are not a security boundary against a privileged operator forging evidence.

GRUB records a boot attempt before loading the rescue image. The RAM application
clears that flag only after a verified job resume. If loading the image fails,
the next firmware boot selects the normal Ubuntu entry. A kernel panic requests
a reboot; hangs, firmware failures and loss of networking still need separate
recovery coverage.

The rehearsal uses a host-derived rescue kernel and tools. It does not qualify
physical hardware, arbitrary Ubuntu releases, Secure Boot, BIOS conversion,
encrypted storage or the actual Ubuntu 20.04/ext3 source. The separate
[two-VM source connectivity test](SOURCE_CONNECTIVITY_LAB.md) exercises SSH
from RAM with generated credentials. There is no
copy engine, interrupted-copy recovery, writer shutdown, imported OS boot or
network cutover. All jobs retain `migration_ready: false`.
