# Docky

A clean terminal-based Docker manager for self-hosted servers. Monitor Compose projects, check image updates, safely upgrade containers, and clean up leftover data.

Zero dependencies: just Python 3.8+ and Docker.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/ts0m1s/Docky/main/install.sh | sh
```

This installs to `~/.local/share/docky` and links `~/.local/bin/docky`. To uninstall, delete those two paths.

## Updating Docky

```sh
docky self-update          # install the latest version
docky self-update --check  # just show what's new
docky --version            # what's installed
```

`self-update` follows the `main` branch and lists the changes since your version. The new version is downloaded and compiled in a temporary folder first, then swapped in, so a failed download never leaves Docky half-updated. If you installed as root, run it with `sudo`.

Once a day, after a command finishes, Docky prints a one-line notice when a newer version exists. It never updates on its own. Turn the notice off with `export DOCKY_NO_UPDATE_CHECK=1`.

Installs from before self-update existed don't record their version. Run the install command once more, and `docky self-update` works from then on. To follow a different branch, install with `DOCKY_REF=<branch>`; self-update keeps following it.

### Versions and channels

Docky has version numbers (`docky --version` → `docky 0.2.0 (a3b20d8, …)`), and every published version is a [GitHub release](https://github.com/ts0m1s/Docky/releases) with notes.

- **main** (default): every change as soon as it's merged.
- **stable**: only published releases. Install with `DOCKY_REF=stable`:

  ```sh
  curl -fsSL https://raw.githubusercontent.com/ts0m1s/Docky/main/install.sh | DOCKY_REF=stable sh
  ```

  On this channel the daily notice reads `Docky 0.3.0 is available (you have 0.2.0)`, and `self-update` links to the release notes. Pre-releases (`v0.3.0-rc.1`) are never offered.

### Publishing a release (maintainers)

1. In a PR, bump `__version__` in `about.py` (`0.2.0` → `0.3.0` for new features, `0.2.1` for fixes only), then merge it.
2. Tag the merge and push the tag:

   ```sh
   git checkout main && git pull
   git tag v0.3.0 && git push origin v0.3.0
   ```

3. The release workflow checks that every module compiles and the tag matches `about.py`, then publishes the release with notes generated from the merged PRs. Tag `v0.3.0-rc.1` to publish a pre-release for testing first.

## Usage

```
docky <command> [target]
```

| Command | Description |
| --- | --- |
| `projects` | List every project Docky found, where it lives, and the compose files it uses |
| `urls [name] [--check]` | Show where each service is reachable: domains from Traefik, LAN and Tailscale addresses from published ports. `--check` tests every URL |
| `status` | Show Docker projects, containers, and system metrics. Flags containers whose shared network (`network_mode: container:...`) points at a container that no longer exists |
| `top [--sort cpu\|mem]` | Live view per container: CPU, memory (and how close it is to its limit), network and disk as per-second rates. Columns fit the terminal; `--sort` puts the heaviest projects and containers first |
| `updates` | Check for available image updates, showing the version change (e.g. `4.0.20-ls325 → 4.0.20-ls326`) and a release-notes link |
| `upgrade [name] [--dry-run]` | Pull and recreate outdated containers, verifying health, and list each version change. Give a project name to upgrade just that one; `--dry-run` shows the plan without changing anything. Services that share an upgraded service's network (`network_mode: service:X`) or depend on it with `restart: true` are recreated along with it, so e.g. qBittorrent behind gluetun keeps its connection. `rollback` does the same |
| `rollback [name] [service]` | Undo the last upgrade. Docky saves the previous image before every upgrade; with no arguments this lists the saved snapshots |
| `sweep` | Find and clear stopped containers and unused images |
| `orphans` | Find volumes belonging to deleted or renamed projects |
| `remove <name\|path> [--volumes] [--images] [--files] [--all] [--dry-run] [-y]` | Remove a project completely. See [Removing a project](#removing-a-project) |
| `start` / `stop` / `restart` `<name\|all>` | Control a project or all of them |

## Where Docky finds your projects

Your stacks can live anywhere. Docky finds them in two ways:

1. **From Docker itself.** Every container Compose starts is labelled with its project name, folder and compose files. Any project with at least one container (running or stopped) is found automatically, wherever its folder is.
2. **By scanning folders**, for projects that exist on disk but haven't been started yet. By default Docky looks in `~/docker`, up to two levels deep. To use other folders, set `DOCKY_ROOT` to one or more paths separated by `:`:

   ```bash
   export DOCKY_ROOT=/opt/stacks:/srv/compose:~/homelab
   ```

Run `docky projects` to see what was found and from where. Commands take a project name, or its folder path when two projects share a name (`docky restart /opt/stacks/app`).

Docky runs Compose with the same files the project was started with, so `docker-compose.override.yml` and friends are kept. If a project's folder is moved or deleted while its containers still exist, `docky status` warns about it instead of guessing.

## Finding your services' URLs

`docky urls` lists where each container can be reached, using only what Docker already knows:

- **Domains** come from Traefik router labels (`Host(...)`, including `PathPrefix`). A router with TLS or a `websecure` entrypoint is shown as `https://`.
- **LAN and Tailscale addresses** come from published ports. A port bound to `0.0.0.0` is shown on the machine's LAN IP and its Tailscale name. A port bound to `127.0.0.1` is marked "this machine only".
- **Containers sharing another's network** (`network_mode: service:gluetun`) are reached through that container. A Traefik router named after the service is shown under it, so qBittorrent's URL appears under qBittorrent, not under the VPN.
- When Traefik labels name the web port, other published ports (such as a torrent port) are listed as "published", not as URLs.

`docky urls --check` opens every URL and marks it ✓ or ✗ (doesn't resolve, refused, timed out, 5xx). A login page (401/403) counts as reachable. Use it to see which domains actually work from this machine; for example, a Cloudflare tunnel's DNS record may be missing.

## Version changes

`updates`, `upgrade --dry-run` and `upgrade` show what each update changes, read from the image's labels:

```
├─ ↑ sonarr     update available  4.0.20.3014-ls325 → 4.0.20.3014-ls326
├─ ↑ homarr     update available  built 2026-09-18 → built 2026-09-29
└─ ✓ radarr     up to date · 6.4.4.10685-ls318
```

- **No pulling:** the registry's version is read without downloading the image (`docker buildx imagetools inspect`).
- **Where the version comes from:** the `org.opencontainers.image.version` label, then linuxserver.io's `build_version`. Images that only say `latest`/`main` fall back to their build date.
- **The "from" side** is the image the container is actually running. A newer image that was pulled but never applied is reported as an update.
- **Summary:** the run ends with the version changes and, when the image names its GitHub/GitLab/Codeberg source, a release-notes link.

## Removing a project

`docky remove <name>` first shows a plan of everything that belongs to the project, then asks before each part:

- **Always removed:** the project's containers (running ones are stopped) and the networks Compose created for it.
- **Optional:**
  - `--volumes`: its named and anonymous volumes. **The data is gone for good.**
  - `--images`: images no other container uses, plus Docky's rollback snapshots for it.
  - `--files`: the project folder (compose files, `.env`, and any data stored inside it).
- **Never touched:** external or shared volumes and networks, images other projects use, and bind-mounted data outside the project folder. These are listed in the plan so you know what stays.

Without flags, Docky asks about each optional part. Deleting volumes or the folder requires typing the project name. `--all` selects everything, `--dry-run` shows the plan and changes nothing, and `-y` skips the prompts (only what you flagged is removed).

Safety rails:
- The folder is never offered if it's your home folder, a `DOCKY_ROOT` scan folder, or contains another project.
- If any file in the folder can't be deleted (for example data a container wrote as root), nothing in the folder is deleted, and Docky prints the `sudo rm -rf` command to finish.
- Projects whose folder was already moved or deleted can still be removed by name.

## License

MIT
