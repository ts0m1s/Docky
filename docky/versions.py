# versions.py
"""
Human-readable versions for images, so updates can say "4.0.20-ls325 ->
4.0.20-ls326" instead of only "update available".

Versions come from the image's labels -- the local image via `docker
image inspect`, the registry's via `docker buildx imagetools inspect`,
which reads the image config without pulling anything. Many images tag
everything `latest` and put nothing useful in their labels; those fall
back to the build date, which still says how old each side is.
"""
import json
import os
import re
import threading
from functools import lru_cache
from .utils import run_command
from . import registry

VERSION_LABELS = ("org.opencontainers.image.version", "org.label-schema.version", "version")
# Label values that name a channel, not a version.
NOT_A_VERSION = {"", "latest", "main", "master", "edge", "nightly", "dev", "develop", "stable", "release", "unknown"}

def _from_config(labels, created):
    labels = labels or {}
    version = None
    for key in VERSION_LABELS:
        value = (labels.get(key) or "").strip()
        if value.lower() not in NOT_A_VERSION:
            version = value
            break
    if not version:
        # linuxserver.io: "Linuxserver.io version:- 4.0.20.3014-ls325 Build-date:- ..."
        match = re.search(r"version:-\s*(\S+)", labels.get("build_version", ""))
        if match:
            version = match.group(1)
    source = (labels.get("org.opencontainers.image.source") or labels.get("org.label-schema.vcs-url") or "").strip()
    return {
        "version": version,
        "created": (created or "")[:10] or None,  # YYYY-MM-DD
        "source": source.removesuffix(".git") or None,
    }

def local_version(image):
    """Version info for a local image (tag or ID), or None."""
    ok, out, _ = run_command(["docker", "image", "inspect", image, "--format", "{{json .Config.Labels}}|{{.Created}}"])
    if not ok or "|" not in out:
        return None
    labels_json, _, created = out.rpartition("|")
    try:
        labels = json.loads(labels_json) or {}
    except json.JSONDecodeError:
        labels = {}
    return _from_config(labels, created)

def local_versions(refs):
    """
    {ref: version info} for many local images (tags or IDs) in a single
    `docker image inspect` -- one call instead of one per container.
    Images that don't exist locally are simply missing from the result.
    """
    refs = sorted({r for r in refs if r})
    if not refs:
        return {}
    _, out, _ = run_command(["docker", "image", "inspect", *refs])  # stdout still lists the ones found
    try:
        images = json.loads(out) if out else []
    except json.JSONDecodeError:
        return {}
    by_key = {}
    for image in images:
        info = _from_config((image.get("Config") or {}).get("Labels") or {}, image.get("Created"))
        for key in [image.get("Id")] + (image.get("RepoTags") or []):
            if key:
                by_key[key] = info
    return {ref: by_key[ref] for ref in refs if ref in by_key}

@lru_cache(maxsize=1)
def _platform():
    ok, out, _ = run_command(["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"])
    return out if ok and out else "linux/amd64"

# --- Remembered remote versions -----------------------------------------------
# An image digest never changes what it points to, so once a digest's version
# is known it's known for good. Remembering it means later checks only ask
# "has the tag moved?" -- a request Docker Hub doesn't rate-limit.

_cache, _cache_lock = None, threading.Lock()
CACHE_SIZE = 500

def _cache_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "docky", "remote-versions.json")

def _cache_get(key):
    global _cache
    with _cache_lock:
        if _cache is None:
            try:
                with open(_cache_path()) as f:
                    _cache = json.load(f)
            except (OSError, ValueError):
                _cache = {}
        return _cache.get(key)

def _cache_put(key, info):
    with _cache_lock:
        _cache[key] = {k: info.get(k) for k in ("version", "created", "source")}
        while len(_cache) > CACHE_SIZE:
            _cache.pop(next(iter(_cache)))  # oldest first
        path = _cache_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                json.dump(_cache, f)
            os.replace(tmp, path)
        except OSError:
            pass  # can't remember it this time; next run asks again

def remote_version(image, digest=None):
    """
    Version info for what the registry serves for this tag right now.
    Tried in order, stopping at the first that knows:
      1. remembered for this digest
      2. the registry itself (labels of our platform's image)
      3. Docker Hub's website: the version tag pointing at the same image
         (also when the labels only say "latest") -- it still answers when
         the registry's pull limit is used up
      4. the docker CLI (private registries, credentials)
    If nothing can tell, the result says why instead of guessing.
    """
    key = f"{image}@{digest}" if digest else None
    if key:
        cached = _cache_get(key)
        if cached:
            return cached

    info, reason = None, None
    try:
        data = registry.remote_config(image, _platform())
        info = _from_config((data.get("config") or {}).get("Labels"), data.get("created"))
    except registry.RateLimited as e:
        reason = str(e)
    except (registry.RegistryError, ValueError, AttributeError):
        info = _cli_remote_version(image)

    if info is None or not info.get("version"):
        try:
            hub = registry.docker_hub_tag(image)
            if not digest or hub["digest"] == digest:
                if info is None:
                    info = {"version": hub["version"], "created": hub["created"], "source": None}
                elif hub["version"]:
                    info = dict(info, version=hub["version"])
        except registry.RegistryError:
            pass

    if info is None or not (info.get("version") or info.get("created")):
        return {"version": None, "created": None, "source": None,
                "unavailable": reason or "the registry didn't answer"}
    if key:
        _cache_put(key, info)
    return info

def _cli_remote_version(image):
    """Version info via `docker buildx imagetools` -- slow, but uses your docker credentials."""
    ok, out, _ = run_command(["docker", "buildx", "imagetools", "inspect", image, "--format", "{{json .Image}}"])
    if not ok or not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    # Multi-arch images come back keyed by platform; pick ours.
    if isinstance(data, dict) and "config" not in data:
        platform = _platform()
        config = data.get(platform) or next(
            (v for k, v in data.items() if k.startswith(platform + "/") or platform.startswith(k)), None)
        if config is None:
            return None
        data = config
    return _from_config((data.get("config") or {}).get("Labels"), data.get("created"))

def describe_change(old, new):
    """
    "4.0.20-ls325 → 4.0.20-ls326", or build dates when there's no real
    version. Returns None when neither side says anything useful.
    """
    if not old and not new:
        return None
    old, new = old or {}, new or {}
    ov, nv = old.get("version"), new.get("version")
    if new.get("unavailable"):
        # Never a bare "?": say what's known and why the rest isn't.
        left = ov or (f"built {old['created']}" if old.get("created") else "current")
        return f"{left} → newer image (version unknown: {new['unavailable']})"
    if ov and nv and ov != nv:
        return f"{ov} → {nv}"
    if ov and nv:  # same version string, new build
        if old.get("created") and new.get("created") and old["created"] != new["created"]:
            return f"{ov} (rebuilt {old['created']} → {new['created']})"
        return f"{ov} (rebuilt)"
    if old.get("created") or new.get("created"):
        left = ov or (f"built {old['created']}" if old.get("created") else "?")
        right = nv or (f"built {new['created']}" if new.get("created") else "?")
        return f"{left} → {right}"
    return None

def describe_current(info):
    """Short label for an up-to-date image: its version, or its build date."""
    if not info:
        return None
    if info.get("version"):
        return info["version"]
    return f"built {info['created']}" if info.get("created") else None

def release_notes_url(*infos):
    """
    Releases page for images that say where their source lives (GitHub/
    GitLab/Codeberg). Takes the first info that names a source -- the new
    image's, else the running one's (a version from hub.docker.com has none).
    """
    source = next(((i or {}).get("source") for i in infos if (i or {}).get("source")), "") or ""
    if re.match(r"https://(github\.com|gitlab\.com|codeberg\.org)/[^/]+/[^/]+/?$", source):
        return source.rstrip("/") + "/releases"
    return None
