# docky/commands/updates.py
"""Image updates: updates, upgrade, rollback."""
import sys
import time
import concurrent.futures
import shutil
from ..utils import Colors, color, run_command, supports_hyperlinks, hyperlink
from .. import docker_api
from .. import versions
from .common import get_spinner, no_projects_message, select_projects, fetch_project_data

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
        # Name column as wide as the longest name, so statuses line up.
        name_w = max([len(c["short_name"]) for d in project_data for c in d["containers"]] + [12]) + 1

        unique_images = {c["image"] for d in project_data for c in d["containers"]}
        image_futures = {img: executor.submit(check_image_with_version, img, is_upgrade and not dry_run) for img in unique_images}

        total_cur, total_upd, total_upg, total_err, total_unk = 0, 0, 0, 0, 0
        errors = []
        unstable = []
        deferred_notes = []  # (name, url) for links that didn't fit on their row
        dependency_maps = {}
        columns = shutil.get_terminal_size((120, 40)).columns
        links_clickable = supports_hyperlinks()
        CHANGE_W = 40

        def change_tail(used, change, notes, name):
            """
            '  old → new   release notes ↗' for an update row. The link is a
            clickable 'release notes ↗' where the terminal supports it, else
            the full URL if it fits; otherwise it's listed after the tree.
            """
            # Starts in the same column as "up to date" on ✓ rows; at least two
            # spaces before the link even when a change is longer than CHANGE_W.
            text = f"{change or 'new image (no version info)':<{CHANGE_W}}  "
            tail = " " + color(text, Colors.DIM)
            used += 1 + len(text)
            if notes:
                if links_clickable:
                    tail += color(hyperlink(notes, "release notes ↗"), Colors.DIM)
                elif used + len(notes) <= columns:
                    tail += color(notes, Colors.DIM)
                else:
                    deferred_notes.append((name, notes))
            return tail

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
                    sys.stdout.write(f"\r{prefix}{color(get_spinner(idx), Colors.CYAN)} {name:<{name_w}} {color('checking...', Colors.DIM)}\033[K")
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

                if status == "current":
                    total_cur += 1
                    current = versions.describe_current(running_info)
                    detail = f"up to date · {current}" if current else "up to date"
                    print(f"\r{prefix}{color('✓', Colors.GREEN)} {name:<{name_w}} {color(detail, Colors.DIM)}\033[K")
                elif status == "unknown":
                    total_unk += 1
                    print(f"\r{prefix}{color('?', Colors.YELLOW)} {name:<{name_w}} {color('cannot verify without pulling', Colors.DIM)}\033[K")
                elif status == "update":
                    if is_upgrade and not dry_run:
                        docker_api.snapshot_service(project, container)
                        upg_future = executor.submit(docker_api.upgrade_service, project, container["service"])
                        while not upg_future.done():
                            sys.stdout.write(f"\r{prefix}{color(get_spinner(idx), Colors.CYAN)} {name:<{name_w}} {color('pulling & recreating...', Colors.YELLOW)}\033[K")
                            sys.stdout.flush()
                            idx += 1; time.sleep(0.08)
                        success, err_msg = upg_future.result()

                        if not success:
                            total_err += 1; errors.append((name, err_msg))
                            print(f"\r{prefix}{color('!', Colors.RED)} {name:<{name_w}} {color('upgrade failed', Colors.RED)}\033[K")
                        else:
                            # Anything sharing this service's network
                            # (e.g. qBittorrent behind gluetun) is now
                            # attached to the container we just replaced,
                            # so it has to be recreated too.
                            follower_results = []
                            if docker_api.dependents_of(dependency_map(project), container["service"]):
                                follow_future = executor.submit(docker_api.recreate_dependents, project, container["service"], dependency_map(project))
                                while not follow_future.done():
                                    sys.stdout.write(f"\r{prefix}{color(get_spinner(idx), Colors.CYAN)} {name:<{name_w}} {color('recreating dependents...', Colors.YELLOW)}\033[K")
                                    sys.stdout.flush()
                                    idx += 1; time.sleep(0.08)
                                follower_results = follow_future.result()

                            # The command succeeding doesn't mean the
                            # container actually came back up cleanly --
                            # confirm it before calling this a success.
                            verify_future = executor.submit(docker_api.verify_container_health, container["name"])
                            while not verify_future.done():
                                sys.stdout.write(f"\r{prefix}{color(get_spinner(idx), Colors.CYAN)} {name:<{name_w}} {color('verifying health...', Colors.CYAN)}\033[K")
                                sys.stdout.flush()
                                idx += 1; time.sleep(0.08)
                            ok, detail = verify_future.result()

                            # What actually got installed, not what the check predicted.
                            installed = versions.local_version(container["image"]) or target_info
                            change = versions.describe_change(running_info, installed)
                            notes = versions.release_notes_url(installed or running_info)
                            label = "upgraded & verified" if ok else "upgraded but unstable"
                            tail = change_tail(len(prefix) + 2 + name_w + 1 + len(label), change, notes, name)
                            if ok:
                                total_upg += 1
                                print(f"\r{prefix}{color('✓', Colors.GREEN)} {name:<{name_w}} {color(label, Colors.GREEN)}{tail}\033[K")
                            else:
                                total_err += 1; unstable.append((name, detail, project["name"]))
                                print(f"\r{prefix}{color('!', Colors.RED)} {name:<{name_w}} {color(label, Colors.RED)}{tail}\033[K")

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
                        # ↑ already says "update available"; the row is just name, change, notes.
                        notes = versions.release_notes_url(target_info or running_info)
                        tail = change_tail(len(prefix) + 2 + name_w, change, notes, name)
                        print(f"\r{prefix}{color('↑', Colors.YELLOW)} {name:<{name_w}}{tail}\033[K")
                        if is_upgrade:
                            followers = docker_api.dependents_of(dependency_map(project), container["service"])
                            if followers:
                                guide = f"{'   ' if is_last_p else '│  '}{'   ' if is_last_c else '│  '}"
                                print(f"{guide}{color('↳ would also recreate: ' + ', '.join(followers), Colors.DIM)}")
                else:
                    total_err += 1; errors.append((container["image"], res["error"]))
                    print(f"\r{prefix}{color('!', Colors.RED)} {name:<{name_w}} {color('check failed', Colors.RED)}\033[K")

        print("\n" + "─" * 55)
        print(color(f"✓ {total_cur} up to date", Colors.GREEN))
        if is_upgrade and total_upg: print(color(f"✓ {total_upg} upgraded successfully", Colors.GREEN))
        elif total_upd:
            noun = "would be upgraded (dry run, nothing changed)" if dry_run else "update(s) available"
            print(color(f"↑ {total_upd} {noun}", Colors.YELLOW))
        if total_unk: print(color(f"? {total_unk} could not be verified without pulling", Colors.DIM))
        if total_err: print(color(f"! {total_err} error(s)", Colors.RED))
        
        if deferred_notes:
            # Only links that didn't fit on their row (terminals without
            # clickable links); one line each, services of one app together.
            groups = {}
            for item, url in deferred_notes:
                groups.setdefault(url, []).append(item)
            rows = [(", ".join(items), url) for url, items in groups.items()]
            label_w = min(max(len(label) for label, _ in rows), 32) + 2
            print("\n" + color("Release notes:", Colors.BOLD))
            for label, url in rows:
                print(f"  {label:<{label_w}}{color(url, Colors.DIM)}")

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
