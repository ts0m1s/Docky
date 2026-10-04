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
import hashlib
import http.client
import json
import re
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

def _token(challenge):
    """Anonymous bearer token from a 'WWW-Authenticate: Bearer realm=...' challenge."""
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    if not challenge.lower().startswith("bearer") or "realm" not in params:
        raise RegistryError("registry needs credentials")
    query = urllib.parse.urlencode({k: v for k, v in params.items() if k in ("service", "scope")})
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
            token = _token(f'Bearer realm="{realm}",service="{service}",scope="repository:{repo}:pull"')
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
            new_token = _token(response_headers.get("WWW-Authenticate", ""))
            with _lock:
                _tokens[key] = new_token
            continue
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
