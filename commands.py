# commands.py
import sys
import time
import re
import concurrent.futures
import shlex
from pathlib import Path
from utils import Colors, color, run_command, get_system_metrics, parse_pct, render_bar
import docker_api
import urls as urls_api
import versions

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


def render_top_frame(project_data, stats_map, metrics):
    lines = []
    lines.append(
        color("● DOCKY", Colors.BOLD + Colors.CYAN)
        + color("  ·  Resource Monitor", Colors.DIM)
        + color("   (Ctrl+C to exit)", Colors.DIM)
    )
    lines.append("")
    lines.append(f"  ○ Storage : {metrics['disk_str']}")
    lines.append(f"  ○ Memory  : {metrics['ram_str']}")
    lines.append("")

    for p_idx, data in enumerate(project_data):
        project, containers = data["project"], data["containers"]
        is_last_p = (p_idx == len(project_data) - 1)
        lines.append(f"{'└─' if is_last_p else '├─'} {color(project['name'], Colors.CYAN + Colors.BOLD)}")

        if not containers:
            lines.append(f"{'   ' if is_last_p else '│  '}└─ {color('no containers', Colors.DIM)}")
            continue

        for c_idx, container in enumerate(containers):
            is_last_c = (c_idx == len(containers) - 1)
            prefix = f"{'   ' if is_last_p else '│  '}{'└─' if is_last_c else '├─'} "
            name = container["short_name"]
            state = container["state"].lower()

            if state == "running" and container["name"] in stats_map:
                s = stats_map[container["name"]]
                cpu_val = parse_pct(s["cpu"])
                mem_val = parse_pct(s["mem_pct"])

                cpu_color = Colors.RED if cpu_val > 50 else (Colors.YELLOW if cpu_val > 10 else Colors.GREEN)
                mem_color = Colors.RED if mem_val > 80 else (Colors.YELLOW if mem_val > 50 else Colors.GREEN)

                cpu_str = color(f"{cpu_val:>5.1f}%", cpu_color)
                mem_str = color(f"{mem_val:>5.1f}%", mem_color)
                mem_used_str = color(f"({s['mem_used']})", Colors.DIM)

                row = (
                    f"{prefix}{container_indicator(state)} {name:<18} "
                    f"CPU {color(render_bar(cpu_val), cpu_color)} {cpu_str}  "
                    f"RAM {color(render_bar(mem_val), mem_color)} {mem_str} {mem_used_str:<20}  "
                    f"NET {color(s['net'], Colors.CYAN)}  "
                    f"IO {color(s['blk'], Colors.CYAN)}"
                )
                lines.append(row)
            else:
                lines.append(f"{prefix}{container_indicator(state)} {name:<18} {color('offline', Colors.DIM)}")

    lines.append("")
    return "\n".join(lines)


def cmd_top():
    projects = docker_api.find_projects()
    if not projects:
        return print(no_projects_message())

    print(f"\n{color('● DOCKY', Colors.BOLD + Colors.CYAN)} {color('  ·  Resource Monitor', Colors.DIM)}\n")

    # Alternate screen buffer: same trick htop/less use. The live
    # view repaints in place instead of spamming scrollback, and
    # the terminal is restored to whatever it showed before on exit.
    sys.stdout.write("\033[?1049h\033[H")
    sys.stdout.write(color("  Loading…", Colors.DIM))
    sys.stdout.flush()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            while True:
                # docker stats --no-stream alone costs the better
                # part of a second (Docker needs two cgroup samples
                # spaced apart to compute a CPU%), so it has to run
                # alongside the per-project container lookups, not
                # before them, or their costs just add up serially.
                stats_future = executor.submit(docker_api.fetch_stats)
                container_futures = [executor.submit(docker_api.get_containers_light, p) for p in projects]

                metrics = get_system_metrics()
                project_data = [{"project": p, "containers": f.result()} for p, f in zip(projects, container_futures)]
                stats_map = stats_future.result()

                frame = render_top_frame(project_data, stats_map, metrics)

                # Move cursor home, draw the new frame, then clear
                # anything left over from a longer previous frame.
                sys.stdout.write("\033[H" + frame + "\033[J")
                sys.stdout.flush()

                time.sleep(1.5)
    except KeyboardInterrupt:
        # Ctrl+C is the normal, expected way to close a live monitor
        # (same as htop/less) -- not an abort mid-action like it
        # would be during upgrade/sweep. Handle it here so it exits
        # quietly instead of falling through to main()'s red
        # "Aborted by user" handler.
        pass
    finally:
        sys.stdout.write("\033[?1049l")
        sys.stdout.flush()

    print(color("  Monitor stopped.", Colors.DIM) + "\n")


def check_image_with_version(image, allow_pull):
    """check_image, plus what version the registry would give us when there's an update."""
    res = docker_api.check_image(image, allow_pull)
    if res["status"] == "update":
        # A pull-based check already has the new image locally; otherwise
        # read the registry's config without pulling.
        res["remote_info"] = versions.local_version(image) if res.get("checked_via") == "pull" else versions.remote_version(image)
    return res

def cmd_updates(is_upgrade=False, target=None, dry_run=False):
    projects = docker_api.find_projects()
    if not projects:
        return print(no_projects_message())

    if target:
        projects, error = select_projects(projects, target)
        if error:
            return print(f"\n{color('!', Colors.RED)} {color(error, Colors.RED)}\n")

    print()
    title = "Image Updates"
    if is_upgrade:
        title = "Upgrade Plan (dry run)" if dry_run else "Upgrading Containers"
    print(color("● DOCKY", Colors.BOLD + Colors.CYAN) + color(f"  ·  {title}", Colors.DIM) + "\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        project_data = [d for d in fetch_project_data(projects, executor) if d["containers"]]
        if not project_data: return

        unique_images = {c["image"] for d in project_data for c in d["containers"]}
        image_futures = {img: executor.submit(check_image_with_version, img, is_upgrade and not dry_run) for img in unique_images}

        total_cur, total_upd, total_upg, total_err, total_unk = 0, 0, 0, 0, 0
        errors = []
        unstable = []
        changes = []  # (name, "from → to", release notes url)
        dependency_maps = {}

        def dependency_map(project):
            if project["name"] not in dependency_maps:
                dependency_maps[project["name"]] = docker_api.get_dependency_map(project)
            return dependency_maps[project["name"]]

        for p_idx, data in enumerate(project_data):
            project, containers = data["project"], data["containers"]
            is_last_p = (p_idx == len(project_data) - 1)
            print(f"{'└─' if is_last_p else '├─'} {color(project['name'], Colors.CYAN + Colors.BOLD)}")

            for c_idx, container in enumerate(containers):
                is_last_c = (c_idx == len(containers) - 1)
                prefix = f"{'   ' if is_last_p else '│  '}{'└─' if is_last_c else '├─'} "
                name = container["short_name"]
                future = image_futures[container["image"]]

                idx = 0
                while not future.done():
                    sys.stdout.write(f"\r{prefix}{color(get_spinner(idx), Colors.CYAN)} {name:<20} {color('checking...', Colors.DIM)}\033[K")
                    sys.stdout.flush()
                    idx += 1; time.sleep(0.08)

                res = future.result()
                status = res["status"]
                running_info = versions.local_version(container["running_id"] or container["image"])
                target_info = res.get("remote_info")
                if status == "current" and container["running_id"] and res.get("local_id"):
                    if container["running_id"] != res["local_id"]:
                        # Newer image already pulled, container not recreated yet.
                        status = "update"
                        target_info = versions.local_version(res["local_id"])
                change = versions.describe_change(running_info, target_info) if status == "update" else None
                change_text = f"  {color(change, Colors.DIM)}" if change else ""

                if status == "current":
                    total_cur += 1
                    current = versions.describe_current(running_info)
                    detail = f"up to date · {current}" if current else "up to date"
                    print(f"\r{prefix}{color('✓', Colors.GREEN)} {name:<20} {color(detail, Colors.DIM)}\033[K")
                elif status == "unknown":
                    total_unk += 1
                    print(f"\r{prefix}{color('?', Colors.YELLOW)} {name:<20} {color('cannot verify without pulling', Colors.DIM)}\033[K")
                elif status == "update":
                    if is_upgrade and not dry_run:
                        docker_api.snapshot_service(project, container)
                        upg_future = executor.submit(docker_api.upgrade_service, project, container["service"])
                        while not upg_future.done():
                            sys.stdout.write(f"\r{prefix}{color(get_spinner(idx), Colors.CYAN)} {name:<20} {color('pulling & recreating...', Colors.YELLOW)}\033[K")
                            sys.stdout.flush()
                            idx += 1; time.sleep(0.08)
                        success, err_msg = upg_future.result()

                        if not success:
                            total_err += 1; errors.append((name, err_msg))
                            print(f"\r{prefix}{color('!', Colors.RED)} {name:<20} {color('upgrade failed', Colors.RED)}\033[K")
                        else:
                            # Anything sharing this service's network
                            # (e.g. qBittorrent behind gluetun) is now
                            # attached to the container we just replaced,
                            # so it has to be recreated too.
                            follower_results = []
                            if docker_api.dependents_of(dependency_map(project), container["service"]):
                                follow_future = executor.submit(docker_api.recreate_dependents, project, container["service"], dependency_map(project))
                                while not follow_future.done():
                                    sys.stdout.write(f"\r{prefix}{color(get_spinner(idx), Colors.CYAN)} {name:<20} {color('recreating dependents...', Colors.YELLOW)}\033[K")
                                    sys.stdout.flush()
                                    idx += 1; time.sleep(0.08)
                                follower_results = follow_future.result()

                            # The command succeeding doesn't mean the
                            # container actually came back up cleanly --
                            # confirm it before calling this a success.
                            verify_future = executor.submit(docker_api.verify_container_health, container["name"])
                            while not verify_future.done():
                                sys.stdout.write(f"\r{prefix}{color(get_spinner(idx), Colors.CYAN)} {name:<20} {color('verifying health...', Colors.CYAN)}\033[K")
                                sys.stdout.flush()
                                idx += 1; time.sleep(0.08)
                            ok, detail = verify_future.result()

                            # What actually got installed, not what the check predicted.
                            installed = versions.local_version(container["image"]) or target_info
                            change = versions.describe_change(running_info, installed)
                            change_text = f"  {color(change, Colors.DIM)}" if change else ""
                            changes.append((name, change, versions.release_notes_url(installed or running_info)))
                            if ok:
                                total_upg += 1
                                print(f"\r{prefix}{color('✓', Colors.GREEN)} {name:<20} {color('upgraded & verified', Colors.GREEN)}{change_text}\033[K")
                            else:
                                total_err += 1; unstable.append((name, detail, project["name"]))
                                print(f"\r{prefix}{color('!', Colors.RED)} {name:<20} {color('upgraded but unstable', Colors.RED)}{change_text}\033[K")

                            guide = f"{'   ' if is_last_p else '│  '}{'   ' if is_last_c else '│  '}"
                            by_service = {c["service"]: c for c in containers}
                            for dep_service, dep_ok, dep_err in follower_results:
                                dep_name = by_service.get(dep_service, {}).get("short_name", dep_service)
                                if dep_ok and dep_service in by_service:
                                    dep_ok, dep_err = docker_api.verify_container_health(by_service[dep_service]["name"])
                                if dep_ok:
                                    print(f"{guide}{color('↳', Colors.GREEN)} {dep_name:<18} {color('recreated (follows ' + name + ')', Colors.GREEN)}")
                                else:
                                    total_err += 1
                                    unstable.append((dep_name, dep_err, project["name"]))
                                    print(f"{guide}{color('↳', Colors.RED)} {dep_name:<18} {color('recreate failed (follows ' + name + ')', Colors.RED)}")
                    else:
                        total_upd += 1
                        label = "would upgrade" if dry_run else "update available"
                        changes.append((name, change, versions.release_notes_url(target_info or running_info)))
                        print(f"\r{prefix}{color('↑', Colors.YELLOW)} {name:<20} {color(label, Colors.YELLOW)}{change_text}\033[K")
                        if is_upgrade:
                            followers = docker_api.dependents_of(dependency_map(project), container["service"])
                            if followers:
                                guide = f"{'   ' if is_last_p else '│  '}{'   ' if is_last_c else '│  '}"
                                print(f"{guide}{color('↳ would also recreate: ' + ', '.join(followers), Colors.DIM)}")
                else:
                    total_err += 1; errors.append((container["image"], res["error"]))
                    print(f"\r{prefix}{color('!', Colors.RED)} {name:<20} {color('check failed', Colors.RED)}\033[K")

        print("\n" + "─" * 55)
        print(color(f"✓ {total_cur} up to date", Colors.GREEN))
        if is_upgrade and total_upg: print(color(f"✓ {total_upg} upgraded successfully", Colors.GREEN))
        elif total_upd:
            noun = "would be upgraded (dry run, nothing changed)" if dry_run else "update(s) available"
            print(color(f"↑ {total_upd} {noun}", Colors.YELLOW))
        if total_unk: print(color(f"? {total_unk} could not be verified without pulling", Colors.DIM))
        if total_err: print(color(f"! {total_err} error(s)", Colors.RED))
        
        if changes:
            heading = "Version changes:" if is_upgrade and not dry_run else "Available versions:"
            print("\n" + color(heading, Colors.BOLD))
            width = max(len(n) for n, _, _ in changes) + 2
            for item, change, notes in changes:
                print(f"  {item:<{width}}{change or color('version not published by the image', Colors.DIM)}")
                if notes:
                    print(color(f"  {'':<{width}}release notes: {notes}", Colors.DIM))

        if errors:
            print("\n" + color("Check details:", Colors.BOLD))
            for item, err in errors: print(f"  {color('!', Colors.RED)} {item}\n    {color(err, Colors.DIM)}")

        if unstable:
            print("\n" + color("Upgraded but did not verify as healthy:", Colors.BOLD))
            for item, detail, _ in unstable: print(f"  {color('!', Colors.RED)} {item}\n    {color(detail, Colors.DIM)}")
            for proj in sorted({u[2] for u in unstable}):
                print(color(f"  Undo with: docky rollback {proj}", Colors.DIM))

        if is_upgrade and total_upg > 0:
            print()
            if unstable:
                print(color("○ Skipping image cleanup -- at least one upgrade this run wasn't verified healthy.", Colors.YELLOW))
                print(color("  Fix or roll back the container(s) above, then re-run 'docky sweep' when you're ready to reclaim space.", Colors.DIM))
            else:
                sys.stdout.write(f"{color('⠋', Colors.CYAN)} {color('Cleaning up old images...', Colors.DIM)}\033[K")
                sys.stdout.flush()
                run_command(["docker", "image", "prune", "-f"])
                sys.stdout.write(f"\r{color('✓', Colors.GREEN)} {color('Cleaned up old images.', Colors.DIM)}\033[K\n")
        print()

def cmd_rollback(target=None, service=None):
    state = docker_api.load_rollback_state()
    print(f"\n{color('● DOCKY', Colors.BOLD + Colors.CYAN)}{color('  ·  Rollback', Colors.DIM)}\n")

    if not target:
        if not state:
            return print(color("  No rollback snapshots yet. One is saved automatically before each upgrade.", Colors.DIM) + "\n")
        print(color("Available snapshots:", Colors.BOLD))
        for proj, services in sorted(state.items()):
            for svc, e in sorted(services.items()):
                print(f"  {color(proj, Colors.CYAN)}/{svc:<18} {e['image']:<36} {color('saved ' + e['saved_at'], Colors.DIM)}")
        return print(f"\n{color('Run:', Colors.DIM)} docky rollback <project> [service]\n")

    projects, error = select_projects(docker_api.find_projects(), target)
    if error:
        return print(f"{color('!', Colors.RED)} {color(error, Colors.RED)}\n")
    project = projects[0]
    entries = state.get(project["name"], {})
    if service:
        entries = {k: v for k, v in entries.items() if k.lower() == service.lower()}
    if not entries:
        return print(color("  Nothing to roll back for that target.", Colors.YELLOW) + "\n")

    containers = {c["service"]: c for c in docker_api.get_project_containers(project)}
    failed = 0
    for svc, entry in entries.items():
        current = containers.get(svc, {}).get("running_id")
        if current and current == entry["previous_id"]:
            print(f"  {color('○', Colors.YELLOW)} {svc:<20} {color('already running the saved image', Colors.DIM)}")
            continue
        ok, err = docker_api.rollback_service(project, svc, entry)
        follower_results = []
        if ok:
            dependency_map = docker_api.get_dependency_map(project)
            follower_results = docker_api.recreate_dependents(project, svc, dependency_map)
        if ok and svc in containers:
            ok, err = docker_api.verify_container_health(containers[svc]["name"])
        if ok:
            docker_api.forget_snapshot(project["name"], svc)
            print(f"  {color('✓', Colors.GREEN)} {svc:<20} {color('rolled back to ' + entry['image'] + ' (saved ' + entry['saved_at'] + ')', Colors.GREEN)}")
            for dep_service, dep_ok, dep_err in follower_results:
                if dep_ok and dep_service in containers:
                    dep_ok, dep_err = docker_api.verify_container_health(containers[dep_service]["name"])
                if dep_ok:
                    print(f"    {color('↳', Colors.GREEN)} {dep_service:<18} {color('recreated (follows ' + svc + ')', Colors.GREEN)}")
                else:
                    failed += 1
                    print(f"    {color('↳', Colors.RED)} {dep_service:<18} {color('recreate failed', Colors.RED)}\n      {color(dep_err, Colors.DIM)}")
        else:
            failed += 1
            print(f"  {color('!', Colors.RED)} {svc:<20} {color('rollback failed', Colors.RED)}\n    {color(err, Colors.DIM)}")
    print()
    if failed:
        sys.exit(1)

def cmd_sweep():
    metrics_before = get_system_metrics()
    
    print(f"\n{color('● DOCKY', Colors.BOLD + Colors.CYAN)} {color('  ·  The Ghost Finder', Colors.DIM)}\n")
    sys.stdout.write(f"{color('⠋', Colors.CYAN)} {color('Analyzing Docker filesystem...', Colors.DIM)}\033[K")
    sys.stdout.flush()

    succ, out, err = run_command(["docker", "system", "df"])
    if not succ:
        return print(f"\r{color('!', Colors.RED)} {color('Failed to analyze Docker filesystem.', Colors.RED)}\n{err}")

    data = {parts[0]: parts[4].split(" ")[0] for line in out.splitlines()[1:] if len(parts := re.split(r'\s{2,}', line.strip())) >= 5}
    
    _, out_det, _ = run_command(["docker", "system", "df", "-v"])
    unused_images, in_imgs = [], False
    for line in out_det.splitlines():
        if line.startswith("Images space usage:"): in_imgs = True; continue
        if line.startswith("Containers space usage:"): in_imgs = False; continue
        if in_imgs and line and not line.startswith("REPOSITORY"):
            p = re.split(r'\s{2,}', line.strip())
            if len(p) >= 8 and p[-1] == '0' and not p[0].startswith("docky-rollback/"):
                unused_images.append(f"{'Untagged Layer' if p[0] == '<none>' else f'{p[0]}:{p[1]}'} {color(f'({p[-4]})', Colors.DIM)} - {p[2]}")

    _, out_c, _ = run_command(["docker", "ps", "-a", "-f", "status=exited", "-f", "status=created", "--format", "{{.Names}} - {{.Status}}"])
    stopped_containers = [line.strip() for line in out_c.splitlines() if line.strip()]

    print(f"\r\033[K{color('○ Ghost Data Found:', Colors.BOLD)}\n")
    
    has_ghosts = False
    for key, label in [("Images", "Unused Images"), ("Containers", "Stopped Containers"), ("Local Volumes", "Orphaned Volumes"), ("Build Cache", "Build Cache")]:
        size = data.get(key, "0B")
        if size != "0B": has_ghosts = True
        print(f"  {color('•', Colors.DIM)} {label:<20} {color(size, Colors.YELLOW) if size != '0B' else color('Clean', Colors.GREEN)}")

    if not has_ghosts:
        return print(f"\n{color('✓ Your system is completely clean! No ghosts found.', Colors.GREEN)}\n")

    if stopped_containers or unused_images:
        print("\n" + "─" * 55 + "\n" + color("Inspection Details:", Colors.BOLD))
        if stopped_containers:
            print(color("\n  Stopped Containers to remove:", Colors.DIM))
            for c in stopped_containers: print(f"    {color('✕', Colors.RED)} {c}")
        if unused_images:
            print(color("\n  Unused Images to remove:", Colors.DIM))
            for i in unused_images: print(f"    {color('✕', Colors.RED)} {i}")
        print("\n" + "─" * 55 + "\n")

    print(color("Deep Sweep Overview:", Colors.DIM))
    print(color("  * Local volumes are kept safe. No app data will be deleted.", Colors.DIM))
    print(color("  * Rollback snapshots from 'docky upgrade' are kept.", Colors.DIM) + "\n")
    
    try: choice = input(color("[?] Do you want to execute a deep sweep? (y/N): ", Colors.BOLD)).strip().lower()
    except EOFError: choice = 'n'
        
    if choice in ['y', 'yes']:
        sys.stdout.write(f"\n{color('⠋', Colors.CYAN)} {color('Sweeping ghosts...', Colors.DIM)}\033[K")
        sys.stdout.flush()
        succ, out, err = run_command(["docker", "system", "prune", "-a", "-f", "--filter", f"label!={docker_api.ROLLBACK_LABEL}=true"])
        
        metrics_after = get_system_metrics()
        
        print(f"\r{color('✓', Colors.GREEN)} {color('Sweep complete!', Colors.GREEN)}\033[K")
        if match := re.search(r"Total reclaimed space: (.*)", out):
            freed = match.group(1)
            context = f"Disk usage dropped from {metrics_before['disk_pct']:.1f}% to {metrics_after['disk_pct']:.1f}%"
            print(f"  {color('Freed Space:', Colors.CYAN)} {color(freed, Colors.BOLD)} {color(f'({context})', Colors.DIM)}\n")
    else:
        print(f"\n{color('Sweep aborted. Your ghosts remain.', Colors.DIM)}\n")

def cmd_lifecycle(action, target):
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

    print(f"\n{color('● DOCKY', Colors.BOLD + Colors.CYAN)} {color(f'  ·  {action.capitalize()}ing Projects', Colors.DIM)}\n")

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
                sys.stdout.write(f"\r{prefix}{color(get_spinner(idx), Colors.CYAN)} {name:<20} {color(f'{action}ing...', Colors.YELLOW)}\033[K")
                sys.stdout.flush()
                idx += 1; time.sleep(0.08)
            succ, _, err = future.result()
            if succ:
                success_c += 1
                print(f"\r{prefix}{color('✓', Colors.GREEN)} {name:<20} {color(f'{action}ed', Colors.GREEN)}\033[K")
            else:
                error_c += 1
                print(f"\r{prefix}{color('✕', Colors.RED)} {name:<20} {color(f'failed', Colors.RED)}\033[K\n  {color(err, Colors.DIM)}")
    print()

def cmd_orphans():
    print(f"\n{color('● DOCKY', Colors.BOLD + Colors.CYAN)} {color('  ·  Orphaned Volumes', Colors.DIM)}\n")
    sys.stdout.write(f"{color('⠋', Colors.CYAN)} {color('Scanning volumes...', Colors.DIM)}\033[K")
    sys.stdout.flush()

    found = docker_api.discover_projects()
    projects = found["projects"]

    # With no projects discovered, every Compose volume would look
    # orphaned. That's almost always a wrong/missing/unmounted
    # DOCKY_ROOT, not a real cleanup opportunity -- refuse to guess.
    if not projects:
        roots = ", ".join(str(r) for r in docker_api.scan_roots())
        return print(
            f"\r\033[K{color('!', Colors.RED)} {color(f'No Compose projects found (Docker has none, and none on disk under {roots}).', Colors.RED)}\n"
            + color("  Refusing to check for orphans: every volume would be flagged. Is the directory missing or unmounted?", Colors.DIM) + "\n"
        )

    # A project whose compose files went missing still has containers,
    # so its volumes are in use -- never call those orphaned.
    known_project_names = {p["name"] for p in found["stale"]}
    for p in projects:
        known_project_names |= docker_api.resolve_project_names(p)

    all_volumes = docker_api.get_all_volumes()

    # "Orphaned" = Compose stamped this volume with a project name,
    # and Docky can't find that project anywhere: no containers run
    # under it and no compose file for it exists under DOCKY_ROOT.
    # Volumes with no project label at all weren't created by
    # Compose (or predate this labeling) -- we can't safely reason
    # about ownership for those, so they're surfaced as a count
    # only, never flagged or touched.
    orphaned = [v for v in all_volumes if v["project"] and v["project"] not in known_project_names]
    unmanaged = [v for v in all_volumes if not v["project"]]

    print(f"\r\033[K{color('○ Scan complete', Colors.BOLD)}\n")

    if not orphaned:
        print(color("✓ No orphaned volumes found.", Colors.GREEN))
        if unmanaged:
            count = len(unmanaged)
            print(color(f"  ({count} volume{'s' if count != 1 else ''} not managed by Compose -- ownership unclear, not checked)", Colors.DIM))
        print()
        return

    count = len(orphaned)
    print(color(f"Found {count} volume{'s' if count != 1 else ''} whose project no longer exists:", Colors.BOLD))
    print()

    for v in orphaned:
        size_str = v["size"] or "unknown size"
        label = f"(was: {v['project']})"
        print(f"  {color('✕', Colors.RED)} {v['name']:<32} {label:<26} {color(size_str, Colors.YELLOW)}")

    print()
    print(color("No container runs under these projects and no compose file for them was found", Colors.DIM))
    print(color(f"under {', '.join(str(r) for r in docker_api.scan_roots())} (set DOCKY_ROOT if your stacks live elsewhere)", Colors.DIM))
    print(color("-- likely deleted or renamed projects.", Colors.DIM))
    print()

    if unmanaged:
        u_count = len(unmanaged)
        verb = "aren't" if u_count != 1 else "isn't"
        print(color(f"({u_count} other volume{'s' if u_count != 1 else ''} {verb} Compose-managed and weren't checked.)", Colors.DIM))
        print()

    try:
        choice = input(color("[?] Remove volumes? This deletes their data permanently. [a]ll / [s]elect / [N]one: ", Colors.BOLD)).strip().lower()
    except EOFError:
        choice = 'n'

    if choice in ('a', 'all'):
        to_remove = orphaned
    elif choice in ('s', 'select'):
        to_remove = []
        for v in orphaned:
            try:
                answer = input(f"  Remove {color(v['name'], Colors.BOLD)} ({v['size'] or 'unknown size'})? (y/N): ").strip().lower()
            except EOFError:
                break
            if answer in ('y', 'yes'):
                to_remove.append(v)
    else:
        to_remove = []

    if not to_remove:
        return print(f"\n{color('Aborted. No volumes were removed.', Colors.DIM)}\n")

    print()
    removed, failed = 0, []
    for v in to_remove:
        success, err = docker_api.remove_volume(v["name"])
        if success:
            print(f"  {color('✓', Colors.GREEN)} Removed {v['name']}")
            removed += 1
        else:
            print(f"  {color('!', Colors.RED)} Failed to remove {v['name']}: {color(err, Colors.DIM)}")
            failed.append(v["name"])

    print()
    print(color(f"{removed} volume{'s' if removed != 1 else ''} removed.", Colors.GREEN))
    if failed:
        print(color(f"{len(failed)} could not be removed (likely still in use by a container).", Colors.RED))
    print()
def _human_size(num_bytes):
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.0f}{unit}" if unit == "B" else f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f}TB"

def _ask(question):
    try:
        return input(color(f"[?] {question} (y/N): ", Colors.BOLD)).strip().lower() in ("y", "yes")
    except EOFError:
        return False

def _print_removal_plan(project, plan):
    dim = lambda t: color(t, Colors.DIM)
    if project.get("missing"):
        source = "compose files missing"
    else:
        source = "running in Docker" if project["source"] == "docker" else "found on disk"
    print(f"  {color(plan['name'], Colors.CYAN + Colors.BOLD)}  {project.get('path') or ''}  {dim(source)}\n")

    print(color("  Always removed", Colors.BOLD))
    if plan["containers"]:
        running = sum(1 for c in plan["containers"] if c["state"] == "running")
        note = f" ({running} running, will be stopped)" if running else ""
        print(f"    {color('✕', Colors.RED)} {len(plan['containers'])} container(s){note}")
        for c in plan["containers"]:
            print(dim(f"        {c['name']}  [{c['state']}]"))
    if plan["networks"]:
        print(f"    {color('✕', Colors.RED)} {len(plan['networks'])} network(s)  {dim(', '.join(plan['networks']))}")
    if not plan["containers"] and not plan["networks"]:
        print(dim("    nothing -- no containers or networks exist for this project"))

    print(color("\n  Optional", Colors.BOLD))
    if plan["volumes"]:
        print(f"    {color('--volumes', Colors.CYAN)}  {len(plan['volumes'])} volume(s)  {color('data is deleted permanently', Colors.RED)}")
        for v in plan["volumes"]:
            kind = "anonymous " if v["anonymous"] else ""
            print(dim(f"        {v['name'][:40]}  {kind}{v['size'] or 'size unknown'}"))
    else:
        print(dim("    --volumes  none"))
    if plan["images"] or plan["snapshots"]:
        total = _human_size(sum(i["size"] for i in plan["images"]))
        snaps = f" + {len(plan['snapshots'])} rollback snapshot(s)" if plan["snapshots"] else ""
        print(f"    {color('--images', Colors.CYAN)}   {len(plan['images'])} image(s), {total}{snaps}  {dim('can be pulled or rebuilt')}")
        for i in plan["images"]:
            print(dim(f"        {', '.join(i['tags']) or i['id'][:19]}  {_human_size(i['size'])}"))
    else:
        print(dim("    --images   none only this project uses"))
    if plan["folder"] and not plan["folder_block"]:
        print(f"    {color('--files', Colors.CYAN)}    folder {plan['folder']}  {color('compose files, .env and any data inside', Colors.RED)}")
    elif plan["folder"]:
        print(dim(f"    --files    not offered: {plan['folder']} -- {plan['folder_block']}"))
    else:
        print(dim("    --files    no folder on disk"))

    kept = []
    kept += [f"volume {v} (not created by this project: external or shared)" for v in plan["kept_volumes"]]
    kept += [f"image {', '.join(i['tags']) or i['id'][:19]} (also used by {i['used_by']})" for i in plan["kept_images"]]
    kept += [f"data outside the folder: {b}" for b in plan["external_binds"]]
    if kept:
        print(color("\n  Never touched", Colors.BOLD))
        for k in kept:
            print(f"    {color('•', Colors.GREEN)} {dim(k)}")
    print()

def cmd_remove(target, volumes=False, images=False, files=False, dry_run=False, assume_yes=False):
    """
    Remove a project completely: containers and networks always, and on
    request its volumes, images and folder. The plan is shown first and
    nothing irreversible happens without an explicit yes.
    """
    print(f"\n{color('● DOCKY', Colors.BOLD + Colors.CYAN)} {color('  ·  Remove Project', Colors.DIM)}\n")
    if target.lower() == "all":
        return print(color("! 'remove all' isn't supported. Remove projects one at a time.", Colors.RED) + "\n")

    found = docker_api.discover_projects()
    everything = found["projects"] + found["stale"]
    matches, error = select_projects(everything, target)
    if error:
        return print(f"{color('!', Colors.RED)} {color(error, Colors.RED)}\n")
    project = matches[0]

    sys.stdout.write(f"{color('⠋', Colors.CYAN)} {color('Working out what belongs to this project...', Colors.DIM)}\033[K")
    sys.stdout.flush()
    others = [p for p in everything if p is not project]
    plan = docker_api.plan_removal(project, others)
    sys.stdout.write("\r\033[K")
    _print_removal_plan(project, plan)

    files = files and plan["folder"] is not None and not plan["folder_block"]
    if dry_run or assume_yes:
        volumes, images = volumes and bool(plan["volumes"]), images and bool(plan["images"] or plan["snapshots"])
    else:
        if plan["volumes"] and not volumes:
            volumes = _ask(f"Also delete {len(plan['volumes'])} volume(s)? Their data cannot be recovered.")
        if (plan["images"] or plan["snapshots"]) and not images:
            images = _ask("Also remove the images only this project uses?")
        if plan["folder"] and not plan["folder_block"] and not files:
            files = _ask(f"Also delete the folder {plan['folder']}?")

    steps = []
    if plan["containers"] or plan["networks"]:
        steps.append("containers & networks")
    if volumes: steps.append("volumes")
    if images: steps.append("images")
    if files: steps.append("folder")
    if not steps:
        return print(color("Nothing to remove.", Colors.DIM) + "\n")

    summary = ", ".join(steps)
    if dry_run:
        return print(f"{color('Dry run:', Colors.YELLOW)} would remove {summary}. Nothing was changed.\n")

    if not assume_yes:
        print(f"\n{color('About to remove:', Colors.BOLD)} {summary}")
        if volumes or files:
            try:
                typed = input(color(f"[?] This is permanent. Type the project name ({plan['name']}) to confirm: ", Colors.BOLD)).strip()
            except EOFError:
                typed = ""
            if typed != plan["name"]:
                return print(f"\n{color('Aborted. Nothing was removed.', Colors.DIM)}\n")
        elif not _ask("Proceed?"):
            return print(f"\n{color('Aborted. Nothing was removed.', Colors.DIM)}\n")
    print()

    failed = 0
    def report(label, errors, failed_label=None):
        nonlocal failed
        if errors:
            failed += 1
            print(f"{color('✕', Colors.RED)} {failed_label or 'Failed: ' + label}")
            for e in errors:
                print(color(f"    {e}", Colors.DIM))
        else:
            print(f"{color('✓', Colors.GREEN)} {label}")

    if plan["containers"] or plan["networks"]:
        report("Containers and networks removed", docker_api.remove_containers_and_networks(project, plan))
    if volumes:
        errors = []
        for v in plan["volumes"]:
            ok, err = docker_api.remove_volume(v["name"])
            if not ok:
                errors.append(f"{v['name']}: {err}")
        report(f"{len(plan['volumes'])} volume(s) deleted", errors)
    if images:
        report("Images removed", docker_api.remove_images(plan))
    if files:
        ok, err = docker_api.remove_folder(plan["folder"])
        report(f"Folder {plan['folder']} deleted", [] if ok else [err, f"Delete it with: sudo rm -rf {shlex.quote(str(plan['folder']))}"],
               failed_label=f"Folder {plan['folder']} was left in place")

    if failed:
        print(f"\n{color('! Some steps failed (see above). Everything else was removed.', Colors.YELLOW)}\n")
    else:
        kept = [label for label, chosen, exists in (
            ("volumes", volumes, plan["volumes"]),
            ("images", images, plan["images"] or plan["snapshots"]),
            ("folder", files, plan["folder"]),
        ) if exists and not chosen]
        done = f"{plan['name']} removed." + (f" Kept: {', '.join(kept)}." if kept else "")
        print(f"\n{color('●', Colors.GREEN)} {color(done, Colors.DIM)}")
        anonymous = [v["name"] for v in plan["volumes"] if v["anonymous"]]
        if anonymous and not volumes:
            print(color(f"  {len(anonymous)} kept volume(s) are anonymous: nothing links them to this project anymore.", Colors.DIM))
            print(color(f"  To delete later: docker volume rm {' '.join(anonymous)}", Colors.DIM))
        print()
