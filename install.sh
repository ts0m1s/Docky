#!/bin/sh
# Docky installer
#   curl -fsSL https://raw.githubusercontent.com/DimiKont/Docky/main/install.sh | sh
#
# Environment overrides:
#   DOCKY_REF      git branch or tag to install (default: main)
#   DOCKY_HOME     where the files go (default: ~/.local/share/docky)
#   DOCKY_BIN_DIR  where the `docky` command is linked
#                  (default: /usr/local/bin as root, otherwise ~/.local/bin)
#   DOCKY_NO_MODIFY_PATH=1  don't edit your shell profile to add BIN_DIR to PATH

set -eu

REPO="DimiKont/Docky"
REF="${DOCKY_REF:-main}"
INSTALL_DIR="${DOCKY_HOME:-$HOME/.local/share/docky}"
if [ "$(id -u)" -eq 0 ]; then
  DEFAULT_BIN_DIR="/usr/local/bin"
else
  DEFAULT_BIN_DIR="$HOME/.local/bin"
fi
BIN_DIR="${DOCKY_BIN_DIR:-$DEFAULT_BIN_DIR}"

say()  { printf '\033[36m●\033[0m %s\n' "$1"; }
warn() { printf '\033[33m!\033[0m %s\n' "$1"; }
die()  { printf '\033[31m✕ %s\033[0m\n' "$1" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 is required but was not found."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || die "Python 3.8 or newer is required."
command -v tar >/dev/null 2>&1 || die "tar is required but was not found."

if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL "$1"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -qO- "$1"; }
else
  die "curl or wget is required."
fi

command -v docker >/dev/null 2>&1 || warn "Docker was not found in PATH. Docky needs it to do anything useful."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say "Downloading Docky ($REF)..."
fetch "https://github.com/$REPO/archive/$REF.tar.gz" | tar -xz -C "$TMP" \
  || die "Download failed. Is the repository public and the ref '$REF' valid?"

SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
[ -f "$SRC/docky.py" ] || die "Downloaded archive doesn't look like Docky."

say "Installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$BIN_DIR"
for f in docky.py commands.py docker_api.py utils.py urls.py versions.py; do
  cp "$SRC/$f" "$INSTALL_DIR/$f"
done
chmod +x "$INSTALL_DIR/docky.py"
ln -sf "$INSTALL_DIR/docky.py" "$BIN_DIR/docky"

say "Installed: $BIN_DIR/docky"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    # A PATH change typed into a terminal dies with that terminal, so
    # persist it in the shell's startup file instead.
    case "${SHELL:-}" in
      */zsh)  RC="$HOME/.zshrc" ;;
      */bash) RC="$HOME/.bashrc" ;;
      *)      RC="$HOME/.profile" ;;
    esac
    LINE="export PATH=\"$BIN_DIR:\$PATH\""
    if [ "${DOCKY_NO_MODIFY_PATH:-0}" = "1" ]; then
      warn "$BIN_DIR is not in your PATH. Add this to your shell profile:"
      printf '    %s\n' "$LINE"
    else
      if ! grep -qsF "$LINE" "$RC"; then
        printf '\n# Added by the Docky installer\n%s\n' "$LINE" >> "$RC"
      fi
      say "Added $BIN_DIR to your PATH in $RC"
      warn "Open a new terminal (or run: source $RC) to use 'docky'."
    fi
    ;;
esac

say "Run 'docky' to get started."
