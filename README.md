# gedit Markdown Preview

A live Markdown preview plugin for **gedit 46**. It renders the document you are
editing with pandoc, styled to resemble Apostrophe, and shows it in the share of
the window you choose, from none of it to all of it. Math renders offline as
native MathML, and Mermaid diagrams render as figures. The editor stays your raw
view.

## Features

- **You choose how much of the window the preview takes**: 0%, 25%, 50%, 75% or
  100%, from the header-bar button, whose label is always the share in effect.
  0% is the editor alone and 100% the preview alone. The default share is 50%.
- **Three ways to toggle**: a button in the header bar, `Ctrl+M`, or the menu
  entry. The button reflects the current mode.
- **Live updates** as you type (debounced).
- **Math offline**: `$...$` and `$$...$$` render as native MathML through
  WebKitGTK, with no JavaScript and no CDN.
- **Mermaid diagrams**: fenced ` ```mermaid ` blocks and whole `.mmd` files
  render as figures (optional, see below).
- **Local images**: relative and `file://` image paths load from disk.
- **Light and dark**: the preview follows your system color scheme.
- **Reading position kept**: the preview restores your scroll offset after each
  re-render, so editing halfway down a long file does not throw you back to the
  top.
- **Syntax highlighting** in fenced code blocks, done offline by pandoc and
  colored by the bundled stylesheet. No JavaScript highlighter, no CDN.
- **Outline for long documents**: three or more headings produce a navigable
  index. It adapts to the space available: a pinned side rail when the window is
  wide enough to hold it beside the text column, and a collapsible block at the
  top of the document when it is not, so the outline is reachable at any width.
- **Export to PDF** through the print dialog, with diagrams and math already
  laid out.
- **Diagram errors are visible**: a malformed Mermaid diagram shows its parse
  error in place instead of leaving a blank area.
- **Zoom the rendered page** with `Ctrl` and plus, minus or zero. The level is a
  property of the view, so it survives the live re-render.
- **The two views scroll together**, in both directions: moving the editor moves
  the preview to the matching place, and moving the preview moves the editor.
  Every rendered block carries the source line it came from, so the match is by
  position in the document rather than by a proportion of the total height,
  which is what keeps a long code block from throwing the two out of step.

## Requirements

- gedit 46 (GTK3 build; the one shipped on Ubuntu 24.04)
- `pandoc`
- `gir1.2-webkit2-4.1` (WebKit2GTK 4.1 introspection)
- The gedit Python plugin support (ships with gedit)

On Debian/Ubuntu:

```bash
sudo apt-get install gedit pandoc gir1.2-webkit2-4.1
```

## Install

```bash
git clone https://github.com/pcbrom/gedit-markdown-preview.git
cd gedit-markdown-preview
./install.sh
```

Then re-open gedit (it reads its plugin list at startup). Open a `.md` file and
press `Ctrl+M` or click the preview button in the header bar.

The installer copies the plugin into `~/.local/share/gedit/plugins` and enables
it via `gsettings`. It is idempotent; run it again to update.

## Usage

- Open any `.md`, `.markdown`, `.mdown`, or `.mkd` file.
- Toggle the preview with the header-bar button, `Ctrl+M`, or the menu entry
  "Markdown Preview".
- The header-bar button shows the share the preview holds and lists the five
  steps; `Ctrl+M` stays a quick show and hide, returning to the share last used.
- On a document with three or more headings, an outline is built: at the left as
  a side rail in a wide window, or as a collapsible "Outline" block at the top
  in a narrow one. Click an entry to jump to that section.
- `Ctrl` with plus or minus zooms the rendered page in steps of ten percent,
  between half and triple size; `Ctrl` with zero returns it to 100%. Zooming
  changes the width available to the outline, so a large enough zoom moves it
  from the side rail to the collapsible block.
- To export, use the menu entry "Export Markdown preview (PDF)" and pick
  "Print to File" in the dialog. Export prints the rendered page, which is why
  it produces PDF rather than HTML: the bundled `mermaid.js` is referenced by
  path, so a saved HTML file would only render on the machine that made it.

Try the files under `examples/`:

- `examples/math.md` for MathML rendering.
- `examples/mermaid.md` for diagrams.

## Mermaid diagrams (optional)

Diagram rendering uses a local `mermaid.js` (the build that sets
`window.mermaid`). `install.sh` provisions it automatically: it copies one from
a local mermaid install if present, otherwise downloads it from jsDelivr.

If `mermaid.js` is absent, everything else still works; diagram blocks simply
show as text. To enable diagrams later, drop a `mermaid.js` at
`~/.local/share/gedit/plugins/mermaid.js` and re-open gedit.

`mermaid.js` is not tracked in this repository (it is a large third-party file);
it is provisioned at install time.

## How it works

The plugin adds a bottom panel holding a `WebKit2.WebView`. On each edit it runs
the buffer through `pandoc --from=gfm+tex_math_dollars+sourcepos --to=html5
--mathml`,
wraps the output in a small stylesheet, and loads it into the view. The chosen
share moves the position of the paned that divides the documents area from the
bottom panel. Showing the panel makes gedit restore its own saved height, which
lands after a single early write, so the position is re-asserted over a few
ticks and the requested share is the one that survives. At 100% the editor is
hidden outright, since a paned position of zero leaves a sliver of editor and a
drag handle in the way. Mermaid blocks are rendered client-side by `mermaid.js`,
loaded only when a diagram is present.

Because each update reloads the page, the reading position would otherwise reset
on every keystroke pause. The page reports its scroll offset back to the plugin
through a WebKit script message handler, and the plugin restores that offset
once the new content has loaded, plus once more shortly after when diagrams or
math are present, since those change the page height asynchronously.

`sourcepos` is what ties the two scroll positions together: it tags every block
with the source line it came from, and the plugin interpolates between the two
anchors that bracket the current position. It also wraps each word in a span
carrying its own position, which inflates the document more than tenfold and
buys nothing for vertical scrolling, where every word on a line shares one y.
Those inline wrappers are pruned before the page is loaded, which keeps the
block anchors at close to the original size. Each side stops reporting for a
moment while the other drives it, and the last position requested during that
pause is applied when it ends, so a continuous scroll lands where it ended
rather than where it began.

## Compatibility

Built and tested against gedit 46 (GTK3, `Gedit-3.0`, WebKit2GTK 4.1). Other
gedit versions may need adjustment, since the panel and header-bar APIs differ
across releases.

## Uninstall

```bash
./uninstall.sh
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

## Credits

Rendering by [pandoc](https://pandoc.org/); diagrams by
[Mermaid](https://mermaid.js.org/); display by
[WebKitGTK](https://webkitgtk.org/). Reading style inspired by
[Apostrophe](https://apps.gnome.org/Apostrophe/).
