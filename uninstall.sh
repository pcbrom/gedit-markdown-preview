#!/usr/bin/env bash
# Remove the Markdown Preview plugin from the current user's gedit: deletes the
# plugin files and disables it in gsettings. Re-open gedit afterwards.
set -euo pipefail

DST="$HOME/.local/share/gedit/plugins"

echo "==> disabling the plugin (gsettings active-plugins)"
python3 - <<'PY'
import ast, subprocess
KEY = ("org.gnome.gedit.plugins", "active-plugins")
cur = subprocess.run(["gsettings", "get", *KEY], capture_output=True, text=True).stdout.strip()
try:
    plugins = ast.literal_eval(cur) if cur.startswith("[") else []
except Exception:
    plugins = []
if "mdpreview" in plugins:
    plugins.remove("mdpreview")
    subprocess.run(["gsettings", "set", *KEY, str(plugins)], check=True)
print("    active-plugins:", plugins)
PY

echo "==> removing plugin files"
rm -f "$DST/mdpreview.py" "$DST/mdpreview.plugin" "$DST/mermaid.js"
rm -rf "$DST/__pycache__"
echo "Done. Re-open gedit."
