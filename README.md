# gedit Markdown Preview

A live Markdown preview for **gedit 46** that you can also write in. It renders
the open document with pandoc, styled to resemble Apostrophe, in whatever share
of the window you ask for, beside the editor or below it. Math renders offline as
native MathML and Mermaid blocks render as figures. Click a block in the preview
and you edit the Markdown that produced it, in place.

Everything runs locally: no CDN, no network, no icon font.

## Features

**Where the preview goes**

- **You choose how much of the window it takes**: 0%, 25%, 50%, 75% or 100%, from
  the header-bar button, whose label is always the share in effect. 0% is the
  editor alone, 100% the preview alone, and the default is 50%.
- **Stacked or side by side**: the button left of the share control swaps the
  split. Stacked, the preview sits below the editor, and that is the default;
  side by side, it moves into gedit's side panel, which the plugin reorders to
  the right of the editor and puts back on the way out.
- **The two views scroll together**, in both directions. Every rendered block
  carries the source line it came from, so the match is by position in the
  document rather than by a proportion of the total height, which is what keeps a
  long code block from throwing the two out of step.
- **Reading position kept** across the live re-render, so editing halfway down a
  long file does not throw you back to the top.

**What it renders**

- **Math offline**: `$...$` and `$$...$$` become native MathML through WebKitGTK,
  with no JavaScript.
- **Mermaid diagrams**: fenced ` ```mermaid ` blocks and whole `.mmd` files
  render as figures (optional, see below).
- **Diagram errors are visible**: a malformed diagram shows its parse error in
  place instead of leaving a blank area.
- **Syntax highlighting** in fenced code blocks, done by pandoc and colored by
  the bundled stylesheet.
- **Local images**: relative and `file://` paths load from disk.
- **Outline for long documents**: three or more headings produce a navigable
  index, a pinned side rail when the window is wide enough to hold it beside the
  text column and a collapsible block at the top when it is not.
- **Light and dark**: the preview follows your system color scheme.
- **Zoom** with `Ctrl` and plus, minus or zero. The level belongs to the view, so
  it survives the live re-render.

**Writing in the preview**

- **Edit one block at a time.** Clicking a block in edit mode replaces it with
  the Markdown of the source lines that produced it. Confirming rewrites exactly
  those lines and nothing else: the page never converts HTML back to Markdown, so
  the rest of your file keeps the formatting you gave it, and the change lands in
  gedit as a single undo step.
- **A bar on the right**, always on screen, with bold, italic, heading, list,
  code, link and table, plus confirm, cancel and the pencil that turns edit mode
  on and off. Every formatting action undoes itself: apply it twice and the text
  is back as it was.
- **It refuses to write a stale range.** If the buffer changes from the editor
  side while a block is open, the edit is dropped rather than applied to line
  numbers that have moved.

**Getting it out**

- **Export to PDF** through the print dialog, with diagrams and math already laid
  out.

## Requirements

- gedit 46 (GTK3 build; the one shipped on Ubuntu 24.04)
- `pandoc`
- `gir1.2-webkit2-4.1` (WebKit2GTK 4.1 introspection)
- The gedit Python plugin support (ships with gedit)

On Debian/Ubuntu:

```bash
sudo apt-get install gedit pandoc gir1.2-webkit2-4.1
```

The side by side split additionally needs the Tepl introspection data, which
gedit itself brings. Without it that one button is disabled and everything else
works unchanged.

## Install

```bash
git clone https://github.com/pcbrom/gedit-markdown-preview.git
cd gedit-markdown-preview
./install.sh
```

Then re-open gedit, which reads its plugin list at startup. Open a `.md` file and
press `Ctrl+M`.

The installer copies the plugin into `~/.local/share/gedit/plugins` and enables
it via `gsettings`. It is idempotent; run it again to update.

## Usage

| Action | How |
| --- | --- |
| Show or hide the preview | `Ctrl+M`, or the menu entry "Markdown Preview" |
| Choose the share | the header-bar button, which lists 0%, 25%, 50%, 75%, 100% |
| Swap stacked and side by side | the button left of the share control |
| Turn edit mode on or off | `Ctrl+E`, the pencil in the bar, or the menu |
| Edit a block | click it in edit mode; `Ctrl+Enter` or a click outside confirms, `Esc` cancels |
| Zoom the page | `Ctrl` with plus, minus or zero |
| Export | menu entry "Export Markdown preview (PDF)", then "Print to File" |

Opens any `.md`, `.markdown`, `.mdown`, `.mkd` or `.mmd` file.

Some details worth knowing:

- `Ctrl+M` returns to the share last in use rather than to a fixed one.
- In edit mode, hovering outlines the block under the pointer. While a block is
  open the live update and the scroll sync are suspended, and the formatting
  buttons never take focus from it, so clicking one does not close what you are
  editing.
- Clicking an outline entry jumps to that section. A large enough zoom narrows
  the page and moves the outline from the side rail to the collapsible block.
- Export prints the rendered page, which is why it produces PDF rather than HTML:
  the bundled `mermaid.js` is referenced by path, so a saved HTML file would only
  render on the machine that made it.

Try the files under `examples/`: `math.md` for MathML and `mermaid.md` for
diagrams.

## Mermaid diagrams (optional)

Diagram rendering uses a local `mermaid.js`, the build that sets
`window.mermaid`. `install.sh` provisions it: it copies one from a local mermaid
install if present, otherwise downloads it from jsDelivr.

If `mermaid.js` is absent everything else still works and diagram blocks show as
text. To enable diagrams later, drop a `mermaid.js` at
`~/.local/share/gedit/plugins/mermaid.js` and re-open gedit.

`mermaid.js` is not tracked here, being a large third-party file; it is
provisioned at install time.

## How it works

The plugin holds a `WebKit2.WebView` in one of gedit's panels. On each edit it
runs the buffer through pandoc:

```
pandoc --from=gfm+tex_math_dollars+sourcepos --to=html5 --mathml
```

wraps the output in a small stylesheet, and loads it into the view. Mermaid
blocks are rendered client-side by `mermaid.js`, loaded only when a diagram is
actually present.

**`sourcepos` is the keystone.** It tags every block with the source line it came
from, which is what makes both the scroll sync and the editing exact. For
scrolling, the plugin interpolates between the two anchors that bracket the
current position; for editing, the block a click lands in names the line range to
replace, so a commit rewrites that range and leaves the rest of the file byte for
byte. The extension also wraps every word in a span carrying its own position,
which inflates the document more than tenfold and buys nothing for vertical
scrolling, where every word on a line shares one y. Those inline wrappers are
pruned before the page loads, which keeps the block anchors at close to the
original size.

**Sizing.** The share sets the position of the pane that divides the editor from
the panel, height when stacked and width when side by side. Showing the bottom
panel makes gedit restore its own saved height, and that restore lands after a
single early write, so the position is re-asserted over a few ticks and the
requested share is the one that survives. At 100% the editor is hidden outright,
since a pane position of zero leaves a sliver of editor and a drag handle in the
way.

**The side panel.** `get_side_panel()` hands back the inner panel object, but the
widget the pane sizes is the wrapper around it, and a hidden child of a pane gets
no width however the position is set. The wrapper is therefore what gets shown,
found through the widget tree, and it is captured while the preview still hangs
below it, because once the preview moves out the same walk would find the other
side of the pane. The preview ends up at the right because the plugin reorders
the pane's two children, and puts them back when the split returns to stacked.

**Scroll sync etiquette.** Each side stops reporting for a moment while the other
drives it, and the last position requested during that pause is applied when it
ends, so a continuous scroll lands where it ended rather than where it began.

**Failures leave a trace.** A plugin launched from the desktop has no visible
stderr, so an error inside a callback would vanish. Failures while swapping the
split are recorded in `~/.cache/gedit-mdpreview.log` and the preview returns to
the bottom panel rather than ending up in no panel at all.

## Icons

The editing bar uses [Lucide](https://lucide.dev/) icons, ISC licensed, inlined
as SVG. Under a kilobyte for the whole set, so the bar needs no network, no icon
font and no second file. Since an icon carries no words, each button also carries
a tooltip stating what the action does, and an `aria-label` with the same text.

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
[WebKitGTK](https://webkitgtk.org/); icons by [Lucide](https://lucide.dev/).
Reading style inspired by [Apostrophe](https://apps.gnome.org/Apostrophe/).
