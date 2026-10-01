# urls.py
"""
Where each service can be reached, worked out from what Docker already
knows: Traefik router labels give domain names, published ports give
LAN / Tailscale addresses. Nothing here changes any state; probing the
URLs (check_urls) is opt-in.
"""
import concurrent.futures
import ipaddress
import json
import re
import shutil
import socket
import ssl
import urllib.error
import urllib.request
from utils import run_command

HTTPS_PORTS = {443, 8443, 9443}
LOCAL_BINDS = {"127.0.0.1", "::1"}
ANY_BINDS = {"0.0.0.0", "::", ""}

def host_addresses():
    """How this machine is reached: its LAN IP and, if present, its Tailscale name."""
    lan = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # picks the outgoing interface; UDP connect sends nothing
            lan = s.getsockname()[0]
    except OSError:
        pass

    tailscale = None
    if shutil.which("tailscale"):
        ok, out, _ = run_command(["tailscale", "status", "--self", "--json"])
        if ok:
            try:
                tailscale = (json.loads(out).get("Self", {}).get("DNSName") or "").rstrip(".") or None
            except json.JSONDecodeError:
                pass
    return {"lan": lan, "tailscale": tailscale}

def inspect_all_containers():
    """One `docker inspect` for every container: {id: details}."""
    ok, ids, _ = run_command(["docker", "ps", "-aq", "--no-trunc"])
    if not ok or not ids:
        return {}
    ok, out, _ = run_command(["docker", "inspect"] + ids.split())
    if not ok:
        return {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}
    containers = {}
    for c in data:
        labels = c.get("Config", {}).get("Labels") or {}
        containers[c["Id"]] = {
            "id": c["Id"],
            "name": c.get("Name", "").lstrip("/"),
            "service": labels.get("com.docker.compose.service"),
            "project": labels.get("com.docker.compose.project"),
            "labels": labels,
            "ports": (c.get("NetworkSettings") or {}).get("Ports") or {},
            "network_mode": (c.get("HostConfig") or {}).get("NetworkMode") or "",
        }
    return containers

_HOST_RE = re.compile(r"Host\(([^)]*)\)")
_PATH_RE = re.compile(r"PathPrefix\(\s*`([^`]*)`\s*\)")

def traefik_routers(labels):
    """
    HTTP routers declared on a container:
      [{"name", "hosts", "path", "https", "port"}]
    `port` is the container port the router forwards to, when known.
    """
    if labels.get("traefik.enable", "").lower() == "false":
        return []
    routers, service_ports = {}, {}
    for key, value in labels.items():
        match = re.fullmatch(r"traefik\.http\.routers\.([^.]+)\.(rule|tls|entrypoints|service)", key)
        if match:
            routers.setdefault(match.group(1), {})[match.group(2)] = value
        match = re.fullmatch(r"traefik\.http\.services\.([^.]+)\.loadbalancer\.server\.port", key)
        if match and value.strip().isdigit():
            service_ports[match.group(1)] = int(value)

    result = []
    for name, router in sorted(routers.items()):
        rule = router.get("rule", "")
        hosts = [h for group in _HOST_RE.findall(rule) for h in re.findall(r"`([^`]+)`", group)]
        if not hosts:
            continue
        path = _PATH_RE.search(rule)
        entrypoints = router.get("entrypoints", "").lower()
        https = router.get("tls", "").lower() == "true" or any(e in entrypoints for e in ("websecure", "https", "443"))
        port = service_ports.get(router.get("service") or name)
        if port is None and len(service_ports) == 1:
            port = next(iter(service_ports.values()))
        result.append({"name": name, "hosts": hosts, "path": path.group(1) if path else "", "https": https, "port": port})
    return result

def published_ports(container):
    """[(container_port, bind, host_port)] for published TCP ports; bind is "any", or a specific IP."""
    seen, result = set(), []
    for spec, bindings in sorted(container["ports"].items()):
        port, _, proto = spec.partition("/")
        if proto != "tcp" or not bindings or not port.isdigit():
            continue
        for b in bindings:
            host_ip, host_port = b.get("HostIp", ""), b.get("HostPort", "")
            if not host_port.isdigit():
                continue
            # 0.0.0.0 and :: are the same exposure; list it once.
            key = (int(port), "any" if host_ip in ANY_BINDS else host_ip, int(host_port))
            if key not in seen:
                seen.add(key)
                result.append(key)
    return result

def _port_urls(host_port, bind, hosts):
    scheme = "https" if host_port in HTTPS_PORTS else "http"
    if bind in LOCAL_BINDS:
        return [{"url": f"{scheme}://localhost:{host_port}", "kind": "this machine only"}]
    if bind != "any":
        if _is_tailscale_ip(bind):
            return [{"url": f"{scheme}://{hosts['tailscale'] or bind}:{host_port}", "kind": "Tailscale"}]
        return [{"url": f"{scheme}://{bind}:{host_port}", "kind": "LAN"}]
    urls = []
    if hosts["lan"]:
        urls.append({"url": f"{scheme}://{hosts['lan']}:{host_port}", "kind": "LAN"})
    if hosts["tailscale"]:
        urls.append({"url": f"{scheme}://{hosts['tailscale']}:{host_port}", "kind": "Tailscale"})
    if not urls:
        urls.append({"url": f"{scheme}://localhost:{host_port}", "kind": "this machine"})
    return urls

def _is_tailscale_ip(ip):
    """Tailscale hands out addresses from the 100.64.0.0/10 (CGNAT) range."""
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False

def network_owner(container, all_containers):
    """The container whose network this one uses: itself, unless network_mode: container:X."""
    mode = container["network_mode"]
    if mode.startswith("container:"):
        target = mode.split(":", 1)[1]
        for c in all_containers.values():
            if c["id"].startswith(target) or c["name"] == target:
                return c
    return container

def service_urls(container, all_containers, hosts):
    """
    {"via": name or None, "urls": [{"url", "kind"}], "other_ports": ["6881/tcp", ...]}

    Containers that share one network (qBittorrent behind gluetun) are
    all reached through the owner's ports and Traefik labels. Each router
    is given to the container it's named after (router "qbittorrent" ->
    the qbittorrent service), so the VPN container doesn't show
    qBittorrent's URL as its own. Unclaimed routers stay with the owner.
    """
    owner = network_owner(container, all_containers)
    via = owner["name"] if owner is not container else None
    group = [owner] + [c for c in all_containers.values()
                       if c is not owner and network_owner(c, all_containers) is owner]

    def claimant(router):
        for c in group:
            if router["name"].lower() in {(c["service"] or "").lower(), c["name"].lower()}:
                return c
        return None

    all_routers = traefik_routers(owner["labels"])
    mine = [r for r in all_routers if claimant(r) is container or (claimant(r) is None and container is owner)]
    # Nothing names this dependent: the owner's unclaimed routers are its best guess.
    if via and not mine and len(group) == 2:
        mine = [r for r in all_routers if claimant(r) in (None, owner)]
    others = [r for r in all_routers if r not in mine]

    urls = []
    for router in mine:
        scheme = "https" if router["https"] else "http"
        for host in router["hosts"]:
            urls.append({"url": f"{scheme}://{host}{router['path']}", "kind": "domain"})

    my_ports = {r["port"] for r in mine if r["port"]}
    taken_ports = {r["port"] for r in others if r["port"]}
    other_ports = []
    for container_port, bind, host_port in published_ports(owner):
        if container_port in taken_ports and container_port not in my_ports:
            continue  # another container sharing this network serves it
        # Once Traefik labels name the web ports, the rest (e.g. a torrent
        # port) aren't web UIs -- list them, don't present them as URLs.
        if (my_ports or taken_ports) and container_port not in my_ports:
            other_ports.append(f"{host_port}/tcp")
            continue
        if not my_ports and via and len(group) > 2 and not mine:
            continue  # can't tell which of several dependents owns it
        urls.extend(_port_urls(host_port, bind, hosts))

    seen, unique = set(), []
    for u in urls:
        if u["url"] not in seen:
            seen.add(u["url"])
            unique.append(u)
    return {"via": via, "urls": unique, "other_ports": [] if via else other_ports}

def probe(url, timeout=5):
    """
    (ok, detail). Any HTTP answer below 500 counts as up -- a 401 or 403
    is the app's login page, which proves it's reachable.
    """
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "docky"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, str(response.status)
    except urllib.error.HTTPError as e:
        return (True, str(e.code)) if e.code < 500 else (False, f"HTTP {e.code}")
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, socket.gaierror):
            return False, "name doesn't resolve"
        if isinstance(reason, ssl.SSLError):
            return False, "TLS error"
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return False, "timed out"
        if isinstance(reason, ConnectionRefusedError):
            return False, "connection refused"
        return False, str(reason)
    except (socket.timeout, TimeoutError):
        return False, "timed out"
    except OSError as e:
        return False, str(e)

def check_urls(urls, max_workers=16):
    """Probe many URLs in parallel: {url: (ok, detail)}."""
    unique = sorted(set(urls))
    if not unique:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        return dict(zip(unique, executor.map(probe, unique)))
