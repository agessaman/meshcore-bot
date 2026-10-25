# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Bot operators at a desk.** The person who runs a meshcore-bot instance, usually a self-hoster on their own LAN, checking health, digging into the mesh, and changing settings from a laptop.
- **Bot operators on a phone.** The same operators checking in away from the desk. Phone use is a real workflow, not an afterthought; every page an operator needs has to work properly at phone width.
- **Mesh community, read-only.** Neighbors on the local mesh who are given the URL. When a `web_viewer_password` is set, anonymous visitors can still reach the public pages (dashboard, contacts, mesh graph) and the anonymous-safe APIs behind them; everything else redirects to login. With no password the whole viewer is open (legacy mode).

## Product Purpose

The web viewer is the browser front end for a MeshCore Bot: a Python bot that connects to a MeshCore LoRa mesh over serial, BLE, or TCP and answers commands, runs scheduled messages, and provides data services. The viewer exists so an operator can see and run the bot without a terminal or hand-editing `config.ini`, and so the community can see what the bot sees of the mesh.

Four jobs, all first-class:

1. **Is the bot healthy?** Radio connected, recent activity, errors, uptime: a glance and done.
2. **Understand the mesh.** Contacts, the mesh graph, paths, multibyte rollout, region warnings: who is out there and how packets travel.
3. **Watch live traffic.** Realtime packets and logs while debugging or testing on air.
4. **Configure the bot.** Plugins, feeds, schedule, greeter, radio, and config from the browser, with changes validated and written back to `config.ini`.

## Positioning

The viewer sees the mesh from the bot's own radio. Its data is what this node actually heard (adverts, paths, SNR, observed hops), recorded over time in the bot's database, and its settings act on a live bot and radio. Mesh-wide maps and analyzers aggregate many observers; this is one station's ground truth plus the controls for that station.

## Operating Context

- Served by Flask from inside the bot process (or standalone via `python3 -m modules.web_viewer.app`), default on the LAN, optionally behind a reverse proxy with HTTPS and auth.
- Live updates over Socket.IO (realtime packets, logs, status).
- Optional single admin password (`web_viewer_password` in `[Web_Viewer]`); there is no per-user account system.
- Settings writes go to `config.ini` with comments preserved and a timestamped backup; command settings hot-reload, service on/off needs a restart.
- Some features are opt-in and their pages only appear when enabled (for example the multibyte monitor).

## Capabilities and Constraints

- **Low-power hosts.** The bot often runs on a Raspberry Pi, down to a Pi 2. Pages, queries, and polling have to stay light on the server and the client.
- **Works without internet.** The viewer may run on a LAN with no internet access. Every library and font is vendored under `static/`, pinned in `scripts/web-vendor/package.json` and kept current by Dependabot (see `static/vendor/README.md`); new work must not add CDN dependencies. Map tiles come from OpenStreetMap and OpenFreeMap and need internet by nature.
- **Light and dark themes.** Both are supported today (system preference plus a manual toggle) and both must keep working.
- **Undecided:** whether to stay on Bootstrap 5 and whether to keep the viewer free of a frontend build step. Neither is a commitment; the current implementation uses server-rendered Jinja templates, inline and static CSS/JS, and Bootstrap 5.1.3.
- **Terminology:** MeshCore terms (repeater, companion, room server, advert, path, hop, prefix, multibyte, region, scope, channel) are used as the firmware and community use them.

## Brand Commitments

- The navbar brand is the operator's configured `bot_name`, falling back to "MeshCore Bot". The viewer belongs to whoever runs it, not to a product brand.
- The project's own voice is cautious about airtime: it discourages adding bots to a mesh that doesn't need them and defaults costly features (like neighbor discovery) to off.

## Evidence on Hand

- Real data comes from the operator's own bot database (`meshcore_bot.db`) and live radio. There are no sample datasets, testimonials, or usage metrics, and none should be invented.
- Docs: `docs/web-viewer.md` and the per-feature pages under `docs/`.

## Product Principles

1. **Ground truth over guesses.** Show what this radio observed, and be honest about provenance and uncertainty; never present inferred mesh data as confirmed.
2. **Glance first, depth on demand.** Health should be readable in seconds, with the detailed mesh and traffic views a click away.
3. **Every operator job works on a phone.**
4. **Respect the host and the air.** Light pages, light polling, nothing that quietly costs Pi CPU or mesh airtime.
5. **Public views stay safe.** Anything shown to anonymous visitors is mesh-visible or aggregate data only: no channel keys, config, logs, or private messages.
