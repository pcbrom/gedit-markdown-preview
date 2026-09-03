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
# highlighted offline by pandoc, and long documents get a navigable outline.
# The bar exports the document to PDF and to DOCX in an academic layout, typeset
# by pandoc rather than printed from the screen, beside the .md file.
import gi
gi.require_version("Gedit", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GObject, Gedit, Gtk, Gio, GLib, WebKit2, GdkPixbuf

# The side panel object implements both Gtk.Container and Tepl.Panel, and a plain
# obj.add() resolves to the container method, which takes only the widget. The
# interface methods have to be called explicitly, so Tepl has to be imported.
try:
    gi.require_version("Tepl", "6")
    from gi.repository import Tepl
except (ValueError, ImportError):  # side by side is simply unavailable
    Tepl = None
import subprocess
import json
import traceback
import os
import re
import io
import shutil
import tempfile
import threading
import zipfile
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
# Delay before a preview left open is opened again, giving gedit time to restore
# the documents of the previous session.
RESTORE_OPEN_MS = 700
ZOOM_MIN = 0.5
ZOOM_MAX = 3.0
ZOOM_STEP = 0.1
# Raster size of the header-bar icon. Larger than the nominal 16 so the
# stroke stays crisp where the desktop scales text up.
ICON_PX = 20
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


# --- export ----------------------------------------------------------------
# Export does not print the rendered page: it runs the Markdown through pandoc
# again, to LaTeX or to Word, so the result is typeset rather than a screenshot
# of a browser. The look is the one journals expect: A4, 2.5 cm margins, an
# 11 pt serif, numbered sections, no first-line indent and space between
# paragraphs instead.
EXPORT_TIMEOUT = 240
# Extensions beyond the preview's: metadata for title and author, footnotes and
# definition lists, since an academic document uses all of them.
EXPORT_FROM = ("gfm+tex_math_dollars+yaml_metadata_block+footnotes"
               "+definition_lists+pipe_tables+task_lists")
EXPORT_COMMON = ["--standalone", "--number-sections", "--from=" + EXPORT_FROM]
# pandoc derives table column widths from the source only when the separator row
# is itself wide, so the idiomatic "| --- |" table arrives with no widths and
# LaTeX lays it out in unbreakable l/r columns that run off the page. This
# filter gives every such table widths taken from its own content, which is what
# lets a long cell wrap. It ships as source because a Lua filter is a file path
# on the command line, so it is written out beside each export.
TABLE_WIDTH_LUA = """
local stringify = pandoc.utils.stringify

local function widest(rows, index)
  local most = 0
  for _, row in ipairs(rows) do
    local cell = row.cells[index]
    if cell then
      local n = utf8.len(stringify(cell.contents) or "") or 0
      if n > most then most = n end
    end
  end
  return most
end

function Table(tbl)
  local count = #tbl.colspecs
  if count == 0 then return nil end
  local total = 0
  for _, spec in ipairs(tbl.colspecs) do
    total = total + (spec[2] or 0)
  end
  -- A table that already carries widths keeps exactly what the author set.
  if total > 0 then return nil end

  local rows = {}
  for _, head in ipairs(tbl.head.rows) do rows[#rows + 1] = head end
  for _, body in ipairs(tbl.bodies) do
    for _, row in ipairs(body.body) do rows[#rows + 1] = row end
  end

  -- Width follows the longest cell in the column, so a column of numbers stays
  -- narrow while a column of prose gets the room it needs. The floor keeps a
  -- short heading from collapsing into an unreadable ribbon.
  local floor = 0.06
  local measures, sum = {}, 0
  for i = 1, count do
    measures[i] = math.max(widest(rows, i), 1)
    sum = sum + measures[i]
  end
  local widths, spare = {}, 0
  for i = 1, count do
    local w = measures[i] / sum
    if w < floor then
      spare = spare + (floor - w)
      w = floor
    end
    widths[i] = w
  end
  -- Raising the narrow columns has to come out of the wide ones, or the row
  -- would add up to more than the page.
  local over = 0
  for i = 1, count do
    if widths[i] > floor then over = over + widths[i] end
  end
  for i = 1, count do
    if widths[i] > floor and over > 0 then
      widths[i] = widths[i] - spare * (widths[i] / over)
    end
    -- 0.97 rather than 1.0: the column separators need somewhere to live.
    tbl.colspecs[i] = { tbl.colspecs[i][1], widths[i] * 0.97 }
  end
  return tbl
end
"""
PDF_VARS = [
    "--pdf-engine=xelatex", "--highlight-style=tango",
    "-V", "papersize=a4", "-V", "geometry:margin=2.5cm", "-V", "fontsize=11pt",
    "-V", "colorlinks=true", "-V", "linkcolor=black",
    "-V", "urlcolor=[HTML]{1A4F8A}", "-V", "citecolor=[HTML]{1A4F8A}",
]
# A Times-alike is the common journal face. The fonts are a separate list so a
# machine without them can be retried with the LaTeX default instead of failing.
PDF_FONTS = ["-V", "mainfont=TeX Gyre Termes", "-V", "mathfont=TeX Gyre Termes Math"]
# Bibliography, when the author keeps one beside the document.
BIB_NAMES = ("references.bib", "referencias.bib", "bibliography.bib")
# Diagrams are rasterised at this multiple of their natural size, so they stay
# sharp on paper rather than at screen resolution.
DIAGRAM_SCALE = 3
DIAGRAM_WAIT_MS = 1500

# A plugin launched from the desktop has no visible stderr, so a failure inside a
# callback would vanish. Recording it gives something to read after the fact.
ERROR_LOG = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "gedit-mdpreview.log",
)


# The share and the split survive between sessions, so the preview opens the way
# it was left rather than at a fixed default. A small file rather than GSettings,
# which would need a schema compiled and installed alongside the plugin.
PREFS_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "gedit-mdpreview.json",
)


def load_prefs():
    try:
        with open(PREFS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # No preferences yet, or a file someone hand edited into nonsense. The
        # defaults are perfectly usable, so this is not worth reporting.
        return {}


def save_prefs(data):
    try:
        os.makedirs(os.path.dirname(PREFS_FILE), exist_ok=True)
        tmp = PREFS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        # Replacing in one step, so a crash mid-write cannot leave a half file
        # that the next session would read as corrupt.
        os.replace(tmp, PREFS_FILE)
    except OSError:
        log_error(traceback.format_exc())


def log_error(what):
    try:
        os.makedirs(os.path.dirname(ERROR_LOG), exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write("--- %s\n%s\n" % (what, traceback.format_exc()))
    except Exception:  # noqa: BLE001 - logging must never raise
        pass


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
  margin: 0 auto; max-width: 46em; padding: 2.5em 3.5em 6em 2em;
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
/* The bar reserves its width on the body instead of floating over the text, so
   it cannot cover the column at any window size. */
/* Only the frame marks edit mode now: the bar itself is always on screen, and
   its width is reserved on the body at all times so the text never runs under
   it, in either mode. */
body.mdedit { box-shadow: inset 0 0 0 2px rgba(42,118,198,.35); }
#mdbar {
  position: fixed; right: .6em; top: 50%; transform: translateY(-50%);
  display: flex; flex-direction: column; gap: .25em; width: 2.4em; z-index: 10;
}
#mdbar button {
  position: relative; width: 2.4em; height: 2.4em; padding: 0; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  border: 1px solid #c3c3c3; border-radius: 6px; background: #fafafa; color: #4a4a4a;
}
#mdbar button svg { width: 1.15em; height: 1.15em; }
#mdbar button:hover { border-color: #2a76c6; color: #2a76c6; }
#mdbar .mdbar-sep { height: 1px; background: #c3c3c3; margin: .3em .2em; }
#mdbar.noblock button.needs-block { opacity: .35; cursor: default; }
#mdbar.noblock button.needs-block:hover { border-color: #c3c3c3; color: #4a4a4a; }
/* The tooltip is the label, since the icon carries no words. It opens to the
   left because the bar sits against the right edge, where a native tooltip
   would be clipped, and it appears at once instead of after the usual delay. */
#mdbar .tip {
  position: absolute; right: calc(100% + .5em); top: 50%;
  transform: translateY(-50%); width: max-content; max-width: 19em;
  background: #2e3436; color: #f5f5f5; padding: .4em .6em; border-radius: 5px;
  font-size: .78em; line-height: 1.35; text-align: left; opacity: 0;
  pointer-events: none; transition: opacity .1s; z-index: 20;
}
#mdbar button:hover .tip { opacity: 1; }
#mdbar.noblock button.needs-block:hover .tip { opacity: 1; }
@media (prefers-color-scheme: dark) {
  body.mdedit { box-shadow: inset 0 0 0 2px rgba(140,180,255,.35); }
  #mdbar button { background: #2b2b2b; color: #b8b8b8; border-color: #555; }
  #mdbar button:hover { border-color: #8cb4ff; color: #8cb4ff; }
  #mdbar.noblock button.needs-block:hover { border-color: #555; color: #b8b8b8; }
  #mdbar .mdbar-sep { background: #555; }
  #mdbar .tip { background: #101010; color: #e6e6e6; }
}
@media print { #mdbar { display: none !important; } }

/* Export runs outside the page, so its outcome has to come back somewhere the
   reader is already looking. The notice sits clear of the bar and says which
   file was written, since the export never opens a dialog to say so. */
#mdtoast {
  position: fixed; right: 4em; bottom: 1.2em; max-width: 26em; z-index: 30;
  background: #2e3436; color: #f5f5f5; padding: .6em .8em; border-radius: 6px;
  font-size: .8em; line-height: 1.4; cursor: pointer; word-break: break-word;
  box-shadow: 0 2px 10px rgba(0,0,0,.25); transition: opacity .2s;
}
#mdtoast.err { background: #8a2020; }
#mdtoast.busy { background: #1a4f8a; }
@media print { #mdtoast { display: none !important; } }

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
# Lucide icons (ISC), taken from lucide-react 0.454.0 and inlined so the bar
# needs no network and no second file. Ten shapes cost under a kilobyte.
ICONS = {
    "bold": "<path d=\"M6 12h9a4 4 0 0 1 0 8H7a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h7a4 4 0 0 1 0 8\"/>",
    "italic": "<line x1=\"19\" x2=\"10\" y1=\"4\" y2=\"4\"/><line x1=\"14\" x2=\"5\" y1=\"20\" y2=\"20\"/><line x1=\"15\" x2=\"9\" y1=\"4\" y2=\"20\"/>",
    "heading": "<path d=\"M6 12h12\"/><path d=\"M6 20V4\"/><path d=\"M18 20V4\"/>",
    "list": "<path d=\"M3 12h.01\"/><path d=\"M3 18h.01\"/><path d=\"M3 6h.01\"/><path d=\"M8 12h13\"/><path d=\"M8 18h13\"/><path d=\"M8 6h13\"/>",
    "code": "<polyline points=\"16 18 22 12 16 6\"/><polyline points=\"8 6 2 12 8 18\"/>",
    "link": "<path d=\"M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71\"/><path d=\"M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71\"/>",
    "table": "<path d=\"M12 3v18\"/><rect width=\"18\" height=\"18\" x=\"3\" y=\"3\" rx=\"2\"/><path d=\"M3 9h18\"/><path d=\"M3 15h18\"/>",
    "check": "<path d=\"M20 6 9 17l-5-5\"/>",
    "x": "<path d=\"M18 6 6 18\"/><path d=\"m6 6 12 12\"/>",
    "pencil": "<path d=\"M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z\"/><path d=\"m15 5 4 4\"/>",
    "pencil-off": "<path d=\"m10 10-6.157 6.162a2 2 0 0 0-.5.833l-1.322 4.36a.5.5 0 0 0 .622.624l4.358-1.323a2 2 0 0 0 .83-.5L14 13.982\"/><path d=\"m12.829 7.172 4.359-4.346a1 1 0 1 1 3.986 3.986l-4.353 4.353\"/><path d=\"m15 5 4 4\"/><path d=\"m2 2 20 20\"/>",
    # The two export icons share the sheet outline and differ in what fills it:
    # an arrow leaving the page for PDF, a letter for the word processor.
    "file-down": "<path d=\"M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z\"/><path d=\"M14 2v4a2 2 0 0 0 2 2h4\"/><path d=\"M12 18v-6\"/><path d=\"m9 15 3 3 3-3\"/>",
    "file-type": "<path d=\"M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z\"/><path d=\"M14 2v4a2 2 0 0 0 2 2h4\"/><path d=\"M9 13v-1h6v1\"/><path d=\"M12 12v6\"/><path d=\"M11 18h2\"/>"
}
# Shapes for the header-bar button that swaps the split. They are rasterised
# into a pixbuf, since a header-bar button takes a widget and not markup.
SPLIT_ICONS = {
    "rows-2": "<rect width=\"18\" height=\"18\" x=\"3\" y=\"3\" rx=\"2\"/><path d=\"M3 12h18\"/>",
    "columns-2": "<rect width=\"18\" height=\"18\" x=\"3\" y=\"3\" rx=\"2\"/><path d=\"M12 3v18\"/>"
}
SPLIT_TIPS = {
    False: "Split: stacked, preview below. Click for side by side.",
    True: "Split: side by side, preview at the right. Click for stacked.",
}
SVG_OPEN = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
)
# The icon carries no words, so the tooltip is the label: it says what the
# action does, not merely what it is called.
TIPS = {
    "bold": "Bold: wraps the selection in **. Apply again to undo.",
    "italic": "Italic: wraps the selection in *. Apply again to undo.",
    "heading": "Heading: prefixes the line with ##. Apply again to remove.",
    "list": "List: prefixes the lines with -. Apply again to remove.",
    "code": "Code: backticks around the selection, or a fenced block if it spans lines.",
    "link": "Link: becomes [selection](url), with the destination already selected.",
    "table": "Table: inserts a header, a separator and one row.",
    "check": "Confirm (Ctrl+Enter): writes the block back to its source lines.",
    "x": "Cancel (Esc): discards the edit.",
    "pencil": "Edit in the render: turns edit mode on (Ctrl+E).",
    "pencil-off": "Leave edit mode (Ctrl+E).",
    "file-down": "Export PDF in an academic layout, beside the .md file.",
    "file-type": "Export DOCX in an academic layout, beside the .md file."
}

EDIT_SCRIPT = (
    "<script>"
    "(function(){"
    " var ICONS=" + json.dumps(ICONS) + ";"
    " var TIPS=" + json.dumps(TIPS) + ";"
    " var SVG=" + json.dumps(SVG_OPEN) + ";"
    " var open=null,on=false,bar=null;"
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
    " var markOpen=function(flag){"
    "  if(bar){bar.className=flag?'':'noblock';}"
    " };"
    " var restore=function(){"
    "  if(open&&open.box&&open.box.parentNode){"
    "   open.box.parentNode.replaceChild(open.el,open.box);"
    "  }"
    "  open=null;"
    "  markOpen(false);"
    " };"
    " var commit=function(){"
    "  if(!open){return;}"
    "  var payload={kind:'commit',pos:open.pos,text:open.box.value};"
    "  restore();"
    "  post(payload);"
    " };"
    " var setv=function(v,a,b){"
    "  var box=open.box;box.value=v;box.focus();box.setSelectionRange(a,b);"
    " };"
    " var wrap=function(mark){"
    "  if(!open){return;}"
    "  var box=open.box,a=box.selectionStart,b=box.selectionEnd,v=box.value;"
    "  var m=mark.length,sel=v.slice(a,b),pre=v.slice(0,a),post2=v.slice(b);"
    "  if(pre.slice(-m)===mark&&post2.slice(0,m)===mark){"
    "   setv(pre.slice(0,-m)+sel+post2.slice(m),a-m,b-m);"
    "  }else if(sel.length>=2*m&&sel.slice(0,m)===mark&&sel.slice(-m)===mark){"
    "   setv(pre+sel.slice(m,-m)+post2,a,b-2*m);"
    "  }else{"
    "   setv(pre+mark+sel+mark+post2,a+m,b+m);"
    "  }"
    " };"
    # Line prefixes act on every line the selection touches, and applying the
    # same prefix again removes it, so a mistaken click is undone by repeating it.
    " var linepre=function(re,pre){"
    "  if(!open){return;}"
    "  var box=open.box,v=box.value;"
    "  var a=v.lastIndexOf('\\n',box.selectionStart-1)+1;"
    "  var e=v.indexOf('\\n',box.selectionEnd);"
    "  if(e<0){e=v.length;}"
    "  var body=v.slice(a,e).split('\\n');"
    "  var off=body.every(function(l){return re.test(l);});"
    "  body=body.map(function(l){return off?l.replace(re,''):pre+l;});"
    "  var out=body.join('\\n');"
    "  setv(v.slice(0,a)+out+v.slice(e),a,a+out.length);"
    " };"
    " var insert=function(text,back){"
    "  if(!open){return;}"
    "  var box=open.box,a=box.selectionStart,b=box.selectionEnd,v=box.value;"
    "  var out=v.slice(0,a)+text+v.slice(b);"
    "  var c=a+text.length-(back||0);"
    "  setv(out,c,c);"
    " };"
    " var link=function(){"
    "  if(!open){return;}"
    "  var box=open.box,a=box.selectionStart,b=box.selectionEnd,v=box.value;"
    "  var sel=v.slice(a,b)||'text';"
    "  var out=v.slice(0,a)+'['+sel+'](url)'+v.slice(b);"
    "  var u=a+sel.length+3;"
    "  setv(out,u,u+3);"
    " };"
    " var code=function(){"
    "  if(!open){return;}"
    "  var box=open.box,a=box.selectionStart,b=box.selectionEnd,v=box.value;"
    "  var sel=v.slice(a,b);"
    "  if(sel.indexOf('\\n')<0&&sel!==''){wrap('`');return;}"
    "  insert('```\\n'+sel+'\\n```\\n',sel.length+5);"
    " };"
    # Preventing the default on mousedown keeps the focus in the open block: a
    # blur would commit it, so a formatting click would close what it was meant
    # to format.
    " var mkbtn=function(id,cls,fn){"
    "  var b=document.createElement('button');"
    "  if(cls){b.className=cls;}"
    "  b.setAttribute('aria-label',TIPS[id]);"
    "  b.innerHTML=SVG+ICONS[id]+'</svg>';"
    "  var tip=document.createElement('span');"
    "  tip.className='tip';"
    "  tip.textContent=TIPS[id];"
    "  b.appendChild(tip);"
    "  b.addEventListener('mousedown',function(ev){ev.preventDefault();});"
    "  b.addEventListener('click',function(ev){ev.preventDefault();fn();});"
    "  return b;"
    " };"
    " var toggleBtn=null;"
    " var setToggle=function(flag){"
    "  if(!toggleBtn){return;}"
    "  var id=flag?'pencil-off':'pencil';"
    "  toggleBtn.innerHTML=SVG+ICONS[id]+'</svg>';"
    "  var t=document.createElement('span');"
    "  t.className='tip';"
    "  t.textContent=TIPS[id];"
    "  toggleBtn.appendChild(t);"
    "  toggleBtn.setAttribute('aria-label',TIPS[id]);"
    " };"
    " var buildBar=function(){"
    "  bar=document.createElement('div');"
    "  bar.id='mdbar';"
    "  bar.className='noblock';"
    "  var items=["
    "   ['bold','needs-block',function(){wrap('**');}],"
    "   ['italic','needs-block',function(){wrap('*');}],"
    "   ['heading','needs-block',function(){linepre(/^#{1,6} /,'## ');}],"
    "   ['list','needs-block',function(){linepre(/^[-*] /,'- ');}],"
    "   ['code','needs-block',code],"
    "   ['link','needs-block',link],"
    "   ['table','needs-block',function(){"
    "    insert('| a | b |\\n| --- | --- |\\n| 1 | 2 |\\n',0);"
    "   }]"
    "  ];"
    "  for(var i=0;i<items.length;i++){"
    "   bar.appendChild(mkbtn(items[i][0],items[i][1],items[i][2]));"
    "  }"
    "  var sep=document.createElement('div');"
    "  sep.className='mdbar-sep';"
    "  bar.appendChild(sep);"
    "  bar.appendChild(mkbtn('check','needs-block',function(){commit();}));"
    "  bar.appendChild(mkbtn('x','needs-block',function(){"
    "   restore();post({kind:'cancel'});"
    "  }));"
    "  toggleBtn=mkbtn('pencil','',function(){post({kind:'toggle'});});"
    "  bar.appendChild(toggleBtn);"
    # Export is not an edit, so its buttons stay live whether or not a block is
    # open, below a rule that separates them from the editing controls.
    "  var sep2=document.createElement('div');"
    "  sep2.className='mdbar-sep';"
    "  bar.appendChild(sep2);"
    "  bar.appendChild(mkbtn('file-down','',function(){"
    "   post({kind:'export',fmt:'pdf'});"
    "  }));"
    "  bar.appendChild(mkbtn('file-type','',function(){"
    "   post({kind:'export',fmt:'docx'});"
    "  }));"
    "  document.body.appendChild(bar);"
    " };"
    # The notice survives a re-render only as long as the page does, which is
    # what we want: it reports one export and then gets out of the way.
    " var toastTimer=0;"
    " window.__mdToast=function(text,state){"
    "  var el=document.getElementById('mdtoast');"
    "  if(!el){"
    "   el=document.createElement('div');"
    "   el.id='mdtoast';"
    "   el.addEventListener('click',function(){el.remove();});"
    "   document.body.appendChild(el);"
    "  }"
    "  el.textContent=text;"
    "  el.className=state||'';"
    "  if(toastTimer){clearTimeout(toastTimer);toastTimer=0;}"
    "  if(state!=='busy'){"
    "   toastTimer=setTimeout(function(){if(el){el.remove();}},"
    "state==='err'?14000:7000);"
    "  }"
    " };"
    " window.__mdSetEdit=function(flag){"
    "  on=!!flag;"
    "  if(!on){restore();}"
    "  document.body.classList.toggle('mdedit',on);"
    "  setToggle(on);"
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
    "  markOpen(true);"
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
    "  if(bar&&bar.contains(ev.target)){return;}"
    "  var el=blockAt(ev.target);"
    "  if(!el){return;}"
    "  ev.preventDefault();"
    "  post({kind:'request',pos:el.getAttribute('data-pos')});"
    " },true);"
    " buildBar();"
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


# --- export helpers ---------------------------------------------------------
# A ```mermaid fence in the source, so it can be swapped for the rendered image
# before pandoc ever sees it. LaTeX and Word have no diagram engine of their own.
_MMD_FENCE = re.compile(r"^[ \t]*```+[ \t]*mermaid[ \t]*\r?\n(.*?)\r?\n[ \t]*```+[ \t]*$",
                        re.M | re.S)
SERIF = "Times New Roman"
_DOCX_FONTS = ('<w:rFonts w:ascii="%s" w:eastAsia="%s" w:hAnsi="%s" w:cs="%s" />'
               % (SERIF, SERIF, SERIF, SERIF))
_DOCX_THEME_FONTS = re.compile(
    r'<w:rFonts w:(?:ascii|eastAsia|hAnsi|cs)Theme="[^"]*"[^/]*/>')
# The stock reference paints headings and the abstract title blue, sometimes as
# a theme colour and sometimes as a literal one. Academic output is black, so
# every colour goes and the link colour is put back afterwards.
_DOCX_COLOR = re.compile(r'<w:color w:val="[0-9A-Fa-f]{6}"[^/]*/>')
_DOCX_STYLE = "<w:style [^>]*w:styleId=\"%s\">.*?</w:style>"
# A4 with 2.5 cm margins, in twentieths of a point, matching the PDF geometry.
_DOCX_SECTPR = (
    "<w:sectPr>"
    '<w:pgSz w:w="11906" w:h="16838" />'
    '<w:pgMar w:top="1418" w:right="1418" w:bottom="1418" w:left="1418"'
    ' w:header="709" w:footer="709" w:gutter="0" />'
    "</w:sectPr>"
)
# pandoc ships no SourceCode paragraph style, so a code block inherits Normal
# and comes out justified, which spreads a single line of code across the page.
_DOCX_SOURCECODE = (
    '<w:style w:type="paragraph" w:customStyle="1" w:styleId="SourceCode">'
    '<w:name w:val="Source Code" /><w:basedOn w:val="Normal" />'
    '<w:link w:val="VerbatimChar" />'
    '<w:pPr><w:jc w:val="left" />'
    '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto" />'
    '<w:shd w:val="clear" w:color="auto" w:fill="F6F6F6" /></w:pPr>'
    '<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas" />'
    '<w:sz w:val="20" /><w:szCs w:val="20" /></w:rPr></w:style>'
)
_DOCX_HEADING_SIZES = {"Title": 32, "Heading1": 28, "Heading2": 24,
                       "Heading3": 22, "Heading4": 22, "Heading5": 22,
                       "Heading6": 22}


def _docx_in_style(xml, style_id, fn):
    match = re.search(_DOCX_STYLE % style_id, xml, re.S)
    if not match:
        return xml
    return xml[:match.start()] + fn(match.group(0)) + xml[match.end():]


def _docx_styles(xml):
    xml = _DOCX_THEME_FONTS.sub(_DOCX_FONTS, xml)
    xml = _DOCX_COLOR.sub('<w:color w:val="000000" />', xml)
    xml = _docx_in_style(xml, "Hyperlink", lambda s: s.replace(
        '<w:color w:val="000000" />', '<w:color w:val="1A4F8A" />'))
    # 11 pt, single spaced, 6 pt between paragraphs, justified.
    xml = xml.replace('<w:sz w:val="24" />\n        <w:szCs w:val="24" />',
                      '<w:sz w:val="22" />\n        <w:szCs w:val="22" />', 1)
    xml = xml.replace('<w:spacing w:after="200" />',
                      '<w:spacing w:after="120" w:line="240" w:lineRule="auto" />'
                      '<w:jc w:val="both" />', 1)
    xml = _docx_in_style(xml, "BodyText", lambda s: s.replace(
        '<w:spacing w:before="180" w:after="180" />',
        '<w:spacing w:before="0" w:after="120" w:line="240" w:lineRule="auto" />'
        '<w:jc w:val="both" />'))
    for style_id, half in _DOCX_HEADING_SIZES.items():
        def resize(s, half=half):
            s = re.sub(r'<w:sz w:val="\d+" />', '<w:sz w:val="%d" />' % half, s)
            return re.sub(r'<w:szCs w:val="\d+" />', '<w:szCs w:val="%d" />' % half, s)
        xml = _docx_in_style(xml, style_id, resize)
    # A heading belongs to the text it introduces, not to the section above it.
    for style_id in ("Heading1", "Heading2", "Heading3"):
        xml = _docx_in_style(xml, style_id,
                             lambda s: s.replace('w:after="0"', 'w:after="120"'))
    # Lists and table cells stay left aligned: justifying a short line stretches
    # its few words across the whole column.
    xml = _docx_in_style(xml, "Compact",
                         lambda s: s.replace("<w:pPr>", '<w:pPr><w:jc w:val="left" />'))
    xml = _docx_in_style(xml, "VerbatimChar", lambda s: re.sub(
        r'<w:sz w:val="\d+" />', '<w:sz w:val="20" />', s))
    return xml.replace("</w:styles>", _DOCX_SOURCECODE + "</w:styles>")


def build_docx_reference(path):
    """Patch pandoc's own reference document into the academic look.

    Patching what pandoc ships, rather than carrying a .docx in the repository,
    keeps the styles in readable source and lets the file follow whatever
    pandoc version is installed.
    """
    raw = subprocess.run(["pandoc", "--print-default-data-file", "reference.docx"],
                         capture_output=True, timeout=30, check=True).stdout
    src = zipfile.ZipFile(io.BytesIO(raw))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "word/styles.xml":
                data = _docx_styles(data.decode("utf-8")).encode("utf-8")
            elif item.filename == "word/document.xml":
                data = data.decode("utf-8").replace(
                    "<w:sectPr />", _DOCX_SECTPR).encode("utf-8")
            out.writestr(item, data)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    return path


def export_env():
    """Environment for the export subprocess, with the home TeX on PATH.

    A desktop launcher never reads the shell profile, so a TeX installed under
    the home directory is invisible to the plugin even though the same command
    works in a terminal. TinyTeX, the usual way to get one without root, puts
    its binaries exactly there.
    """
    env = dict(os.environ)
    home = os.path.expanduser("~")
    extra = [os.path.join(home, "bin")]
    tex = os.path.join(home, ".TinyTeX", "bin")
    if os.path.isdir(tex):
        extra += [os.path.join(tex, d) for d in sorted(os.listdir(tex))]
    path = env.get("PATH", "").split(os.pathsep)
    env["PATH"] = os.pathsep.join(
        [d for d in extra if os.path.isdir(d) and d not in path] + path)
    return env


def svg_to_png(svg, path, scale=DIAGRAM_SCALE):
    """Rasterise an SVG string through the loader that draws the header icon.

    Nothing external is involved. The obvious tool, mmdc, is packaged as a snap
    and can read neither /mnt nor a hidden directory, so it cannot see the very
    documents this plugin opens.
    """
    data = GLib.Bytes.new(svg.encode("utf-8"))
    pixbuf = GdkPixbuf.Pixbuf.new_from_stream(
        Gio.MemoryInputStream.new_from_bytes(data), None)
    pixbuf = GdkPixbuf.Pixbuf.new_from_stream_at_scale(
        Gio.MemoryInputStream.new_from_bytes(data),
        pixbuf.get_width() * scale, pixbuf.get_height() * scale, True, None)
    pixbuf.savev(path, "png", [], [])
    return path


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
        self._exporting = False
        self._scroll_y = 0
        self._pending_async = False
        self._level = 0
        prefs = load_prefs()
        saved = prefs.get("level")
        self._last_shown = (saved if isinstance(saved, int) and saved in LEVELS
                            and saved > 0 else DEFAULT_LEVEL)
        # Applied the first time the preview opens, not now: moving the panel
        # needs the window's widget tree, which does not exist yet.
        saved_side = prefs.get("side")
        self._want_side = (bool(saved_side) if isinstance(saved_side, bool)
                           and Tepl is not None else None)
        # A preview left open comes back open. Restoring only the share and the
        # split is no restore from where the reader sits: the window opens on a
        # bare editor and nothing looks remembered at all.
        self._want_open = bool(prefs.get("open"))
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
        self._side_mode = False
        self._panel_item = None
        self._split_button = None
        self._split_image = None
        self._ui_settings = None
        self._side_was_visible = None
        self._side_widget = None
        self._side_paned = None

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

        export_docx = Gio.SimpleAction(name="markdown-preview-export-docx")
        export_docx.connect("activate", lambda *_a: self._export(fmt="docx"))
        self.window.add_action(export_docx)

        printing = Gio.SimpleAction(name="markdown-preview-print")
        printing.connect("activate", self._print_dialog)
        self.window.add_action(printing)

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
        if self._want_open:
            # Late enough for gedit to have restored its session documents, and
            # harmless if the tab handler already got there first.
            GLib.timeout_add(RESTORE_OPEN_MS, self._restore_open)

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
        if self._split_button is not None:
            self._split_button.get_parent().remove(self._split_button)
            self._split_button = None
        # The preview leaves whichever panel is hosting it.
        if self._side_mode:
            if self._panel_item is not None:
                Tepl.Panel.remove(self.window.get_side_panel(), self._panel_item)
                self._panel_item = None
            if self._side_was_visible is not None:
                self._show_side_panel(self._side_was_visible)
                self._side_was_visible = None
        else:
            panel.remove(self._scrolled)
        self.window.remove_action("markdown-preview")
        self.window.remove_action("markdown-preview-export")
        self.window.remove_action("markdown-preview-export-docx")
        self.window.remove_action("markdown-preview-print")
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

        # Packed after the level button, which puts it to the left of it:
        # pack_end stacks inward from the right edge.
        split = Gtk.Button()
        self._split_image = Gtk.Image.new_from_pixbuf(self._split_pixbuf())
        split.set_image(self._split_image)
        split.set_tooltip_text(
            SPLIT_TIPS[self._side_mode] if Tepl is not None
            else "Side by side is unavailable: the Tepl library is missing."
        )
        split.set_sensitive(Tepl is not None)
        split.connect("clicked", self._on_split_clicked)
        split.show_all()
        headerbar.pack_end(split)
        self._split_button = split
        self._sync_button()

    def _split_pixbuf(self):
        # Lucide art is markup, and a header-bar button takes a widget, so the
        # shape is rasterised. The stroke follows the theme foreground so the
        # icon reads on light and on dark.
        color = "#777777"
        ctx = self._button.get_style_context() if self._button is not None else None
        if ctx is not None:
            # get_color returns the foreground actually in effect. Looking up
            # theme_fg_color can miss, and the grey fallback reads as disabled.
            rgba = ctx.get_color(ctx.get_state())
            color = "#%02x%02x%02x" % (
                int(rgba.red * 255), int(rgba.green * 255), int(rgba.blue * 255)
            )
        name = "columns-2" if self._side_mode else "rows-2"
        svg = (SVG_OPEN.replace('stroke="currentColor"', 'stroke="%s"' % color)
               + SPLIT_ICONS[name] + "</svg>")
        loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
        loader.set_size(ICON_PX, ICON_PX)
        loader.write(svg.encode("utf-8"))
        loader.close()
        return loader.get_pixbuf()

    def _on_split_clicked(self, *_args):
        if Tepl is None:
            return
        try:
            self._set_side_mode(not self._side_mode)
        except Exception:  # noqa: BLE001 - a failed swap must not kill the panel
            log_error("orientation swap")
            # Put the preview back where it works rather than leave it nowhere.
            try:
                self._recover_bottom()
            except Exception:  # noqa: BLE001
                log_error("orientation recovery")

    def _recover_bottom(self):
        if self._scrolled.get_parent() is not None:
            self._scrolled.get_parent().remove(self._scrolled)
        self._side_mode = False
        self._panel_item = None
        self.window.get_bottom_panel().add_titled(
            self._scrolled, PANEL_NAME, "Markdown Preview"
        )
        self._sync_split_button()
        self._set_level(self._level if self._level > 0 else self._last_shown)

    def _sync_split_button(self):
        if self._split_button is None:
            return
        self._split_image.set_from_pixbuf(self._split_pixbuf())
        self._split_button.set_tooltip_text(SPLIT_TIPS[self._side_mode])

    def _ui(self):
        # gedit keeps panel visibility in its own settings, and Tepl.Panel is an
        # interface with no widget API, so this is the only handle on it.
        if self._ui_settings is None:
            try:
                self._ui_settings = Gio.Settings.new("org.gnome.gedit.preferences.ui")
            except Exception:  # noqa: BLE001 - schema absent means no side mode
                self._ui_settings = False
        return self._ui_settings or None

    def _side_container(self):
        # get_side_panel() hands back the inner panel object, but the widget the
        # pane sizes is an ancestor of it (GeditSidePanel). Showing the inner one
        # leaves the wrapper hidden, and a hidden child of a pane gets no width
        # however the position is set, which is why the panel stayed at a pixel.
        node = self._scrolled.get_parent()
        while node is not None:
            parent = node.get_parent()
            if (isinstance(parent, Gtk.Paned)
                    and parent.get_orientation() == Gtk.Orientation.HORIZONTAL):
                return node
            node = parent
        return None

    def _swap_side(self, paned, to_right):
        # gedit docks the side panel as the first child, which puts it at the
        # left. Reordering the two children moves it to the right, and it is put
        # back when the split returns to stacked, so gedit keeps its own layout.
        first, second = paned.get_child1(), paned.get_child2()
        if first is None or second is None:
            return
        side_is_first = first is self._side_widget
        if side_is_first == (not to_right):
            return
        paned.remove(first)
        paned.remove(second)
        if to_right:
            paned.pack1(second, True, False)
            paned.pack2(first, False, False)
        else:
            paned.pack1(second, False, False)
            paned.pack2(first, True, False)

    def _show_side_panel(self, show, container=None):
        # Two steps, and both are needed. The setting is what gedit persists, but
        # it is read when the window is built, so writing it does not move the
        # panel that is already on screen. The panel object is also a container,
        # which is why showing the widget is what actually reveals it.
        try:
            inner = self.window.get_side_panel()
            if hasattr(inner, "set_visible"):
                inner.set_visible(show)
            container = container or self._side_container()
            if container is not None:
                # show(), not show_all(): the wrapper is what is hidden, and its
                # children are gedit's business.
                container.set_visible(show)
        except Exception:  # noqa: BLE001
            log_error("side panel visibility")
        ui = self._ui()
        if ui is not None:
            ui.set_boolean("side-panel-visible", show)

    def _host_panel(self):
        return (self.window.get_side_panel() if self._side_mode
                else self.window.get_bottom_panel())

    def _set_side_mode(self, side):
        if side == self._side_mode:
            return
        self._busy = True
        docs = self._doc_area()
        if docs is not None:
            docs.show()
        # Leave the current host before joining the other one.
        if self._side_mode:
            if self._side_paned is not None:
                self._swap_side(self._side_paned, False)
            if self._panel_item is not None:
                Tepl.Panel.remove(self.window.get_side_panel(), self._panel_item)
                self._panel_item = None
        else:
            self.window.get_bottom_panel().remove(self._scrolled)
        self._side_mode = side
        if side:
            ui = self._ui()
            if ui is not None and self._side_was_visible is None:
                self._side_was_visible = ui.get_boolean("side-panel-visible")
            self._panel_item = Tepl.Panel.add(
                self.window.get_side_panel(), self._scrolled, PANEL_NAME,
                "Markdown Preview", "format-text-rich-symbolic",
            )
            # Captured while the preview still hangs below them: after it moves
            # out, walking the tree finds the other side of the pane instead.
            self._side_widget = self._side_container()
            self._side_paned = self._paned()
            if self._side_paned is not None:
                self._swap_side(self._side_paned, True)
        else:
            self.window.get_bottom_panel().add_titled(
                self._scrolled, PANEL_NAME, "Markdown Preview"
            )
            if self._side_was_visible is not None:
                self._show_side_panel(self._side_was_visible, self._side_widget)
                self._side_was_visible = None
            self._side_widget = None
            self._side_paned = None
        self._busy = False
        self._sync_split_button()
        self._remember()
        # The share means width in one orientation and height in the other, so
        # it is applied again rather than carried over.
        level, self._level = self._level, 0
        self._set_level(level if level > 0 else self._last_shown)

    def _remember(self):
        save_prefs({"level": self._last_shown, "side": bool(self._side_mode),
                    "open": self._level > 0})

    def _restore_open(self, *_args):
        """Reopen the preview if it was open when the window last closed.

        Deferred rather than done while activating: gedit restores its session
        documents after the window exists, so at activation there is often no
        document yet to preview. Whichever comes first, the timer or the tab
        settling, opens it and the other finds nothing left to do.
        """
        if not self._want_open or self._level > 0:
            return False
        doc = self.window.get_active_document()
        if doc is None or not self._is_markdown(doc):
            return False
        self._want_open = False
        try:
            self._set_level(self._last_shown)
        except Exception:  # noqa: BLE001 - a bad restore must not block gedit
            log_error(traceback.format_exc())
        return False

    def _sync_button(self, *_args):
        if self._button_label is not None:
            self._button_label.set_text("%d%%" % self._level)
        if self._level_action is not None:
            self._syncing = True
            self._level_action.set_state(GLib.Variant.new_int32(self._level))
            self._syncing = False

    # --- share of the window held by the preview ----------------------------
    def _paned(self):
        # The window nests two panes: a horizontal one holding the side panel and
        # a vertical one holding the bottom panel. Walking up to the nearest pane
        # finds the vertical one either way, so the pane is chosen by the
        # orientation the current split actually needs.
        want = (Gtk.Orientation.HORIZONTAL if self._side_mode
                else Gtk.Orientation.VERTICAL)
        node = self._scrolled.get_parent()
        while node is not None:
            if isinstance(node, Gtk.Paned) and node.get_orientation() == want:
                return node
            node = node.get_parent()
        return None

    def _doc_area(self):
        # Stacked, the documents area is the first child and the panel the
        # second; side by side the side panel comes first, so the editor is the
        # other one.
        # With the panel moved to the right, the editor is the first child in
        # both orientations, so the share is measured the same way in each.
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
            total = (paned.get_allocated_width() if self._side_mode
                     else paned.get_allocated_height())
            if total > 1:
                paned.set_position(int(total * (100 - level) / 100.0))
            self._place_tries += 1
            return self._place_tries < 6

        GLib.timeout_add(100, apply_position)

    def _set_level(self, level):
        level = min(LEVELS, key=lambda step: abs(step - int(level)))
        # The split last in use is restored here rather than at load, because
        # moving the panel while nothing is visible is what once left the
        # preview in no panel at all. It sits in this method and not in the
        # Ctrl+M path because the header-bar button opens the preview too, and
        # a restore that only one of the two ways triggers is no restore.
        if level > 0 and self._want_side is not None:
            want, self._want_side = self._want_side, None
            if want != self._side_mode:
                try:
                    # _set_side_mode returns here with the share applied, and by
                    # then _want_side is already cleared, so this runs once.
                    self._last_shown = level
                    self._set_side_mode(want)
                    return
                except Exception:  # noqa: BLE001 - never open a broken layout
                    log_error("restore split")
                    try:
                        self._recover_bottom()
                    except Exception:  # noqa: BLE001
                        log_error("restore recovery")
                    return
        self._busy = True
        self._level = level
        if level > 0:
            self._last_shown = level
            self._remember()
        panel = self._host_panel()
        docs = self._doc_area()
        if level == 0:
            if docs is not None:
                docs.show()
            if self._side_mode:
                self._show_side_panel(False)
            else:
                panel.set_visible(False)
        else:
            self._update()
            if self._side_mode:
                self._scrolled.show()
                if self._panel_item is not None:
                    Tepl.Panel.set_active(panel, self._panel_item)
                self._show_side_panel(True)
            else:
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
        # Only the bottom panel is watched: the side panel is not a stack.
        if self._busy or self._side_mode:
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
        if kind == "toggle":
            self._toggle_edit_mode()
        elif kind == "cancel":
            self._edit_pos = None
            self._edit_text = None
        elif kind == "request":
            self._open_block(report.get("pos", ""))
        elif kind == "commit":
            self._commit_block(report.get("pos", ""), report.get("text", ""))
        elif kind == "export":
            fmt = report.get("fmt", "pdf")
            if fmt in ("pdf", "docx"):
                self._export(fmt=fmt)

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
    def _toast(self, text, state=""):
        # Export happens outside the page, so it reports back inside it: there
        # is no dialog, and a plugin has nowhere else to speak.
        self._webview.run_javascript(
            "if(window.__mdToast)window.__mdToast(%s,%s);"
            % (json.dumps(text), json.dumps(state)), None, None, None,
        )

    def _print_dialog(self, *_args):
        # Kept beside the typeset exports: this one prints the page as it looks
        # on screen, which is the right answer when the point is the preview
        # itself rather than a document to hand in.
        try:
            WebKit2.PrintOperation.new(self._webview).run_dialog(self.window)
        except Exception:  # noqa: BLE001 - export must never break the editor
            log_error(traceback.format_exc())

    def _export(self, _action=None, _param=None, fmt="pdf"):
        if self._exporting:
            self._toast("An export is already running.", "busy")
            return
        doc = self.window.get_active_document()
        if doc is None or not self._is_markdown(doc):
            self._toast("This document is not Markdown.", "err")
            return
        gfile = doc.get_file()
        loc = gfile.get_location() if gfile is not None else None
        path = loc.get_path() if loc is not None else None
        if not path:
            # The output goes beside the source, so there has to be a source.
            self._toast("Save the document before exporting.", "err")
            return
        out = os.path.splitext(path)[0] + "." + fmt
        start, end = doc.get_bounds()
        text = doc.get_text(start, end, False)
        self._exporting = True
        self._toast("Exporting %s..." % fmt.upper(), "busy")
        sources = ([text] if self._is_mmd(doc)
                   else [m.group(1) for m in _MMD_FENCE.finditer(text)])
        if sources:
            self._render_diagrams(
                sources,
                lambda svgs: self._run_export(fmt, text, out, path, svgs,
                                              self._is_mmd(doc)),
            )
        else:
            self._run_export(fmt, text, out, path, [], False)

    def _render_diagrams(self, sources, done):
        """Draw the diagrams offscreen and hand back one SVG each.

        The preview's own page is not scraped: its diagrams are drawn with HTML
        labels, which live in a foreignObject that the SVG rasteriser ignores,
        and would reach the page as empty boxes. This render turns those labels
        into plain SVG text.
        """
        blocks = "".join('<pre class="mermaid">%s</pre>' % _html.escape(s)
                         for s in sources)
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
            + blocks + '<script src="file://' + _MERMAID_JS + '"></script>'
            "<script>if(window.mermaid){mermaid.initialize({startOnLoad:false,"
            "securityLevel:'loose',htmlLabels:false,theme:'neutral',"
            "flowchart:{htmlLabels:false},class:{htmlLabels:false}});"
            "mermaid.run({nodes:document.querySelectorAll('.mermaid')})"
            ".catch(function(){}).then(function(){window.__mdDrawn=true;});}"
            "</script></body></html>"
        )
        win = Gtk.OffscreenWindow()
        win.set_default_size(1400, 900)
        view = WebKit2.WebView()
        win.add(view)
        win.show_all()
        state = {"done": False}

        def finish(svgs):
            if state["done"]:
                return False
            state["done"] = True
            win.destroy()
            done(svgs)
            return False

        def on_grab(webview, result, _data):
            svgs = []
            try:
                value = webview.run_javascript_finish(result)
                svgs = json.loads(value.get_js_value().to_string())
            except Exception:  # noqa: BLE001 - a diagram failure is not fatal
                log_error(traceback.format_exc())
            finish(svgs)

        def grab():
            view.run_javascript(
                "JSON.stringify(Array.prototype.map.call("
                "document.querySelectorAll('.mermaid'),function(n){"
                "var s=n.querySelector('svg');return s?s.outerHTML:null;}));",
                None, on_grab, None,
            )
            return False

        def on_load(_view, event):
            if event == WebKit2.LoadEvent.FINISHED:
                GLib.timeout_add(DIAGRAM_WAIT_MS, grab)

        view.connect("load-changed", on_load)
        view.load_html(html, "file:///")
        # A diagram that never finishes must not strand the export.
        GLib.timeout_add(EXPORT_TIMEOUT * 100, lambda: finish([]))

    def _substitute_diagrams(self, text, svgs, tmp, is_mmd):
        """Swap each diagram fence for the image drawn from it.

        A fence whose render failed is left as it was, so the export still runs
        and that one diagram arrives as code instead of vanishing.
        """
        made = []
        for index, svg in enumerate(svgs):
            if not svg:
                made.append(None)
                continue
            try:
                made.append(svg_to_png(svg, os.path.join(tmp, "d%d.png" % index)))
            except Exception:  # noqa: BLE001
                log_error(traceback.format_exc())
                made.append(None)
        if is_mmd:
            return "![](%s)\n" % made[0] if made and made[0] else text
        counter = {"i": 0}

        def swap(match):
            index = counter["i"]
            counter["i"] += 1
            if index < len(made) and made[index]:
                return "![](%s)\n" % made[index]
            return match.group(0)

        return _MMD_FENCE.sub(swap, text)

    def _pandoc_command(self, fmt, out, doc_dir, reference, table_filter=""):
        cmd = ["pandoc"] + list(EXPORT_COMMON)
        cmd += ["--resource-path=" + doc_dir, "-o", out]
        if table_filter:
            cmd += ["--lua-filter=" + table_filter]
        for name in BIB_NAMES:
            bib = os.path.join(doc_dir, name)
            if os.path.exists(bib):
                cmd += ["--citeproc", "--bibliography=" + bib]
                break
        if fmt == "pdf":
            cmd += list(PDF_VARS)
        elif reference:
            cmd += ["--reference-doc=" + reference]
        return cmd

    def _run_export(self, fmt, text, out, doc_path, svgs, is_mmd):
        tmp = tempfile.mkdtemp(prefix="mdpreview-export-")
        doc_dir = os.path.dirname(doc_path) or "."
        try:
            body = self._substitute_diagrams(text, svgs, tmp, is_mmd)
        except Exception:  # noqa: BLE001
            log_error(traceback.format_exc())
            body = text

        def work():
            ok, message = False, ""
            try:
                reference = ""
                if fmt == "docx":
                    reference = os.path.join(tmp, "academic-reference.docx")
                    build_docx_reference(reference)
                table_filter = os.path.join(tmp, "tablewidth.lua")
                with open(table_filter, "w", encoding="utf-8") as fh:
                    fh.write(TABLE_WIDTH_LUA)
                cmd = self._pandoc_command(fmt, out, doc_dir, reference, table_filter)
                attempts = [cmd + list(PDF_FONTS), cmd] if fmt == "pdf" else [cmd]
                env = export_env()
                for attempt in attempts:
                    proc = subprocess.run(attempt, input=body, capture_output=True,
                                          text=True, timeout=EXPORT_TIMEOUT,
                                          cwd=doc_dir, env=env)
                    if proc.returncode == 0:
                        ok, message = True, os.path.basename(out)
                        break
                    message = (proc.stderr or "").strip()
                if not ok:
                    log_error("export %s: %s" % (fmt, message))
                    message = message.splitlines()[-1] if message else "pandoc failed"
            except FileNotFoundError:
                message = "pandoc not found. Install it: sudo apt-get install pandoc"
            except subprocess.TimeoutExpired:
                message = "the export went past %d s and was stopped." % EXPORT_TIMEOUT
            except Exception as exc:  # noqa: BLE001 - never break the editor
                log_error(traceback.format_exc())
                message = str(exc)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            GLib.idle_add(self._export_finished, ok, fmt, message)

        # pandoc with xelatex takes seconds, and the main loop draws the editor,
        # so the run happens off it and only the report comes back.
        threading.Thread(target=work, daemon=True).start()

    def _export_finished(self, ok, fmt, message):
        self._exporting = False
        if ok:
            self._toast("%s written beside the .md file: %s" % (fmt.upper(), message))
        else:
            self._toast("Failed to export %s: %s" % (fmt.upper(), message), "err")
        return False

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
        if self._want_open:
            self._restore_open()
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
                "Export academic PDF", "win.markdown-preview-export"
            )
            self._menu_ext.append_menu_item(export)
            export_docx = Gio.MenuItem.new(
                "Export academic DOCX", "win.markdown-preview-export-docx"
            )
            self._menu_ext.append_menu_item(export_docx)
            printing = Gio.MenuItem.new(
                "Print the preview as it looks on screen", "win.markdown-preview-print"
            )
            self._menu_ext.append_menu_item(printing)

    def do_deactivate(self):
        self.app.set_accels_for_action("win.markdown-preview", [])
        self.app.set_accels_for_action("win.markdown-preview-edit", [])
        for name in ("zoom-in", "zoom-out", "zoom-reset"):
            self.app.set_accels_for_action("win.markdown-preview-" + name, [])
        self._menu_ext = None
