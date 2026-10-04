#!/usr/bin/env python3
# docky.py

import difflib
import os
import shutil
import sys
from utils import Colors, color, run_command
import commands
import completion
import selfupdate

# The help text, and the source of the command list for tab completion.
COMMANDS = [
    ("status", "Show Docker projects, containers, and system metrics"),
    ("projects", "List every project Docky found and where it lives"),
    ("urls [name] [--check]", "Show where each service is reachable; --check tests every URL"),
    ("top [--sort cpu|mem]", "Live CPU, memory, network and disk use per container"),
    ("updates", "Check for image updates, showing old → new version"),
    ("upgrade [name] [--dry-run]", "Pull and recreate outdated containers, listing version changes"),
    ("rollback [name] [service]", "Undo the last upgrade (no args: list saved snapshots)"),
    ("sweep", "Find and safely clear ghost data & unused images"),
    ("orphans", "Find volumes belonging to deleted or renamed projects"),
    ("remove <name> [options]", "Remove a project: plan first, then containers & networks, optionally"),
    ("", "  --volumes, --images, --files (or --all); --dry-run, -y to skip prompts"),
    ("start <name|all>", "Start a specific project or 'all'"),
    ("stop <name|all>", "Stop a specific project or 'all'"),
    ("restart <name|all>", "Restart a specific project or 'all'"),
    ("self-update [--check]", "Update Docky itself to the latest version"),
    ("completion [zsh|bash]", "Print the tab-completion script (installed automatically)"),
    ("--version", "Show the installed Docky version"),
    ("help", "Show this help"),
]

# What tab completion offers after each command.
COMPLETION_SPEC = {
    "urls": {"args": [completion.PROJECTS], "flags": ["--check"]},
    "top": {"flags": ["--sort=cpu", "--sort=mem"]},
    "upgrade": {"args": [completion.PROJECTS], "flags": ["--dry-run"]},
    "rollback": {"args": [completion.PROJECTS, completion.SERVICES]},
    "remove": {"args": [completion.PROJECTS], "flags": ["--volumes", "--images", "--files", "--all", "--dry-run", "--yes"]},
    "start": {"args": [completion.PROJECTS_OR_ALL]},
    "stop": {"args": [completion.PROJECTS_OR_ALL]},
    "restart": {"args": [completion.PROJECTS_OR_ALL]},
    "self-update": {"flags": ["--check"]},
    "completion": {"args": [completion.SHELLS]},
}
ALIASES = {"rm": "remove", "update": "updates", "selfupdate": "self-update", "version": "--version"}

def command_names():
    return [usage.split()[0] for usage, _ in COMMANDS if usage]

def completion_commands():
    """[(name, short description)] for the completion menu."""
    return [(usage.split()[0], desc.split(";")[0].split(", optionally")[0]) for usage, desc in COMMANDS if usage]

def show_usage(full=False):
    """The command list; full=True (docky --help) adds the background notes and settings."""
    print(f"\n{color('● DOCKY', Colors.BOLD + Colors.CYAN)} {color(selfupdate.local_version() or '', Colors.DIM)}\n{color('Docker Server Manager', Colors.DIM)}\n")
    print(color("Usage:", Colors.BOLD) + "\n  docky <command> [target]\n")
    print(color("Commands:", Colors.BOLD))
    for cmd, desc in COMMANDS:
        if cmd == "help":
            continue
        print(f"  {color(f'{cmd:<28}', Colors.CYAN)} {desc}")
    if not full:
        print(f"\n{color('Run', Colors.DIM)} docky --help {color('for how projects are found, updates, tab completion and settings.', Colors.DIM)}\n")
        return
    print(f"\n{color('Projects:', Colors.BOLD)} found from Docker itself, wherever they live, plus folders in")
    print(f"  {color('DOCKY_ROOT', Colors.CYAN)} (':'-separated paths, default ~/docker). A path works as a target too.\n")
    print(f"{color('Updates:', Colors.BOLD)} Docky checks once a day for a newer version of itself and says so.")
    print(f"  Turn that off with {color('DOCKY_NO_UPDATE_CHECK=1', Colors.CYAN)}.\n")
    print(f"{color('Tab completion:', Colors.BOLD)} press Tab after 'docky' for commands, project names and flags.\n")
    print(color("Settings (environment variables):", Colors.BOLD))
    settings = [
        ("DOCKY_ROOT", "Folders to scan for stacks that were never started (default ~/docker)"),
        ("DOCKY_NO_UPDATE_CHECK=1", "Turn off the daily new-version notice"),
        ("DOCKY_REF", "What self-update follows: main (default), stable, or a branch"),
        ("DOCKY_NO_MODIFY_PATH=1", "At install: don't edit ~/.zshrc / ~/.bashrc, print the lines instead"),
    ]
    for name, desc in settings:
        print(f"  {color(f'{name:<28}', Colors.CYAN)} {desc}")
    print()

def unknown_command(cmd):
    guess = difflib.get_close_matches(cmd, command_names() + list(ALIASES), n=1, cutoff=0.6)
    if guess:
        suggestion = ALIASES.get(guess[0], guess[0])
        print(f"\n{color(f'! Unknown command: {cmd}', Colors.RED)}  Did you mean {color(f'docky {suggestion}', Colors.BOLD)}?\n")
    else:
        print(f"\n{color(f'! Unknown command: {cmd}', Colors.RED)}")
        show_usage()

def complete_values(args):
    """Hidden `docky __complete projects|services <project>`: one value per line, for the shell scripts."""
    import docker_api
    kind = args[0] if args else ""
    try:
        if kind == "projects":
            found = docker_api.discover_projects()
            names = {p["name"] for p in found["projects"] + found["stale"]}
            print("\n".join(sorted(names)))
        elif kind == "services" and len(args) > 1:
            state = docker_api.load_rollback_state()
            print("\n".join(sorted(state.get(args[1], {}))))
    except Exception:
        pass  # completion must never print a traceback into the prompt

def cmd_completion(args):
    if "--install" in args:
        results = completion.install(completion_commands(), COMPLETION_SPEC, ALIASES,
                                     modify_rc=os.environ.get("DOCKY_NO_MODIFY_PATH", "0") != "1")
        if "--quiet" in args:
            return
        for rc, status in results:
            if status == "added":
                print(f"{color('●', Colors.CYAN)} Tab completion enabled in {rc} (open a new terminal to use it)")
            elif status == "skipped":
                shell = "zsh" if rc.name == ".zshrc" else "bash"
                print(f"{color('!', Colors.YELLOW)} For tab completion, add this to {rc}:\n    {completion.source_line(shell)}")
        return
    shell = next((a for a in args if a in ("zsh", "bash")), None) or os.path.basename(os.environ.get("SHELL", "")) or "bash"
    if shell not in ("zsh", "bash"):
        return print(f"{color('! Tab completion is available for zsh and bash.', Colors.RED)}")
    script = completion.zsh_script if shell == "zsh" else completion.bash_script
    print(script(completion_commands(), COMPLETION_SPEC, ALIASES), end="")

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
        # None of these need Docker -- self-update must work even when Docker doesn't.
        if cmd == "__complete":
            return complete_values(sys.argv[2:])
        if cmd == "completion":
            return cmd_completion(sys.argv[2:])
        if cmd not in set(command_names()) | set(ALIASES) | {"-v", "-h", "--help"}:
            return unknown_command(cmd)  # before the Docker check: a typo shouldn't need Docker
        if cmd in ("--version", "-v", "version"):
            return selfupdate.cmd_version()
        if cmd in ("self-update", "selfupdate"):
            return selfupdate.cmd_self_update(check_only="--check" in sys.argv[2:])
        if cmd not in ("help", "-h", "--help") and not preflight():
            sys.exit(1)
        update_check = selfupdate.BackgroundCheck()
        if cmd in ("help", "-h", "--help"):
            return show_usage(full=True)
        if cmd == "status": commands.cmd_status()
        elif cmd == "projects": commands.cmd_projects()
        elif cmd == "urls":
            args = sys.argv[2:]
            target = next((a for a in args if not a.startswith("-")), None)
            commands.cmd_urls(target=target, check="--check" in args)
        elif cmd == "top":
            args = sys.argv[2:]
            sort = "name"
            for i, a in enumerate(args):
                if a.startswith("--sort="):
                    sort = a.split("=", 1)[1]
                elif a == "--sort":
                    sort = args[i + 1] if i + 1 < len(args) else ""
            commands.cmd_top(sort=sort.lower())
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
            unknown_command(cmd)

        notice = update_check.notice()
        if notice:
            print(color(f"↑ {notice}", Colors.YELLOW) + "\n")

    except KeyboardInterrupt:
        sys.stdout.write("\r\033[K\n")
        print(color("Aborted by user.", Colors.RED))
        sys.exit(130)

if __name__ == "__main__":
    main()