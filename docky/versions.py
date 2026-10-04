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
    version, channel = None, None
    for key in VERSION_LABELS:
        value = (labels.get(key) or "").strip()
        if value.lower() not in NOT_A_VERSION:
            version = value
            break
        if value and not channel:
            channel = value  # "main", "latest": a branch build, not a release
    if not version:
        # linuxserver.io: "Linuxserver.io version:- 4.0.20.3014-ls325 Build-date:- ..."
        match = re.search(r"version:-\s*(\S+)", labels.get("build_version", ""))
        if match:
            version = match.group(1)
    source = (labels.get("org.opencontainers.image.source") or labels.get("org.label-schema.vcs-url") or "").strip()
    revision = (labels.get("org.opencontainers.image.revision") or labels.get("org.label-schema.vcs-ref") or "").strip()
    return {
        "version": version,
        "created": (created or "")[:10] or None,  # YYYY-MM-DD
        "source": source.removesuffix(".git") or None,
        "revision": revision if re.fullmatch(r"[0-9a-f]{7,40}", revision) else None,  # the commit it was built from
        "channel": channel,
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
        info["digests"] = [d.split("@", 1)[1] for d in image.get("RepoDigests") or [] if "@" in d]
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
        _cache[key] = {k: info.get(k) for k in ("version", "created", "source", "revision", "channel")}
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
    Gathered from, in order:
      1. remembered for this digest (a digest never changes)
      2. the image's own labels: version, commit (revision), source repo
         -- from the registry, or the docker CLI for private registries
      3. no version label: the version tag pointing at the same image
         (latest == v1.40.0), via HEAD requests that aren't counted as pulls
      4. Docker Hub rate-limited: hub.docker.com, which isn't affected
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

    if info is not None and not info.get("version") and digest:
        try:
            tag = registry.version_tag(image, digest)
            if tag:
                info = dict(info, version=tag)
        except registry.RegistryError:
            pass

    if info is None:
        try:
            hub = registry.docker_hub_tag(image)
            if not digest or hub["digest"] == digest:
                info = _from_config({}, hub["created"])
                info["version"] = hub["version"]
        except registry.RegistryError:
            pass

    if info is None or not (info.get("version") or info.get("revision") or info.get("created")):
        return {"version": None, "created": None, "source": None, "revision": None, "channel": None,
                "unavailable": reason or "the registry didn't answer"}
    if key:
        _cache_put(key, info)
    return info

def running_version(info, image, digests):
    """
    The running image's version, filled in from a version tag that still
    points at it when its labels don't say (e.g. an older release). Only
    asked for containers with an update, and remembered per digest.
    """
    if not info or info.get("version") or not digests:
        return info
    for digest in sorted(digests):
        key = f"{image}@{digest}#running"
        cached = _cache_get(key)
        if cached is not None:
            return dict(info, version=cached.get("version")) if cached.get("version") else info
        try:
            tag = registry.version_tag(image, digest)
        except registry.RegistryError:
            continue
        _cache_put(key, {"version": tag})
        if tag:
            return dict(info, version=tag)
    return info

def build_label(info):
    """How to name one image: its version, else its commit on a branch, else its build date."""
    if not info:
        return None
    if info.get("version"):
        return info["version"]
    if info.get("revision"):
        return f"{info.get('channel') or 'build'}@{info['revision'][:7]}"
    if info.get("created"):
        return f"build of {info['created']}"
    return None

def commits_between(source, base, head):
    """'+12 commits' between two builds of a GitHub repo, remembered; None if unknown."""
    if not (source and base and head) or base == head:
        return None
    key = f"compare:{source}:{base}...{head}"
    cached = _cache_get(key)
    if cached is not None:
        return cached.get("version")
    count = registry.github_commits_between(source, base, head)
    if count is not None:
        _cache_put(key, {"version": count})  # stored in the version slot
    return count

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
    What changes, never with a "?":
      releases:        "0.20.0 → 0.21.0"
      same version:    "0.21.0 (rebuilt 2026-10-01 → 2026-10-02)"
      branch builds:   "main@e2e4a06 → main@9f3b2c1 (+12 commits)"
      nothing better:  "build of 2026-09-15 → build of 2026-09-26"
    """
    if not old and not new:
        return None
    old, new = old or {}, new or {}
    left = build_label(old) or "current image"
    if new.get("unavailable"):
        return f"{left} → newer image (version unknown: {new['unavailable']})"
    ov, nv = old.get("version"), new.get("version")
    if ov and nv and ov == nv:  # same version string, new build
        if old.get("revision") and new.get("revision") and old["revision"] != new["revision"]:
            return f"{ov} (rebuilt: {old['revision'][:7]} → {new['revision'][:7]})"
        if old.get("created") and new.get("created") and old["created"] != new["created"]:
            return f"{ov} (rebuilt {old['created']} → {new['created']})"
        return f"{ov} (rebuilt)"
    right = build_label(new) or "new image"
    text = f"{left} → {right}"
    if not nv and old.get("revision") and new.get("revision"):
        count = commits_between(new.get("source") or old.get("source"), old["revision"], new["revision"])
        if count:
            text += f" (+{count} commit{'s' if count != 1 else ''})"
    return text

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

def notes_link(new, old):
    """
    (url, text) for an update row: "release notes ↗" for a new release, or
    "changes ↗" -- the exact commits between two branch builds on GitHub.
    (None, None) when the image doesn't say where its source lives.
    """
    new, old = new or {}, old or {}
    source = (new.get("source") or old.get("source") or "").rstrip("/")
    if (not new.get("version") and old.get("revision") and new.get("revision")
            and old["revision"] != new["revision"] and source.startswith("https://github.com/")):
        return f"{source}/compare/{old['revision'][:12]}...{new['revision'][:12]}", "changes ↗"
    url = release_notes_url(new, old)
    return (url, "release notes ↗") if url else (None, None)
