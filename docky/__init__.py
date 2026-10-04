"""
Docky: a terminal manager for self-hosted Docker Compose stacks.

Layout:
  cli.py         argument handling, help, the command list
  commands/      one module per area (status, updates, cleanup, lifecycle, remove)
  docker_api.py  everything that talks to Docker / Compose
  monitor.py     the live `docky top` view
  urls.py        where services are reachable (`docky urls`)
  versions.py    image versions for updates
  completion.py  zsh / bash tab completion
  selfupdate.py  `docky self-update` and the daily new-version notice
  utils.py       colours, shell commands, terminal helpers

The `docky` command itself is the small docky.py launcher next to this folder.
"""

# Docky's own version. Bump it in the release PR, then tag the merge as
# v<version>; the release workflow refuses a tag that doesn't match.
__version__ = "0.2.0"
