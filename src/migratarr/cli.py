import argparse
import json
from pathlib import Path
import sys

from .assessment import assess
from .inventory import collect
from .remote import collect_remote
from .wizard import load_inventory, printable, run
from .planning import build_plan
from .profiles import load_profile, setup_profile
from .recovery import create_job, inspect_job, check_source
from .recovery_bundle import build_vm_bundle
from .recovery_vm import prepare_vm, boot_vm, resume_vm
from .transfer import transfer_fixture
from .cleanup import preview_cleanup, apply_cleanup
from .host_trust import ensure_host_trust


DEFAULT_PROFILE = "~/.config/migratarr/config.json"


def run_profile(path, non_interactive=False):
    profile_path = Path(path).expanduser()
    if not profile_path.exists():
        if non_interactive or not sys.stdin.isatty():
            raise ValueError("No saved profile. Run migratarr setup --config PATH in a terminal first")
        profile = setup_profile(profile_path)
    else:
        profile = load_profile(profile_path)
    source = profile["source"]
    destination = profile["destination"]
    options = {}
    if source["mode"] == "ssh":
        if non_interactive and source["authentication"] == "password":
            raise ValueError("Non-interactive runs require key/agent authentication; password mode needs a terminal")
        if not non_interactive and sys.stdin.isatty():
            ensure_host_trust(source)
        options = {"source_host": source["host"], "source_user": source["user"],
                   "ssh_options": {"port": source["port"], "identity_file": source.get("identity_file"),
                                   "known_hosts": source.get("known_hosts"), "sudo": source["sudo"],
                                   "inspect_docker": source["inspect_docker"],
                                   "password_auth": source["authentication"] == "password"}}
    else:
        options["source_path"] = source["path"]
    if destination["mode"] == "inventory":
        options["destination_path"] = destination["path"]
    print("Using saved profile: " + printable(profile_path.resolve()))
    return run(profile["output_dir"], interactive=False,
               ignored_source_mounts=profile["ignored_source_mounts"], **options)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["run"]
    parser = argparse.ArgumentParser(prog="migratarr", description="Standalone whole-server assessment and planning; migration execution is not implemented")
    commands = parser.add_subparsers(dest="command", required=True)
    recovery = commands.add_parser("recovery", help="Saved recovery rehearsal jobs; physical boot installation and migration are disabled")
    recovery_commands = recovery.add_subparsers(dest="recovery_command", required=True)
    create = recovery_commands.add_parser("create", help="Save a durable recovery-only job; does not alter boot settings")
    create.add_argument("--config", required=True)
    create.add_argument("--plan", required=True)
    create.add_argument("--state-dir", required=True)
    status = recovery_commands.add_parser("status", help="Validate and inspect an existing recovery job")
    status.add_argument("--job", required=True)
    source_check = recovery_commands.add_parser("check-source", help="Read-only source inventory using credentials restored with a recovery job")
    source_check.add_argument("--runtime-dir", default="/run/migratarr")
    transfer = recovery_commands.add_parser("transfer-fixture", help="Disposable QEMU guests only: bounded fixture copy onto the mounted target disk")
    transfer.add_argument("--vm-token", required=True)
    transfer.add_argument("--bwlimit", type=int, default=0, help="Fixture test rate limit in KiB/s; zero is unlimited")
    transfer.add_argument("--root-policy", action="store_true", help="Apply root policy inside the fixed policy-case fixture; requires a seeded sandbox")
    for name in ("cleanup-preview-vm", "cleanup-apply-vm"):
        cleanup = recovery_commands.add_parser(name, help="Disposable VMs only: exact-preview cleanup inside the bounded policy sandbox")
        cleanup.add_argument("--vm-token", required=True)
        if name == "cleanup-apply-vm":
            cleanup.add_argument("--preview-sha256", required=True)
    bundle = recovery_commands.add_parser("build-vm-image", help="Build a self-contained RAM image for a disposable VM rehearsal")
    bundle.add_argument("--output", required=True)
    bundle.add_argument("--kernel", required=True)
    bundle.add_argument("--kernel-version", required=True)
    bundle.add_argument("--application", required=True)
    bundle.add_argument("--management-public-key", required=True)
    bundle.add_argument("--vm-token", required=True)
    bundle.add_argument("--extra-network", action="append", default=[], metavar="INTERFACE=IPv4/CIDR",
                        help="Additional directly connected IPv4 interface in the VM RAM image; repeatable")
    for name in ("prepare-vm", "boot-vm", "resume"):
        action = recovery_commands.add_parser(name, help="Disposable QEMU guests only; no physical server boot changes")
        action.add_argument("--job", required=True)
        action.add_argument("--vm-token", required=True)
        if name == "prepare-vm":
            action.add_argument("--bundle", required=True)
    setup = commands.add_parser("setup", help="Save source connection and planning inputs; never saves passwords")
    setup.add_argument("--config", default=DEFAULT_PROFILE)
    execute = commands.add_parser("run", help="Collect and plan from a saved profile without an LLM")
    execute.add_argument("--config", default=DEFAULT_PROFILE)
    execute.add_argument("--non-interactive", action="store_true", help="Do not prompt or start setup; key/agent auth required for live source access")
    inventory = commands.add_parser("inventory", help="Inspect this machine; JSON to stdout")
    inventory.add_argument("--role", choices=("source", "destination"), required=True)
    inventory.add_argument("--inspect-docker", action="store_true", help="Source role only: query metadata through the local Docker daemon")
    assessment = commands.add_parser("assess", help="Compare saved inventories; always blocked until execution is implemented")
    assessment.add_argument("--source", required=True)
    assessment.add_argument("--destination", required=True)
    plan = commands.add_parser("plan", help="Build a blocked whole-server draft plan from saved evidence; JSON only, no execution")
    plan.add_argument("--source", required=True)
    plan.add_argument("--destination", required=True)
    remote = commands.add_parser("source-ssh", help="Collect a source inventory over verified SSH; JSON to stdout")
    remote.add_argument("--host", required=True, help="Source hostname or IP; no SSH config alias expansion")
    remote.add_argument("--user", required=True, help="SSH username")
    remote.add_argument("--inspect-docker", action="store_true", help="Include optional source Docker metadata")
    wizard = commands.add_parser("wizard", help="Guide a read-only destination assessment and save private reports")
    wizard.add_argument("--output-dir", default="reports", help="Parent directory for a new private assessment folder")
    source_options = wizard.add_mutually_exclusive_group()
    source_options.add_argument("--source", help="Existing source inventory JSON; compare offline")
    source_options.add_argument("--source-host", help="Connect to this source over SSH for read-only collection")
    wizard.add_argument("--source-user", help="SSH username for --source-host")
    wizard.add_argument("--inspect-source-docker", action="store_true", help="Include optional Docker metadata when collecting over SSH")
    wizard.add_argument("--non-interactive", action="store_true", help="Skip the optional source-path prompt")
    for command in (assessment, plan, wizard):
        command.add_argument("--ignore-source-mount", action="append", default=[], help="Exclude this source mount and nested mounts from assessment review; repeatable, metadata only")
    for command in (remote, wizard):
        command.add_argument("--ssh-port", type=int, default=22)
        command.add_argument("--identity-file", help="Local SSH identity path; private key contents are never collected")
        command.add_argument("--known-hosts", help="Existing verified known_hosts file; defaults to ~/.ssh/known_hosts")
        command.add_argument("--sudo", action="store_true", help="Run source probes through sudo -n; never prompt for a sudo password")
        command.add_argument("--password-auth", action="store_true", help="Let OpenSSH prompt privately for the source password in this terminal")
    args = parser.parse_args(argv)
    try:
        if args.command == "recovery":
            if args.recovery_command == "create":
                result = create_job(args.config, args.plan, args.state_dir)
            elif args.recovery_command == "status":
                result = inspect_job(args.job)
            elif args.recovery_command == "check-source":
                result = check_source(args.runtime_dir)
            elif args.recovery_command == "transfer-fixture":
                result = transfer_fixture(args.vm_token, args.bwlimit, root_policy=args.root_policy)
            elif args.recovery_command == "cleanup-preview-vm":
                result = preview_cleanup(args.vm_token)
            elif args.recovery_command == "cleanup-apply-vm":
                result = apply_cleanup(args.vm_token, args.preview_sha256)
            elif args.recovery_command == "build-vm-image":
                result = build_vm_bundle(args.output, args.kernel, args.kernel_version, args.application,
                                         Path(args.management_public_key).read_text(), args.vm_token,
                                         extra_networks=args.extra_network)
            elif args.recovery_command == "prepare-vm":
                result = prepare_vm(args.job, str(Path(args.bundle) / "kernel"), str(Path(args.bundle) / "initrd.img"), args.vm_token)
            elif args.recovery_command == "boot-vm":
                result = boot_vm(args.job, args.vm_token)
            else:
                result = resume_vm(args.job, args.vm_token)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 2 if result.get("status") in ("source_incomplete", "blocked", "interrupted") or result.get("phase") in ("failed", "interrupted") else 0
        if args.command == "setup":
            if not sys.stdin.isatty():
                raise ValueError("Setup requires a terminal; saved JSON profiles can also be supplied to migratarr run")
            setup_profile(Path(args.config).expanduser())
            print("Saved profile: " + printable(Path(args.config).expanduser().resolve()))
            print("Run migratarr run --config with this path to collect and generate a draft plan.")
            return 0
        if args.command == "run":
            return run_profile(args.config, args.non_interactive)
        if args.command == "wizard":
            return run(args.output_dir, args.source,
                       interactive=not args.non_interactive and sys.stdin.isatty(),
                       source_host=args.source_host, source_user=args.source_user,
                       ignored_source_mounts=args.ignore_source_mount,
                       ssh_options={"port": args.ssh_port, "identity_file": args.identity_file,
                                    "known_hosts": args.known_hosts, "sudo": args.sudo,
                                    "password_auth": args.password_auth,
                                    "inspect_docker": args.inspect_source_docker})
        if args.command == "source-ssh":
            result = collect_remote(args.host, args.user, port=args.ssh_port,
                                    identity_file=args.identity_file, known_hosts=args.known_hosts,
                                    sudo=args.sudo, inspect_docker=args.inspect_docker,
                                    password_auth=args.password_auth)
            code = 2 if result["errors"] else 0
        elif args.command == "inventory":
            result = collect(args.role, inspect_docker=args.inspect_docker)
            code = 2 if result["errors"] else 0
        else:
            source = load_inventory(args.source, "source")
            destination = load_inventory(args.destination, "destination")
            result = (build_plan if args.command == "plan" else assess)(source, destination, ignored_source_mounts=args.ignore_source_mount)
            code = 2
        print(json.dumps(result, indent=2, sort_keys=True))
        return code
    except (OSError, ValueError, TypeError, KeyError, EOFError) as exc:
        print(f"migratarr: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("migratarr: assessment cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
