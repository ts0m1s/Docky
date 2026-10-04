# docky/registry.py
"""
Talks to container registries directly over HTTPS (the Docker Registry
HTTP API v2), for the two questions update checks ask:

  remote_digest(image)  -- has this tag changed?  One HEAD request.
  remote_config(image)  -- what version is it?    Index -> our platform's
                           manifest -> its config: three small GETs.

That's much faster than shelling out to `docker buildx imagetools`
(which starts a process per call and fetches details for every platform),
and Docker Hub doesn't count HEAD requests against its pull rate limit.

Only anonymous (public) access is attempted. Anything unexpected --
private images, odd auth, network trouble -- raises RegistryError, and
the caller falls back to the docker CLI, which knows your credentials.
"""
import base64
import concurrent.futures
import hashlib
import http.client
import json
import os
import re
import subprocess
import threading
import urllib.parse

TIMEOUT = 8
INDEX_TYPES = [
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
]
MANIFEST_TYPES = [
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
]

class RegistryError(Exception):
    pass

class RateLimited(RegistryError):
    """The registry refused for now (HTTP 429), e.g. Docker Hub's anonymous pull limit."""

def parse_reference(image):
    """'lscr.io/linuxserver/sonarr:latest' -> ('lscr.io', 'linuxserver/sonarr', 'latest')."""
    if "@" in image:
        raise RegistryError("pinned by digest")
    name, tag = image, "latest"
    if ":" in image.rsplit("/", 1)[-1]:
        name, tag = image.rsplit(":", 1)
    first = name.split("/", 1)[0]
    if "/" in name and ("." in first or ":" in first or first == "localhost"):
        registry, repo = first, name.split("/", 1)[1]
    else:
        registry, repo = "docker.io", name
    if registry in ("docker.io", "index.docker.io"):
        registry = "registry-1.docker.io"
        if "/" not in repo:
            repo = f"library/{repo}"
    return registry, repo, tag

# Each request costs a network round trip (~150 ms from a home connection),
# and a new HTTPS connection costs another for the TLS handshake. So
# connections are kept alive and reused: one per host, per thread.
_local = threading.local()
_tokens = {}
_lock = threading.Lock()

def _connection(host):
    pool = getattr(_local, "pool", None)
    if pool is None:
        pool = _local.pool = {}
    if host not in pool:
        pool[host] = http.client.HTTPSConnection(host, timeout=TIMEOUT)
    return pool[host]

def _drop(host):
    conn = getattr(_local, "pool", {}).pop(host, None)
    if conn:
        conn.close()

def _fetch(url, method="GET", headers=None, redirects=3):
    """(status, headers, body). Follows redirects; the token never leaves its host."""
    parts = urllib.parse.urlsplit(url)
    host, path = parts.netloc, parts.path + (f"?{parts.query}" if parts.query else "")
    headers = dict(headers or {}, **{"User-Agent": "docky"})
    for attempt in (1, 2):
        conn = _connection(host)
        try:
            conn.request(method, path, headers=headers)
            response = conn.getresponse()
            body = response.read()
            break
        except (http.client.HTTPException, OSError) as e:
            _drop(host)  # a kept-alive connection the server already closed: retry on a new one
            if attempt == 2:
                raise RegistryError(f"{host}: {e}")
    if response.status in (301, 302, 303, 307, 308) and redirects:
        target = urllib.parse.urljoin(url, response.getheader("Location", ""))
        if urllib.parse.urlsplit(target).netloc != host:
            headers.pop("Authorization", None)  # e.g. blob storage: must not get the registry token
        return _fetch(target, method, headers, redirects - 1)
    return response.status, response.msg, body

_credential_cache = {}

def _credentials(registry):
    """
    (user, secret) from `docker login`, if you've logged in to this registry:
    credential helpers (credsStore / credHelpers) or plain entries in
    ~/.docker/config.json. Logged-in requests get higher rate limits.
    """
    key = {"registry-1.docker.io": "https://index.docker.io/v1/", "lscr.io": "ghcr.io"}.get(registry, registry)
    if key in _credential_cache:
        return _credential_cache[key]
    found = None
    try:
        config_dir = os.environ.get("DOCKER_CONFIG") or os.path.expanduser("~/.docker")
        with open(os.path.join(config_dir, "config.json")) as f:
            config = json.load(f)
        helper = (config.get("credHelpers") or {}).get(key) or config.get("credsStore")
        if helper:
            result = subprocess.run([f"docker-credential-{helper}", "get"], input=key,
                                    capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if data.get("Username") and data.get("Secret"):
                    found = (data["Username"], data["Secret"])
        if not found:
            auth = ((config.get("auths") or {}).get(key) or {}).get("auth")
            if auth:
                user, _, secret = base64.b64decode(auth).decode().partition(":")
                found = (user, secret) if user and secret else None
    except (OSError, ValueError, subprocess.SubprocessError):
        found = None
    _credential_cache[key] = found
    return found

def _token(challenge, registry=None):
    """Bearer token for a 'WWW-Authenticate: Bearer realm=...' challenge (logged in if possible)."""
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    if not challenge.lower().startswith("bearer") or "realm" not in params:
        raise RegistryError("registry needs credentials")
    query = urllib.parse.urlencode({k: v for k, v in params.items() if k in ("service", "scope")})
    status, body = 0, b""
    creds = _credentials(registry) if registry else None
    if creds:
        basic = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
        status, _, body = _fetch(f"{params['realm']}?{query}", headers={"Authorization": f"Basic {basic}"})
    if status != 200:  # not logged in, or the saved login was refused: anonymous
        status, _, body = _fetch(f"{params['realm']}?{query}")
    if status != 200:
        raise RegistryError(f"token request: HTTP {status}")
    try:
        data = json.loads(body)
    except ValueError:
        raise RegistryError("token response isn't JSON")
    token = data.get("token") or data.get("access_token")
    if not token:
        raise RegistryError("no token in response")
    return token

# Token servers of common registries. Knowing them up front skips the
# "401, go get a token" round trip; other registries are discovered.
KNOWN_AUTH = {
    "registry-1.docker.io": ("https://auth.docker.io/token", "registry.docker.io"),
    "ghcr.io": ("https://ghcr.io/token", "ghcr.io"),
    "lscr.io": ("https://ghcr.io/token", "ghcr.io"),
}

def _call(registry, repo, path, method="GET", accept=None):
    """(headers, body) for /v2/<repo>/<path>, fetching a token on the first 401."""
    url = f"https://{registry}/v2/{repo}/{path}"
    key = (registry, repo)
    with _lock:
        have_token = key in _tokens
    if not have_token and registry in KNOWN_AUTH:
        realm, service = KNOWN_AUTH[registry]
        try:
            token = _token(f'Bearer realm="{realm}",service="{service}",scope="repository:{repo}:pull"', registry)
            with _lock:
                _tokens[key] = token
        except RegistryError:
            pass  # fall back to discovering it from the 401
    for attempt in (1, 2):
        headers = {"Accept": accept} if accept else {}
        with _lock:
            token = _tokens.get(key)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        status, response_headers, body = _fetch(url, method, headers)
        if status == 401 and attempt == 1:
            new_token = _token(response_headers.get("WWW-Authenticate", ""), registry)
            with _lock:
                _tokens[key] = new_token
            continue
        if status == 429:
            raise RateLimited("Docker Hub rate limit" if registry == "registry-1.docker.io" else f"{registry} rate limit")
        if status != 200:
            raise RegistryError(f"HTTP {status} for {path}")
        return response_headers, body
    raise RegistryError("unauthorized")

def remote_digest(image):
    """The digest the registry serves for this tag right now (what `docker pull` would get)."""
    registry, repo, tag = parse_reference(image)
    accept = ", ".join(INDEX_TYPES + MANIFEST_TYPES)
    headers, _ = _call(registry, repo, f"manifests/{tag}", "HEAD", accept)
    digest = headers.get("Docker-Content-Digest")
    if digest:
        return digest
    _, body = _call(registry, repo, f"manifests/{tag}", "GET", accept)
    return "sha256:" + hashlib.sha256(body).hexdigest()

def _matches(platform_entry, platform):
    os_name, _, rest = platform.partition("/")
    arch, _, variant = rest.partition("/")
    p = platform_entry or {}
    return (p.get("os") == os_name and p.get("architecture") == arch
            and (not variant or p.get("variant") in (None, variant)))

def remote_config(image, platform):
    """The image config ({"config": {"Labels": ...}, "created": ...}) for our platform."""
    registry, repo, tag = parse_reference(image)
    _, body = _call(registry, repo, f"manifests/{tag}", "GET", ", ".join(INDEX_TYPES + MANIFEST_TYPES))
    manifest = json.loads(body)
    if "manifests" in manifest:  # multi-platform index: pick ours
        entry = next((m for m in manifest["manifests"] if _matches(m.get("platform"), platform)), None)
        if not entry:
            raise RegistryError(f"no {platform} image")
        _, body = _call(registry, repo, f"manifests/{entry['digest']}", "GET", ", ".join(MANIFEST_TYPES))
        manifest = json.loads(body)
    config_digest = (manifest.get("config") or {}).get("digest")
    if not config_digest:
        raise RegistryError("manifest has no config")
    _, body = _call(registry, repo, f"blobs/{config_digest}")
    return json.loads(body)

def _version_key(tag):
    """Sort key for version tags: v3.40.1 > v3.40.0 > v3.9.9."""
    return [int(n) for n in re.findall(r"\d+", tag)]

def _specificity(tag):
    return (tag.count(".") + tag.count("-"), len(tag))

def registry_version_tag(image, digest, candidates=12):
    """
    The version tag that points at the same image as `image` (e.g. latest ==
    v1.40.0), using the registry's tag list plus HEAD requests -- neither is
    counted as a pull. Checks the newest version-like tags, in parallel.
    None if no version tag matches (a branch build).
    """
    registry, repo, tag = parse_reference(image)
    tags, page = [], "tags/list?n=1000"
    for _ in range(20):  # registries hand out tag lists in pages; follow them (up to 20,000 tags)
        headers, body = _call(registry, repo, page)
        try:
            tags += json.loads(body).get("tags") or []
        except ValueError:
            raise RegistryError("tag list isn't JSON")
        link = re.search(r'<[^>]*/tags/list\?([^>]*)>;\s*rel="next"', headers.get("Link", "") or "")
        if not link:
            break
        page = f"tags/list?{link.group(1)}"
    newest = sorted((t for t in tags if VERSION_TAG.match(t) and t != tag), key=_version_key, reverse=True)[:candidates]
    if not newest:
        return None
    accept = ", ".join(INDEX_TYPES + MANIFEST_TYPES)

    def digest_of(candidate):
        try:
            headers, _ = _call(registry, repo, f"manifests/{candidate}", "HEAD", accept)
            return candidate, headers.get("Docker-Content-Digest")
        except RegistryError:
            return candidate, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(newest))) as pool:
        matches = [t for t, d in pool.map(digest_of, newest) if d == digest]
    return max(matches, key=_specificity) if matches else None

def version_tag(image, digest):
    """registry_version_tag, via hub.docker.com for Docker Hub images (its tag lists are huge)."""
    if parse_reference(image)[0] == "registry-1.docker.io":
        hub = docker_hub_tag(image)
        return hub["version"] if hub["digest"] == digest else None
    return registry_version_tag(image, digest)

# --- GitHub: how far apart two builds are -------------------------------------

def github_commits_between(source, base, head):
    """
    How many commits `head` is ahead of `base` in a GitHub repo, or None.
    GitHub's public API allows 60 requests an hour without a login; callers
    remember every answer, so each pair of builds is asked about only once.
    """
    match = re.match(r"https://github\.com/([^/]+)/([^/]+?)/?$", source or "")
    if not match or not base or not head:
        return None
    url = f"https://api.github.com/repos/{match.group(1)}/{match.group(2)}/compare/{base}...{head}"
    try:
        status, _, body = _fetch(url, headers={"Accept": "application/vnd.github+json"})
    except RegistryError:
        return None
    if status != 200:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data.get("ahead_by") if data.get("status") in ("ahead", "identical") else None

# --- Docker Hub's website API -----------------------------------------------
# hub.docker.com is separate from the registry and its pull rate limit, so it
# still answers when the registry says 429. It knows each tag's digest and
# which other tags point at the same image -- e.g. latest == 0.21.0.

VERSION_TAG = re.compile(r"^v?\d+(\.\d+)+([-_.+][0-9A-Za-z.]+)?$")

def _hub_json(path):
    status, _, body = _fetch(f"https://hub.docker.com{path}")
    if status == 429:
        raise RateLimited("Docker Hub rate limit")
    if status != 200:
        raise RegistryError(f"hub.docker.com: HTTP {status}")
    try:
        return json.loads(body)
    except ValueError:
        raise RegistryError("hub.docker.com: not JSON")

def docker_hub_tag(image):
    """{"digest", "version", "created"} for a Docker Hub image, from hub.docker.com."""
    registry, repo, tag = parse_reference(image)
    if registry != "registry-1.docker.io":
        raise RegistryError("not a Docker Hub image")
    this = _hub_json(f"/v2/repositories/{repo}/tags/{urllib.parse.quote(tag)}")
    digest = this.get("digest")
    if not digest:
        raise RegistryError("hub.docker.com: no digest for tag")
    version = tag if VERSION_TAG.match(tag) else None
    if not version:
        # The most specific version tag pointing at the same image: 0.21.0 over 0.21 and 0.
        recent = _hub_json(f"/v2/repositories/{repo}/tags?page_size=100&ordering=last_updated")
        siblings = [t["name"] for t in recent.get("results", [])
                    if t.get("digest") == digest and VERSION_TAG.match(t.get("name", ""))]
        if siblings:
            version = max(siblings, key=lambda n: (n.count(".") + n.count("-"), len(n)))
    return {"digest": digest, "version": version, "created": (this.get("last_updated") or "")[:10] or None}
