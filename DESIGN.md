---
name: MeshCore Bot Web Viewer
description: The operating console for one MeshCore bot and the radio it listens through.
colors:
  signal-blue: "#0d6efd"
  console-black: "#212529"
  paper: "#ffffff"
  paper-raised: "#f8f9fa"
  paper-sunk: "#e9ecef"
  ink: "#212529"
  ink-muted: "#5f666d"
  rule: "#dee2e6"
  night: "#1a1a1a"
  night-raised: "#2d2d2d"
  night-sunk: "#3d3d3d"
  night-ink: "#e9ecef"
  night-ink-muted: "#adb5bd"
  night-rule: "#495057"
  lamp-green: "#198754"
  lamp-red: "#dc3545"
  lamp-amber: "#ffc107"
  status-text-success: "#146c43"
  status-text-warning: "#7a5d02"
  status-text-danger: "#b02a37"
  status-text-info: "#0a6c80"
  night-status-text-success: "#75b798"
  night-status-text-warning: "#ffda6a"
  night-status-text-danger: "#f1aeb5"
  night-status-text-info: "#6edff6"
  night-signal-blue-text: "#6ea8fe"
  route-direct: "#20c997"
  scope-black: "#0d1117"
  scope-rule: "#30363d"
  scope-ink: "#c9d1d9"
typography:
  headline:
    fontFamily: "DM Sans, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "calc(1.375rem + 1.5vw)"
    fontWeight: 500
    lineHeight: 1.2
  title:
    fontFamily: "DM Sans, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "1rem"
    fontWeight: 600
    lineHeight: 1.5
  body:
    fontFamily: "DM Sans, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
  readout:
    fontFamily: "DM Sans, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "1.6rem"
    fontWeight: 700
    lineHeight: 1.1
    fontFeature: "tnum"
  label:
    fontFamily: "DM Sans, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "0.7rem"
    fontWeight: 600
    letterSpacing: "0.04em"
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
rounded:
  hair: "2px"
  sm: "0.25rem"
  md: "0.375rem"
  lg: "8px"
  xl: "10px"
  meter: "5px"
  pill: "999px"
spacing:
  "1": "0.25rem"
  "2": "0.5rem"
  "3": "1rem"
  "4": "1.5rem"
  "5": "3rem"
components:
  button-primary:
    backgroundColor: "{colors.signal-blue}"
    textColor: "{colors.paper}"
    rounded: "{rounded.md}"
    padding: "0.375rem 0.75rem"
  button-primary-sm:
    backgroundColor: "{colors.signal-blue}"
    textColor: "{colors.paper}"
    rounded: "{rounded.sm}"
    padding: "0.25rem 0.5rem"
  button-console:
    backgroundColor: "{colors.console-black}"
    textColor: "{colors.paper}"
    rounded: "{rounded.sm}"
    padding: "0.25rem 0.5rem"
  card:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "1rem"
  card-header:
    backgroundColor: "{colors.paper-raised}"
    textColor: "{colors.ink}"
    typography: "{typography.title}"
    padding: "0.5rem 1rem"
  stat-tile:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.xl}"
    padding: "0.75rem 0.9rem 0.5rem"
  health-strip:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.xl}"
    padding: "0.5rem 1rem"
    height: "56px"
  input:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "0.375rem 0.75rem"
  navbar:
    backgroundColor: "{colors.console-black}"
    textColor: "{colors.paper}"
  status-lamp:
    size: "12px"
    rounded: "{rounded.pill}"
  delta-chip:
    backgroundColor: "{colors.paper-sunk}"
    textColor: "{colors.ink-muted}"
    rounded: "{rounded.pill}"
    padding: "0.05rem 0.35rem"
  log-console:
    backgroundColor: "{colors.scope-black}"
    textColor: "{colors.scope-ink}"
    typography: "{typography.mono}"
    rounded: "{rounded.md}"
---

# Design System: MeshCore Bot Web Viewer

## Overview

**Creative North Star: "The Shack Console"**

The viewer is the operating desk in a radio shack: a dark rail across the top with the station's name on it, status lamps that say at a glance whether the radio is up, readouts and meters in front of you, and the log console off to one side scrolling what came over the air. Nothing on the desk is decorative. Every surface exists because the operator reads it or reaches for it, and the desk works the same at night as in daylight.

It is calm, readable, and dense. Density is welcome when it stays legible: tight rows, small caps labels, tabular numbers, many readings per screen. Calm means high contrast, little ornament, and color reserved for meaning, so a red lamp is the loudest thing on the page because nothing else is shouting. The base is Bootstrap 5, and the viewer should still feel like a well-kept self-hosted admin tool, tightened into compact instrument controls rather than restyled into something unfamiliar.

The console has two lighting conditions, light and dark, and both are first-class. Every hard-coded color has its other-theme counterpart, and every surface comes from the theme tokens declared in `base.html`.

**Key Characteristics:**
- A console-black top rail with the operator's bot name in Signal Blue.
- Status lamps (green, amber, red) carry health; color is reserved for state and data, never decoration.
- Compact controls: small buttons, dense rows, uppercase micro-labels, tabular-numeral readouts.
- Faint, ambient lift under cards and tiles; borders do the structural work.
- A terminal-style log console with per-level line colors, in a light or dark set to match the theme.
- Light and dark themes kept in lockstep through shared tokens.

## Colors

A neutral Bootstrap-gray desk with one blue signal and a set of lamp colors that only ever mean state.

### Primary
- **Signal Blue** (`signal-blue`): the single accent. Primary buttons, links, focus outlines, the navbar brand, the active refresh affordance, flood-route data, and the info log level in light theme.

### Secondary
- **Route Teal** (`route-direct`): the partner to Signal Blue in route data. Direct routes in charts and route-mix bars, so flood versus direct reads as blue versus teal everywhere.

### Tertiary
- **Lamp Green** (`lamp-green`): connected, up, success, positive deltas.
- **Lamp Amber** (`lamp-amber`): warning and degraded states, and the packet-entry rule in the activity log.
- **Lamp Red** (`lamp-red`): disconnected, error, negative deltas, destructive actions.

### Status text
The lamp hues are fill tones. As text they fail contrast (amber on white is 1.6:1), so text that carries state uses a separate per-theme set, exposed in `base.html` as `--text-success`, `--text-warning`, `--text-danger`, `--text-info`, `--text-primary`, `--link-color` and `--code-color`. Bootstrap's `.text-*` utilities, `.text-muted`, `.form-text`, and the outline buttons all resolve through these, so a page gets the right tone in both themes by using the utility. Light values pass 4.5:1 on paper, paper-raised and paper-sunk; dark values pass on night, night-raised and night-sunk. Solid `bg-info` and `bg-warning` badges carry dark text.

### Neutral
- **Paper** (`paper`) / **Night** (`night`): page background in light and dark.
- **Paper Raised** (`paper-raised`) / **Night Raised** (`night-raised`): card headers, footer, log-entry blocks in light; cards themselves in dark.
- **Paper Sunk** (`paper-sunk`) / **Night Sunk** (`night-sunk`): meter tracks, empty chip fills, connection-info panels, striped and hovered table rows in dark.
- **Ink** (`ink`) / **Night Ink** (`night-ink`): body text.
- **Ink Muted** (`ink-muted`) / **Night Ink Muted** (`night-ink-muted`): labels, secondary values, empty states, inactive icons. Ink Muted is a step darker than Bootstrap's `#6c757d` so it holds 4.5:1 on paper-sunk too. On the console-black rail and table headers, muted text uses the night tone in either theme.
- **Rule** (`rule`) / **Night Rule** (`night-rule`): 1px borders, dividers, the hairline after section titles.
- **Console Black** (`console-black`): the navbar rail in both themes.
- **Scope Black**, **Scope Rule**, **Scope Ink** (`scope-black`, `scope-rule`, `scope-ink`): the log console's own dark surface, border, and default line color, borrowed from a terminal rather than from the page theme.

### Data palettes
Charts use their own validated palettes in `static/js/dashboard.js`, not the lamp colors: `SERIES_PALETTE` (eight categorical hues plus a neutral "other", stepped separately for the light `#ffffff` and dark `#2d2d2d` card surfaces) the payload mix takes each packet type's fixed slot from that same palette, so a type has one color everywhere on the dashboard, and `ROLE_COLORS` gives each role one color that matches the Mesh page's node fills (repeaters Signal Blue, room servers Route Teal), with Unknown and Other in a neutral. Real-time's channel dots draw from 20 per-theme colors (`--ch-0` to `--ch-19`), ten hues kept clear of the lamp red, amber and green at two lightness steps, assigned by hash with probing so up to 20 channels never share one. Log levels have their own light and dark sets in `logs.html` (debug teal, info blue, warning amber, error red, critical magenta or violet).

The Mesh page has its own display palette, declared as `--mesh-*` tokens at the top of `mesh.html`. The map and graph have a light/dark toggle of their own, separate from the site theme, so the tokens switch on `.mesh-dark` rather than `[data-theme]`, and the JS reads them back through `meshColor()`. Every role has a legend on the map:
- **Recency** (`--mesh-age-*`): one magenta-rose hue that fades toward the basemap as a link goes unheard (under 24 hours, 1–7 days, 7–30 days, older), with a neutral gray for unknown. Old means faded, so the lamp colors stay out of it.
- **Heat** (`viridis`): observation counts on a log scale, matplotlib's viridis stops exactly. On the dark map the ramp starts a quarter of the way in, since its near-black end disappears there.
- **Nodes:** repeaters in Signal Blue, room servers in Route Teal. Starred nodes get a Star Gold ring, the color of the star icon: it marks a favorite, not a state.
- **Selection and traced paths** are drawn in ink (near-black on the light map, near-white on the dark), so they never read as data.
- **Direction** from a selected node: the dashboard's series blue (incoming), orange (outgoing) and violet (both ways).
- The stat chips above the map color each value like the thing it counts: repeaters in Signal Blue, edges in the recency hue, observations in viridis.

### Named Rules
**The Lamp Rule.** Green, amber, and red mean connected, degraded, and failed. They never decorate a heading, an icon, or a button that isn't about state.

**The Fill-Is-Not-Text Rule.** A lamp or accent hue used as text goes through its status-text token, never the raw fill hex. Tinted surfaces (message bubbles, delta chips) are a low-alpha wash of the hue over the card, so body text and the status-text tokens keep their contrast on them.

**The Twin Token Rule.** A color that is hard-coded for one theme ships with its `[data-theme="dark"]` (or light) counterpart in the same change. Bootstrap 5.1.3 has no dark theme of its own here; nothing inherits one for free.

**The Legend Rule.** Several chart hues sit under 3:1 against their card, so a series is always identified by a legend or a label as well as by color.

## Typography

**Body Font:** DM Sans (with `system-ui, -apple-system, Segoe UI, Roboto, sans-serif`)
**Mono Font:** `ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace`

**Character:** A clean, slightly geometric sans that stays legible at the console's small sizes, paired with the platform's own monospace for logs, keys, packet hex, and template fields. DM Sans is the chosen viewer-wide face; today only the login page loads it (with Outfit for its title). Both are self-hosted under `static/fonts/` through `static/css/fonts.css` (Latin subset), so rolling DM Sans out to the rest of the viewer is a stylesheet change, not a new dependency; until then the system stack is what renders.

### Hierarchy
- **Headline** (500, Bootstrap's responsive `h1`, 1.2): one page title per page, usually with a Font Awesome icon before it.
- **Title** (600, 1rem, 1.5): card headers and section headers inside pages; Bootstrap `h5`-sized modal titles share this role.
- **Body** (400, 1rem, 1.5): tables, forms, descriptions.
- **Readout** (700, 1.6rem, 1.1, tabular numerals): the big number on a stat tile.
- **Label** (600, 0.7rem, 0.04em tracking, uppercase): stat-tile and health-strip labels. The dashboard section title is its larger sibling (700, 0.8rem, 0.08em tracking, uppercase, followed by a hairline rule).
- **Mono** (400, ~0.875rem): log lines, packet and key fields, template references. Every monospace use reads `--font-mono` from `base.html`, which also feeds Bootstrap's `--bs-font-monospace`, so `.font-monospace`, `code` and `pre` match.

### Named Rules
**The Tabular Rule.** Any number that updates in place (counts, SNR, RSSI, percentages, uptime) uses tabular numerals so the digits don't shuffle while you read them.

**The Small Caps Floor Rule.** Uppercase micro-labels stop at 0.7rem. Nothing in the viewer goes smaller, and the existing 0.7rem labels are the first candidates for a bump when contrast or legibility is in question.

## Layout

Full-width Bootstrap `container-fluid` pages under a collapsing `navbar-expand-lg` rail: the nav collapses to a toggler below 992px. Content is laid out with the Bootstrap grid (`row` / `col-*`) and its spacer scale (0.25, 0.5, 1, 1.5, 3rem), with `mb-4` separating a page title from its content.

The dashboard sets the house rhythm for monitoring pages: a single health strip first, then uppercase section titles with a trailing hairline, then equal-height rows of stat tiles and cards whose charts fill the slack instead of leaving dead space. Rows inside cards (neighbors, leaderboards, mixes) are separated by 1px rules rather than gaps, and use a fixed three-column grid of name, meter, and value.

Responsive behavior follows Bootstrap breakpoints (576, 768, 992px). Below 576px the dashboard tightens its strip and tiles; tables scroll inside `.table-responsive`. Phone use is a real operator workflow, so new layouts are checked at phone width, not just desktop. On a coarse pointer, `base.html` grows standard controls to 44px, small (`-sm`) controls to 36px and checkboxes, radios and switches to 24px; desktop density is untouched. Card headers that pair a title with controls wrap the controls onto a second row rather than squeeze the title.

### Named Rules
**The Fixed Scale Rule.** A meter means the same thing on every refresh: SNR bars use a fixed -12 to +14 dB scale, and a missing reading is drawn hatched, never as an empty bar that reads as zero.

## Elevation & Depth

Soft lift, as now. Cards and tiles sit on a faint ambient shadow and a 1px rule; the shadow says "this is a surface", the border says where it ends. Shadows deepen in dark mode through `--shadow-color` (10% black in light, 30% in dark) rather than through different offsets.

### Shadow Vocabulary
- **Tile lift** (`box-shadow: 0 1px 3px var(--shadow-color)`): stat tiles and the health strip.
- **Card lift** (`box-shadow: 0 2px 4px var(--shadow-color)`): Bootstrap cards, which drop their border and rely on this.
- **Menu lift** (`box-shadow: 0 0.5rem 1rem var(--shadow-color)`): dropdown menus in dark mode.

### Named Rules
**The Quiet Lift Rule.** Shadows are ambient and small. Nothing floats more than a dropdown does; state is shown with color and borders, not with bigger shadows.

## Shapes

Gently rounded, never pill-shaped except where the shape is the signal. Bootstrap's own radii (0.25rem small, 0.375rem standard) cover buttons, inputs, and cards; dashboard tiles and the health strip soften to 10px; panels and responsive tables use 8px. Pills are reserved for delta chips and badges, circles for status lamps, and `hair` (2px) corners for legend swatches and focus rings. Meter tracks and fills use `meter` (5px), half the height of their 10px bar.

## Components

### Buttons
Compact instrument controls on a Bootstrap base.
- **Shape:** standard corners (0.375rem), small corners on `btn-sm` (0.25rem).
- **Primary:** Signal Blue fill, white text. Most page actions use `btn-sm`.
- **Outline / secondary:** Bootstrap outline variants for secondary actions; `btn-outline-light` on the console rail for login and logout.
- **Icon buttons:** the theme toggle and refresh controls are bare icons (no fill) that pick up a faint white or Signal Blue tint on hover, with a visible focus ring.
- **Destructive:** Bootstrap danger variants, always behind a confirmation modal.

### Status lamps
A 12px circle beside a label: green for connected, amber for warning, red for disconnected. They appear in the navbar connection indicator and the dashboard health strip, and they always sit next to words, never alone.

### Cards / Containers
- **Corner Style:** 0.375rem (Bootstrap cards), 10px (stat tiles and health strip).
- **Background:** Paper in light, Night Raised in dark; headers one step raised (Paper Raised / Night Sunk) with a 1px rule underneath and a 600-weight title.
- **Shadow Strategy:** tile lift or card lift, per Elevation & Depth.
- **Internal Padding:** 1rem body, 0.75–0.9rem on tiles.

### Stat tiles and health strip (signature)
The dashboard's instrument faces. A tile is an uppercase label, a large tabular readout with an optional delta chip, a muted sub-line, and a 48px sparkline pinned to the bottom. The health strip is one 56px row of label/value pairs with lamps, a spacer, and a bare refresh button at the end. Delta chips are pills tinted at 12% (22% in dark) of green or red with matching text.

### Inputs / Fields
- **Style:** Bootstrap `form-control` / `form-select`; in dark mode they fill with Night Sunk and take the Night Rule border.
- **Focus:** Signal Blue border (Bootstrap's focus ring in light).
- **Disabled / read-only:** in dark mode, page background with muted text.
- **Template fields:** monospace, with a `{ }` reference button and a live preview beneath.

### Navigation
A console-black `navbar-dark` rail across the top. The brand is a satellite-dish icon plus the operator's `bot_name` in bold Signal Blue. Primary pages are inline links with icons (Dashboard, Realtime, Contacts, Mesh, optional Multibyte, Logs); configuration pages live under one Settings dropdown. The right side holds login/logout, the theme toggle, and the connection lamp. Below 992px the links collapse behind the toggler.

### Log console
A terminal-dark well (Scope Black, Scope Rule border, 0.375rem corners) of monospace lines colored by level, with level toggles that light up in the same color as their lines. In light theme the well and line colors switch to a light set so the page doesn't carry a black hole, but the per-level color mapping is kept.

## Do's and Don'ts

### Do:
- **Do** take every surface, text, and border color from the `base.html` theme variables (`--bg-*`, `--text-*`, `--border-color`, `--card-*`, `--shadow-color`).
- **Do** ship the dark-theme counterpart of any hard-coded color in the same change (The Twin Token Rule).
- **Do** take Mesh page colors from its `--mesh-*` tokens, which follow the map's own theme toggle.
- **Do** use Signal Blue (`#0d6efd`) as the only accent; the older `#007bff` and `#28a745` are retired.
- **Do** use tabular numerals for live numbers and fixed scales for meters.
- **Do** pair every status lamp and chart series with a text label or legend.
- **Do** vendor any new font, script, or stylesheet under `static/` so the viewer works without internet.
- **Do** check new layouts at phone width and in both themes.

### Don't:
- **Don't** use lamp green, amber, or red for decoration (The Lamp Rule).
- **Don't** add another accent hue; route teal and the chart palettes are data colors, not UI colors.
- **Don't** set uppercase labels below 0.7rem.
- **Don't** add new `'Courier New'` uses; monospace comes from the `ui-monospace` stack.
- **Don't** add heavier or more numerous shadows; depth stays at tile, card, and menu lift.
- **Don't** load fonts or libraries from a CDN in new work.
