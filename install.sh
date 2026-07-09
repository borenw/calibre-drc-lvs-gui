#!/usr/bin/env bash
# One-line installer for the Calibre DRC/LVS GUI.
# The app is a single self-contained Python file -- "install" just fetches it.
#
#   curl -fsSL https://raw.githubusercontent.com/borenw/calibre-drc-lvs-gui/main/install.sh | bash
#
# Optional: pass a destination path as the first argument.
set -euo pipefail

# Fetch via the GitHub API (Accept: raw) -- this bypasses the raw.githubusercontent
# CDN, which caches for ~5 min and would otherwise serve a stale copy right after a
# push. Falls back to the raw URL if the API is unavailable.
API="https://api.github.com/repos/borenw/calibre-drc-lvs-gui/contents/calibre_drc_lvs_gui.py?ref=main"
RAW="https://raw.githubusercontent.com/borenw/calibre-drc-lvs-gui/main/calibre_drc_lvs_gui.py"
DEST="${1:-$PWD/calibre_drc_lvs_gui.py}"

echo ">> downloading calibre_drc_lvs_gui.py -> $DEST"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL -H "Accept: application/vnd.github.raw" "$API" -o "$DEST" \
    || curl -fsSL "$RAW" -o "$DEST"
elif command -v wget >/dev/null 2>&1; then
  wget -q --header="Accept: application/vnd.github.raw" -O "$DEST" "$API" \
    || wget -qO "$DEST" "$RAW"
else
  echo "!! need curl or wget" >&2; exit 1
fi

echo ">> done. start it with:"
echo "     python3 \"$DEST\" --open"
echo
read -r -p "Launch it now? [Y/n] " ans || ans="n"
case "$ans" in
  ""|y|Y) exec python3 "$DEST" --open ;;
  *) echo "ok — run the command above when ready." ;;
esac
