# Docky

A clean terminal-based Docker manager for self-hosted servers. Monitor Compose projects, check image updates, safely upgrade containers, and clean up leftover data.

Zero dependencies: just Python 3.8+ and Docker.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/DimiKont/Docky/main/install.sh | sh
```

This installs to `~/.local/share/docky` and links `~/.local/bin/docky`. Pin a version with `DOCKY_REF=v0.1.0`. To uninstall, delete those two paths.

## Usage

```
docky <command> [target]
```

| Command | Description |
| --- | --- |
| `projects` | List every project Docky found, where it lives, and the compose files it uses |
| `status` | Show Docker projects, containers, and system metrics. Flags containers whose shared network (`network_mode: container:...`) points at a container that no longer exists |
| `top` | Live CPU and RAM usage mapped to your projects |
| `updates` | Check for available image updates, showing the version change (e.g. `4.0.20-ls325 → 4.0.20-ls326`) and a release-notes link |
| `upgrade [name] [--dry-run]` | Pull and recreate outdated containers, verifying health. Give a project name to upgrade just that one; `--dry-run` shows the plan without changing anything. Services that share an upgraded service's network (`network_mode: service:X`) or depend on it with `restart: true` are recreated along with it, so e.g. qBittorrent behind gluetun keeps its connection. `rollback` does the same |
| `rollback [name] [service]` | Undo the last upgrade. Docky saves the previous image before every upgrade; with no arguments this lists the saved snapshots |
| `sweep` | Find and clear stopped containers and unused images |
| `orphans` | Find volumes belonging to deleted or renamed projects |
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

## License

MIT
