# docky/commands/common.py
"""Helpers shared by the commands: spinners, project selection, shared messages."""
import sys
import time
from pathlib import Path
from ..utils import Colors, color
from .. import docker_api

def get_spinner(idx):
    chars = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
    return chars[idx % len(chars)]

def container_indicator(state):
    state = state.lower()
    if state == "running": return color("●", Colors.GREEN)
    if state == "exited": return color("✕", Colors.RED)
    if state in ("restarting", "created"): return color("↻", Colors.YELLOW)
    return color("○", Colors.YELLOW)

def no_projects_message():
    roots = ", ".join(str(r) for r in docker_api.scan_roots())
    return (
        f"\n{color('● DOCKY', Colors.BOLD + Colors.CYAN)}\n\n{color('  No Docker Compose projects found.', Colors.YELLOW)}\n"
        + color(f"  Docky checks every Compose project Docker is running, plus folders under: {roots}\n", Colors.DIM)
        + color("  Keep stacks elsewhere? Point Docky at them: export DOCKY_ROOT=/path/one:/path/two\n", Colors.DIM)
    )

def select_projects(projects, target):
    """
    Projects matching `target` -- a project name or its folder path.
    Returns (matches, error). Two projects can share a folder name in
    different places; a name that's ambiguous is refused rather than
    acting on both.
    """
    try:
        target_path = Path(target).expanduser().resolve()
    except OSError:
        target_path = None
    by_path = [p for p in projects if p["path"] and target_path and Path(p["path"]).resolve() == target_path]
    if by_path:
        return by_path[:1], None
    by_name = [p for p in projects if p["name"].lower() == target.lower()]
    if not by_name:
        return [], f"Project {target} not found. Run 'docky projects' to see what Docky found."
    if len(by_name) > 1:
        paths = "\n".join(f"    {p['path']}" for p in by_name)
        return [], f"'{target}' matches more than one project. Use its folder path instead:\n{paths}"
    return by_name, None

def print_stale(stale):
    if not stale:
        return
    print(color("! These projects have containers, but their compose files are gone (folder moved or deleted?):", Colors.YELLOW))
    for project in stale:
        print(color(f"    {project['name']:<18} expected {', '.join(str(f) for f in project['missing'])}", Colors.DIM))
    print(color("  Docky can't manage them until the files are back. If you moved the folder, add its parent to DOCKY_ROOT.", Colors.DIM))
    print()

def fetch_project_data(projects, executor):
    sys.stdout.write(f"\r{color('⠋', Colors.CYAN)} {color('Discovering containers...', Colors.DIM)}\033[K")
    sys.stdout.flush()
    futures = [executor.submit(docker_api.get_project_containers, p) for p in projects]
    idx = 0
    while not all(f.done() for f in futures):
        sys.stdout.write(f"\r{color(get_spinner(idx), Colors.CYAN)} {color('Discovering containers...', Colors.DIM)}\033[K")
        sys.stdout.flush()
        idx += 1
        time.sleep(0.08)
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()
    return [{"project": p, "containers": f.result()} for p, f in zip(projects, futures)]
