# monitor.py
"""
`docky top`: a live, aligned view of what every container is using.

Docker reports network and disk I/O as totals since a container
started, which barely move between refreshes; the monitor keeps the
previous sample and shows per-second rates instead. Everything is laid
out in fixed columns sized to the terminal, so long names or narrow
windows never break the alignment.
"""
import concurrent.futures
import re
import shutil
import sys
import time
from datetime import datetime
from .utils import Colors, color, get_system_metrics, parse_pct
from . import docker_api

INTERVAL = 2.0  # seconds between refreshes (docker stats itself takes ~1s)

_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
          "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}

def parse_size(text):
    """'1.34GB' / '237.5MiB' / '0B' -> bytes (float), or None."""
    match = re.match(r"\s*([\d.]+)\s*([a-zA-Z]*)", text or "")
    if not match:
        return None
    return float(match.group(1)) * _UNITS.get((match.group(2) or "b").lower(), 1)

def split_pair(text):
    """'1.34GB / 488MB' -> (bytes, bytes)."""
    left, _, right = (text or "").partition(" / ")
    return parse_size(left), parse_size(right)

def human_bytes(n):
    """Memory, binary units: '237.5 MiB'."""
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024

def short_bytes(n):
    """Compact size for the limit column: '1000M', '2.0G'."""
    return f"{n / 1024 ** 2:.0f}M" if n < 1024 ** 3 else f"{n / 1024 ** 3:.1f}G"

def human_rate(n):
    """Throughput, decimal units like Docker's: '1.2 MB/s'."""
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}/s" if unit == "B" else f"{n:.1f} {unit}/s"
        n /= 1000

class CpuSampler:
    """Whole-machine CPU % from /proc/stat, measured between two refreshes."""
    def __init__(self):
        self.prev = self._read()

    @staticmethod
    def _read():
        try:
            with open("/proc/stat") as f:
                values = [int(v) for v in f.readline().split()[1:]]
            return sum(values), values[3] + (values[4] if len(values) > 4 else 0)
        except (OSError, ValueError, IndexError):
            return None

    def percent(self):
        now = self._read()
        if not now or not self.prev or now[0] == self.prev[0]:
            self.prev = now
            return None
        total, idle = now[0] - self.prev[0], now[1] - self.prev[1]
        self.prev = now
        return max(0.0, 100.0 * (1 - idle / total))

class RateTracker:
    """Turns Docker's since-start counters into per-second rates."""
    def __init__(self):
        self.prev, self.prev_time = {}, None

    def update(self, stats_map):
        now = time.monotonic()
        rates = {}
        if self.prev_time is not None:
            elapsed = max(now - self.prev_time, 0.001)
            for name, s in stats_map.items():
                before = self.prev.get(name)
                if not before:
                    continue
                after = (*split_pair(s["net"]), *split_pair(s["blk"]))
                if None in after or None in before:
                    continue
                # A restarted container resets its counters; never show negative rates.
                rates[name] = tuple(max(0.0, (a - b) / elapsed) for a, b in zip(after, before))
        self.prev = {name: (*split_pair(s["net"]), *split_pair(s["blk"])) for name, s in stats_map.items()}
        self.prev_time = now
        return rates

# --- Layout ------------------------------------------------------------------

def _fit(text, width):
    return text if len(text) <= width else text[: max(width - 1, 0)] + "…"

def cell(text, width, colour=None, align="<"):
    """Pad/truncate on the plain text, then colour -- so ANSI codes never break alignment."""
    plain = f"{_fit(text, width):{align}{width}}"
    return color(plain, colour) if colour else plain

def _cpu_colour(pct):
    return Colors.RED if pct >= 100 else (Colors.YELLOW if pct >= 50 else None)

def _limit_colour(pct):
    return Colors.RED if pct >= 90 else (Colors.YELLOW if pct >= 75 else Colors.DIM)

def layout(width):
    """Which columns fit: (name width, show net, show disk)."""
    fixed = 6 + 2 + 8 + 11 + 14  # tree, status, CPU, MEM, LIMIT
    show_disk = width >= fixed + 18 + 2 * 11 + 2 * 11
    show_net = width >= fixed + 18 + 2 * 11
    used = fixed + (2 * 11 if show_net else 0) + (2 * 11 if show_disk else 0)
    name_width = max(12, min(28, width - used - 1))
    return name_width, show_net, show_disk

SORT_KEYS = ("name", "cpu", "mem")

def _usage(stats_map, container, key):
    s = stats_map.get(container["name"])
    if not s or container["state"].lower() != "running":
        return -1.0  # stopped: always last
    return parse_pct(s["cpu"]) if key == "cpu" else (parse_size(s["mem_used"]) or 0.0)

def sort_data(project_data, stats_map, key):
    """Heaviest projects first, heaviest containers first within each; stopped stacks last."""
    if key == "name":
        return project_data
    ordered = []
    for data in project_data:
        containers = sorted(data["containers"], key=lambda c: _usage(stats_map, c, key), reverse=True)
        total = sum(max(_usage(stats_map, c, key), 0.0) for c in containers)
        running = any(_usage(stats_map, c, key) >= 0 for c in containers)
        ordered.append((running, total, {"project": data["project"], "containers": containers}))
    ordered.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [d for _, _, d in ordered]

def render(project_data, stats_map, rates, owners, metrics, host_cpu, ncpu, width, height, sort="name"):
    name_w, show_net, show_disk = layout(width)
    dim = lambda t: color(t, Colors.DIM)
    lines = []
    project_data = sort_data(project_data, stats_map, sort)

    cpu_text = f"CPU {host_cpu:.0f}%" if host_cpu is not None else "CPU …"
    summary = (f"{cpu_text} of {ncpu} cores  ·  RAM {metrics['ram_used_gb']:.1f}/{metrics['ram_total_gb']:.1f} GB "
               f"({metrics['ram_pct']:.0f}%)  ·  Disk {metrics['disk_used_gb']:.0f}/{metrics['disk_total_gb']:.0f} GB "
               f"({metrics['disk_pct']:.0f}%)")
    if len(summary) + 2 > width:  # narrow terminal: compact form
        summary = (f"{cpu_text}  ·  RAM {metrics['ram_used_gb']:.1f}/{metrics['ram_total_gb']:.0f}G"
                   f"  ·  Disk {metrics['disk_used_gb']:.0f}/{metrics['disk_total_gb']:.0f}G")
    right = f"{datetime.now():%H:%M:%S} · every {INTERVAL:.0f}s · Ctrl+C to quit"
    lines.append(color("● DOCKY", Colors.BOLD + Colors.CYAN) + dim("  top"))
    lines.append(f"  {summary}" + (dim(f"   {right}") if len(summary) + len(right) + 5 <= width else ""))
    lines.append("")

    # ▼ marks the column the view is sorted by.
    header = ("   " + cell("PROJECT / CONTAINER", name_w + 5)
              + cell(("▼ " if sort == "cpu" else "") + "CPU", 8, align=">")
              + cell(("▼ " if sort == "mem" else "") + "MEM", 11, align=">")
              + cell("LIMIT", 14, align=">"))
    if show_net:
        header += cell("NET ↓", 11, align=">") + cell("NET ↑", 11, align=">")
    if show_disk:
        header += cell("DISK R", 11, align=">") + cell("DISK W", 11, align=">")
    lines.append(color(header, Colors.BOLD))

    def rate_cells(values):
        out = ""
        for v in values:
            out += cell("–", 11, Colors.DIM, ">") if v is None or v < 1 else cell(human_rate(v), 11, Colors.CYAN, ">")
        return out

    total_cpu = total_mem = 0.0
    for p_idx, data in enumerate(project_data):
        project, containers = data["project"], data["containers"]
        last_p = p_idx == len(project_data) - 1
        branch, stem = ("└─", "   ") if last_p else ("├─", "│  ")
        running = [c for c in containers if c["state"].lower() == "running" and c["name"] in stats_map]
        stopped = [c for c in containers if c not in running]

        p_cpu = sum(parse_pct(stats_map[c["name"]]["cpu"]) for c in running)
        p_mem = sum(parse_size(stats_map[c["name"]]["mem_used"]) or 0 for c in running)
        total_cpu, total_mem = total_cpu + p_cpu, total_mem + p_mem

        # Container rows spend 8 columns on tree + status before the name; the
        # project name starts right after its branch and is padded to match,
        # so the number columns line up on every row.
        title = cell(project["name"], name_w + 5, Colors.CYAN + Colors.BOLD)
        if not running:
            note = f"✕ {len(stopped)} stopped" if stopped else "no containers"
            lines.append(f"{branch} {title}{color(cell(note, 19, align='>'), Colors.RED if stopped else Colors.DIM)}")
            continue
        lines.append(f"{branch} {title}" + dim(cell(f"{p_cpu:.1f}%", 8, align=">") + cell(human_bytes(p_mem), 11, align=">")))

        for c_idx, c in enumerate(running + stopped):
            last_c = c_idx == len(running) + len(stopped) - 1
            prefix = f"{stem}{'└─' if last_c else '├─'} "
            if c in stopped:
                lines.append(f"{prefix}{color('✕', Colors.RED)} {cell(c['short_name'], name_w)}{dim('stopped')}")
                continue
            s = stats_map[c["name"]]
            cpu = parse_pct(s["cpu"])
            used, limit = parse_size(s["mem_used"]) or 0, parse_size(s["mem_limit"])
            # Docker reports the host's RAM as the "limit" when there is none.
            limited = limit and metrics["ram_total_gb"] and limit < metrics["ram_total_gb"] * 1024 ** 3 * 0.95
            row = (f"{prefix}{color('●', Colors.GREEN)} {cell(c['short_name'], name_w)}"
                   f"{cell(f'{cpu:.1f}%', 8, _cpu_colour(cpu), '>')}"
                   f"{cell(human_bytes(used), 11, align='>')}")
            if limited:
                pct = 100 * used / limit
                row += cell(f"{pct:.0f}% of {short_bytes(limit)}", 14, _limit_colour(pct), ">")
            else:
                row += cell("", 14)
            r = rates.get(c["name"])
            if show_net:
                owner = owners.get(c["name"])
                if owner:
                    row += cell(f"via {owner}", 22, Colors.DIM, ">")
                else:
                    row += rate_cells(r[:2] if r else (None, None))
            if show_disk:
                row += rate_cells(r[2:] if r else (None, None))
            lines.append(row)

    lines.append("")
    lines.append(dim(f"  {len(stats_map)} running  ·  containers use {total_cpu:.1f}% CPU and {human_bytes(total_mem)} RAM"
                     + ("" if rates else "  ·  measuring rates…")))

    # Never scroll: cut what doesn't fit and say so.
    if len(lines) > height - 1:
        hidden = len(lines) - (height - 2)
        lines = lines[: height - 2] + [color(f"  … {hidden} more lines (make the window taller)", Colors.YELLOW)]
    return "\n".join(lines)

def run(projects, sort="name"):
    sys.stdout.write("\033[?1049h\033[?25l\033[H")  # alternate screen (like htop/less), hide cursor
    sys.stdout.write(color("  Loading…", Colors.DIM))
    sys.stdout.flush()
    cpu_sampler, tracker = CpuSampler(), RateTracker()
    import os
    ncpu = os.cpu_count() or 1
    owners, owners_checked = {}, 0.0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            while True:
                started = time.monotonic()
                # docker stats needs ~1s on its own (two cgroup samples), so the
                # per-project lookups run alongside it rather than after it.
                stats_future = executor.submit(docker_api.fetch_stats)
                container_futures = [executor.submit(docker_api.get_containers_light, p) for p in projects]
                if time.monotonic() - owners_checked > 30:  # containers get recreated; refresh now and then
                    owners, owners_checked = docker_api.network_owners(), time.monotonic()
                project_data = [{"project": p, "containers": f.result()} for p, f in zip(projects, container_futures)]
                stats_map = stats_future.result()
                rates = tracker.update(stats_map)
                size = shutil.get_terminal_size((120, 40))
                frame = render(project_data, stats_map, rates, owners, get_system_metrics(),
                               cpu_sampler.percent(), ncpu, size.columns, size.lines, sort)
                # Home the cursor, clear each line's tail, draw, clear anything below.
                sys.stdout.write("\033[H" + frame.replace("\n", "\033[K\n") + "\033[K\033[J")
                sys.stdout.flush()
                time.sleep(max(0.0, INTERVAL - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass  # Ctrl+C is how you close a monitor, not an error
    finally:
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
    print(color("  Monitor stopped.", Colors.DIM) + "\n")
