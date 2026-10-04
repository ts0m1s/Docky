# docky/commands/lifecycle.py
"""start / stop / restart."""
import sys
import time
import concurrent.futures
from ..utils import Colors, color, run_command
from .. import docker_api
from .common import get_spinner, no_projects_message, select_projects

WORDS = {"start": ("starting", "started"), "stop": ("stopping", "stopped"), "restart": ("restarting", "restarted")}

def cmd_lifecycle(action, target):
    doing, done = WORDS[action]
    projects = docker_api.find_projects()
    if not projects: return print(no_projects_message())
    
    if target.lower() != "all":
        projects, error = select_projects(projects, target)
        if error:
            return print(f"\n{color('!', Colors.RED)} {color(error, Colors.RED)}\n")
    else:
        print(f"\n{color(f'! WARNING: You are about to {action} ALL {len(projects)} projects.', Colors.YELLOW)}")
        try: choice = input(color("[?] Proceed? (y/N): ", Colors.BOLD)).strip().lower()
        except EOFError: choice = 'n'
        if choice not in ['y', 'yes']: return print(f"\n{color('Aborted.', Colors.DIM)}\n")

    print(f"\n{color('● DOCKY', Colors.BOLD + Colors.CYAN)} {color(f'  ·  {doing.capitalize()} Projects', Colors.DIM)}\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        # `compose start` only wakes existing containers. A project found on
        # disk has none yet, so starting it means creating them.
        def lifecycle_args(p):
            return ("up", "-d") if action == "start" and p["source"] == "folder" else (action,)
        futures = [executor.submit(run_command, docker_api.compose_cmd(p, *lifecycle_args(p))) for p in projects]
        success_c, error_c = 0, 0
        for p_idx, project in enumerate(projects):
            name = project["name"]
            is_last = (p_idx == len(projects) - 1)
            prefix = f"{'└─' if is_last else '├─'} "
            future = futures[p_idx]
            idx = 0
            while not future.done():
                sys.stdout.write(f"\r{prefix}{color(get_spinner(idx), Colors.CYAN)} {name:<20} {color(f'{doing}...', Colors.YELLOW)}\033[K")
                sys.stdout.flush()
                idx += 1; time.sleep(0.08)
            succ, _, err = future.result()
            if succ:
                success_c += 1
                print(f"\r{prefix}{color('✓', Colors.GREEN)} {name:<20} {color(done, Colors.GREEN)}\033[K")
            else:
                error_c += 1
                print(f"\r{prefix}{color('✕', Colors.RED)} {name:<20} {color(f'failed', Colors.RED)}\033[K\n  {color(err, Colors.DIM)}")
    print()
