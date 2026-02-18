# Custom command reference website

Use the **`generate_website.py`** script to build a single-page HTML command reference from your `config.ini`. The output lists your enabled commands, keywords, usage, and channel restrictions so you can host it on your own site for users.

## Basic usage

```bash
python generate_website.py [config.ini]
```

This reads your config (default: `config.ini`), loads your commands and channels, and writes **`website/index.html`** in the same directory as the config file. Upload that file (and the directory if you use assets) to your web host.

## Choose a style

The `--style` option selects a theme (colors, typography, layout). Default is `default` (modern dark). To see all themes:

```bash
python generate_website.py --list-styles
```

Then generate with a specific style:

```bash
python generate_website.py config.ini --style minimalist
python generate_website.py config.ini --style terminal
```

Available styles include: **default** (modern dark), **minimalist** (light, clean), **terminal** (green/amber on black), **glass** (glassmorphism), **neon** (cyberpunk), **brutalist** (bold, high contrast), **gradient** (colorful gradients), **pixel** (retro gaming). Run `--list-styles` for the full list and short descriptions.

## Preview all styles

To generate a sample page for every style plus an index that links to them (useful to pick a theme):

```bash
python generate_website.py config.ini --sample
```

Output goes to `website/` with one HTML file per style and an `index.html` you can open locally.

## Custom title and intro

Optional `[Website]` section in `config.ini`:

```ini
[Website]
website_title = My Mesh Bot - Commands
introduction_text = Welcome! Here are the commands you can use on the mesh.
```

If omitted, the script uses the bot name and a default intro.

## Uploading

The script produces a self-contained HTML file (with embedded CSS). Upload `website/index.html` to any static host (e.g. GitHub Pages, Netlify, or your group's web server). No server-side processing is required.
