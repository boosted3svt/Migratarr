# Start here after reinstalling or opening a new chat

The latest saved development work is on `wip/os-interruption-recovery`.
Main retains the last tested implementation; the checkpoint contains unfinished
whole-OS interruption recovery, not a qualified release.

```sh
git fetch origin
git switch --track origin/wip/os-interruption-recovery
```

If already on that branch, use `git pull --ff-only`. Read its `START_HERE.md`,
then `docs/SESSION_HANDOFF.md` and `docs/FRESH_MACHINE.md` before continuing.

[Open the full handoff](https://github.com/boosted3svt/Migratarr/blob/wip/os-interruption-recovery/docs/SESSION_HANDOFF.md)
and [fresh-machine instructions](https://github.com/boosted3svt/Migratarr/blob/wip/os-interruption-recovery/docs/FRESH_MACHINE.md).

Suggested instruction for a new coding assistant:

> Read START_HERE.md and follow the checkpoint handoff. Continue the unfinished
> VM-only interruption-recovery milestone. Keep real machines unchanged and
> defer the recorded sudo/authentication issue. Do not depend on the old chat
> or private lab artifacts still existing after a reinstall.

All development context is in Git. Private connection profiles, SSH keys,
inventories, VM images and raw reports are excluded; privately back up what you
want to retain, or recreate/re-enter it as described in the fresh-machine guide.
