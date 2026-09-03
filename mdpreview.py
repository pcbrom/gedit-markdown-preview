# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 pcbrom
#
# Markdown Preview plugin for gedit 46 (GTK3 / Gedit-3.0 / WebKit2-4.1).
# Renders the active Markdown document with pandoc, styled to resemble
# Apostrophe, and shows it full-view in place of the editor. Toggle with the
# header-bar button, Ctrl+M, or the menu; the editor itself is the "raw" view.
# Math renders offline as native MathML; ```mermaid blocks and .mmd files render
# as diagrams via an optional local mermaid.js. The preview updates live (debounced) and
# keeps the reading position across re-renders. Code blocks are syntax
# highlighted offline by pandoc, long documents get a navigable outline, and the
# rendered page can be exported to PDF through the print dialog.
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
# Full view hides the editor so the preview fills the window; split keeps both
# visible. Split is what live editing wants, since the buffer can only change
# while the editor is reachable, and it is the mode the scroll restore serves.
FULL_VIEW = True
DEBOUNCE_MS = 500
# Second scroll restore, after async content (mermaid, MathML) changes the page
# height and clamps the first one.
RESCROLL_MS = 160
MD_SUFFIXES = (".md", ".markdown", ".mmd", ".mdown", ".mkd")
MMD_SUFFIXES = (".mmd",)
# A document shorter than this many headings gets no outline.
TOC_MIN_HEADINGS = 3

# Mermaid is loaded from the plugin directory (bundled mermaid.js exposes the
# global window.mermaid). The <script> is injected only when a diagram is
# present, so plain Markdown stays lightweight.
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_MERMAID_JS = os.path.join(_PLUGIN_DIR, "mermaid.js")
# Each diagram is parsed before it is rendered, and a failure replaces that one
# diagram with its error message. Without this a malformed diagram fails
# silently and leaves a blank area. mermaid 11 returns promises from both parse
# and run, so async rejections are caught in the promise chain, not by try.
MERMAID_SCRIPT = (
    '<script src="file://' + _MERMAID_JS + '"></script>'
    "<script>"
    "(function(){"
    " if(!window.mermaid){return;}"
    " var dark=window.matchMedia('(prefers-color-scheme: dark)').matches;"
    " mermaid.initialize({startOnLoad:false,securityLevel:'loose',"
    "theme:dark?'dark':'default'});"
    " var nodes=document.querySelectorAll('.mermaid');"
    " var fail=function(node,err){"
    "  var box=document.createElement('pre');"
    "  box.className='mmd-error';"
    "  box.textContent='Mermaid diagram error:\\n'+"
    "(err&&err.message?err.message:String(err));"
    "  if(node.parentNode){node.parentNode.replaceChild(box,node);}"
    " };"
    " for(var i=0;i<nodes.length;i++){"
    "  (function(node){"
    "   var src=node.textContent;"
    "   Promise.resolve().then(function(){return mermaid.parse(src);})"
    "    .then(function(){return mermaid.run({nodes:[node]});})"
    "    .catch(function(err){fail(node,err);});"
    "  })(nodes[i]);"
    " }"
    "})();"
    "</script>"
)
_MERMAID_UNWRAP = re.compile(r'<pre class="mermaid"><code>(.*?)</code></pre>', re.S)

# Injected into every page: reports the scroll position back to the plugin so a
# re-render can restore it, and builds the outline for long documents. Plain
# ES5 so it does not depend on the WebKit version shipped with the desktop.
BASE_SCRIPT = (
    "<script>"
    "(function(){"
    " var send=function(){try{"
    "window.webkit.messageHandlers.mdscroll.postMessage(String(window.scrollY));"
    "}catch(e){}};"
    " window.addEventListener('scroll',send,{passive:true});"
    " var hs=document.querySelectorAll('h1,h2,h3');"
    " if(hs.length<" + str(TOC_MIN_HEADINGS) + "){return;}"
    " var nav=document.createElement('nav');"
    " nav.id='mdtoc';"
    " for(var i=0;i<hs.length;i++){"
    "  var h=hs[i];"
    "  if(!h.id){h.id='mdh'+i;}"
    "  var a=document.createElement('a');"
    "  a.href='#'+h.id;"
    "  a.textContent=h.textContent;"
    "  a.className='lv'+h.tagName.charAt(1);"
    "  nav.appendChild(a);"
    " }"
    " document.body.appendChild(nav);"
    "})();"
    "</script>"
)

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

/* Diagram that failed to parse or render, shown in place of the diagram. */
.mmd-error {
  background: #fff4f4; border-left: 4px solid #d33; color: #8a1f1f;
  white-space: pre-wrap; font-size: .85em;
}
@media (prefers-color-scheme: dark) {
  .mmd-error { background: #2a1b1b; border-left-color: #e06c6c; color: #f0b0b0; }
}

/* Outline for long documents. Only shown when the window is wide enough that
   it does not crowd the text column. */
#mdtoc {
  position: fixed; top: 2.5em; left: 1.5em; width: 14em; max-height: 80vh;
  overflow-y: auto; font-size: .82em; line-height: 1.45; display: none;
}
#mdtoc a { display: block; color: #6b6b6b; text-decoration: none; padding: .1em 0; }
#mdtoc a:hover { color: #2a76c6; text-decoration: none; }
#mdtoc a.lv2 { padding-left: .9em; }
#mdtoc a.lv3 { padding-left: 1.8em; }
@media (min-width: 82em) { #mdtoc { display: block; } }
@media (prefers-color-scheme: dark) {
  #mdtoc a { color: #9a9a9a; }
  #mdtoc a:hover { color: #8cb4ff; }
}
@media print { #mdtoc { display: none; } }

/* pandoc syntax highlighting (offline, no JS). Token classes are the ones
   pandoc emits for fenced code with a language. */
div.sourceCode { overflow-x: auto; }
code span.kw { color: #007020; font-weight: 700; }
code span.cf { color: #007020; font-weight: 700; }
code span.im { color: #007020; font-weight: 700; }
code span.dt { color: #902000; }
code span.dv, code span.bn, code span.fl { color: #40a070; }
code span.ch, code span.st, code span.vs, code span.sc, code span.ss { color: #4070a0; }
code span.co { color: #60a0b0; font-style: italic; }
code span.do { color: #ba2121; font-style: italic; }
code span.al, code span.er { color: #d00000; font-weight: 700; }
code span.wa, code span.an { color: #60a0b0; font-weight: 700; font-style: italic; }
code span.fu { color: #06287e; }
code span.cn { color: #880000; }
code span.va { color: #19177c; }
code span.op { color: #666666; }
code span.bu, code span.ex { color: #008000; }
code span.pp { color: #bc7a00; }
code span.at { color: #7d9029; }
@media (prefers-color-scheme: dark) {
  code span.kw, code span.cf, code span.im { color: #8ec07c; }
  code span.dt { color: #fb8b5c; }
  code span.dv, code span.bn, code span.fl { color: #b8bb26; }
  code span.ch, code span.st, code span.vs, code span.sc, code span.ss { color: #83a598; }
  code span.co, code span.wa, code span.an { color: #928374; }
  code span.do { color: #fb4934; }
  code span.al, code span.er { color: #fb4934; }
  code span.fu { color: #83a598; }
  code span.cn { color: #d3869b; }
  code span.va { color: #d5c4a1; }
  code span.op { color: #a89984; }
  code span.bu, code span.ex { color: #b8bb26; }
  code span.pp { color: #fabd2f; }
  code span.at { color: #d3869b; }
}
"""

HTML_TEMPLATE = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<style>{css}</style></head><body>{body}{base}{script}</body></html>"
)


def render_body(text, is_mmd):
    # A .mmd file is a single Mermaid diagram; wrap it verbatim. Otherwise run
    # Markdown through pandoc. gfm matches the author's GitHub-targeted syntax;
    # failures surface as visible text instead of a blank panel.
    if is_mmd:
        return '<pre class="mermaid">\n' + _html.escape(text) + "\n</pre>"
    try:
        # gfm is GitHub-Flavored Markdown; tex_math_dollars enables $...$ and
        # $$...$$; --mathml emits MathML that
        # WebKitGTK renders natively, so math works offline without JS or a CDN.
        # Highlighting is left on: pandoc emits token spans that the stylesheet
        # colors, which keeps code readable without loading a JS highlighter.
        proc = subprocess.run(
            ["pandoc", "--from=gfm+tex_math_dollars", "--to=html5", "--mathml"],
            input=text, capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return "<pre>pandoc error:\n" + GLib.markup_escape_text(proc.stderr) + "</pre>"
        # pandoc wraps a ```mermaid fence as <pre class="mermaid"><code>...;
        # strip the inner <code> so mermaid reads the diagram source.
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
        self._scroll_y = 0
        self._pending_async = False

    def do_activate(self):
        self._scrolled = Gtk.ScrolledWindow()
        # A user content manager is needed for the page to post its scroll
        # position back; without it the reading position is lost on every
        # re-render, which the debounced live update makes constant.
        ucm = WebKit2.UserContentManager()
        ucm.register_script_message_handler("mdscroll")
        ucm.connect("script-message-received::mdscroll", self._on_scroll_message)
        self._webview = WebKit2.WebView.new_with_user_content_manager(ucm)
        # Allow the rendered page (loaded via load_html with a file:// base) to
        # read local image files referenced by relative or file:// paths.
        wsettings = self._webview.get_settings()
        wsettings.set_property("allow-file-access-from-file-urls", True)
        wsettings.set_property("allow-universal-access-from-file-urls", True)
        self._webview.connect("load-changed", self._on_load_changed)
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

        export = Gio.SimpleAction(name="markdown-preview-export")
        export.connect("activate", self._export)
        self.window.add_action(export)

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
        self.window.remove_action("markdown-preview-export")

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
            if FULL_VIEW and docs is not None:
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
        elif not self._active and not away:
            # The panel was opened by other means (its own tab, the View menu).
            # Adopt it, so the header-bar button reflects what is on screen and
            # the preview is refreshed for the current document.
            self._active = True
            self._update()
        self._sync_button()

    def _toggle(self, *_args):
        self._set_visible(not self._is_visible())

    # --- export -------------------------------------------------------------
    def _export(self, *_args):
        # The GTK print dialog includes "Print to File", which writes the
        # rendered page as PDF with diagrams and math already laid out. That is
        # why export goes through printing instead of writing HTML: the bundled
        # mermaid.js is referenced by path, so a saved HTML file would only
        # render on this machine.
        try:
            op = WebKit2.PrintOperation.new(self._webview)
            op.run_dialog(self.window)
        except Exception:  # noqa: BLE001 - export must never break the editor
            pass

    # --- scroll position ----------------------------------------------------
    def _on_scroll_message(self, _ucm, result):
        # The page posts its scrollY on every scroll event; the last value seen
        # is what a re-render restores.
        try:
            value = result.get_js_value().to_string()
        except AttributeError:
            try:
                value = result.get_value().to_string()
            except Exception:  # noqa: BLE001
                return
        except Exception:  # noqa: BLE001
            return
        try:
            self._scroll_y = max(0, int(float(value)))
        except (TypeError, ValueError):
            pass

    def _restore_scroll(self):
        if self._scroll_y <= 0:
            return False
        self._webview.run_javascript(
            "window.scrollTo(0,%d);" % self._scroll_y, None, None, None
        )
        return False

    def _on_load_changed(self, _webview, event):
        if event != WebKit2.LoadEvent.FINISHED:
            return
        self._restore_scroll()
        # Mermaid and MathML change the page height after load, which clamps the
        # first restore on a document that grows below the fold.
        if self._pending_async:
            GLib.timeout_add(RESCROLL_MS, self._restore_scroll)

    # --- document tracking --------------------------------------------------
    def _on_tab_changed(self, *_args):
        # A different document starts at the top; keeping the old offset would
        # drop the reader in an unrelated place.
        self._scroll_y = 0
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
        return HTML_TEMPLATE.format(css=CSS, body=body, base=BASE_SCRIPT, script=script)

    def _update(self):
        doc = self.window.get_active_document()
        if doc is None:
            self._pending_async = False
            self._webview.load_html(self._render(""), "file:///")
            return
        if not self._is_markdown(doc):
            self._pending_async = False
            body = "<p style='opacity:.6'>This document is not Markdown.</p>"
            self._webview.load_html(self._render(body), "file:///")
            return
        start, end = doc.get_bounds()
        text = doc.get_text(start, end, False)
        body = render_body(text, self._is_mmd(doc))
        # Load mermaid only when a diagram is actually present.
        script = MERMAID_SCRIPT if 'class="mermaid"' in body else ""
        self._pending_async = bool(script) or "<math" in body
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
            export = Gio.MenuItem.new(
                "Export Markdown preview (PDF)", "win.markdown-preview-export"
            )
            self._menu_ext.append_menu_item(export)

    def do_deactivate(self):
        self.app.set_accels_for_action("win.markdown-preview", [])
        self._menu_ext = None
