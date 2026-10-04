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

CHANGE_W = 40

def check_image_with_version(image, allow_pull):
    """check_image, plus what version the registry would give us when there's an update."""
    res = docker_api.check_image(image, allow_pull)
    if res["status"] == "update":
        # A pull-based check already has the new image locally; otherwise
        # read the registry's config without pulling.
        res["remote_info"] = versions.local_version(image) if res.get("checked_via") == "pull" else versions.remote_version(image, res.get("remote"))
    return res

def classify(container, res, local_infos):
    """(status, running version, target version, "old → new") for one container."""
    status = res["status"]
    running_info = local_infos.get(container["running_id"]) or local_infos.get(container["image"])
    target_info = res.get("remote_info")
    if status == "current" and container["running_id"] and res.get("local_id") and container["running_id"] != res["local_id"]:
        # Newer image already pulled, container not recreated yet.
        status = "update"
        target_info = local_infos.get(res["local_id"]) or versions.local_version(res["local_id"])
    change = None
    if status == "update":
        # The running image's labels may not say its version (an older
        # release): a version tag that still points at it can.
        running_info = versions.running_version(running_info, container["image"], (running_info or {}).get("digests"))
        change = versions.describe_change(running_info, target_info)
    return status, running_info, target_info, change

class Notes:
    """Release-notes links: clickable on the row, else the URL if it fits, else listed at the end."""
    def __init__(self):
        self.columns = shutil.get_terminal_size((120, 40)).columns
        self.clickable = supports_hyperlinks()
        self.deferred = []

    def tail(self, used, change, link, name):
        # Starts in the same column as "up to date" on ✓ rows; at least two
        # spaces before the link even when a change is longer than CHANGE_W.
        url, link_text = link or (None, None)
        text = f"{change or 'newer image':<{CHANGE_W}}  "
        out = " " + color(text, Colors.DIM)
        used += 1 + len(text)
        if url:
            if self.clickable:
                out += color(hyperlink(url, link_text), Colors.DIM)
            elif used + len(url) <= self.columns:
                out += color(url, Colors.DIM)
            else:
                self.deferred.append((name, url))
        return out

    def print_deferred(self):
        if not self.deferred:
            return
        groups = {}
        for item, url in self.deferred:
            groups.setdefault(url, []).append(item)
        rows = [(", ".join(items), url) for url, items in groups.items()]
        label_w = min(max(len(label) for label, _ in rows), 32) + 2
        print("\n" + color("Release notes:", Colors.BOLD))
        for label, url in rows:
            print(f"  {label:<{label_w}}{color(url, Colors.DIM)}")

def _spin(futures, label):
    """Animate one status line until every future is done; label(done, total) gives the text."""
    idx, total = 0, len(futures)
    while True:
        done = sum(f.done() for f in futures)
        sys.stdout.write(f"\r{color(get_spinner(idx), Colors.CYAN)} {color(label(done, total), Colors.DIM)}\033[K")
        sys.stdout.flush()
        if done == total:
            break
        idx += 1
        time.sleep(0.08)
    sys.stdout.write("\r\033[K")

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

    # Every image is checked at once; registry round trips are what take time.
    with concurrent.futures.ThreadPoolExecutor(max_workers=48) as executor:
        project_data = [d for d in fetch_project_data(projects, executor) if d["containers"]]
        if not project_data: return
        containers = [c for d in project_data for c in d["containers"]]
        unique_images = {c["image"] for c in containers}
        image_futures = {img: executor.submit(check_image_with_version, img, is_upgrade and not dry_run) for img in unique_images}
        # All local versions in one `docker image inspect`, alongside the checks.
        local_future = executor.submit(versions.local_versions,
                                       {c["running_id"] for c in containers} | unique_images)
        if is_upgrade and not dry_run:
            return _upgrade(project_data, image_futures, local_future)
        _show_checks(project_data, image_futures, local_future, is_upgrade)

def _show_checks(project_data, image_futures, local_future, is_upgrade):
    """`docky updates` and `upgrade --dry-run`: the tree, each row as soon as its image is checked."""
    name_w = max([len(c["short_name"]) for d in project_data for c in d["containers"]] + [12]) + 1
    notes = Notes()
    local_infos = local_future.result()
    total_cur, total_upd, total_err, total_unk = 0, 0, 0, 0
    errors = []

    for p_idx, data in enumerate(project_data):
        project, containers = data["project"], data["containers"]
        is_last_p = (p_idx == len(project_data) - 1)
        print(f"{'└─' if is_last_p else '├─'} {color(project['name'], Colors.CYAN + Colors.BOLD)}")
        dependency_map = None

        for c_idx, container in enumerate(containers):
            is_last_c = (c_idx == len(containers) - 1)
            prefix = f"{'   ' if is_last_p else '│  '}{'└─' if is_last_c else '├─'} "
            name = container["short_name"]
            future = image_futures[container["image"]]

            idx = 0
            while not future.done():
                sys.stdout.write(f"\r{prefix}{color(get_spinner(idx), Colors.CYAN)} {name:<{name_w}} {color('checking...', Colors.DIM)}\033[K")
                sys.stdout.flush()
                idx += 1; time.sleep(0.05)

            res = future.result()
            status, running_info, target_info, change = classify(container, res, local_infos)
            if status == "current":
                total_cur += 1
                current = versions.describe_current(running_info)
                detail = f"up to date · {current}" if current else "up to date"
                print(f"\r{prefix}{color('✓', Colors.GREEN)} {name:<{name_w}} {color(detail, Colors.DIM)}\033[K")
            elif status == "unknown":
                total_unk += 1
                print(f"\r{prefix}{color('?', Colors.YELLOW)} {name:<{name_w}} {color('cannot verify without pulling', Colors.DIM)}\033[K")
            elif status == "update":
                total_upd += 1
                # ↑ already says "update available"; the row is just name, change, notes.
                link = versions.notes_link(target_info, running_info)
                print(f"\r{prefix}{color('↑', Colors.YELLOW)} {name:<{name_w}}{notes.tail(len(prefix) + 2 + name_w, change, link, name)}\033[K")
                if is_upgrade:
                    if dependency_map is None:
                        dependency_map = docker_api.get_dependency_map(project)
                    followers = docker_api.dependents_of(dependency_map, container["service"])
                    if followers:
                        guide = f"{'   ' if is_last_p else '│  '}{'   ' if is_last_c else '│  '}"
                        print(f"{guide}{color('↳ would also recreate: ' + ', '.join(followers), Colors.DIM)}")
            else:
                total_err += 1; errors.append((container["image"], res["error"]))
                print(f"\r{prefix}{color('!', Colors.RED)} {name:<{name_w}} {color('check failed', Colors.RED)}\033[K")

    print("\n" + "─" * 55)
    print(color(f"✓ {total_cur} up to date", Colors.GREEN))
    if total_upd:
        noun = "would be upgraded (dry run, nothing changed)" if is_upgrade else "update(s) available"
        print(color(f"↑ {total_upd} {noun}", Colors.YELLOW))
    if total_unk: print(color(f"? {total_unk} could not be verified without pulling", Colors.DIM))
    if total_err: print(color(f"! {total_err} error(s)", Colors.RED))
    notes.print_deferred()
    if errors:
        print("\n" + color("Check details:", Colors.BOLD))
        for item, err in errors: print(f"  {color('!', Colors.RED)} {item}\n    {color(err, Colors.DIM)}")
    print()

def _upgrade_project(project, containers, items):
    """
    One project's whole upgrade, as fast as it safely goes:
      1. save rollback snapshots and pull every new image -- at the same time
      2. recreate all upgraded services in one `compose up`
      3. re-attach services that share an upgraded one's network
      4. watch every recreated container's health -- at the same time
    Returns [(container, ok, label, detail, change, notes)] and follower rows.
    """
    services = [c["service"] for c, _, _ in items]
    by_service = {c["service"]: c for c in containers}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(items) + 2) as pool:
        snapshots = [pool.submit(docker_api.snapshot_service, project, c) for c, _, _ in items]
        dependency_future = pool.submit(docker_api.get_dependency_map, project)
        pulled = docker_api.pull_services(project, services)
        for f in snapshots:
            f.result()  # the old image must be saved before its container is replaced
        dependency_map = dependency_future.result()

    ready = [s for s in services if pulled[s][0]]
    recreated = docker_api.recreate_services(project, ready)
    upgraded = [s for s in ready if recreated[s][0]]

    parents = {}
    for s in upgraded:
        for dependent in docker_api.dependents_of(dependency_map, s):
            if dependent not in services:  # upgraded services were recreated already, in order
                parents.setdefault(dependent, s)
    follower_results = docker_api.recreate_followers(project, sorted(parents)) if parents else []

    to_watch = [by_service[s]["name"] for s in upgraded]
    to_watch += [by_service[f]["name"] for f, ok, _ in follower_results if ok and f in by_service]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(to_watch))) as pool:
        health = dict(zip(to_watch, pool.map(docker_api.verify_container_health, to_watch)))

    rows = []
    for c, running_info, target_info in items:
        s = c["service"]
        if not pulled[s][0]:
            rows.append((c, False, "pull failed", pulled[s][1], None, None))
        elif not recreated[s][0]:
            rows.append((c, False, "recreate failed", recreated[s][1], None, None))
        else:
            ok, detail = health[c["name"]]
            installed = versions.local_version(c["image"]) or target_info  # what actually got installed
            if target_info and target_info.get("version") and not installed.get("version"):
                installed = dict(installed, version=target_info["version"])  # found via its version tag
            rows.append((c, ok, "upgraded & verified" if ok else "upgraded but unstable", detail,
                         versions.describe_change(running_info, installed),
                         versions.notes_link(installed, running_info)))
    followers = []
    for f, ok, err in follower_results:
        if ok and f in by_service:
            ok, err = health[by_service[f]["name"]]
        followers.append((by_service.get(f, {}).get("short_name", f), ok, err, parents[f]))
    return rows, followers

def _upgrade(project_data, image_futures, local_future):
    """`docky upgrade`: check everything, upgrade every project at once, then report."""
    _spin(list(image_futures.values()), lambda done, total: f"Checking {total} images… {done}/{total}")
    local_infos = local_future.result()

    plan, current, errors = [], 0, []
    for data in project_data:
        items = []
        for c in data["containers"]:
            res = image_futures[c["image"]].result()
            status, running_info, target_info, _ = classify(c, res, local_infos)
            if status == "update":
                items.append((c, running_info, target_info))
            elif status == "current":
                current += 1
            else:
                errors.append((c["short_name"], res.get("error") or "could not be checked"))
        if items:
            plan.append((data["project"], data["containers"], items))

    count = sum(len(items) for _, _, items in plan)
    print(f"{color('✓', Colors.GREEN)} {color(f'Checked {len(image_futures)} images · {count} to upgrade · {current} already up to date', Colors.DIM)}")
    results = []
    if plan:
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(plan)) as pool:
            futures = [pool.submit(_upgrade_project, project, containers, items) for project, containers, items in plan]
            _spin(futures, lambda done, total: f"Upgrading {count} container{'s' if count != 1 else ''} in {total} project{'s' if total != 1 else ''}… {done}/{total} done")
            results = [f.result() for f in futures]
        print(f"{color('✓', Colors.GREEN)} {color(f'Upgrade finished in {time.monotonic() - started:.0f}s', Colors.DIM)}\n")

    # One tree: only what changed.
    notes = Notes()
    name_w = max([len(c["short_name"]) for _, _, items in plan for c, _, _ in items] + [12]) + 1
    upgraded, unstable, failed = 0, [], []
    for p_idx, ((project, _, _), (rows, followers)) in enumerate(zip(plan, results)):
        last_p = p_idx == len(plan) - 1
        print(f"{'└─' if last_p else '├─'} {color(project['name'], Colors.CYAN + Colors.BOLD)}")
        stem = "   " if last_p else "│  "
        lines = len(rows) + len(followers)
        for r_idx, (c, ok, label, detail, change, url) in enumerate(rows):
            prefix = f"{stem}{'└─' if r_idx == lines - 1 else '├─'} "
            name = c["short_name"]
            if ok:
                upgraded += 1
                mark, colour = "✓", Colors.GREEN
            elif label == "upgraded but unstable":
                unstable.append((name, detail, project["name"]))
                mark, colour = "!", Colors.RED
            else:
                failed.append((name, detail))
                mark, colour = "✕", Colors.RED
            label_cell = f"{label:<22}"  # fixed width, so the versions line up across rows
            tail = notes.tail(len(prefix) + 2 + name_w + 1 + len(label_cell), change, url, name) if change or (url and url[0]) else ""
            print(f"{prefix}{color(mark, colour)} {name:<{name_w}} {color(label_cell, colour)}{tail}")
        for f_idx, (name, ok, err, parent) in enumerate(followers):
            prefix = f"{stem}{'└─' if len(rows) + f_idx == lines - 1 else '├─'} "
            if ok:
                print(f"{prefix}{color('↳', Colors.GREEN)} {name:<{name_w}} {color(f'recreated to follow {parent}', Colors.GREEN)}")
            else:
                unstable.append((name, err, project["name"]))
                print(f"{prefix}{color('↳', Colors.RED)} {name:<{name_w}} {color(f'recreate failed (follows {parent})', Colors.RED)}")

    print("\n" + "─" * 55)
    if upgraded: print(color(f"✓ {upgraded} upgraded & verified", Colors.GREEN))
    print(color(f"✓ {current} already up to date", Colors.GREEN))
    if unstable: print(color(f"! {len(unstable)} upgraded but unstable", Colors.RED))
    if failed: print(color(f"✕ {len(failed)} failed", Colors.RED))
    if errors: print(color(f"? {len(errors)} could not be checked", Colors.YELLOW))
    notes.print_deferred()

    for heading, entries in (("Failed:", failed), ("Could not be checked:", errors)):
        if entries:
            print("\n" + color(heading, Colors.BOLD))
            for item, err in entries:
                print(f"  {color('!', Colors.RED)} {item}\n    {color(err, Colors.DIM)}")
    if unstable:
        print("\n" + color("Upgraded but did not verify as healthy:", Colors.BOLD))
        for item, detail, _ in unstable:
            print(f"  {color('!', Colors.RED)} {item}\n    {color(detail, Colors.DIM)}")
        for proj in sorted({u[2] for u in unstable}):
            print(color(f"  Undo with: docky rollback {proj}", Colors.DIM))

    if upgraded:
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
