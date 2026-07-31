# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 pcbrom
#
# Markdown Preview plugin for gedit 46 (GTK3 / Gedit-3.0 / WebKit2-4.1).
# Renders the active Markdown document with pandoc, styled to resemble
# Apostrophe, and shows it full-view in place of the editor. Toggle with the
# header-bar button, Ctrl+M, or the menu; the editor itself is the "raw" view.
# Math renders offline as native MathML; ```mermaid blocks and .mmd files render
# as diagrams via an optional local mermaid.js. The preview updates live
# (debounced).
import gi
gi.require_version("Gedit", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GObject, Gedit, Gtk, Gio, GLib, WebKit2
import subprocess
import os
import re
import html as _html

PANEL_NAME = "MdPreviewPanel"
DEBOUNCE_MS = 500
MD_SUFFIXES = (".md", ".markdown", ".mmd", ".mdown", ".mkd")
MMD_SUFFIXES = (".mmd",)

# Mermaid is loaded from the plugin directory (bundled mermaid.js exposes the
# global window.mermaid). The <script> is injected only when a diagram is
# present, so plain Markdown stays lightweight.
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_MERMAID_JS = os.path.join(_PLUGIN_DIR, "mermaid.js")
MERMAID_SCRIPT = (
    '<script src="file://' + _MERMAID_JS + '"></script>'
    "<script>"
    "if (window.mermaid) {"
    " mermaid.initialize({ startOnLoad: false, securityLevel: 'loose',"
    " theme: window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'default' });"
    " mermaid.run({ querySelector: '.mermaid' });"
    "}"
    "</script>"
)
_MERMAID_UNWRAP = re.compile(r'<pre class="mermaid"><code>(.*?)</code></pre>', re.S)

# Apostrophe-like reading style, light and dark aware.
CSS = """
:root { color-scheme: light dark; }
html { font-size: 16px; }
body {
  margin: 0 auto; max-width: 46em; padding: 2.5em 2em 6em;
  font-family: "Cantarell", "Noto Sans", system-ui, sans-serif;
  line-height: 1.65; color: #2e3436; background: #fdfdfd;
  -webkit-font-smoothing: antialiased; word-wrap: break-word;
}
@media (prefers-color-scheme: dark) {
  body { color: #d3d7cf; background: #242424; }
  a { color: #8cb4ff; }
  h1,h2,h3,h4,h5,h6 { color: #eeeeec; }
  pre, code { background: #1b1b1b; }
  blockquote { color: #b5b5b5; border-left-color: #555; }
  hr { border-top-color: #555; }
  table th, table td { border-color: #555; }
  table th { background: #2f2f2f; }
}
h1,h2,h3,h4,h5,h6 { font-weight: 700; line-height: 1.25; margin: 1.6em 0 .5em; color: #1a1a1a; }
h1 { font-size: 1.9em; } h2 { font-size: 1.5em; } h3 { font-size: 1.25em; }
p { margin: 0 0 1em; }
a { color: #2a76c6; text-decoration: none; }
a:hover { text-decoration: underline; }
img { max-width: 100%; height: auto; }
code { font-family: "Source Code Pro", "Noto Sans Mono", monospace; font-size: .9em;
       background: #f0f0f0; padding: .12em .35em; border-radius: 4px; }
pre { background: #f0f0f0; padding: 1em 1.2em; border-radius: 8px; overflow-x: auto; }
pre code { background: none; padding: 0; }
blockquote { margin: 1em 0; padding: .1em 1.2em; color: #555;
             border-left: 4px solid #ccc; }
hr { border: 0; border-top: 1px solid #ddd; margin: 2em 0; }
ul, ol { padding-left: 1.6em; }
li { margin: .2em 0; }
table { border-collapse: collapse; margin: 1em 0; width: 100%; }
table th, table td { border: 1px solid #ccc; padding: .4em .7em; text-align: left; }
table th { background: #f4f4f4; }
"""

HTML_TEMPLATE = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<style>{css}</style></head><body>{body}{script}</body></html>"
)


def render_body(text, is_mmd):
    # A .mmd file is a single Mermaid diagram; wrap it verbatim. Otherwise run
    # Markdown through pandoc. gfm matches the author's GitHub-targeted syntax;
    # failures surface as visible text instead of a blank panel.
    if is_mmd:
        return '<pre class="mermaid">\n' + _html.escape(text) + "\n</pre>"
    try:
        # gfm is GitHub-Flavored Markdown; tex_math_dollars enables $...$ and
        # $$...$$; --mathml emits MathML that WebKitGTK renders natively, so math
        # works offline without JS or a CDN.
        proc = subprocess.run(
            ["pandoc", "--from=gfm+tex_math_dollars", "--to=html5", "--mathml", "--no-highlight"],
            input=text, capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return "<pre>pandoc error:\n" + GLib.markup_escape_text(proc.stderr) + "</pre>"
        # pandoc wraps a ```mermaid fence as <pre class="mermaid"><code>...;
        # strip the inner <code> so mermaid.run reads the diagram source.
        return _MERMAID_UNWRAP.sub(r'<pre class="mermaid">\1</pre>', proc.stdout)
    except FileNotFoundError:
        return "<pre>pandoc not found. Install it: sudo apt-get install pandoc</pre>"
    except Exception as exc:  # noqa: BLE001 - surface any failure in the panel
        return "<pre>" + GLib.markup_escape_text(str(exc)) + "</pre>"


class MdPreviewWindowActivatable(GObject.Object, Gedit.WindowActivatable):
    window = GObject.Property(type=Gedit.Window)

    def __init__(self):
        super().__init__()
        self._timeout = 0
        self._doc = None
        self._doc_handler = 0
        self._tab_handler = 0
        self._button = None
        self._button_handler = 0
        self._panel_handlers = []
        self._syncing = False
        self._active = False
        self._busy = False

    def do_activate(self):
        self._scrolled = Gtk.ScrolledWindow()
        self._webview = WebKit2.WebView()
        # Allow the rendered page (loaded via load_html with a file:// base) to
        # read local image files referenced by relative or file:// paths.
        wsettings = self._webview.get_settings()
        wsettings.set_property("allow-file-access-from-file-urls", True)
        wsettings.set_property("allow-universal-access-from-file-urls", True)
        self._scrolled.add(self._webview)
        self._scrolled.show_all()

        # Bottom panel: this is the panel type that exposes add_titled in
        # gedit 46 (the same one the bundled External Tools plugin uses). The
        # side panel is a PanelContainer that does not expose it here.
        panel = self.window.get_bottom_panel()
        panel.add_titled(self._scrolled, PANEL_NAME, "Markdown Preview")

        action = Gio.SimpleAction(name="markdown-preview")
        action.connect("activate", self._toggle)
        self.window.add_action(action)

        self._add_headerbar_button()
        # Keep the button in sync when the panel is toggled by other means
        # (Ctrl+M, the menu item, or closing the bottom panel).
        self._panel_handlers = [
            panel.connect("notify::visible", self._on_panel_notify),
            panel.connect("notify::visible-child", self._on_panel_notify),
        ]

        self._tab_handler = self.window.connect("active-tab-changed", self._on_tab_changed)
        self._connect_active_doc()
        self._update()

    def do_deactivate(self):
        if self._timeout:
            GLib.source_remove(self._timeout)
            self._timeout = 0
        self._disconnect_doc()
        if self._tab_handler:
            self.window.disconnect(self._tab_handler)
            self._tab_handler = 0
        panel = self.window.get_bottom_panel()
        for hid in self._panel_handlers:
            panel.disconnect(hid)
        self._panel_handlers = []
        if self._active:
            docs = self._doc_area()
            if docs is not None:
                docs.show()
            self._active = False
        if self._button is not None:
            if self._button_handler:
                self._button.disconnect(self._button_handler)
            self._button.get_parent().remove(self._button)
            self._button = None
        panel.remove(self._scrolled)
        self.window.remove_action("markdown-preview")

    # --- headerbar button ---------------------------------------------------
    def _search_headerbar(self, widget):
        if isinstance(widget, Gtk.HeaderBar):
            return widget
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                found = self._search_headerbar(child)
                if found is not None:
                    return found
        return None

    def _find_headerbar(self):
        # gedit's titlebar is a split-header GtkPaned: child2 (the end pane) is
        # the document-side header bar holding Save and the menu. Prefer it so
        # the button sits with those controls, not on the narrow side-panel
        # header bar where it gets clipped at the divider.
        tb = self.window.get_titlebar()
        if isinstance(tb, Gtk.Paned):
            end = tb.get_child2()
            found = self._search_headerbar(end) if end is not None else None
            if found is not None:
                return found
        for root in (tb, self.window):
            if root is None:
                continue
            found = self._search_headerbar(root)
            if found is not None:
                return found
        return None

    def _add_headerbar_button(self):
        headerbar = self._find_headerbar()
        if headerbar is None:
            return
        btn = Gtk.ToggleButton()
        btn.set_image(Gtk.Image.new_from_icon_name("format-text-rich-symbolic", Gtk.IconSize.BUTTON))
        btn.set_tooltip_text("Toggle Markdown preview (Ctrl+M)")
        btn.show_all()
        headerbar.pack_end(btn)
        self._button_handler = btn.connect("toggled", self._on_button_toggled)
        self._button = btn
        self._sync_button()

    def _on_button_toggled(self, btn):
        if self._syncing:
            return
        self._set_visible(btn.get_active())

    def _sync_button(self, *_args):
        if self._button is None:
            return
        self._syncing = True
        self._button.set_active(self._is_visible())
        self._syncing = False

    # --- toggling (full-view mode: preview replaces the editor) -------------
    def _doc_area(self):
        # The documents area is child1 of the vertical GtkPaned whose child2
        # holds the bottom panel. Hiding it lets the panel fill the whole area,
        # so the preview reads as a full page rather than a split.
        node = self._scrolled.get_parent()
        while node is not None:
            if isinstance(node, Gtk.Paned):
                return node.get_child1()
            node = node.get_parent()
        return None

    def _is_visible(self):
        return self._active

    def _set_visible(self, show):
        self._busy = True
        panel = self.window.get_bottom_panel()
        docs = self._doc_area()
        if show:
            self._update()
            panel.props.visible_child = self._scrolled
            panel.set_visible(True)
            if docs is not None:
                docs.hide()
        else:
            if docs is not None:
                docs.show()
            panel.set_visible(False)
        self._active = show
        self._busy = False
        self._sync_button()

    def _on_panel_notify(self, *_args):
        # If the panel is closed or switched away while the preview is full,
        # restore the editor so the user is never left on a blank area.
        if self._busy:
            return
        panel = self.window.get_bottom_panel()
        away = not panel.props.visible or panel.props.visible_child is not self._scrolled
        if self._active and away:
            docs = self._doc_area()
            if docs is not None:
                docs.show()
            self._active = False
        self._sync_button()

    def _toggle(self, *_args):
        self._set_visible(not self._is_visible())

    # --- document tracking --------------------------------------------------
    def _on_tab_changed(self, *_args):
        self._connect_active_doc()
        self._update()

    def _connect_active_doc(self):
        self._disconnect_doc()
        doc = self.window.get_active_document()
        if doc is None:
            return
        self._doc = doc
        self._doc_handler = doc.connect("changed", self._on_changed)

    def _disconnect_doc(self):
        if self._doc is not None and self._doc_handler:
            self._doc.disconnect(self._doc_handler)
        self._doc = None
        self._doc_handler = 0

    def _on_changed(self, *_args):
        if self._timeout:
            GLib.source_remove(self._timeout)
        self._timeout = GLib.timeout_add(DEBOUNCE_MS, self._update_now)

    def _update_now(self):
        self._timeout = 0
        self._update()
        return False

    # --- rendering ----------------------------------------------------------
    def _is_markdown(self, doc):
        gfile = doc.get_file()
        loc = gfile.get_location() if gfile is not None else None
        if loc is not None:
            name = loc.get_basename() or ""
            if name.lower().endswith(MD_SUFFIXES):
                return True
        lang = doc.get_language()
        return lang is not None and lang.get_id() == "markdown"

    def _is_mmd(self, doc):
        gfile = doc.get_file()
        loc = gfile.get_location() if gfile is not None else None
        name = (loc.get_basename() or "") if loc is not None else ""
        return name.lower().endswith(MMD_SUFFIXES)

    def _base_uri(self, doc):
        gfile = doc.get_file()
        loc = gfile.get_location() if gfile is not None else None
        if loc is not None:
            parent = loc.get_parent()
            if parent is not None:
                return parent.get_uri() + "/"
        return "file:///"

    def _render(self, body, script=""):
        return HTML_TEMPLATE.format(css=CSS, body=body, script=script)

    def _update(self):
        doc = self.window.get_active_document()
        if doc is None:
            self._webview.load_html(self._render(""), "file:///")
            return
        if not self._is_markdown(doc):
            body = "<p style='opacity:.6'>This document is not Markdown.</p>"
            self._webview.load_html(self._render(body), "file:///")
            return
        start, end = doc.get_bounds()
        text = doc.get_text(start, end, False)
        body = render_body(text, self._is_mmd(doc))
        # Load mermaid only when a diagram is actually present.
        script = MERMAID_SCRIPT if 'class="mermaid"' in body else ""
        self._webview.load_html(self._render(body, script), self._base_uri(doc))


class MdPreviewAppActivatable(GObject.Object, Gedit.AppActivatable):
    app = GObject.Property(type=Gedit.App)

    def do_activate(self):
        self.app.set_accels_for_action("win.markdown-preview", ["<Primary>m"])
        # "tools-section-1" is a valid extension point in gedit 46 (used by the
        # bundled External Tools plugin); "view-menu" is not and returns None.
        self._menu_ext = self.extend_menu("tools-section-1")
        if self._menu_ext is not None:
            item = Gio.MenuItem.new("Markdown Preview", "win.markdown-preview")
            self._menu_ext.append_menu_item(item)

    def do_deactivate(self):
        self.app.set_accels_for_action("win.markdown-preview", [])
        self._menu_ext = None
