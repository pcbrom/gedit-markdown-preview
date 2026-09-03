# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 pcbrom
#
# Markdown Preview plugin for gedit 46 (GTK3 / Gedit-3.0 / WebKit2-4.1).
# Renders the active Markdown document with pandoc, styled to resemble
# Apostrophe, in the share of the window chosen on the header-bar button. Ctrl+M
# and the menu toggle it; the editor itself is the "raw" view, and the two scroll
# together in both directions.
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
import json
import os
import re
import html as _html

PANEL_NAME = "MdPreviewPanel"
# How much of the window the preview holds, in percent: 0 is the editor alone,
# 100 the preview alone, and the values between split the window. The header-bar
# button offers these steps and its label shows the one in effect. The default is
# the share a preview opens at, not a share forced at startup.
LEVELS = (0, 25, 50, 75, 100)
DEFAULT_LEVEL = 50
DEBOUNCE_MS = 500
# Second scroll restore, after async content (mermaid, MathML) changes the page
# height and clamps the first one.
RESCROLL_MS = 160
# Window during which a side that was moved by the other stops reporting, so
# the two do not chase each other.
SYNC_UNLOCK_MS = 150
# Zoom of the rendered page, driven by Ctrl with plus, minus and zero. The
# WebView keeps the level across reloads, so it survives the live update.
ZOOM_MIN = 0.5
ZOOM_MAX = 3.0
ZOOM_STEP = 0.1
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
# The fence keeps whatever attributes pandoc put on it (the source position among
# them), so the diagram stays an anchor for scroll sync.
_MERMAID_UNWRAP = re.compile(
    r'<pre class="mermaid"(?P<attrs>[^>]*)><code[^>]*>(?P<src>.*?)</code></pre>', re.S
)

# Source positions come from pandoc's sourcepos extension, which also wraps every
# word in a span carrying its position. Those inline wrappers inflate the document
# by an order of magnitude and are useless for vertical scrolling, where every word
# on a line shares one y. Pruning them keeps the block anchors and the size.
_SPAN_TAG = re.compile(r"<span(?P<attrs>[^>]*)>|</span>")


def strip_pos_spans(html):
    out, stack, pos = [], [], 0
    for match in _SPAN_TAG.finditer(html):
        out.append(html[pos:match.start()])
        pos = match.end()
        if match.group(0).startswith("</"):
            drop = stack.pop() if stack else False
            if not drop:
                out.append(match.group(0))
        else:
            attrs = match.group("attrs")
            drop = "data-pos=" in attrs and "class=" not in attrs and "id=" not in attrs
            stack.append(drop)
            if not drop:
                out.append(match.group(0))
    out.append(html[pos:])
    return "".join(out)

# Injected into every page: reports the scroll position back to the plugin so a
# re-render can restore it, and builds the outline for long documents. Plain
# ES5 so it does not depend on the WebKit version shipped with the desktop.
BASE_SCRIPT = (
    "<script>"
    "(function(){"
    " (function(){"
    " var hs=document.querySelectorAll('h1,h2,h3');"
    " if(hs.length<" + str(TOC_MIN_HEADINGS) + "){return;}"
    " var nav=document.createElement('details');"
    " nav.id='mdtoc';"
    " var cap=document.createElement('summary');"
    " cap.textContent='Outline';"
    " nav.appendChild(cap);"
    " for(var i=0;i<hs.length;i++){"
    "  var h=hs[i];"
    "  if(!h.id){h.id='mdh'+i;}"
    "  var a=document.createElement('a');"
    "  a.href='#'+h.id;"
    "  a.textContent=h.textContent;"
    "  a.className='lv'+h.tagName.charAt(1);"
    "  nav.appendChild(a);"
    " }"
    # Wide enough for the side rail: open it and hide the caption. Narrow: it
    # becomes a collapsed block at the top, so it never squeezes the text.
    " nav.open=window.matchMedia('(min-width: 66em)').matches;"
    " document.body.insertBefore(nav,document.body.firstChild);"
    " })();"
    # Scroll sync. Every block pandoc emitted carries the source line it came
    # from, so the two positions are tied by interpolating between the anchors
    # that bracket the current one. The map is built after the outline is in
    # place, since inserting it shifts every offset.
    " var anchors=[];"
    " var build=function(){"
    "  anchors=[];"
    "  var els=document.querySelectorAll('[data-pos]');"
    "  var seen={};"
    "  for(var i=0;i<els.length;i++){"
    "   var ln=parseInt(els[i].getAttribute('data-pos'),10);"
    "   if(isNaN(ln)||seen[ln]){continue;}"
    "   seen[ln]=1;"
    "   anchors.push([ln-1,els[i].getBoundingClientRect().top+window.scrollY]);"
    "  }"
    "  anchors.sort(function(a,b){return a[0]-b[0];});"
    " };"
    " var interp=function(v,from,to){"
    "  if(!anchors.length){return 0;}"
    "  if(v<=anchors[0][from]){return anchors[0][to];}"
    "  for(var i=0;i<anchors.length-1;i++){"
    "   var a=anchors[i],b=anchors[i+1];"
    "   if(v>=a[from]&&v<=b[from]){"
    "    var d=b[from]-a[from];"
    "    return a[to]+(d?(v-a[from])/d:0)*(b[to]-a[to]);"
    "   }"
    "  }"
    "  return anchors[anchors.length-1][to];"
    " };"
    # While the plugin drives the page, the page must not report back, or the
    # two sides chase each other.
    " var quiet=false;"
    " var hush=function(){quiet=true;setTimeout(function(){quiet=false;},120);};"
    " window.__mdGoToLine=function(line){"
    "  if(!anchors.length){build();}"
    "  hush();"
    "  window.scrollTo(0,Math.max(0,interp(line,0,1)));"
    " };"
    " window.__mdQuietScroll=function(y){hush();window.scrollTo(0,y);};"
    # Zoom relays out the page, so the plugin asks for a fresh anchor map.
    " window.__mdRebuild=build;"
    " var send=function(){"
    "  if(quiet){return;}"
    "  try{window.webkit.messageHandlers.mdscroll.postMessage(JSON.stringify("
    "{y:Math.round(window.scrollY),line:Math.round(interp(window.scrollY,1,0))}));"
    "}catch(e){}"
    " };"
    " window.addEventListener('scroll',send,{passive:true});"
    " window.addEventListener('resize',build,{passive:true});"
    " build();"
    # Diagrams and math settle after load and move everything below them.
    " setTimeout(build,400);"
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

/* Outline for long documents. Only shown when the window is wide enough that it
   does not crowd the text column: the 46em column plus a 14em outline plus the
   left margin and a gutter need 64.5em, so 66em is the first safe step. */
#mdtoc { font-size: .82em; line-height: 1.45; margin: 0 0 2em; }
#mdtoc > summary { cursor: pointer; color: #6b6b6b; font-weight: 600; }
#mdtoc a { display: block; color: #6b6b6b; text-decoration: none; padding: .1em 0; }
#mdtoc a:hover { color: #2a76c6; text-decoration: none; }
#mdtoc a.lv2 { padding-left: .9em; }
#mdtoc a.lv3 { padding-left: 1.8em; }
/* Wide enough for a side rail beside the text column: pin it and drop the
   caption. Below that it stays a collapsed block at the top of the document,
   which is why the outline is available at any width. */
@media (min-width: 66em) {
  #mdtoc {
    position: fixed; top: 2.5em; left: 1.5em; width: 14em; max-height: 80vh;
    overflow-y: auto; margin: 0;
  }
  #mdtoc > summary { display: none; }
}
@media (prefers-color-scheme: dark) {
  #mdtoc > summary { color: #9a9a9a; }
  #mdtoc a { color: #9a9a9a; }
  #mdtoc a:hover { color: #8cb4ff; }
}
@media print { #mdtoc { display: none; } }

/* Edit mode. Only in this mode is a block clickable, so an ordinary reading
   click never turns into an edit. */
body.mdedit [data-pos]:hover {
  outline: 2px dashed #2a76c6; outline-offset: 3px; cursor: text;
}
textarea.mdedit-box {
  width: 100%; box-sizing: border-box; font-family: "Source Code Pro", monospace;
  font-size: .92em; line-height: 1.5; padding: .6em .8em; border: 2px solid #2a76c6;
  border-radius: 6px; background: #fff; color: #2e3436; resize: vertical;
}
@media (prefers-color-scheme: dark) {
  textarea.mdedit-box { background: #1b1b1b; color: #d3d7cf; border-color: #8cb4ff; }
  body.mdedit [data-pos]:hover { outline-color: #8cb4ff; }
}

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

# Editing happens on the block the click landed in, and the text it shows is the
# Markdown of the source lines that produced it, fetched from the plugin. The
# page never converts HTML back to Markdown: the plugin owns the file and
# replaces only the line range it handed out.
EDIT_SCRIPT = (
    "<script>"
    "(function(){"
    " var open=null,on=false;"
    " var post=function(o){try{window.webkit.messageHandlers.mdedit.postMessage("
    "JSON.stringify(o));}catch(e){}};"
    # The first ancestor carrying a position is the smallest block containing the
    # click, which is what makes a list item win over the list around it.
    " var blockAt=function(node){"
    "  while(node&&node!==document.body){"
    "   if(node.getAttribute&&node.getAttribute('data-pos')){return node;}"
    "   node=node.parentNode;"
    "  }"
    "  return null;"
    " };"
    " var restore=function(){"
    "  if(open&&open.box&&open.box.parentNode){"
    "   open.box.parentNode.replaceChild(open.el,open.box);"
    "  }"
    "  open=null;"
    " };"
    " var commit=function(){"
    "  if(!open){return;}"
    "  var payload={kind:'commit',pos:open.pos,text:open.box.value};"
    "  restore();"
    "  post(payload);"
    " };"
    " window.__mdSetEdit=function(flag){"
    "  on=!!flag;"
    "  if(!on){restore();}"
    "  document.body.classList.toggle('mdedit',on);"
    " };"
    " window.__mdCancelEdit=restore;"
    " window.__mdOpenBlock=function(pos,text){"
    "  var els=document.querySelectorAll('[data-pos]'),el=null,i;"
    "  for(i=0;i<els.length;i++){"
    "   if(els[i].getAttribute('data-pos')===pos){el=els[i];break;}"
    "  }"
    "  if(!el){return;}"
    "  restore();"
    "  var box=document.createElement('textarea');"
    "  box.className='mdedit-box';"
    "  box.value=text;"
    "  box.rows=Math.max(2,text.split('\\n').length);"
    "  el.parentNode.replaceChild(box,el);"
    "  open={pos:pos,el:el,box:box};"
    "  box.addEventListener('keydown',function(ev){"
    "   if(ev.key==='Escape'){ev.preventDefault();restore();post({kind:'cancel'});}"
    "   else if(ev.key==='Enter'&&(ev.ctrlKey||ev.metaKey)){ev.preventDefault();commit();}"
    "  });"
    "  box.addEventListener('blur',function(){if(open&&open.box===box){commit();}});"
    "  box.focus();"
    " };"
    " document.addEventListener('click',function(ev){"
    "  if(!on){return;}"
    "  if(open&&open.box&&open.box.contains(ev.target)){return;}"
    "  var el=blockAt(ev.target);"
    "  if(!el){return;}"
    "  ev.preventDefault();"
    "  post({kind:'request',pos:el.getAttribute('data-pos')});"
    " },true);"
    "})();"
    "</script>"
)

HTML_TEMPLATE = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<style>{css}</style></head><body>{body}{base}{edit}{script}</body></html>"
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
        # sourcepos tags each block with the source line it came from, which is
        # what ties the two scroll positions together.
        proc = subprocess.run(
            ["pandoc", "--from=gfm+tex_math_dollars+sourcepos", "--to=html5", "--mathml"],
            input=text, capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return "<pre>pandoc error:\n" + GLib.markup_escape_text(proc.stderr) + "</pre>"
        body = strip_pos_spans(proc.stdout)
        # pandoc wraps a ```mermaid fence as <pre class="mermaid"><code>...;
        # strip the inner <code> so mermaid reads the diagram source.
        return _MERMAID_UNWRAP.sub(r'<pre class="mermaid"\g<attrs>>\g<src></pre>', body)
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
        self._level = 0
        self._last_shown = DEFAULT_LEVEL
        self._level_action = None
        self._button_label = None
        self._place_tries = 0
        self._view = None
        self._vadj = None
        self._vadj_handler = 0
        self._sync_lock = False
        self._lock_owner = None
        self._pending_line = None
        self._edit_mode = False
        self._edit_pos = None
        self._edit_text = None
        self._applying_edit = False

    def do_activate(self):
        self._scrolled = Gtk.ScrolledWindow()
        # A user content manager is needed for the page to post its scroll
        # position back; without it the reading position is lost on every
        # re-render, which the debounced live update makes constant.
        ucm = WebKit2.UserContentManager()
        ucm.register_script_message_handler("mdscroll")
        ucm.connect("script-message-received::mdscroll", self._on_scroll_message)
        ucm.register_script_message_handler("mdedit")
        ucm.connect("script-message-received::mdedit", self._on_edit_message)
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

        # Stateful so the popover marks the share in effect and the label can
        # follow it from a single source of truth.
        self._level_action = Gio.SimpleAction.new_stateful(
            "markdown-preview-level",
            GLib.VariantType.new("i"),
            GLib.Variant.new_int32(self._level),
        )
        self._level_action.connect("change-state", self._on_level_change)
        self.window.add_action(self._level_action)

        edit = Gio.SimpleAction(name="markdown-preview-edit")
        edit.connect("activate", self._toggle_edit_mode)
        self.window.add_action(edit)

        for name, delta in (("zoom-in", ZOOM_STEP), ("zoom-out", -ZOOM_STEP),
                            ("zoom-reset", 0.0)):
            action = Gio.SimpleAction(name="markdown-preview-" + name)
            action.connect("activate", self._on_zoom, delta)
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
        self.window.remove_action("markdown-preview-export")
        self.window.remove_action("markdown-preview-level")
        self.window.remove_action("markdown-preview-edit")
        for name in ("zoom-in", "zoom-out", "zoom-reset"):
            self.window.remove_action("markdown-preview-" + name)

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
        # A menu button rather than a toggle: the five shares are listed in the
        # popover and the label always shows the one in effect, so the control
        # says how much of the window the preview holds, not merely whether it
        # is on.
        btn = Gtk.MenuButton()
        self._button_label = Gtk.Label(label="%d%%" % self._level)
        btn.add(self._button_label)
        menu = Gio.Menu()
        for level in LEVELS:
            item = Gio.MenuItem.new("%d%%" % level, None)
            item.set_action_and_target_value(
                "win.markdown-preview-level", GLib.Variant.new_int32(level)
            )
            menu.append_item(item)
        btn.set_menu_model(menu)
        btn.set_tooltip_text("How much of the window the preview holds (Ctrl+M toggles)")
        btn.show_all()
        headerbar.pack_end(btn)
        self._button = btn
        self._sync_button()

    def _sync_button(self, *_args):
        if self._button_label is not None:
            self._button_label.set_text("%d%%" % self._level)
        if self._level_action is not None:
            self._syncing = True
            self._level_action.set_state(GLib.Variant.new_int32(self._level))
            self._syncing = False

    # --- share of the window held by the preview ----------------------------
    def _paned(self):
        # The documents area and the bottom panel are the two children of a
        # vertical GtkPaned, and its position is what divides the window between
        # them. child1 is the documents area.
        node = self._scrolled.get_parent()
        while node is not None:
            if isinstance(node, Gtk.Paned):
                return node
            node = node.get_parent()
        return None

    def _doc_area(self):
        paned = self._paned()
        return paned.get_child1() if paned is not None else None

    def _is_visible(self):
        return self._level > 0

    def _place_paned(self, level):
        # The position is asserted on a few ticks rather than once: showing the
        # panel makes gedit restore its own saved height, and that restore lands
        # after a single early application would have run. Re-applying for a
        # short while leaves the requested share as the one that survives, and
        # the bounded count keeps the handle free right after.
        paned = self._paned()
        if paned is None:
            return
        self._place_tries = 0

        def apply_position():
            total = paned.get_allocated_height()
            if total > 1:
                paned.set_position(int(total * (100 - level) / 100.0))
            self._place_tries += 1
            return self._place_tries < 6

        GLib.timeout_add(100, apply_position)

    def _set_level(self, level):
        level = min(LEVELS, key=lambda step: abs(step - int(level)))
        self._busy = True
        self._level = level
        if level > 0:
            self._last_shown = level
        panel = self.window.get_bottom_panel()
        docs = self._doc_area()
        if level == 0:
            if docs is not None:
                docs.show()
            panel.set_visible(False)
        else:
            self._update()
            panel.props.visible_child = self._scrolled
            panel.set_visible(True)
            if level >= 100:
                # Hide the editor outright: a paned position of zero would leave
                # a sliver of editor and a draggable handle in the way.
                if docs is not None:
                    docs.hide()
            else:
                if docs is not None:
                    docs.show()
                self._place_paned(level)
        self._active = level > 0
        self._busy = False
        self._sync_button()

    def _on_level_change(self, action, value):
        action.set_state(value)
        if self._syncing:
            return
        self._set_level(value.get_int32())

    def _on_panel_notify(self, *_args):
        # Keep the level honest when the panel is closed or switched away by
        # other means, so the button never claims a share the preview lost.
        if self._busy:
            return
        panel = self.window.get_bottom_panel()
        away = not panel.props.visible or panel.props.visible_child is not self._scrolled
        if self._level > 0 and away:
            docs = self._doc_area()
            if docs is not None:
                docs.show()
            self._level = 0
            self._active = False
        elif self._level == 0 and not away:
            # Opened by other means (its own tab, the View menu): adopt it at the
            # share last in use.
            self._level = self._last_shown
            self._active = True
            self._update()
            if self._level < 100:
                self._place_paned(self._level)
        self._sync_button()

    def _toggle(self, *_args):
        # Ctrl+M and the menu entry stay a quick show/hide, returning to the
        # share last in use rather than to a fixed one.
        self._set_level(0 if self._level > 0 else self._last_shown)

    # --- editing a block in the rendered page --------------------------------
    def _pos_range(self, pos):
        # pandoc writes "start:col-end:col" with the end exclusive, so a block on
        # a single line reads 1:1-2:1. Returned zero based, end exclusive.
        head, _, tail = pos.partition("-")
        start = int(head.split(":")[0]) - 1
        end = int(tail.split(":")[0]) - 1
        return max(0, start), max(start + 1, end)

    def _range_iters(self, start, end):
        buf = self._doc
        first = buf.get_iter_at_line(min(start, buf.get_line_count() - 1))
        if end >= buf.get_line_count():
            last = buf.get_end_iter()
        else:
            last = buf.get_iter_at_line(end)
        return first, last

    def _range_text(self, start, end):
        first, last = self._range_iters(start, end)
        return self._doc.get_text(first, last, False)

    def _toggle_edit_mode(self, *_args):
        self._edit_mode = not self._edit_mode
        if not self._edit_mode:
            self._edit_pos = None
            self._edit_text = None
        self._push_edit_mode()

    def _push_edit_mode(self):
        self._webview.run_javascript(
            "if(window.__mdSetEdit)window.__mdSetEdit(%s);"
            % ("true" if self._edit_mode else "false"),
            None, None, None,
        )

    def _cancel_open_block(self):
        self._edit_pos = None
        self._edit_text = None
        self._webview.run_javascript(
            "if(window.__mdCancelEdit)window.__mdCancelEdit();", None, None, None
        )

    def _on_edit_message(self, _ucm, result):
        try:
            report = json.loads(result.get_js_value().to_string())
        except Exception:  # noqa: BLE001 - a malformed message must not break the panel
            return
        kind = report.get("kind")
        if kind == "cancel":
            self._edit_pos = None
            self._edit_text = None
        elif kind == "request":
            self._open_block(report.get("pos", ""))
        elif kind == "commit":
            self._commit_block(report.get("pos", ""), report.get("text", ""))

    def _open_block(self, pos):
        if self._doc is None or not pos:
            return
        try:
            start, end = self._pos_range(pos)
        except ValueError:
            return
        text = self._range_text(start, end)
        self._edit_pos = pos
        self._edit_text = text
        self._webview.run_javascript(
            "if(window.__mdOpenBlock)window.__mdOpenBlock(%s,%s);"
            % (json.dumps(pos), json.dumps(text)),
            None, None, None,
        )

    def _commit_block(self, pos, text):
        if self._doc is None or pos != self._edit_pos:
            return
        start, end = self._pos_range(pos)
        # The range is only writable while it still holds exactly what was handed
        # to the page. Anything else means the buffer moved underneath, and
        # writing a stale range is how a document gets corrupted.
        if self._range_text(start, end) != self._edit_text:
            self._cancel_open_block()
            self._update()
            return
        if self._edit_text.endswith("\n") and not text.endswith("\n"):
            text += "\n"
        buf = self._doc
        first, last = self._range_iters(start, end)
        self._applying_edit = True
        buf.begin_user_action()
        buf.delete(first, last)
        buf.insert(first, text)
        buf.end_user_action()
        self._applying_edit = False
        self._edit_pos = None
        self._edit_text = None
        self._update()

    # --- zoom of the rendered page ------------------------------------------
    def _on_zoom(self, _action, _param, delta):
        if delta:
            level = min(ZOOM_MAX, max(ZOOM_MIN, self._webview.get_zoom_level() + delta))
        else:
            level = 1.0
        self._webview.set_zoom_level(level)
        # Zooming relays out the page, which moves every anchor the scroll sync
        # interpolates between, so the map is rebuilt once the new layout settled.
        GLib.timeout_add(RESCROLL_MS, self._rebuild_anchors)

    def _rebuild_anchors(self):
        self._webview.run_javascript(
            "if(window.__mdRebuild)window.__mdRebuild();", None, None, None
        )
        return False

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
            report = json.loads(value)
            self._scroll_y = max(0, int(report.get("y", 0)))
        except (TypeError, ValueError, AttributeError):
            return
        if (not self._sync_lock and self._level > 0 and "line" in report
                and self._edit_pos is None):
            self._scroll_editor_to_line(report["line"])

    def _restore_scroll(self):
        if self._scroll_y <= 0:
            return False
        self._webview.run_javascript(
            "(window.__mdQuietScroll||function(y){window.scrollTo(0,y);})(%d);"
            % self._scroll_y, None, None, None
        )
        return False

    def _on_load_changed(self, _webview, event):
        if event != WebKit2.LoadEvent.FINISHED:
            return
        if self._edit_mode:
            self._push_edit_mode()
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
        # The view is a scrollable text view, so its vertical adjustment is where
        # the editor's own scrolling shows up.
        view = self.window.get_active_view()
        adj = view.get_vadjustment() if view is not None else None
        if adj is not None:
            self._view = view
            self._vadj = adj
            self._vadj_handler = adj.connect("value-changed", self._on_editor_scroll)

    def _disconnect_doc(self):
        if self._doc is not None and self._doc_handler:
            self._doc.disconnect(self._doc_handler)
        self._doc = None
        self._doc_handler = 0
        if self._vadj is not None and self._vadj_handler:
            self._vadj.disconnect(self._vadj_handler)
        self._vadj = None
        self._vadj_handler = 0
        self._view = None

    # --- scroll sync between editor and preview -----------------------------
    def _hold_sync(self, owner):
        # The owner records which side started the move, so the other side can
        # tell a genuine scroll from the echo of its own.
        self._sync_lock = True
        self._lock_owner = owner
        GLib.timeout_add(SYNC_UNLOCK_MS, self._release_sync)

    def _release_sync(self):
        self._sync_lock = False
        owner, self._lock_owner = self._lock_owner, None
        pending, self._pending_line = self._pending_line, None
        # Scrolling produces a burst of events; the ones that arrive while the
        # lock is held would otherwise be dropped and leave the preview at the
        # position the burst started from instead of where it ended.
        if owner == "editor" and pending is not None and self._level > 0:
            self._push_line_to_preview(pending)
        return False

    def _push_line_to_preview(self, line):
        self._hold_sync("editor")
        self._webview.run_javascript(
            "if(window.__mdGoToLine)window.__mdGoToLine(%d);" % line, None, None, None
        )

    def _on_editor_scroll(self, adj):
        if self._level <= 0 or self._view is None or self._edit_pos is not None:
            return
        if self._sync_lock and self._lock_owner == "preview":
            return
        found = self._view.get_line_at_y(int(adj.get_value()))
        if not found:
            return
        line = found[0].get_line()
        if self._sync_lock:
            self._pending_line = line
            return
        self._push_line_to_preview(line)

    def _scroll_editor_to_line(self, line):
        if self._view is None or self._vadj is None:
            return
        buf = self._view.get_buffer()
        line = max(0, min(int(line), buf.get_line_count() - 1))
        y = self._view.get_line_yrange(buf.get_iter_at_line(line))[0]
        self._hold_sync("preview")
        self._vadj.set_value(y)

    def _on_changed(self, *_args):
        if self._applying_edit:
            return
        if self._edit_pos is not None:
            # The buffer moved under the open block, so its line range can no
            # longer be trusted and the edit is dropped rather than misapplied.
            self._cancel_open_block()
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
        return HTML_TEMPLATE.format(
            css=CSS, body=body, base=BASE_SCRIPT, edit=EDIT_SCRIPT, script=script
        )

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
        self.app.set_accels_for_action("win.markdown-preview-edit", ["<Primary>e"])
        # Plus needs shift on most layouts, and the shift stays in the modifier
        # state that GTK matches against, so the shifted spellings are bound too;
        # equal covers pressing the same key without shift, and the keypad has
        # its own symbol.
        self.app.set_accels_for_action(
            "win.markdown-preview-zoom-in",
            [
                "<Primary>plus",
                "<Primary>equal",
                "<Primary>KP_Add",
                "<Primary><Shift>plus",
                "<Primary><Shift>equal",
            ],
        )
        self.app.set_accels_for_action(
            "win.markdown-preview-zoom-out",
            ["<Primary>minus", "<Primary>KP_Subtract"],
        )
        self.app.set_accels_for_action(
            "win.markdown-preview-zoom-reset", ["<Primary>0", "<Primary>KP_0"]
        )
        # "tools-section-1" is a valid extension point in gedit 46 (used by the
        # bundled External Tools plugin); "view-menu" is not and returns None.
        self._menu_ext = self.extend_menu("tools-section-1")
        if self._menu_ext is not None:
            item = Gio.MenuItem.new("Markdown Preview", "win.markdown-preview")
            self._menu_ext.append_menu_item(item)
            edit = Gio.MenuItem.new(
                "Edit in the render (Ctrl+E)", "win.markdown-preview-edit"
            )
            self._menu_ext.append_menu_item(edit)
            export = Gio.MenuItem.new(
                "Export Markdown preview (PDF)", "win.markdown-preview-export"
            )
            self._menu_ext.append_menu_item(export)

    def do_deactivate(self):
        self.app.set_accels_for_action("win.markdown-preview", [])
        self.app.set_accels_for_action("win.markdown-preview-edit", [])
        for name in ("zoom-in", "zoom-out", "zoom-reset"):
            self.app.set_accels_for_action("win.markdown-preview-" + name, [])
        self._menu_ext = None
