#!/bin/sh
# Docky installer
#   curl -fsSL https://raw.githubusercontent.com/ts0m1s/Docky/main/install.sh | sh
#
# Once installed, update with `docky self-update`; no need to run this again.
#
# Environment overrides:
#   DOCKY_REF      what to install and follow (default: main): a branch, a tag,
#                  or "stable" for the latest published release
#   DOCKY_REPO     GitHub repo to install from (default: ts0m1s/Docky)
#   DOCKY_HOME     where the files go (default: ~/.local/share/docky)
#   DOCKY_BIN_DIR  where the `docky` command is linked
#                  (default: /usr/local/bin as root, otherwise ~/.local/bin)
#   DOCKY_NO_MODIFY_PATH=1  don't edit your shell profile (PATH, tab completion);
#                           print the lines to add instead

set -eu

REPO="${DOCKY_REPO:-ts0m1s/Docky}"
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

# "stable" follows releases: resolve it to the newest release's tag.
TARGET="$REF"
if [ "$REF" = "stable" ]; then
  TARGET="$(fetch "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
    | sed -n 's/^  "tag_name": "\([^"]*\)".*/\1/p' | head -n 1)"
  [ -n "$TARGET" ] || die "No Docky release has been published yet. Install from main instead (leave DOCKY_REF unset)."
fi

# Pin the exact commit, so `docky self-update` knows what's installed.
COMMIT_JSON="$(fetch "https://api.github.com/repos/$REPO/commits/$TARGET" 2>/dev/null || true)"
COMMIT="$(printf '%s\n' "$COMMIT_JSON" | sed -n 's/^  "sha": "\([0-9a-f]\{40\}\)".*/\1/p' | head -n 1)"
DATE="$(printf '%s\n' "$COMMIT_JSON" | sed -n 's/.*"date": "\([0-9-]\{10\}\)T.*/\1/p' | tail -n 1)"
if [ -z "$COMMIT" ]; then
  warn "Couldn't look up the exact commit (GitHub API unreachable?); 'docky self-update' will reinstall to be sure."
fi

say "Downloading Docky ($TARGET${COMMIT:+ @ $(printf %.7s "$COMMIT")})..."
fetch "https://codeload.github.com/$REPO/tar.gz/${COMMIT:-$TARGET}" | tar -xz -C "$TMP" \
  || die "Download failed. Is the repository public and the ref '$REF' valid?"

SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
[ -f "$SRC/docky.py" ] || die "Downloaded archive doesn't look like Docky."

say "Installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$BIN_DIR"
FILES=""
for path in "$SRC"/*.py; do
  f="$(basename "$path")"
  cp "$path" "$INSTALL_DIR/$f"
  FILES="$FILES${FILES:+, }\"$f\""
done
chmod +x "$INSTALL_DIR/docky.py"
rm -rf "$INSTALL_DIR/__pycache__"
cat > "$INSTALL_DIR/VERSION" <<EOF
{
  "repo": "$REPO",
  "ref": "$REF",
  "commit": "$COMMIT",
  "date": "$DATE",
  "files": [$FILES]
}
EOF
ln -sf "$INSTALL_DIR/docky.py" "$BIN_DIR/docky"

VERSION_NUMBER="$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' "$INSTALL_DIR/about.py" 2>/dev/null || true)"
say "Installed Docky${VERSION_NUMBER:+ $VERSION_NUMBER}: $BIN_DIR/docky"

# Tab completion for zsh/bash (sourced from ~/.zshrc / ~/.bashrc;
# DOCKY_NO_MODIFY_PATH=1 prints the line to add instead).
python3 "$INSTALL_DIR/docky.py" completion --install || warn "Couldn't set up tab completion; run 'docky completion --install' later."

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
