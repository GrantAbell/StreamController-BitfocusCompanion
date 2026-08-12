# Bitfocus Companion plugin for StreamController

Turns StreamController-controlled Stream Deck hardware into a native
[Bitfocus Companion](https://bitfocus.io/companion) control surface.

> **Disclaimer:** This plugin was built with the assistance of AI tools. Review
> the code yourself before relying on it, and please open an issue if you spot
> a bug or a mistake.

The plugin connects persistently to Companion over the Satellite protocol,
registers StreamController as a surface, and mirrors Companion's **own rendered
button imagery** onto your keys and dials. Presses, releases, holds and encoder
rotations are delivered to Companion with correct semantics, and button graphics
update live as Companion feedback, variables and states change.

Companion stays responsible for button logic and rendering. StreamController
provides the hardware and the plugin host.

> Not an officially supported Bitfocus product.

## Status

Under active development.

## Requirements

- StreamController 1.5.0-beta.15 or newer
- Bitfocus Companion with the **Satellite TCP** API enabled (default port `16622`)

## Configuration

Connection settings live at the plugin level and are configured once:

| Setting | Default |
|---|---|
| Connection mode | Satellite TCP |
| Host | `127.0.0.1` |
| Port | `16622` |

Each `Companion Button` action then specifies which Companion button the
physical control represents, using 1-based page/row/column coordinates, and
whether it follows Companion's currently active page.

## Pages

Companion gives **every surface its own current page**, and this plugin
registers as one surface. That has a consequence worth knowing before you
configure anything:

- **Dynamic** controls (`Follow the current Companion page` on) mirror whatever
  page *this surface* is on.
- Browsing to another page in the Companion web UI's **Buttons** tab is an
  editor view. It moves no surface, so dynamic controls will not follow it.
- **Static** controls name a page outright and ignore surface paging entirely.

To move the surface, use the `Companion Page` action, set to next or previous
page. On a dial it also pages on rotation, clockwise for next. Alternatively,
change the page from Companion's **Surfaces** tab, or map a Companion button
that runs an internal page action — presses arrive tagged with this surface, so
Companion pages the right one.

> **The `Companion Page` action needs permission.** The plugin declares the
> capability, but Companion gates it behind a per-surface checkbox that is
> **unticked by default**. Tick `Allow StreamController to change this surface
> page` in Companion under **Surfaces → your StreamController surface →
> Settings**. Until you do, Companion accepts the request and discards it
> silently — the key will appear to do nothing.

## Development

The Companion protocol, domain and transport code under `companion/` imports
nothing from StreamController, so the test suite runs standalone:

```sh
python3 -m venv .venv
.venv/bin/pip install pillow pytest
.venv/bin/python -m pytest
```

Pillow is the only thing the suite needs beyond the standard library, and
StreamController already ships it — the venv exists so tests can run outside
the Flatpak, not because the plugin adds a dependency.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE). Chosen to match StreamController,
the application this plugin runs inside.

## Attribution

Behaviourally based on Bitfocus's Stream Deck Companion plugin
([io.bitfocus.companion-plugin](https://github.com/bitfocus/io.bitfocus.companion-plugin),
MIT). No source code was copied; this is an independent Python reimplementation.
