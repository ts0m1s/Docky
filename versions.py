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
import re
from functools import lru_cache
from utils import run_command

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

@lru_cache(maxsize=1)
def _platform():
    ok, out, _ = run_command(["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"])
    return out if ok and out else "linux/amd64"

def remote_version(image):
    """Version info for the image the registry serves for this tag, without pulling. None if unreadable."""
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

def release_notes_url(info):
    """Releases page for images that say where their source lives (GitHub/GitLab/Codeberg)."""
    source = (info or {}).get("source") or ""
    if re.match(r"https://(github\.com|gitlab\.com|codeberg\.org)/[^/]+/[^/]+/?$", source):
        return source.rstrip("/") + "/releases"
    return None
