"""Guided read-only assessment, using local JSON or explicit SSH source access."""

import json
import os
from pathlib import Path
import tempfile

from .assessment import assess, validate, normalize_ignored_mounts
from .inventory import collect
from .remote import collect_remote
from .planning import build_plan, format_plan


def load_inventory(path, role):
    with open(Path(path).expanduser(), encoding="utf-8") as handle:
        data = json.load(handle)
    validate(data, role)
    return data


def write_private(path, content):
    """Create a new report without replacing existing files or following links."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def dump_report(path, report):
    write_private(path, json.dumps(report, indent=2, sort_keys=True) + "\n")


def printable(value):
    # Reports can contain service names and imported filesystem paths. Escape
    # control characters rather than letting inventory content control a terminal.
    return json.dumps(str(value), ensure_ascii=True)[1:-1]


def summarize(inventory, assessment=None):
    system = inventory["system"]
    lines = ["Migratarr assessment wizard", "", "Status: BLOCKED — migration is not implemented or qualified.",
             "", "Destination evidence:",
             f"  OS: {printable(system['os'].get('PRETTY_NAME', system['os'].get('ID', 'unknown')))}",
             f"  Architecture: {printable(system['architecture'])}; collector: {system['userspace_bits']} bit",
             f"  Boot signal: {printable(system.get('boot_mode', 'unknown'))}",
             f"  Probe errors: {len(inventory['errors'])}"]
    for mount in inventory["mounts"]:
        if mount["kind"] == "local":
            flags = " (mapping review required)" if mount["shared_device"] or mount["subtree_or_bind_candidate"] else ""
            lines.append(f"  Local mount: {printable(mount['target'])} [{printable(mount['filesystem'])}]{flags}")
    lines.append(f"  Remote mounts excluded from data scanning: {sum(m['kind'] == 'remote' for m in inventory['mounts'])}")
    memory = system.get("memory_bytes")
    if isinstance(memory, int):
        lines.append(f"  RAM: {memory / (1024 ** 3):.1f} GiB")
    for space in inventory["space"]:
        lines.append(f"  Available space at {printable(space['target'])}: {space['available_bytes'] / (1024 ** 3):.2f} GiB (per filesystem; do not sum shared mounts)")
    lines.append("  Scope: destination infrastructure; existing applications will be replaced.")
    if inventory["errors"]:
        lines.extend(["", "Failed or inaccessible probes:"])
        for error in inventory["errors"]:
            lines.append(f"  {printable(error.get('probe', 'unknown'))}: {printable(error.get('error', 'unknown'))}")
    if assessment is None:
        lines.extend(["", "Next: collect a source inventory on the source machine, then run this",
                      "wizard with --source /path/to/inventory-source.json to compare offline.",
                      "Or run wizard --source-host HOST --source-user USER for read-only SSH collection.",
                      "Use the same checkout there with:",
                      "  PYTHONPATH=src python3 -m migratarr.cli inventory --role source",
                      "Save stdout privately to inventory-source.json; transfer it privately."])
    else:
        source = assessment.get("source_summary", {})
        source_system = source.get("system", {})
        lines.extend(["", "Source evidence:",
                      "  OS: " + printable(source_system.get("os", {}).get("PRETTY_NAME", "unknown")),
                      "  Python: " + printable(source_system.get("python_version", "unknown")),
                      "  Boot signal: " + printable(source_system.get("boot_mode", "unknown")),
                      "  Probe errors: " + str(len(source.get("probe_errors", [])))])
        for field, label in (("package_count", "Installed packages"), ("service_count", "System services"),
                             ("active_service_count", "Active system services"), ("timer_count", "System timers")):
            if source.get(field) is not None:
                lines.append("  " + label + ": " + str(source[field]))
        survey = source.get("server_survey") or {}
        for field, label in (("local_users", "Local users"), ("local_groups", "Local groups"), ("fstab", "Configured filesystem entries")):
            if isinstance(survey.get(field), list):
                lines.append("  " + label + ": " + str(len(survey[field])))
        lines.append("  Migration scope: entire local installation, including all software and state.")
        containers = (source.get("docker") or {}).get("containers")
        if isinstance(containers, list):
            running = sum(container.get("State") == "running" for container in containers)
            lines.append(f"  Docker containers: {len(containers)} total, {running} running at collection time")
        for row in assessment.get("mount_review", {}).get("source", []):
            if row["kind"] == "local":
                lines.append("  Local mount: " + printable(row["target"]) + " [" + printable(row["filesystem"]) + "]")
        for path in assessment.get("ignored_source_mounts", []):
            lines.append("  Source mount excluded by operator: " + printable(path))
        for error in source.get("probe_errors", []):
            lines.append("  Incomplete source probe: " + printable(error.get("probe", "unknown")) + ": " +
                         printable(error.get("error", "unknown")) +
                         (" at " + printable(error["path"]) if "path" in error else ""))
        lines.extend(["", "Comparison blockers:"])
        for finding in assessment["findings"]:
            lines.append(f"  {printable(finding['code'])}: {printable(finding['message'])}")
    lines.extend(["", "Still required: complete application/data and writer discovery, reviewed",
                  "filesystem mapping, and a VM-proven single-disk RAM boot/recovery workflow.",
                  "Assessment does not change system configuration or execute migration."])
    return "\n".join(lines) + "\n"


def run(output_dir, source_path=None, interactive=True, *, source_host=None,
        source_user=None, ssh_options=None, ignored_source_mounts=(), destination_path=None):
    ignored_source_mounts = normalize_ignored_mounts(ignored_source_mounts)
    ssh_options = ssh_options or {}
    if source_host and source_path:
        raise ValueError("choose a saved source inventory or an SSH source")
    if not source_host and (source_user or ssh_options.get("sudo") or ssh_options.get("password_auth") or ssh_options.get("inspect_docker") or ssh_options.get("identity_file") or ssh_options.get("known_hosts") or ssh_options.get("port", 22) != 22):
        raise ValueError("SSH options require --source-host")
    if interactive and source_path is None and not source_host:
        print("Migratarr will collect read-only metadata from this destination.")
        print("An existing source inventory is optional. Use --source-host HOST for SSH collection.")
        try:
            source_path = input("Source inventory JSON path (Enter to skip): ").strip() or None
        except EOFError:
            source_path = None
    if source_host:
        if not source_user and interactive:
            source_user = input("Source SSH username: ").strip()
        if not source_user:
            raise ValueError("--source-user is required with --source-host")
        print("Collecting source metadata over verified SSH (read-only)...")
        source = collect_remote(source_host, source_user, **ssh_options)
    else:
        source = load_inventory(source_path, "source") if source_path is not None else None
    if destination_path:
        print("Loading saved destination evidence...")
        destination = load_inventory(destination_path, "destination")
    else:
        print("Collecting destination metadata...")
        destination = collect("destination")
    validate(destination, "destination")
    assessment = assess(source, destination, ignored_source_mounts=ignored_source_mounts) if source is not None else None
    plan = build_plan(source, destination, ignored_source_mounts=ignored_source_mounts) if source is not None else None
    parent = Path(output_dir).expanduser()
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = Path(tempfile.mkdtemp(prefix="assessment-", dir=parent))
    dump_report(directory / "inventory-destination.json", destination)
    if source is not None:
        dump_report(directory / "inventory-source.json", source)
    if assessment is not None:
        dump_report(directory / "assessment.json", assessment)
    if plan is not None:
        dump_report(directory / "plan.json", plan)
        write_private(directory / "plan.txt", format_plan(plan))
    summary = summarize(destination, assessment)
    write_private(directory / "summary.txt", summary)
    print()
    print(summary, end="")
    print(f"\nPrivate reports: {printable(directory.resolve())}")
    # No completed wizard run currently constitutes a migration-ready outcome.
    return 2
