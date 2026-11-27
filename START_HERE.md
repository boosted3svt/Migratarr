# Start a new development session here

This repository contains the development context; the old chat is not required.
The unfinished work is on **`wip/os-interruption-recovery`**. `main` remains the
last tested milestone. Read these in order:

1. [Current session handoff](docs/SESSION_HANDOFF.md): exact state, preferences,
   validation, pending work and what must not be assumed.
2. [Fresh-machine setup and recovery](docs/FRESH_MACHINE.md): rebuild development
   tools/assets after reinstalling the machine or Codex.
3. [Current roadmap](docs/ROADMAP.md) and [whole-OS lab](docs/WHOLE_OS_LAB.md).
4. [Historical requirements](docs/MIGRATARR_HANDOFF.md), with the clarifications
   in the current handoff taking precedence over its early proposals.

To retrieve this checkpoint on a fresh machine:

```sh
git clone https://github.com/boosted3svt/Migratarr.git
cd Migratarr
git fetch origin
git switch --track origin/wip/os-interruption-recovery
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

If the branch is already checked out, use `git pull --ff-only`. Do not merge the
checkpoint into `main` until its interruption rehearsal is implemented and passes.

The intended next instruction for a new coding assistant is:

> Read START_HERE.md and the linked session handoff. Continue the unfinished
> whole-OS interruption-recovery milestone in disposable VMs. Keep the real
> machines unchanged, defer the recorded sudo/authentication issue, and work in
> meaningful batches with focused validation. Do not rely on the old chat or on
> private artifacts still existing after a reinstall.

There are no repository-specific agents/skills that must be recovered from the
old Codex installation. Standard Python and Linux/QEMU tooling are sufficient.
New Codex sessions must use whatever execution permissions and GitHub connection
are actually available; previous credentials or app authorizations are not
implied by this document.
