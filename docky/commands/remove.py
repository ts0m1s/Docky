# docky/commands/remove.py
"""Removing a whole project: plan first, then confirm each part."""
import sys
import shlex
from ..utils import Colors, color
from .. import docker_api
from .common import select_projects

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
