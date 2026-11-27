# Migratarr

For a new chat or a fresh machine, read [START_HERE.md](START_HERE.md) for the
latest development checkpoint and recovery instructions.

Migratarr is a separate project for migrating an entire Linux installation to
new hardware while retaining the destination's existing partitions and
filesystems. It is intended to preserve the source's operating system, users, application
settings and identities. See the
[requirements handoff](docs/MIGRATARR_HANDOFF.md).

Migratarr is a standalone command-line application. Operators supply connection
details and decisions directly to the tool; no LLM or chat session is used at
runtime. Discovery, compatibility checks and planning rules must live in tested
application code. The machines used during development are test cases, not
hardcoded source/destination requirements.

The migration scope is the entire local installation: operating system, installed
and manually deployed software, users/groups, services, scheduled jobs, local
data and all container-engine state. Applications are not selected for individual
migrations. Container metadata identifies storage dependencies and potential
writers; application names in earlier examples are not a survey whitelist or a
signal that those applications are currently used. Remote mounted contents and
explicitly excluded duplicate mounts remain outside the local copy scope.

**Current milestone: standalone assessment and experimental VM-only OS replacement.**
Saved recovery jobs and a self-contained rescue image are implemented for
disposable QEMU guests. Physical rescue installation, production migration,
general writer coordination and network cutover remain unqualified and disabled.
The bounded [VM fixture transfer](docs/TRANSFER_LAB.md) tests copying onto the
destination disk with metadata preservation and interruption recovery.
The [failure-recovery extension](docs/TRANSFER_FAILURES_LAB.md) exercises lost
connectivity, source changes during copying and destination space exhaustion.
Draft plans also contain a [whole-system path policy](docs/ROOT_PATH_POLICY.md).
Its VM-only cleanup prototype applies an exact, revalidated preview inside a
bounded root-shaped sandbox; physical-host replacement and deletion remain disabled.
The fixture transfer can also apply that policy during copying, retaining
protected boot/recovery, network and identity paths before exact-preview cleanup.
The [whole-OS lab](docs/WHOLE_OS_LAB.md) implements actual Ubuntu 20.04 source
replacement of an Ubuntu 24.04 destination from RAM, including exact cleanup,
separate root/boot copying and boot reconciliation for one guarded VM layout.
Every assessment and recovery job returns `migration_ready: false`.
No physical migration combination is qualified yet.

## Install on a fresh Ubuntu destination

```sh
git clone https://github.com/boosted3svt/Migratarr.git
cd Migratarr
./install.sh
```

Install Git first if the Ubuntu installation does not include it. The installer
checks Python 3.8+ and required system tools, installs missing tools using Ubuntu
packages, and installs a single executable at `~/.local/bin/migratarr`. It then
starts guided assessment in an interactive terminal. It does not upgrade an
existing Python interpreter or require pip, Docker or VM tools for assessment.

This release assesses and plans; it cannot migrate a physical server yet.
The first SSH connection asks you to verify the source fingerprint independently;
SSH handles password authentication directly. See [installation details](docs/INSTALLATION.md)
for unattended installation, updates and current limitations.

## Run without installation

Requires Linux and Python 3.8+ on the destination and SSH source. No Python runtime dependencies.
Linux tools `lsblk`, `ip` and `systemctl` provide additional metadata. Older
`systemctl` versions can use plain-text output, and older `lsblk` versions can
use the singular `MOUNTPOINT` column. Missing tools and inaccessible probes
appear in `errors`; no upgrade is performed.

From the checkout, create a saved profile and run the assessment:

```sh
./migratarr setup
./migratarr run
```

Running `./migratarr` with no arguments starts `run`; if there is no profile it
offers setup in an interactive terminal. The default profile path is
`~/.config/migratarr/config.json`; use `--config /private/settings.json` with
`setup` and `run` to choose another path. Setup asks for source host, username,
authentication method, a known-hosts file (verified on first interactive use), optional sudo,
Docker metadata, mount exclusions and report directory. It never saves passwords.
SSH handles password entry in the terminal when that authentication method is
selected. Profiles are private JSON files, created without replacing existing
files; edit a profile or use a new path to change saved settings.

For unattended runs with an existing SSH key or agent:

```sh
./migratarr run --config /private/settings.json --non-interactive
```

Paths inside a profile are resolved relative to the profile's directory, so runs
work from another current directory. Profiles can also select saved source and
destination inventories for reproducible offline planning. Invalid or unsupported
settings fail with an explanation; arbitrary Linux inputs do not imply that every
combination is qualified for migration execution.

Build a single-file distribution using the Python standard library:

```sh
python3 scripts/build.py
python3 dist/migratarr.pyz setup
python3 dist/migratarr.pyz run
```

Copy `dist/migratarr.pyz` to another destination with Python 3.8+ and OpenSSH.
The archive includes application code only, excluding profiles, keys and reports.
It streams its embedded collector to the source without installing Python modules
there. The current tool produces assessment/draft-plan reports; migration
execution remains unimplemented. VM-only RAM recovery is a separate developer
workflow described in [recovery jobs](docs/RECOVERY_JOBS.md). No one-off chat
report is a required input.

The lower-level terminal wizard is also available:

```sh
PYTHONPATH=src python3 -m migratarr.cli wizard
```

The wizard collects destination infrastructure evidence and optionally prompts
for an existing source inventory JSON file. It creates a new private folder under
`reports/` containing `inventory-destination.json` and a readable `summary.txt`.
With a source inventory, it also writes `inventory-source.json` and `assessment.json`. Each run uses a new
folder with permissions `0700` and new files with permissions `0600`. Reports
are excluded from Git. SSH source access is explicit; the wizard never executes migration.

For unattended use, or to compare a saved source inventory:

```sh
PYTHONPATH=src python3 -m migratarr.cli wizard --non-interactive
PYTHONPATH=src python3 -m migratarr.cli wizard --source /private/inventory-source.json
```

`--output-dir /private/reports` chooses the parent report directory. Wizard
exit code `2` means the assessment remains blocked, including when report
generation succeeds; it does not indicate a failed report write.

With both inventories, the wizard also saves `plan.json` and a readable `plan.txt`.
These are blocked review drafts, not executable migration scripts. To build a
plan entirely offline from two saved snapshots:

```sh
PYTHONPATH=src python3 -m migratarr.cli plan \
  --source /private/inventory-source.json \
  --destination /private/inventory-destination.json
```

Planning maps unambiguous matching local mount targets, identifies destination
boot adaptations and reports unresolved mappings. It retains existing destination
partitions, filesystems and identifiers, and imports the source operating-system
release. `--ignore-source-mount` also applies to `plan`.

Capacity uses aggregate source filesystem allocation (`total - free`, not
`total - available`, which includes reserved free space), plus a 20% planning
allowance and a proposed 2 GiB recovery reserve on root. It compares that budget
with each destination filesystem's currently available space. Shared devices and
ambiguous subvolumes are not added together. Older snapshots lacking used-space
evidence return unknown capacity. A sufficient budget is preliminary evidence;
live writes and filesystem feature differences still need validation. This does
not require a per-file size or ownership census. Preservation of ownership and
other file metadata is a transfer requirement to verify in disposable VM tests.

When a source mount is intentionally outside the migration review, use
`--ignore-source-mount /path` with `wizard` or `assess`. The option is repeatable
and applies to that mount and nested mounts. It records the exclusion in the
assessment and leaves the original inventory and actual mount unchanged. The
source root cannot be excluded. Remaining duplicate-device checks are computed
after exclusions; an excluded second view of root does not make root itself a
duplicate. For example:

```sh
PYTHONPATH=src python3 -m migratarr.cli wizard \
  --source /private/inventory-source.json \
  --ignore-source-mount /mnt/local-root --non-interactive
```

Assessment JSON includes a `mount_review` for both machines. Kernel/runtime
mounts, image mounts such as Snap's squashfs, and layered mounts such as Docker's
overlay are recognized separately. Source image and layered mounts require
backing-data review; recognizing them does not qualify storage for copying.
Destination image and layered mounts are bootstrap evidence, not source
application-preservation blockers. Unknown filesystem types still block review.
Source summaries include failed probes and paths; saved probe failures remain
part of the evidence until a new collection demonstrates success.

## Source investigation over SSH

No installation on the source is required. The destination streams the collector
modules to a Python process in memory on the source and validates the JSON reply.
The source needs Python 3.8+ and a POSIX login shell; its existing interpreter is
used without installing or upgrading Python. No source packages, files,
services, boot entries, network settings or disks are changed. SSH/server audit
logs and ordinary metadata accesses may still be recorded. The default command
collects as the specified user, so permission gaps are reported explicitly.

The lower-level SSH commands require a verified key in `~/.ssh/known_hosts`, or
an existing file selected by `--known-hosts`. Unknown/changed keys cause failure.
Interactive `run` can enroll a new host through OpenSSH after independent
fingerprint verification; it never accepts keys automatically.
When establishing trust for a new host, compare its fingerprint through an
independent trusted channel (for example, from the source console). A key retrieved
from `ssh-keyscan` alone is not proof of the server's identity.

```sh
PYTHONPATH=src python3 -m migratarr.cli wizard \
  --source-host SOURCE_IP --source-user SOURCE_USER \
  --known-hosts /private/verified-known-hosts --non-interactive
```

Authentication defaults to an existing key or agent. Use `--identity-file` for
an explicit local identity path, or add `--password-auth` to let OpenSSH prompt
privately through your local terminal. No password argument, file or environment
variable is accepted. Password mode requires a controlling terminal and cannot
be used unattended. This program does not enroll keys on the source. SSH config
aliases, proxy/jump settings and forwarding are not used; supply the actual host,
user and optional `--ssh-port` explicitly.

Add `--inspect-source-docker` to query the source's local Docker daemon. Optional
`--sudo` uses `sudo -n` and requires existing noninteractive authorization; it
does not collect or prompt for a sudo password. A source requiring interactive
sudo can still be inventoried without that option, with permission gaps recorded.

For source JSON alone, use `source-ssh --host HOST --user USER`; the same SSH
flags apply, with `--inspect-docker` to include Docker evidence. It writes JSON
to stdout and returns `2` when probes are incomplete. Choose a private report
destination when redirecting output. Connection failures return `1` without
printing raw remote error output.

Host trust and password prompting follow the
[OpenSSH client configuration documentation](https://man.openbsd.org/ssh_config).

From this checkout, inventory the **current machine**:

```sh
umask 077
PYTHONPATH=src python3 -m migratarr.cli inventory --role destination > inventory-destination.json
```

The role selects which local evidence to collect; `--role source` does not connect to a source server. When
source assessment is authorized, run this same local command on that machine
with `--role source`, and bring the resulting JSON back through your chosen
private channel. No source access is needed to develop or test this project.
Do not put passwords or application secrets in command arguments or reports.

Destination collection focuses on OS/bootstrap information, architecture, RAM,
boot-mode evidence, partitions, filesystem identifiers, capacity and networking.
Its installed packages, services, cron jobs and Docker state will be replaced
and are intentionally skipped. Focused destination preparation/conflict checks
remain future work.

Source collection additionally records installed dpkg packages and architectures,
systemd services/timers, installed service/timer unit files, and cron entry names
and crontab presence. A server-wide survey also records local numeric user/group
identities, subordinate ID ranges, sanitized filesystem configuration, swap and
boot-root references, and metadata for system identity/security files. Password
fields, account descriptions, remote mount source strings, mount option values,
private keys and system-file contents outside the explicit metadata parsers are
omitted. Missing or inaccessible evidence remains explicit. No job contents are
read. To include source Docker metadata,
run **on the source**:

```sh
umask 077
PYTHONPATH=src python3 -m migratarr.cli inventory --role source \
  --inspect-docker > inventory-source.json
```

Docker inspection is opt-in and uses only `/var/run/docker.sock`; remote contexts
and rootless/custom sockets are outside this milestone. Reports contain selected
storage-driver/root metadata and container names, images and states, with no
container commands or environments. Driver compatibility, volume dependencies,
application state locations and complete writer discovery remain unresolved.
The destination role rejects `--inspect-docker` before running any probes.

Compare saved inventories offline:

```sh
PYTHONPATH=src python3 -m migratarr.cli assess \
  --source inventory-source.json \
  --destination inventory-destination.json > assessment.json
```

Exit codes: `0` inventory collection completed its probes; `1` invalid input or
I/O error; `2` incomplete inventory or blocked assessment. A successful inventory
does not establish migration compatibility. JSON output goes to stdout; the
shell redirects it only when requested. Use a private directory for reports;
they include network addresses and storage identifiers. JSON input is treated
as local evidence, not authenticated proof of a machine's state.

An optional editable install (`python3 -m pip install -e .`) provides the
`migratarr` command. It is not necessary for development.

## Evidence collected

- OS release, kernel architecture, collector bitness, available boot-mode signal,
  RAM and privilege level.
- Mount IDs, parent IDs, device IDs, mount roots, filesystem classification,
  repeated-device and subtree flags. Remote data is not traversed and symlinks
  are not copied. Bind flags are candidates requiring review, not a transfer map.
- Block device metadata, filesystem identifiers and capacities; free capacity
  on recognized local mounts, without recursively scanning file contents.
- Current network interface metadata.
- On the source only: package metadata, systemd service/timer states, installed
  unit files, cron entry names/presence, and optional Docker metadata.
- Probe errors and explicit inventory limitations.

No application configuration, process command lines, environment variables,
mount sources or database contents are collected. Collection does not change
services, mounts, boot entries, network settings or disks. Commands are bounded
by a timeout; probes may be incomplete inside containers, restricted namespaces
or unprivileged sessions. A BIOS result cannot be inferred conclusively from an
absent EFI directory. Free capacity is per mount, **not additive** across bind
mounts/subvolumes. Installed binary architectures and disk topology still need
qualification.

## Development

The [RAM recovery VM lab](docs/RECOVERY_LAB.md) exercises a single virtual disk,
disk-to-RAM handover, SSH with the target unmounted, committed-checkpoint recovery
and missing-image fallback. It is a separate developer test; it does not install
a rescue environment on a real server or enable physical migration.

The [Ubuntu recovery rehearsal](docs/RECOVERY_JOBS.md) uses the packaged
application inside a full Ubuntu guest to prepare GRUB, reboot into RAM and
automatically reload a protected job. It uses generated test credentials and
does not contact the real source.

The [source connectivity rehearsal](docs/SOURCE_CONNECTIVITY_LAB.md) adds a
second disposable Ubuntu guest. `recovery check-source` uses the credentials
restored into RAM to run the read-only source collector, preserving probe errors
and keeping migration blocked when host trust or connectivity fails. The RAM
image includes OpenSSH; no running SSH agent or bootstrap filesystem is needed.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The tests use synthetic mount tables and mocked probes. They do not contact a
source server or manipulate host services. See the [implementation roadmap and
qualification gates](docs/ROADMAP.md) for the remaining work.
