# docky/commands/cleanup.py
"""Reclaiming space: sweep, orphans."""
import sys
import re
from ..utils import Colors, color, run_command, get_system_metrics
from .. import docker_api

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
