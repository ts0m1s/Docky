# selfupdate.py
"""
Keep Docky itself up to date without re-running the installer.

The installer (and every self-update) writes a VERSION file next to the
code recording which repo, branch and commit is installed. `docky
self-update` asks GitHub for the branch's latest commit, downloads that
exact commit, checks it, and swaps the files in place. A once-a-day
check prints a one-line notice when a newer version exists; it never
installs anything on its own.
"""
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_REPO = "ts0m1s/Docky"
DEFAULT_REF = "main"
INSTALL_DIR = Path(__file__).resolve().parent
VERSION_FILE = INSTALL_DIR / "VERSION"
CHECK_INTERVAL = 24 * 3600

class UpdateError(Exception):
    pass

class SwapError(UpdateError):
    """Failed after some files were already replaced."""

# --- What's installed ----------------------------------------------------

def installed():
    """{"repo", "ref", "commit", "date", "files"} from the VERSION file, or None."""
    try:
        data = json.loads(VERSION_FILE.read_text())
        return data if data.get("commit") else None
    except (OSError, json.JSONDecodeError):
        return None

def repo_and_ref():
    info = installed() or {}
    repo = os.environ.get("DOCKY_REPO") or info.get("repo") or DEFAULT_REPO
    ref = os.environ.get("DOCKY_REF") or info.get("ref") or DEFAULT_REF
    return repo, ref

def is_git_checkout():
    return (INSTALL_DIR / ".git").exists()

# --- GitHub --------------------------------------------------------------

def _get(url, timeout):
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "docky-self-update",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError(f"not found: {url}")
        if e.code == 403:
            raise UpdateError("GitHub rate limit reached; try again in a while")
        raise UpdateError(f"GitHub returned HTTP {e.code}")
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"can't reach GitHub ({getattr(e, 'reason', e)})")

def latest(repo, ref, timeout=10):
    """{"commit", "date", "message"} for the newest commit on ref."""
    data = json.loads(_get(f"https://api.github.com/repos/{repo}/commits/{ref}", timeout))
    return {
        "commit": data["sha"],
        "date": data["commit"]["committer"]["date"][:10],
        "message": data["commit"]["message"].splitlines()[0],
    }

def changes_between(repo, old, new, timeout=10):
    """Readable list of what changed: merged PR titles, plus direct commits."""
    try:
        data = json.loads(_get(f"https://api.github.com/repos/{repo}/compare/{old}...{new}", timeout))
    except UpdateError:
        return []
    lines = []
    for c in data.get("commits", []):
        message = c["commit"]["message"].strip().splitlines()
        first = message[0] if message else ""
        if first.startswith("Merge pull request"):
            # GitHub puts the PR title after a blank line.
            title = next((l for l in message[1:] if l.strip()), "")
            if title:
                lines.append(title.strip())
        elif not first.startswith("Merge "):
            lines.append(first)
    # A merged PR's own commits usually repeat its title; keep order, drop duplicates.
    seen, unique = set(), []
    for line in lines:
        if line not in seen:
            seen.add(line)
            unique.append(line)
    return unique

# --- Installing ----------------------------------------------------------

def _download(repo, commit, dest):
    archive = Path(dest) / "docky.tar.gz"
    archive.write_bytes(_get(f"https://codeload.github.com/{repo}/tar.gz/{commit}", timeout=60))
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            target = (Path(dest) / member.name).resolve()
            if not str(target).startswith(str(Path(dest).resolve()) + os.sep) or member.issym() or member.islnk():
                raise UpdateError(f"unexpected path in archive: {member.name}")
        tar.extractall(dest)
    roots = [p for p in Path(dest).iterdir() if p.is_dir()]
    if len(roots) != 1 or not (roots[0] / "docky.py").is_file():
        raise UpdateError("downloaded archive doesn't look like Docky")
    return roots[0]

def _validate(source):
    files = sorted(p.name for p in source.glob("*.py"))
    for name in files:
        try:
            compile((source / name).read_text(encoding="utf-8"), name, "exec")
        except (SyntaxError, ValueError, UnicodeDecodeError) as e:
            raise UpdateError(f"new version doesn't compile ({name}): {e}")
    return files

def write_version(repo, ref, commit, date, files):
    tmp = VERSION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"repo": repo, "ref": ref, "commit": commit, "date": date, "files": files}, indent=2) + "\n")
    os.replace(tmp, VERSION_FILE)

def install(repo, ref, target):
    """
    Replace the installed code with `target` (a latest() result).
    The new files are downloaded and compiled in a temporary folder
    first; only then is each file swapped in with an atomic rename, so a
    failed download or a broken commit never leaves Docky half-updated.
    """
    if not os.access(INSTALL_DIR, os.W_OK):
        raise UpdateError(f"no permission to write {INSTALL_DIR} (installed as root? run: sudo docky self-update)")
    with tempfile.TemporaryDirectory(prefix="docky-update-") as tmp:
        source = _download(repo, target["commit"], tmp)
        files = _validate(source)

        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=INSTALL_DIR))
        try:
            for name in files:
                shutil.copy2(source / name, staging / name)
            swapped = 0
            try:
                for name in files:
                    os.replace(staging / name, INSTALL_DIR / name)
                    swapped += 1
            except OSError as e:
                if swapped:
                    raise SwapError(f"stopped partway through replacing files ({e}). Re-run the installer to repair.")
                raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    (INSTALL_DIR / "docky.py").chmod(0o755)
    old = set((installed() or {}).get("files") or [])
    for gone in old - set(files):
        # Only files the previous version shipped; anything else is left alone.
        (INSTALL_DIR / gone).unlink(missing_ok=True)
    shutil.rmtree(INSTALL_DIR / "__pycache__", ignore_errors=True)
    write_version(repo, ref, target["commit"], target["date"], files)
    return files

# --- Daily "new version" notice ------------------------------------------

def _state_file():
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "docky" / "update-check.json"

def _notice_enabled():
    return (os.environ.get("DOCKY_NO_UPDATE_CHECK", "") in ("", "0")
            and sys.stdout.isatty()
            and installed() is not None
            and not is_git_checkout())

class BackgroundCheck:
    """
    Started when a command begins, read when it ends, so the network call
    overlaps with the command instead of adding to it. Checks GitHub at
    most once a day; in between, the cached answer is used.
    """
    def __init__(self):
        self.latest_commit = None
        self.thread = None
        if not _notice_enabled():
            return
        try:
            cache = json.loads(_state_file().read_text())
        except (OSError, json.JSONDecodeError):
            cache = {}
        repo, ref = repo_and_ref()
        if cache.get("ref") == f"{repo}@{ref}" and time.time() - cache.get("checked_at", 0) < CHECK_INTERVAL:
            self.latest_commit = cache.get("latest")
            return
        self.thread = threading.Thread(target=self._run, args=(repo, ref), daemon=True)
        self.thread.start()

    def _run(self, repo, ref):
        try:
            commit = latest(repo, ref, timeout=2)["commit"]
        except (UpdateError, KeyError, ValueError):
            return
        self.latest_commit = commit
        try:
            path = _state_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"ref": f"{repo}@{ref}", "latest": commit, "checked_at": time.time()}))
        except OSError:
            pass

    def notice(self, wait=2.0):
        """The one-line notice to print, or None."""
        if self.thread:
            self.thread.join(timeout=wait)
        info = installed()
        if self.latest_commit and info and self.latest_commit != info["commit"]:
            return "A new version of Docky is available. Run: docky self-update"
        return None

# --- Commands ------------------------------------------------------------

def _short(commit):
    return (commit or "unknown")[:7]

def cmd_version():
    info = installed()
    if info:
        print(f"docky {_short(info['commit'])} ({info.get('date') or 'unknown date'}, {info.get('repo')}@{info.get('ref')})")
    elif is_git_checkout():
        print(f"docky (running from a git checkout: {INSTALL_DIR})")
    else:
        print("docky (version unknown: installed before self-update existed; reinstall once to enable it)")

def cmd_self_update(check_only=False):
    from utils import Colors, color
    print(f"\n{color('● DOCKY', Colors.BOLD + Colors.CYAN)} {color('  ·  Self-update', Colors.DIM)}\n")
    if is_git_checkout():
        return print(color(f"  Docky is running from a git checkout ({INSTALL_DIR}). Update it with: git pull", Colors.YELLOW) + "\n")

    repo, ref = repo_and_ref()
    current = installed()
    try:
        target = latest(repo, ref)
    except UpdateError as e:
        print(f"{color('✕', Colors.RED)} {color(f'Could not check for updates: {e}', Colors.RED)}\n")
        sys.exit(1)

    here = f"{_short(current['commit'])} ({current.get('date') or '?'})" if current else "unknown (installed before self-update existed)"
    print(f"  installed  {here}")
    print(f"  latest     {_short(target['commit'])} ({target['date']})  {color(f'{repo}@{ref}', Colors.DIM)}\n")

    if current and current["commit"] == target["commit"]:
        return print(f"{color('✓', Colors.GREEN)} Docky is up to date.\n")

    if current:
        changes = changes_between(repo, current["commit"], target["commit"])
        if changes:
            print(color("  Changes:", Colors.BOLD))
            for line in changes[:15]:
                print(f"    • {line}")
            if len(changes) > 15:
                print(color(f"    … and {len(changes) - 15} more", Colors.DIM))
            print()

    if check_only:
        return print(color("Run 'docky self-update' to install it.", Colors.DIM) + "\n")

    try:
        install(repo, ref, target)
    except (UpdateError, OSError, tarfile.TarError) as e:
        print(f"{color('✕', Colors.RED)} {color(f'Update failed: {e}', Colors.RED)}")
        if isinstance(e, SwapError):
            return print(color("  curl -fsSL https://raw.githubusercontent.com/ts0m1s/Docky/main/install.sh | sh", Colors.DIM) + "\n")
        return print(color("  Nothing was changed; your current version still works.", Colors.DIM) + "\n")
    try:
        _state_file().unlink()  # the cached "new version" answer is now stale
    except OSError:
        pass
    print(f"{color('✓', Colors.GREEN)} Updated to {_short(target['commit'])}. Run 'docky help' to see what's new.\n")
