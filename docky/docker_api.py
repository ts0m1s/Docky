# docker_api.py
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from .utils import run_command

COMPOSE_FILENAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
DEFAULT_ROOT = Path.home() / "docker"
SCAN_DEPTH = 2

LABEL_PROJECT = "com.docker.compose.project"
LABEL_WORKDIR = "com.docker.compose.project.working_dir"
LABEL_CONFIG_FILES = "com.docker.compose.project.config_files"
LABEL_ENV_FILE = "com.docker.compose.project.environment_file"

def scan_roots():
    """
    Folders to scan for Compose projects that Docker doesn't know
    about yet (never started, or all containers removed).

    DOCKY_ROOT takes one or more paths separated by ':' (like PATH).
    Unset, Docky falls back to ~/docker.
    """
    raw = os.environ.get("DOCKY_ROOT", "")
    roots = [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]
    return roots or [DEFAULT_ROOT]

def _override_for(compose_file):
    """The override file Compose would pick up on its own, if any."""
    stem = compose_file.stem
    for ext in (".yaml", ".yml"):
        candidate = compose_file.with_name(f"{stem}.override{ext}")
        if candidate.exists():
            return candidate
    return None

def _subdirs(directory):
    try:
        return sorted(d for d in directory.iterdir() if d.is_dir() and not d.name.startswith("."))
    except OSError:
        # Unreadable (e.g. a root-owned data folder) -- skip, don't crash.
        return []

def _compose_file_in(directory):
    for filename in COMPOSE_FILENAMES:
        candidate = directory / filename
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            return None
    return None

def _discover_from_docker():
    """
    Every Compose project Docker has containers for, wherever it lives.

    Compose stamps each container with the project's name, working
    directory and the exact compose files it was started from
    (overrides included). That's the ground truth -- no guessing
    about folder layout.
    """
    fmt = "|".join('{{.Label "%s"}}' % l for l in (LABEL_PROJECT, LABEL_WORKDIR, LABEL_CONFIG_FILES, LABEL_ENV_FILE))
    success, output, _ = run_command(["docker", "ps", "-a", "--filter", f"label={LABEL_PROJECT}", "--format", fmt])
    if not success:
        return []

    found = {}
    for line in output.splitlines():
        parts = (line.split("|") + ["", "", "", ""])[:4]
        name, workdir, config_files, env_files = (p.strip() for p in parts)
        if not name or name in found:
            continue
        base = Path(workdir) if workdir else None

        def resolve(raw):
            path = Path(raw)
            return path if path.is_absolute() or base is None else base / path

        files = [resolve(f) for f in config_files.split(",") if f.strip()]
        envs = [resolve(e) for e in env_files.split(",") if e.strip()]
        found[name] = {
            "name": name,
            "project_name": name,
            "path": base or (files[0].parent if files else None),
            "files": files,
            "env_files": [e for e in envs if e.exists()],
            "source": "docker",
            "missing": [f for f in files if not f.exists()] or ([] if files else ["(no compose file recorded)"]),
        }
    return list(found.values())

def _discover_from_folders(roots):
    """Compose files on disk under the scan roots (and the roots themselves)."""
    projects = []
    for root in roots:
        level = [root] if root.is_dir() else []
        candidates = []
        for depth in range(SCAN_DEPTH + 1):
            candidates.extend(level)
            if depth < SCAN_DEPTH:
                level = [child for d in level for child in _subdirs(d)]
        for directory in candidates:
            compose_file = _compose_file_in(directory)
            if not compose_file:
                continue
            override = _override_for(compose_file)
            projects.append({
                "name": directory.resolve().name,
                "project_name": None,  # let Compose derive it (honours a `name:` key)
                "path": directory,
                "files": [compose_file] + ([override] if override else []),
                "env_files": [],
                "source": "folder",
                "missing": [],
            })
    return projects

def _same_location(a, b):
    try:
        return a is not None and b is not None and Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False

def discover_projects():
    """
    All Compose projects, from both sources, merged:
      {"projects": usable projects, "stale": deployed projects whose
       compose files are gone (moved or deleted folder)}

    Docker's record wins when both sources see the same project, since
    it knows the exact files and project name the stack runs with.
    """
    from_docker = _discover_from_docker()
    projects = [p for p in from_docker if not p["missing"]]
    stale = [p for p in from_docker if p["missing"]]

    known_files = {f.resolve() for p in projects for f in p["files"]}
    for candidate in _discover_from_folders(scan_roots()):
        if candidate["files"][0].resolve() in known_files:
            continue
        if any(_same_location(candidate["path"], p["path"]) for p in projects):
            continue
        # A stale Docker record whose folder still has a compose file (e.g. it
        # was renamed from docker-compose.yml to compose.yaml) is that project:
        # keep the name its containers run under.
        match = next((s for s in stale if _same_location(candidate["path"], s["path"])), None)
        if match:
            stale.remove(match)
            candidate.update(name=match["name"], project_name=match["project_name"])
        projects.append(candidate)
        known_files.update(f.resolve() for f in candidate["files"])

    projects.sort(key=lambda p: (p["name"].lower(), str(p["path"])))
    stale.sort(key=lambda p: p["name"].lower())
    return {"projects": projects, "stale": stale}

def find_projects():
    return discover_projects()["projects"]

def compose_cmd(project, *args):
    """
    `docker compose ...` pointed at exactly this project.

    Every compose file is passed with its own -f: giving -f at all turns
    off Compose's automatic pick-up of docker-compose.override.yml, so
    the override has to be listed explicitly or its settings are lost.
    """
    cmd = ["docker", "compose"]
    if project.get("project_name"):
        cmd += ["-p", project["project_name"]]
    if project.get("path"):
        cmd += ["--project-directory", str(project["path"])]
    for f in project["files"]:
        cmd += ["-f", str(f)]
    for e in project.get("env_files", []):
        cmd += ["--env-file", str(e)]
    return cmd + list(args)

def sanitize_project_name(name):
    """Compose's own normalisation: lowercase, keep only [a-z0-9_-]."""
    return re.sub(r"[^a-z0-9_-]", "", name.lower())

def resolve_project_names(project):
    """
    Every name Compose could have stamped on this project's volumes.

    The folder name alone isn't reliable: Compose sanitizes it
    ("My App" -> "myapp") and a `name:` key in the compose file (or
    COMPOSE_PROJECT_NAME) overrides it entirely. Ask Compose for the
    real name, and keep the sanitized folder name as a fallback so a
    failed lookup only ever widens what counts as "known".
    """
    names = {project["name"], sanitize_project_name(project["name"])}
    success, output, _ = run_command(
        compose_cmd(project, "config", "--format", "json")
    )
    if success and output:
        try:
            resolved = json.loads(output).get("name")
            if resolved:
                names.add(resolved)
        except json.JSONDecodeError:
            pass
    return names

def derive_short_name(name, project_name):
    prefix = f"{project_name}-"
    short_name = name[len(prefix):] if name.startswith(prefix) else name
    short_name = short_name[:-2] if short_name.endswith("-1") else short_name
    return short_name

def get_project_containers(project):
    success, output, _ = run_command(
        compose_cmd(project, "ps", "-a", "--format", "{{.Name}}|{{.State}}|{{.Status}}|{{.Image}}|{{.Service}}")
    )
    if not success:
        return []

    containers = []
    for line in output.splitlines():
        parts = line.split("|")
        if len(parts) < 4: continue
        name, state, status, image = parts[0], parts[1], parts[2], parts[3]

        short_name = derive_short_name(name, project["name"])
        service = parts[4] if len(parts) >= 5 and parts[4] and "{{" not in parts[4] else short_name

        inspect_success, inspect_output, _ = run_command(["docker", "container", "inspect", name, "--format", "{{.Image}}|{{.Config.Image}}|{{.HostConfig.NetworkMode}}"])
        running_id, config_image, network_mode = "", image, ""
        if inspect_success and inspect_output and inspect_output.count("|") >= 2:
            running_id, config_image, network_mode = inspect_output.split("|", 2)

        containers.append({"name": name, "short_name": short_name, "state": state, "status": status, "image": config_image, "running_id": running_id, "service": service, "network_mode": network_mode})
    return containers

def get_containers_light(project):
    """
    Lightweight container listing for the live 'top' monitor.

    get_project_containers() above does one `docker container
    inspect` per container to resolve the real image reference --
    needed for update checks, but wasted work on a view that just
    refreshed a second ago. This version is just `docker compose
    ps` with two fields, so it stays cheap enough to re-run every
    refresh tick.
    """
    success, output, _ = run_command(
        compose_cmd(project, "ps", "-a", "--format", "{{.Name}}|{{.State}}")
    )
    if not success:
        return []

    containers = []
    for line in output.splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        name, state = parts[0], parts[1]
        containers.append({
            "name": name,
            "short_name": derive_short_name(name, project["name"]),
            "state": state,
        })
    return containers

def fetch_stats():
    """
    One-shot snapshot of live resource usage for every running
    container on the host. This is a single `docker stats` call
    regardless of how many projects or containers exist -- it's
    what keeps each refresh tick of the live monitor cheap.
    """
    success, output, _ = run_command([
        "docker", "stats", "--no-stream", "--format",
        "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.NetIO}}|{{.BlockIO}}",
    ])

    stats_map = {}
    if not success:
        return stats_map

    for line in output.splitlines():
        parts = line.split("|")
        if len(parts) < 6:
            continue
        name, cpu, mem_usage, mem_pct, net, blk = parts[:6]
        used, _, limit = mem_usage.partition(" / ")
        stats_map[name] = {
            "cpu": cpu,
            "mem_used": used,
            "mem_limit": limit,  # the container's limit, or the host's RAM when it has none
            "mem_pct": mem_pct,
            "net": net,
            "blk": blk,
        }
    return stats_map

def network_owners():
    """
    {container name: name of the container whose network it shares} for
    containers started with network_mode: container:X / service:X (e.g.
    qBittorrent -> gluetun). Their network counters are the owner's.
    """
    success, ids, _ = run_command(["docker", "ps", "-q", "--no-trunc"])
    if not success or not ids:
        return {}
    success, output, _ = run_command(["docker", "inspect", "--format", "{{.Id}}|{{.Name}}|{{.HostConfig.NetworkMode}}"] + ids.split())
    if not success:
        return {}
    rows = [line.split("|", 2) for line in output.splitlines() if line.count("|") == 2]
    name_by_id = {cid: name.lstrip("/") for cid, name, _ in rows}
    owners = {}
    for cid, name, mode in rows:
        if mode.startswith("container:"):
            target = mode.split(":", 1)[1]
            owner = name_by_id.get(target) or next((n for i, n in name_by_id.items() if i.startswith(target)), target)
            owners[name.lstrip("/")] = owner
    return owners

def get_local_image_id(image):
    succ, out, err = run_command(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
    return (out, None) if succ and out else (None, err or "failed to read id")

def check_image_via_pull(image):
    local_id_before, err = get_local_image_id(image)
    if not local_id_before:
        return {"status": "error", "local": None, "remote": None, "local_id": None, "error": f"local: {err}", "checked_via": "pull"}

    success, _, err = run_command(["docker", "pull", image])
    if not success:
        return {"status": "error", "local": local_id_before, "remote": None, "local_id": local_id_before, "error": f"registry: {err}", "checked_via": "pull"}

    local_id_after, err = get_local_image_id(image)
    status = "current" if local_id_before == local_id_after else "update"
    return {"status": status, "local": local_id_before, "remote": local_id_after, "local_id": local_id_after, "error": None, "checked_via": "pull"}

def check_image(image, allow_pull=True):
    """
    Compare local vs. registry digest. When digests can't be
    compared, the fallback is a real `docker pull`, which changes
    local state -- pass allow_pull=False (dry runs) to report
    "unknown" instead of mutating anything.
    """
    local_id, _ = get_local_image_id(image)
    
    succ, out, _ = run_command(["docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}"])
    local_digest = None
    if succ and out:
        try:
            digests = json.loads(out)
            if digests:
                match = re.search(r"@sha256:([a-f0-9]{64})$", digests[0])
                if match: local_digest = f"sha256:{match.group(1)}"
        except json.JSONDecodeError: pass

    succ, out, _ = run_command(["docker", "buildx", "imagetools", "inspect", image])
    remote_digest = None
    if succ:
        for line in out.splitlines():
            match = re.match(r"^Digest:\s+(sha256:[a-f0-9]{64})$", line.strip())
            if match:
                remote_digest = match.group(1)
                break

    if not local_digest or not remote_digest:
        if not allow_pull:
            return {"status": "unknown", "local": local_digest, "remote": remote_digest, "local_id": local_id,
                    "error": "digests unavailable; verifying would require pulling", "checked_via": "none"}
        res = check_image_via_pull(image)
        if res.get("local_id") is None: res["local_id"] = local_id
        return res

    status = "current" if local_digest == remote_digest else "update"
    return {"status": status, "local": local_digest, "remote": remote_digest, "local_id": local_id, "error": None, "checked_via": "digest"}

def get_dependency_map(project):
    """
    Which services have to follow another one when it is recreated:
    {service: [dependents]}.

    A service that joins another's network namespace
    (`network_mode: service:X` / `container:X`, e.g. qBittorrent
    behind gluetun) is bound to that one container instance. When
    X is recreated on its own, the dependent keeps pointing at the
    old, deleted container and ends up with no network at all --
    it still shows as "running". `depends_on` entries with
    `restart: true` are treated the same way: the author asked for
    them to follow.
    """
    success, output, _ = run_command(
        compose_cmd(project, "config", "--format", "json")
    )
    if not success or not output:
        return {}
    try:
        services = json.loads(output).get("services", {})
    except json.JSONDecodeError:
        return {}

    by_container_name = {s["container_name"]: n for n, s in services.items() if s.get("container_name")}
    dependents = {}
    for name, svc in services.items():
        parents = set()
        mode = svc.get("network_mode") or ""
        if mode.startswith("service:"):
            parents.add(mode.split(":", 1)[1])
        elif mode.startswith("container:") and mode.split(":", 1)[1] in by_container_name:
            parents.add(by_container_name[mode.split(":", 1)[1]])
        depends_on = svc.get("depends_on") or {}
        if isinstance(depends_on, dict):
            parents.update(p for p, opts in depends_on.items() if isinstance(opts, dict) and opts.get("restart"))
        for parent in parents:
            if parent != name and parent in services:
                dependents.setdefault(parent, set()).add(name)
    return {parent: sorted(children) for parent, children in dependents.items()}

def dependents_of(dependency_map, service):
    """Everything downstream of `service`, transitively, parents before children."""
    ordered, queue, seen = [], [service], {service}
    while queue:
        for child in dependency_map.get(queue.pop(0), []):
            if child not in seen:
                seen.add(child)
                ordered.append(child)
                queue.append(child)
    return ordered

def recreate_dependents(project, service, dependency_map):
    """
    Recreate everything that follows `service` so it re-attaches to
    the new container. Returns [(service, ok, error)].

    --force-recreate is required: a plain restart would reuse the
    stale network reference. --no-deps/--pull never keeps this from
    touching anything but the dependent itself.
    """
    results = []
    for dependent in dependents_of(dependency_map, service):
        succ, _, err = run_command(compose_cmd(
            project, "up", "-d", "--no-deps", "--force-recreate", "--pull", "never", dependent,
        ))
        results.append((dependent, succ, "" if succ else f"up failed: {err}"))
    return results

def find_orphaned_containers(containers):
    """
    Containers whose `network_mode: container:<id>` target no longer
    exists. They still report "running" but have no usable network.
    """
    orphans = []
    for c in containers:
        mode = c.get("network_mode") or ""
        if not mode.startswith("container:"):
            continue
        target = mode.split(":", 1)[1]
        succ, _, _ = run_command(["docker", "container", "inspect", target, "--format", "{{.Id}}"])
        if not succ:
            orphans.append(c)
    return orphans

def upgrade_service(project, service_name):
    succ, _, err = run_command(compose_cmd(project, "pull", service_name))
    if not succ: return False, f"pull failed: {err}"
    succ, _, err = run_command(compose_cmd(project, "up", "-d", service_name))
    if not succ: return False, f"up failed: {err}"
    return True, ""

ROLLBACK_LABEL = "docky.rollback"

def rollback_state_path():
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "docky" / "rollback.json"

def load_rollback_state():
    try:
        return json.loads(rollback_state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}

def save_rollback_state(state):
    path = rollback_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)

def rollback_tag(project_name, service):
    return f"docky-rollback/{sanitize_project_name(project_name)}-{sanitize_project_name(service)}:previous"

def snapshot_service(project, container):
    """
    Keep a copy of the image a service is running right now, so a
    bad upgrade can be undone. Only one level is kept per service.

    A bare tag would stop the old image from being pruned as
    dangling, but `docker system prune -a` (used by sweep) would
    still remove it, so the snapshot is rebuilt with a label
    (metadata only, no new layers) that sweep filters on.
    """
    image_id = container.get("running_id")
    if not image_id:
        return False
    tag = rollback_tag(project["name"], container["service"])
    succ, _, _ = run_command(["docker", "tag", image_id, tag])
    if not succ:
        return False
    result = subprocess.run(
        ["docker", "build", "-q", "--label", f"{ROLLBACK_LABEL}=true", "-t", tag, "-"],
        input=f"FROM {tag}\n", capture_output=True, text=True,
    )
    labelled = result.returncode == 0

    state = load_rollback_state()
    state.setdefault(project["name"], {})[container["service"]] = {
        "image": container["image"],
        "tag": tag,
        "previous_id": image_id,
        "protected": labelled,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_rollback_state(state)
    return True

def rollback_service(project, service, entry):
    """Point the service's image reference back at the snapshot and recreate it."""
    image_ref = entry["image"]
    if "@" in image_ref:
        return False, "image is pinned by digest in the compose file; edit it there instead"
    succ, _, _ = run_command(["docker", "image", "inspect", entry["tag"]])
    if not succ:
        return False, "the saved image no longer exists (it was removed from Docker)"
    succ, _, err = run_command(["docker", "tag", entry["tag"], image_ref])
    if not succ:
        return False, f"tag failed: {err}"
    succ, _, err = run_command(compose_cmd(
        project, "up", "-d", "--no-deps", "--pull", "never", service,
    ))
    if not succ:
        return False, f"up failed: {err}"
    return True, ""

def forget_snapshot(project_name, service):
    state = load_rollback_state()
    entry = state.get(project_name, {}).pop(service, None)
    if not state.get(project_name, True):
        state.pop(project_name, None)
    save_rollback_state(state)
    if entry:
        run_command(["docker", "rmi", entry["tag"]])

def verify_container_health(container_name, healthcheck_timeout=30, no_healthcheck_grace=6, poll_interval=1.0):
    """
    Confirm a container actually came back up cleanly after
    'up -d', rather than trusting the command's exit code alone --
    a container can be recreated successfully and still crash-loop
    or fail its healthcheck seconds later.

    Containers WITH a healthcheck get the full timeout, since some
    apps (databases warming up, etc.) legitimately take a while to
    report their first result. Containers WITHOUT one only get a
    short grace window -- most real startup failures (bad env var,
    port conflict, permission error) show up as an exit or a
    restart within the first couple seconds, so there's no reason
    to burn the full 30s waiting on something that was never going
    to report "healthy" in the first place.

    Returns:
        (ok: bool, detail: str)
    """
    def inspect_once():
        success, output, _ = run_command([
            "docker", "inspect", container_name,
            "--format",
            "{{.State.Status}}|{{.RestartCount}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
        ])
        if not success or "|" not in output:
            return None
        status, restarts_str, health = output.split("|", 2)
        restarts = int(restarts_str) if restarts_str.isdigit() else 0
        return status, restarts, health

    reading = inspect_once()
    if reading is None:
        return False, "could not inspect container"

    status, baseline_restarts, health = reading
    has_healthcheck = health != "none"
    timeout = healthcheck_timeout if has_healthcheck else no_healthcheck_grace
    deadline = time.time() + timeout
    restarts = baseline_restarts

    while True:
        if status == "exited":
            return False, "container exited shortly after starting"

        if health == "unhealthy":
            return False, "healthcheck reports unhealthy"

        if restarts > baseline_restarts + 1:
            return False, f"container is restart-looping ({restarts} restarts)"

        if health == "healthy":
            return True, "healthcheck passed"

        if time.time() >= deadline:
            break

        time.sleep(poll_interval)
        reading = inspect_once()
        if reading is not None:
            status, restarts, health = reading

    if has_healthcheck:
        if health == "starting":
            return False, "healthcheck did not become healthy in time"
        return False, f"container did not stabilize (status: {status})"

    if status == "running":
        return True, "running (no healthcheck defined)"
    return False, f"container did not stabilize (status: {status})"

def get_volume_sizes():
    """
    Parse `docker system df -v` for real per-volume disk usage.

    This is the only place Docker exposes volume sizes at all --
    `docker volume ls` has no size field, since computing it means
    walking the filesystem. That's also why this is only ever
    called for an on-demand report like orphan detection, never on
    a refresh loop like 'top'.
    """
    success, output, _ = run_command(["docker", "system", "df", "-v"])
    if not success:
        return {}

    sizes = {}
    state = "seek_section"

    for line in output.splitlines():
        stripped = line.strip()

        if state == "seek_section":
            if stripped.startswith("Local Volumes"):
                state = "seek_header"
            continue

        if state == "seek_header":
            if stripped.startswith("VOLUME NAME"):
                state = "data"
            continue

        if state == "data":
            if not stripped:
                break
            columns = stripped.split()
            if len(columns) >= 3:
                sizes[columns[0]] = columns[-1]

    return sizes

def get_all_volumes():
    """
    Every volume on the host, with its Compose project label (if
    Compose created it) and on-disk size (if Docker has already
    computed one).

    A volume's "project" here is the value Compose stamped on it
    at creation time -- it's what lets orphan detection tell "this
    volume belongs to a project that still exists" apart from
    "this volume's project folder is gone."
    """
    success, output, _ = run_command(
        ["docker", "volume", "ls", "--format", "{{.Name}}|{{.Labels}}"]
    )
    if not success:
        return []

    volumes = []
    for line in output.splitlines():
        if not line:
            continue
        parts = line.split("|", 1)
        name = parts[0]
        labels_str = parts[1] if len(parts) > 1 else ""

        labels = {}
        for pair in labels_str.split(","):
            if "=" in pair:
                key, _, value = pair.partition("=")
                labels[key.strip()] = value.strip()

        volumes.append({
            "name": name,
            "project": labels.get("com.docker.compose.project"),
        })

    sizes = get_volume_sizes()
    for volume in volumes:
        volume["size"] = sizes.get(volume["name"])

    return volumes

def remove_volume(name):
    success, _, err = run_command(["docker", "volume", "rm", name])
    return success, err
# --- Removing a whole project -------------------------------------------

def compose_project_name(project):
    """The name Compose labels this project's resources with."""
    if project.get("project_name"):
        return project["project_name"]
    success, output, _ = run_command(compose_cmd(project, "config", "--format", "json"))
    if success and output:
        try:
            resolved = json.loads(output).get("name")
            if resolved:
                return resolved
        except json.JSONDecodeError:
            pass
    return sanitize_project_name(project["name"])

def _lines(command):
    success, output, _ = run_command(command)
    return [l for l in output.splitlines() if l.strip()] if success else []

def _is_anonymous_volume(name):
    return bool(re.fullmatch(r"[0-9a-f]{64}", name))

def _inside(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError):
        return False

def folder_removal_block(path, other_projects):
    """Why a project folder must not be deleted, or None if it's safe to offer."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        return "path can't be resolved"
    home = Path.home().resolve()
    if resolved == Path("/") or resolved == home or _inside(home, resolved):
        return "it is (or contains) your home folder"
    if len(resolved.parts) < 3:
        return "it is a top-level system folder"
    for root in scan_roots():
        if _same_location(resolved, root):
            return "it is a DOCKY_ROOT scan folder, not a single project"
    for other in other_projects:
        if other.get("path") and _inside(other["path"], resolved):
            return f"it also contains project '{other['name']}'"
    return None

def plan_removal(project, other_projects):
    """
    Everything that belongs to `project`, gathered without changing
    anything. Ownership comes from the label Compose stamps on what it
    creates, so external/shared volumes and networks -- which Compose
    never labels with this project -- can't be swept up by mistake.
    """
    name = compose_project_name(project)
    label = f"label={LABEL_PROJECT}={name}"

    containers = []
    for line in _lines(["docker", "ps", "-a", "--filter", label, "--format", "{{.ID}}|{{.Names}}|{{.State}}"]):
        cid, cname, state = (line.split("|") + ["", ""])[:3]
        containers.append({"id": cid, "name": cname, "state": state})

    # What the project's containers run and mount.
    image_ids, mounted_volumes, bind_sources = set(), set(), set()
    if containers:
        fmt = "{{.Image}}|{{json .Mounts}}"
        for line in _lines(["docker", "inspect", "--format", fmt] + [c["id"] for c in containers]):
            image_id, _, mounts_json = line.partition("|")
            image_ids.add(image_id)
            try:
                mounts = json.loads(mounts_json) or []
            except json.JSONDecodeError:
                mounts = []
            for m in mounts:
                if m.get("Type") == "volume" and m.get("Name"):
                    mounted_volumes.add(m["Name"])
                elif m.get("Type") == "bind" and m.get("Source"):
                    bind_sources.add(m["Source"])

    networks = _lines(["docker", "network", "ls", "--filter", label, "--format", "{{.Name}}"])

    sizes = get_volume_sizes()
    labelled_volumes = set(_lines(["docker", "volume", "ls", "--filter", label, "--format", "{{.Name}}"]))
    owned_volumes = labelled_volumes | {v for v in mounted_volumes if _is_anonymous_volume(v)}
    volumes = [{"name": v, "size": sizes.get(v), "anonymous": _is_anonymous_volume(v)} for v in sorted(owned_volumes)]
    kept_volumes = sorted(mounted_volumes - owned_volumes)

    # Images: also the ones a never-started project would use, if present locally.
    if project.get("files") and all(f.exists() for f in project["files"]):
        for ref in _lines(compose_cmd(project, "config", "--images")):
            image_id, _ = get_local_image_id(ref)
            if image_id:
                image_ids.add(image_id)

    used_elsewhere = {}
    all_ids = _lines(["docker", "ps", "-aq"])
    if all_ids:
        fmt = '{{.Image}}|{{index .Config.Labels "%s"}}|{{.Name}}' % LABEL_PROJECT
        for line in _lines(["docker", "inspect", "--format", fmt] + all_ids):
            image_id, owner, cname = (line.split("|") + ["", ""])[:3]
            if owner != name:
                used_elsewhere.setdefault(image_id, owner or cname.lstrip("/"))

    images, kept_images = [], []
    for image_id in sorted(image_ids):
        out = _lines(["docker", "image", "inspect", "--format", "{{.Id}}|{{join .RepoTags \",\"}}|{{.Size}}", image_id])
        if not out:
            continue
        full_id, tags, size = (out[0].split("|") + ["", ""])[:3]
        entry = {"id": full_id, "tags": [t for t in tags.split(",") if t], "size": int(size) if size.isdigit() else 0}
        if full_id in used_elsewhere:
            entry["used_by"] = used_elsewhere[full_id]
            kept_images.append(entry)
        else:
            images.append(entry)

    snapshots = []
    state = load_rollback_state()
    for key in {project["name"], name}:
        for service, entry in state.get(key, {}).items():
            snapshots.append({"state_key": key, "service": service, "tag": entry.get("tag")})

    folder, folder_block = None, None
    if project.get("path") and Path(project["path"]).is_dir():
        folder = Path(project["path"])
        others = [p for p in other_projects if not _same_location(p.get("path"), folder)]
        folder_block = folder_removal_block(folder, others)

    external_binds = sorted(b for b in bind_sources if not (folder and _inside(b, folder)))

    return {
        "name": name,
        "containers": containers,
        "networks": networks,
        "volumes": volumes,
        "kept_volumes": kept_volumes,
        "images": images,
        "kept_images": kept_images,
        "snapshots": snapshots,
        "folder": folder,
        "folder_block": folder_block,
        "external_binds": external_binds,
    }

def remove_containers_and_networks(project, plan):
    """compose down when the files are there; label cleanup for whatever's left."""
    errors = []
    files_ok = project.get("files") and all(f.exists() for f in project["files"])
    if files_ok:
        succ, _, err = run_command(compose_cmd(dict(project, project_name=plan["name"]), "down", "--remove-orphans"))
        if not succ:
            errors.append(f"compose down: {err}")

    label = f"label={LABEL_PROJECT}={plan['name']}"
    leftover = _lines(["docker", "ps", "-aq", "--filter", label])
    if leftover:
        succ, _, err = run_command(["docker", "rm", "-f"] + leftover)
        if not succ:
            errors.append(f"container removal: {err}")
    for network in _lines(["docker", "network", "ls", "--filter", label, "--format", "{{.Name}}"]):
        succ, _, err = run_command(["docker", "network", "rm", network])
        if not succ:
            errors.append(f"network {network}: {err}")
    return errors

def remove_images(plan):
    errors = []
    for image in plan["images"]:
        succ, _, err = run_command(["docker", "image", "rm", "-f", image["id"]])
        if not succ:
            errors.append(f"{', '.join(image['tags']) or image['id'][:19]}: {err}")
    state = load_rollback_state()
    for snap in plan["snapshots"]:
        if snap["tag"]:
            run_command(["docker", "rmi", snap["tag"]])
        state.get(snap["state_key"], {}).pop(snap["service"], None)
        if not state.get(snap["state_key"], True):
            state.pop(snap["state_key"], None)
    if plan["snapshots"]:
        save_rollback_state(state)
    return errors

def _undeletable(path):
    """First entry we couldn't delete, or None. Checked before deleting anything."""
    for current, dirs, files in os.walk(path, onerror=lambda e: (_ for _ in ()).throw(e)):
        if (dirs or files) and not os.access(current, os.W_OK | os.X_OK):
            return os.path.join(current, (dirs + files)[0])
    return None

def remove_folder(path):
    """
    Delete the project folder -- all or nothing. Containers often write
    bind-mounted data as root; finding that halfway through would leave a
    half-deleted project, so permissions are checked up front.
    """
    try:
        blocked = _undeletable(path)
    except OSError as e:
        blocked = e.filename or str(path)
    if blocked:
        return False, f"no permission to delete {blocked} (written by a container as root?); nothing in the folder was deleted"
    try:
        shutil.rmtree(path)
        return True, ""
    except OSError as e:
        return False, str(e)
