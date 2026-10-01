#!/usr/bin/env python3
# docky.py

import shutil
import sys
from utils import Colors, color, run_command
import commands
import selfupdate

def show_usage():
    print(f"\n{color('● DOCKY', Colors.BOLD + Colors.CYAN)}\n{color('Docker Server Manager', Colors.DIM)}\n")
    print(color("Usage:", Colors.BOLD) + "\n  docky <command> [target]\n")
    print(color("Commands:", Colors.BOLD))
    cmds = [
        ("status", "Show Docker projects, containers, and system metrics"),
        ("projects", "List every project Docky found and where it lives"),
        ("top", "Show real-time CPU and RAM usage mapped to your projects"),
        ("updates", "Check for available image updates"),
        ("upgrade [name] [--dry-run]", "Pull and recreate outdated containers (all, or one project)"),
        ("rollback [name] [service]", "Undo the last upgrade (no args: list saved snapshots)"),
        ("sweep", "Find and safely clear ghost data & unused images"),
        ("orphans", "Find volumes belonging to deleted or renamed projects"),
        ("remove <name> [options]", "Remove a project: plan first, then containers & networks, optionally"),
        ("", "  --volumes, --images, --files (or --all); --dry-run, -y to skip prompts"),
        ("start <name|all>", "Start a specific project or 'all'"),
        ("stop <name|all>", "Stop a specific project or 'all'"),
        ("restart <name|all>", "Restart a specific project or 'all'"),
        ("self-update [--check]", "Update Docky itself to the latest version"),
        ("--version", "Show the installed Docky version"),
    ]
    for cmd, desc in cmds:
        print(f"  {color(f'{cmd:<28}', Colors.CYAN)} {desc}")
    print(f"\n{color('Projects:', Colors.BOLD)} found from Docker itself, wherever they live, plus folders in")
    print(f"  {color('DOCKY_ROOT', Colors.CYAN)} (':'-separated paths, default ~/docker). A path works as a target too.\n")
    print(f"{color('Updates:', Colors.BOLD)} Docky checks once a day for a newer version of itself and says so.")
    print(f"  Turn that off with {color('DOCKY_NO_UPDATE_CHECK=1', Colors.CYAN)}.\n")

def preflight():
    """Fail with a readable message if Docker isn't installed or reachable."""
    if shutil.which("docker") is None:
        print(f"\n{color('! Docker was not found in PATH.', Colors.RED)}\n  Install Docker first: {color('https://docs.docker.com/engine/install/', Colors.DIM)}\n")
        return False
    ok, _, err = run_command(["docker", "info", "--format", "{{.ServerVersion}}"])
    if not ok:
        hint = "You may need to add your user to the 'docker' group, or use sudo." if "permission denied" in err.lower() else "Is the Docker daemon running?"
        print(f"\n{color('! Cannot talk to the Docker daemon.', Colors.RED)}\n  {color(hint, Colors.DIM)}\n")
        return False
    return True

def main():
    try:
        if len(sys.argv) < 2:
            return show_usage()

        cmd = sys.argv[1].lower()
        # Neither needs Docker -- self-update must work even when Docker doesn't.
        if cmd in ("--version", "-v", "version"):
            return selfupdate.cmd_version()
        if cmd in ("self-update", "selfupdate"):
            return selfupdate.cmd_self_update(check_only="--check" in sys.argv[2:])
        if cmd not in ("help", "-h", "--help") and not preflight():
            sys.exit(1)
        update_check = selfupdate.BackgroundCheck()
        if cmd in ("help", "-h", "--help"):
            return show_usage()
        if cmd == "status": commands.cmd_status()
        elif cmd == "projects": commands.cmd_projects()
        elif cmd == "top": commands.cmd_top()
        elif cmd in ("updates", "update"): commands.cmd_updates(is_upgrade=False)
        elif cmd == "upgrade":
            args = sys.argv[2:]
            dry_run = any(a in ("--dry-run", "-n") for a in args)
            target = next((a for a in args if not a.startswith("-")), None)
            commands.cmd_updates(is_upgrade=True, target=target, dry_run=dry_run)
        elif cmd == "rollback":
            args = [a for a in sys.argv[2:] if not a.startswith("-")]
            commands.cmd_rollback(*(args[:2]))
        elif cmd == "sweep": commands.cmd_sweep()
        elif cmd == "orphans": commands.cmd_orphans()
        elif cmd in ("remove", "rm"):
            args = sys.argv[2:]
            flags = {a for a in args if a.startswith("-")}
            target = next((a for a in args if not a.startswith("-")), None)
            unknown = flags - {"--volumes", "--images", "--files", "--all", "--dry-run", "-n", "-y", "--yes"}
            if not target or unknown:
                problem = f"Unknown option: {', '.join(sorted(unknown))}" if unknown else "Missing target."
                print(f"\n{color('! ' + problem, Colors.RED)}\nUsage: {color('docky remove <project_name|path> [--volumes] [--images] [--files] [--all] [--dry-run] [-y]', Colors.BOLD)}\n")
            else:
                everything = "--all" in flags
                commands.cmd_remove(
                    target,
                    volumes=everything or "--volumes" in flags,
                    images=everything or "--images" in flags,
                    files=everything or "--files" in flags,
                    dry_run=bool(flags & {"--dry-run", "-n"}),
                    assume_yes=bool(flags & {"-y", "--yes"}),
                )
        elif cmd in ("start", "stop", "restart"):
            if len(sys.argv) < 3:
                print(f"\n{color('! Missing target.', Colors.RED)}\nUsage: {color(f'docky {cmd} <project_name|all>', Colors.BOLD)}\n")
            else:
                commands.cmd_lifecycle(cmd, sys.argv[2])
        else:
            print(f"\n{color(f'! Unknown command: {cmd}', Colors.RED)}")
            show_usage()

        notice = update_check.notice()
        if notice:
            print(color(f"↑ {notice}", Colors.YELLOW) + "\n")

    except KeyboardInterrupt:
        sys.stdout.write("\r\033[K\n")
        print(color("Aborted by user.", Colors.RED))
        sys.exit(130)

if __name__ == "__main__":
    main()