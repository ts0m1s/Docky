# docky/commands/status.py
"""Looking at what's running: status, projects, urls, top."""
import sys
import concurrent.futures
import shlex
from ..utils import Colors, color, run_command, get_system_metrics
from .. import docker_api
from .. import monitor
from .. import urls as urls_api
from .common import container_indicator, no_projects_message, select_projects, print_stale, fetch_project_data

def cmd_status():
    found = docker_api.discover_projects()
    projects, stale = found["projects"], found["stale"]
    if not projects:
        print_stale(stale)
        print(no_projects_message())
        return

    print()
    metrics = get_system_metrics()
    print(color("● DOCKY", Colors.BOLD + Colors.CYAN) + color("  ·  Server Status", Colors.DIM))
    print(f"\n  ○ Storage : {metrics['disk_str']}\n  ○ Memory  : {metrics['ram_str']}\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        project_data = fetch_project_data(projects, executor)
        
    total = sum(len(d["containers"]) for d in project_data)
    running = sum(1 for d in project_data for c in d["containers"] if c["state"].lower() == "running")
    broken = []

    for p_idx, data in enumerate(project_data):
        project, containers = data["project"], data["containers"]
        is_last_p = (p_idx == len(project_data) - 1)
        p_branch = "└─" if is_last_p else "├─"
        print(f"{p_branch} {color(project['name'], Colors.CYAN + Colors.BOLD)}")
        
        if not containers:
            print(f"{'   ' if is_last_p else '│  '}└─ {color('no containers', Colors.DIM)}")
            continue

        orphaned = {c["name"] for c in docker_api.find_orphaned_containers(containers)}
        for c_idx, container in enumerate(containers):
            is_last_c = (c_idx == len(containers) - 1)
            c_branch = "└─" if is_last_c else "├─"
            prefix = f"{'   ' if is_last_p else '│  '}{c_branch} "
            note = ""
            if container["name"] in orphaned:
                note = "  " + color("! network target is gone (no connectivity)", Colors.RED)
                broken.append((project, container))
            print(f"{prefix}{container_indicator(container['state'])} {container['short_name']}{note}")

    print(f"\n{color('●', Colors.GREEN)} {color(f'{running}/{total} containers running', Colors.DIM)}\n")
    if broken:
        print(color("! These containers share another container's network, but it was replaced or removed.", Colors.RED))
        print(color("  They report as running but cannot reach anything. Recreate them:", Colors.DIM))
        for project, container in broken:
            print(f"    {shlex.join(docker_api.compose_cmd(project, 'up', '-d', '--no-deps', '--force-recreate', container['service']))}")
        print()
    print_stale(stale)

def cmd_projects():
    """Where every project was found -- the quickest way to check discovery."""
    found = docker_api.discover_projects()
    projects, stale = found["projects"], found["stale"]
    print(f"\n{color('● DOCKY', Colors.BOLD + Colors.CYAN)} {color('  ·  Projects', Colors.DIM)}\n")
    if not projects and not stale:
        return print(no_projects_message())
    for project in projects:
        source = "running in Docker" if project["source"] == "docker" else "found on disk"
        name = project["name"].ljust(20)
        print(f"  {color('●', Colors.GREEN)} {color(name, Colors.CYAN + Colors.BOLD)} {project['path']}  {color(source, Colors.DIM)}")
        for f in project["files"]:
            print(color(f"      {f}", Colors.DIM))
    for project in stale:
        name = project["name"].ljust(20)
        print(f"  {color('!', Colors.RED)} {color(name, Colors.CYAN + Colors.BOLD)} {project['path']}  {color('compose files missing', Colors.RED)}")
    roots = ", ".join(str(r) for r in docker_api.scan_roots())
    print(color(f"\n  Scanned folders: {roots}  (set DOCKY_ROOT to change)\n", Colors.DIM))

def cmd_urls(target=None, check=False):
    """Where each service can be reached: domains from Traefik, LAN and Tailscale addresses from published ports."""
    print(f"\n{color('● DOCKY', Colors.BOLD + Colors.CYAN)} {color('  ·  Service URLs', Colors.DIM)}\n")
    projects = docker_api.find_projects()
    if target:
        projects, error = select_projects(projects, target)
        if error:
            return print(f"{color('!', Colors.RED)} {color(error, Colors.RED)}\n")

    all_containers = urls_api.inspect_all_containers()
    hosts = urls_api.host_addresses()

    groups = []
    for project in projects:
        name = project.get("project_name") or docker_api.sanitize_project_name(project["name"])
        members = [c for c in all_containers.values() if c["project"] == name]
        groups.append((project["name"], members))
    if not target:
        loose = [c for c in all_containers.values() if not c["project"]]
        if loose:
            groups.append(("(not in a Compose project)", loose))

    rows = []
    for title, members in groups:
        entries = []
        for c in sorted(members, key=lambda c: c["name"]):
            entries.append((c, urls_api.service_urls(c, all_containers, hosts)))
        rows.append((title, entries))

    results = {}
    if check:
        wanted = [u["url"] for _, entries in rows for _, info in entries for u in info["urls"]]
        sys.stdout.write(f"{color('⠋', Colors.CYAN)} {color(f'Checking {len(set(wanted))} URLs...', Colors.DIM)}\033[K")
        sys.stdout.flush()
        results = urls_api.check_urls(wanted)
        sys.stdout.write("\r\033[K")

    states = {}
    ok, out, _ = run_command(["docker", "ps", "-a", "--format", "{{.Names}}|{{.State}}"])
    for line in out.splitlines() if ok else []:
        n, _, st = line.partition("|")
        states[n] = st

    width = max((len(c["name"]) for _, entries in rows for c, _ in entries), default=10) + 2
    for p_idx, (title, entries) in enumerate(rows):
        last_p = p_idx == len(rows) - 1
        print(f"{'└─' if last_p else '├─'} {color(title, Colors.CYAN + Colors.BOLD)}")
        indent = "   " if last_p else "│  "
        if not entries:
            print(f"{indent}└─ {color('no containers (not started)', Colors.DIM)}")
            continue
        for c_idx, (c, info) in enumerate(entries):
            last_c = c_idx == len(entries) - 1
            branch, cont = ("└─", "   ") if last_c else ("├─", "│  ")
            stopped = states.get(c["name"], "running") != "running"
            label = c["name"].ljust(width)
            via = color(f"via {info['via']}", Colors.DIM) + "  " if info["via"] else ""
            if not info["urls"]:
                if info["via"]:
                    why = "no URL of its own on that network"
                elif info["other_ports"]:
                    why = f"no web URL · published: {', '.join(info['other_ports'])}"
                else:
                    why = "internal only (no published port or Traefik route)"
                print(f"{indent}{branch} {label}{via}{color(why, Colors.DIM)}")
                continue
            print(f"{indent}{branch} {label}{via}{color('(stopped)', Colors.YELLOW) if stopped else ''}".rstrip())
            for u in info["urls"]:
                mark = ""
                if check:
                    up, detail = results.get(u["url"], (False, "not checked"))
                    mark = f"{color('✓', Colors.GREEN)} " if up else f"{color('✗', Colors.RED)} "
                    tail = color(f"{u['kind']}  {detail if not up else ''}".rstrip(), Colors.DIM if up else Colors.RED)
                else:
                    tail = color(u["kind"], Colors.DIM)
                print(f"{indent}{cont}   {mark}{u['url']:<46} {tail}")
            if info["other_ports"]:
                print(color(f"{indent}{cont}   also published: {', '.join(info['other_ports'])}", Colors.DIM))
    print()
    if not check:
        print(color("  Domains come from Traefik labels; whether they resolve depends on your DNS/tunnel.", Colors.DIM))
        print(color("  Run 'docky urls --check' to test every URL.\n", Colors.DIM))
    else:
        down = sum(1 for up, _ in results.values() if not up)
        summary = f"{len(results) - down}/{len(results)} URLs reachable"
        print(f"{color('●', Colors.GREEN if not down else Colors.YELLOW)} {color(summary, Colors.DIM)}\n")

def cmd_top(sort="name"):
    if sort not in monitor.SORT_KEYS:
        return print(f"\n{color(f'! Unknown sort: {sort}', Colors.RED)}  Use one of: {', '.join(monitor.SORT_KEYS)}\n")
    projects = docker_api.find_projects()
    if not projects:
        return print(no_projects_message())
    monitor.run(projects, sort)
