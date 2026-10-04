# utils.py
import os
import subprocess
import shutil
import sys

class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"

def color(text, colour):
    return f"{colour}{text}{Colors.RESET}"

def supports_hyperlinks():
    """
    Whether the terminal shows OSC 8 links (clickable text). There's no way
    to ask the terminal, so known ones are recognised by their environment;
    anything else -- including tmux/screen, SSH sessions and pipes -- gets
    plain URLs. DOCKY_LINKS=plain|clickable overrides the guess.
    """
    choice = os.environ.get("DOCKY_LINKS", "").lower()
    if choice in ("plain", "clickable"):
        return choice == "clickable"
    if not sys.stdout.isatty() or os.environ.get("TMUX") or os.environ.get("STY"):
        return False
    env = os.environ
    vte = env.get("VTE_VERSION", "")
    return bool(
        (vte.isdigit() and int(vte) >= 5000)  # GNOME Terminal, Ptyxis, Tilix, …
        or env.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm", "vscode", "ghostty")
        or env.get("WT_SESSION") or env.get("KITTY_WINDOW_ID") or env.get("KONSOLE_VERSION")
        or env.get("TERM") in ("xterm-kitty", "foot", "foot-extra", "xterm-ghostty")
    )

def hyperlink(url, text):
    """Clickable `text` pointing at `url` (OSC 8)."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"

def run_command(command):
    result = subprocess.run(command, capture_output=True, text=True)
    return (result.returncode == 0, result.stdout.strip(), result.stderr.strip())

def parse_pct(value):
    try:
        return float(value.replace('%', '').strip())
    except (ValueError, AttributeError):
        return 0.0

def render_bar(pct, width=10):
    pct = max(0.0, min(100.0, pct))
    filled = round((pct / 100) * width)
    return "▓" * filled + "░" * (width - filled)

def get_system_metrics():
    # Disk space on root (/)
    total, used, free = shutil.disk_usage("/")
    disk_total_gb = total / (1024**3)
    disk_used_gb = used / (1024**3)
    disk_pct = (used / total) * 100

    # Linux RAM calculation via /proc/meminfo
    try:
        with open('/proc/meminfo', 'r') as f:
            lines = f.readlines()
        mem_info = {}
        for line in lines:
            parts = line.split(':')
            mem_info[parts[0]] = int(parts[1].strip().split()[0]) * 1024
            
        ram_total = mem_info['MemTotal']
        ram_available = mem_info.get('MemAvailable', mem_info['MemFree'])
        ram_used = ram_total - ram_available
        
        ram_total_gb = ram_total / (1024**3)
        ram_used_gb = ram_used / (1024**3)
        ram_pct = (ram_used / ram_total) * 100
    except Exception:
        ram_total_gb, ram_used_gb, ram_pct = 0, 0, 0

    return {
        "disk_str": f"{disk_used_gb:.1f}GB / {disk_total_gb:.1f}GB ({disk_pct:.1f}%)",
        "disk_pct": disk_pct,
        "ram_str": f"{ram_used_gb:.1f}GB / {ram_total_gb:.1f}GB ({ram_pct:.1f}%)",
        "disk_used_gb": disk_used_gb, "disk_total_gb": disk_total_gb,
        "ram_used_gb": ram_used_gb, "ram_total_gb": ram_total_gb, "ram_pct": ram_pct,
    }