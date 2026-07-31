#!/usr/bin/env bash
# Install the Markdown Preview plugin into the current user's gedit.
# Copies the plugin files to ~/.local/share/gedit/plugins, enables the plugin
# via gsettings, and (optionally) provisions mermaid.js for diagram rendering.
#
# Mermaid is optional: without mermaid.js the plugin still works, it just shows
# ```mermaid blocks and .mmd files as text instead of diagrams.
#
# Run from a graphical session (gsettings needs the user bus). Idempotent.
# Re-open gedit afterwards so it reloads the plugin list.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST="$HOME/.local/share/gedit/plugins"

echo "==> checking dependencies"
missing=""
command -v gedit >/dev/null || missing="$missing gedit"
command -v pandoc >/dev/null || missing="$missing pandoc"
python3 -c "import gi; gi.require_version('Gedit','3.0')" 2>/dev/null \
    || echo "NOTE: the gedit Python introspection may be missing (package: gedit)."
[ -f /usr/lib/x86_64-linux-gnu/girepository-1.0/WebKit2-4.1.typelib ] \
    || missing="$missing gir1.2-webkit2-4.1"
if [ -n "$missing" ]; then
    echo "Missing dependencies:$missing"
    echo "On Debian/Ubuntu: sudo apt-get install$missing"
    echo "Continuing anyway; the plugin will not work until they are present."
fi

echo "==> copying plugin to $DST"
mkdir -p "$DST"
cp "$HERE/mdpreview.py" "$HERE/mdpreview.plugin" "$DST/"

echo "==> enabling the plugin (gsettings active-plugins)"
python3 - <<'PY'
import ast, subprocess
KEY = ("org.gnome.gedit.plugins", "active-plugins")
cur = subprocess.run(["gsettings", "get", *KEY], capture_output=True, text=True).stdout.strip()
try:
    plugins = ast.literal_eval(cur) if cur.startswith("[") else []
except Exception:
    plugins = []
if "mdpreview" not in plugins:
    plugins.append("mdpreview")
    subprocess.run(["gsettings", "set", *KEY, str(plugins)], check=True)
print("    active-plugins:", plugins)
PY

# --- mermaid.js (optional) -------------------------------------------------
MERMAID_DST="$DST/mermaid.js"
MERMAID_CDN="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.js"
if [ -f "$MERMAID_DST" ]; then
    echo "==> mermaid.js already present, keeping it"
else
    # Prefer a local copy from a mermaid-cli install; fall back to a download.
    LOCAL="$(find /snap /usr/lib/node_modules "$HOME" -path '*/mermaid/dist/mermaid.js' 2>/dev/null | head -1 || true)"
    if [ -n "$LOCAL" ] && grep -q 'globalThis\["mermaid"\]' "$LOCAL" 2>/dev/null; then
        echo "==> copying mermaid.js from $LOCAL"
        cp "$LOCAL" "$MERMAID_DST"
    elif command -v curl >/dev/null; then
        echo "==> downloading mermaid.js from jsDelivr"
        curl -fsSL --connect-timeout 15 --max-time 120 "$MERMAID_CDN" -o "$MERMAID_DST" \
            && echo "    mermaid.js installed" \
            || { rm -f "$MERMAID_DST"; echo "    download failed; diagrams will render as text (see README)"; }
    else
        echo "==> no local mermaid.js and no curl; skipping."
        echo "    Diagrams will render as text. To enable them, place a mermaid.js"
        echo "    (the build that sets window.mermaid) at: $MERMAID_DST"
    fi
fi

echo
echo "Done. Re-open gedit, open a .md file, and press Ctrl+M (or use the"
echo "header-bar button) to toggle the preview."
