# gedit Markdown Preview

A live Markdown preview plugin for **gedit 46**. It renders the document you are
editing with pandoc, styled to resemble Apostrophe, and shows it full-view in
place of the editor. Math renders offline as native MathML, and Mermaid diagrams
render as figures. The editor stays your raw view; one click flips between raw
and formatted.

## Features

- **Full-view toggle**, not a split: the preview takes the whole area, so it
  reads like a page. Flip back to the editor with the same control.
- **Three ways to toggle**: a button in the header bar, `Ctrl+M`, or the menu
  entry. The button reflects the current mode.
- **Live updates** as you type (debounced).
- **Math offline**: `$...$` and `$$...$$` render as native MathML through
  WebKitGTK, with no JavaScript and no CDN.
- **Mermaid diagrams**: fenced ` ```mermaid ` blocks and whole `.mmd` files
  render as figures (optional, see below).
- **Local images**: relative and `file://` image paths load from disk.
- **Light and dark**: the preview follows your system color scheme.

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
- The preview replaces the editor while active; toggle again to return to the
  raw text.

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
the buffer through `pandoc --from=gfm+tex_math_dollars --to=html5 --mathml`,
wraps the output in a small stylesheet, and loads it into the view. When the
preview is on, the editor area is hidden so the panel fills the window, giving a
full-page view rather than a split. Mermaid blocks are rendered client-side by
`mermaid.js`, loaded only when a diagram is present.

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
