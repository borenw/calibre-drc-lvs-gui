#!/usr/bin/env bash
# One-line installer for the Calibre DRC/LVS GUI.
# The app is a single self-contained Python file -- "install" just fetches it.
#
#   curl -fsSL https://raw.githubusercontent.com/borenw/calibre-drc-lvs-gui/main/install.sh | bash
#
# Optional: pass a destination path as the first argument.
set -euo pipefail

RAW="https://raw.githubusercontent.com/borenw/calibre-drc-lvs-gui/main/calibre_drc_lvs_gui.py"
DEST="${1:-$PWD/calibre_drc_lvs_gui.py}"

echo ">> downloading calibre_drc_lvs_gui.py -> $DEST"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$RAW" -o "$DEST"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$DEST" "$RAW"
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
