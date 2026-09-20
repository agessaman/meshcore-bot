# Custom command reference website

Use the **`generate_website.py`** script to build a single-page HTML command reference from your `config.ini`. The output lists your enabled commands, keywords, usage, and channel restrictions so you can host it on your own site for users.

## Basic usage

```bash
python generate_website.py [config.ini]
```

This reads your config (default: `config.ini`), loads your commands and channels, and writes **`website/index.html`** in the same directory as the config file. Upload that file (and the directory if you use assets) to your web host.

## Which commands get listed

The page lists what your bot actually answers:

- **Local commands are included.** Any command installed under `[Bot] local_dir_path` (default `local/commands`) appears alongside the built-in ones.
- **Disabled commands are omitted.** A command turned off with `enabled = false` in its `[*_Command]` section is left out, including the legacy spellings such as `[Jokes] joke_enabled`.
- **The local config overlay is read.** Settings saved from the web viewer's plugin settings page land in `<local_dir_path>/config.ini`, and the generator reads that on top of your base config just as the bot does.

Admin-only, hidden, and keyword-less commands are never listed.

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

## Add custom CSS

You can layer your own CSS on top of the built-in style chosen with `--style` (`default` if you don't pass one). The built-in CSS always loads first, so you only write the rules you want to change, and your rules win over built-in rules of equal specificity.

### Link to external CSS

Use `--link-css` to reference a stylesheet hosted on your server or a CDN:

```bash
python generate_website.py config.ini --link-css https://example.com/my-custom.css
python generate_website.py config.ini --style minimalist --link-css https://example.com/overrides.css
```

The `<link>` tag is placed after the built-in `<style>` block. A relative path such as `--link-css overrides.css` is resolved by the browser against `website/index.html`, so upload the stylesheet next to it.

### Embed CSS from a file

Use `--embed-css` to inline CSS from a local file into the generated HTML:

```bash
python generate_website.py config.ini --embed-css tweaks.css
python generate_website.py config.ini --style terminal --embed-css tweaks.css
```

The file's contents are appended to the built-in `<style>` block, so the page stays a single self-contained file. The path is relative to the directory you run the script from. If the file can't be read, the script exits with an error instead of generating the page.

This is useful for:
- Small tweaks to a built-in style (colors, fonts, spacing)
- Offline deployments, or hosts where you can only upload one file

### Using both

You can pass `--embed-css` and `--link-css` together. The page loads the built-in style, then the embedded CSS, then the linked stylesheet, so the linked stylesheet wins any conflict.

### Example: Custom color scheme

Create a file `my-colors.css`:

```css
:root {
    --bg-primary: #1a1a2e;
    --bg-card: #16213e;
    --accent-blue: #0f3460;
    --accent-cyan: #e94560;
    --text-primary: #eee;
}
```

Then generate with:

```bash
python generate_website.py config.ini --style default --embed-css my-colors.css
```

This keeps all the layout and styling from the default theme but applies your custom color palette.

### Example workflow: Fine-tuning a built-in style

1. Generate with a built-in style you like:
   ```bash
   python generate_website.py config.ini --style minimalist
   ```

2. Inspect the output and identify what you want to change (e.g., "I want blue accents instead of the default colors")

3. Create a small override file `tweaks.css` with just the changes:
   ```css
   :root {
       --accent-blue: #2563eb;
       --accent-cyan: #0891b2;
   }
   ```

4. Regenerate with your tweaks:
   ```bash
   python generate_website.py config.ini --style minimalist --embed-css tweaks.css
   ```

This approach is much easier than writing CSS from scratch, since you only override specific properties while keeping all the responsive design, layouts, and other styling intact.

## Preview all styles

To generate a sample page for every style plus an index that links to them (useful to pick a theme):

```bash
python generate_website.py config.ini --sample
```

Output goes to `website/` with one HTML file per style and an `index.html` you can open locally. `--link-css` and `--embed-css` apply to every sample page, so you can preview your overrides against each style.

## Custom title and intro

Optional `[Website]` section in `config.ini`:

```ini
[Website]
website_title = My Mesh Bot - Commands
introduction_text = Welcome! Here are the commands you can use on the mesh.
```

If omitted, the script uses the bot name and a default intro.

## Uploading

The script produces a self-contained HTML file (with embedded CSS). Upload `website/index.html` to any static host (e.g. GitHub Pages, Netlify, or your group's web server). No server-side processing is required. If you used `--link-css` with a relative path, upload that stylesheet alongside `index.html`.

## CSS architecture and customization

The generated HTML includes embedded CSS that uses a **CSS custom properties (variables) system** for easy theming. This section documents the CSS architecture and how to create your own custom styles.

### CSS structure overview

The stylesheet is organized into several sections:

1. **CSS Custom Properties (`:root`)** - Theme variables for colors, fonts, spacing
2. **Reset & Base Styles** - Global resets and base element styles
3. **Layout Components** - Container, grid, sidebar navigation
4. **Header & Introduction** - Title area and intro text
5. **Command Cards** - Individual command display cards
6. **Channel Cards** - Channel listing cards
7. **Mobile Responsive** - Breakpoints and mobile menu
8. **Theme Overrides** - Style-specific customizations

### CSS custom properties (theme variables)

Most colors are defined as CSS custom properties in the `:root` selector, so you can retheme a page without touching the rest of the CSS. A few effects (background gradients, badge tints) and some style-specific overrides use literal colors instead.

#### Color variables

```css
:root {
    /* Backgrounds */
    --bg-primary: #0a0e14;           /* Main page background */
    --bg-secondary: #111820;         /* Secondary background (usage boxes) */
    --bg-card: #151c25;              /* Card backgrounds */
    --bg-card-hover: #1a232e;        /* Card hover state */

    /* Accent Colors */
    --accent-blue: #00d4ff;          /* Primary accent (command names, links) */
    --accent-cyan: #00ffc8;          /* Secondary accent (keywords, highlights) */
    --accent-orange: #ff8a00;        /* Parameter names */
    --accent-purple: #a855f7;        /* Only used by the neon style */
    --accent-red: #ff4757;           /* Not currently used */
    --accent-yellow: #ffd700;        /* Only used by the terminal style */

    /* Text Colors */
    --text-primary: #e8edf4;         /* Main text color */
    --text-secondary: #8892a4;       /* Secondary text (descriptions) */
    --text-muted: #6b7280;           /* Muted text (labels, footers) */

    /* Borders & Effects */
    --border-subtle: rgba(255,255,255,0.06);  /* Subtle borders */
    --glow-blue: rgba(0, 212, 255, 0.15);     /* Only used by the neon style */
    --glow-cyan: rgba(0, 255, 200, 0.1);      /* Not currently used */
}
```

#### Typography variables

Fonts are loaded via Google Fonts and applied through the CSS. The default theme uses:

- **Outfit** - Sans-serif for body text and headings
- **JetBrains Mono** - Monospace for code, keywords, and technical text

### HTML structure and CSS classes

#### Page layout

```html
<body>
  <div class="atmosphere"></div>      <!-- Background gradient effect -->
  <div class="grid-overlay"></div>    <!-- Subtle grid pattern -->
  <div class="sidebar-overlay"></div> <!-- Mobile menu overlay -->

  <button class="mobile-menu-toggle">  <!-- Mobile hamburger menu -->
    <span class="hamburger"></span>
    <span class="hamburger"></span>
    <span class="hamburger"></span>
  </button>

  <div class="container">
    <nav class="sidebar-nav">        <!-- Left sidebar navigation -->
      <div class="nav-header">
        <h3>Navigation</h3>
      </div>
      <ul class="nav-list">
        <li class="nav-section-header">Commands</li>
        <ul class="nav-sublist">
          <li><a href="#..." class="nav-link nav-sublink">Category</a></li>
        </ul>
      </ul>
    </nav>

    <div class="main-content">
      <header>
        <div class="header-content">
          <div class="header-title">
            <h1>Bot Name</h1>
          </div>
          <div class="intro">Introduction text...</div>
        </div>
      </header>

      <main>
        <!-- Command categories -->
        <div class="category-section" id="commands-category">
          <h2 class="category-title">
            <a href="#..." class="anchor-link">Category Name</a>
          </h2>
          <div class="commands-grid">
            <!-- Command cards go here -->
          </div>
        </div>

        <!-- Channels section -->
        <div class="category-section" id="channels">
          <h2 class="category-title">
            <a href="#channels" class="anchor-link">Available Channels</a>
          </h2>
          <p class="channels-intro">...</p>
          <div class="channel-category">
            <h3 class="channel-category-title">...</h3>
            <div class="channels-grid">
              <!-- Channel cards go here -->
            </div>
          </div>
        </div>
      </main>

      <footer>
        <p>Generated command reference...</p>
      </footer>
    </div>
  </div>
</body>
```

#### Command card structure

```html
<div class="command-card">
  <div class="command-header">
    <h3 class="command-name">commandname</h3>
    <div class="command-keywords">
      <span class="keyword-badge">alias1</span>
      <span class="keyword-badge">alias2</span>
      <span class="keyword-badge keyword-expand" data-hidden="...">+3 more</span>
    </div>
  </div>

  <p class="command-description">Command description text</p>

  <div class="command-usage">
    <code>!commandname [args]</code>
  </div>

  <div class="command-params">
    <div class="params-header">Parameters:</div>
    <div class="param-item">
      <span class="param-name">param1</span>
      <span class="param-desc">Description</span>
    </div>
  </div>

  <div class="command-subcommands">
    <div class="subcommands-header">Sub-commands:</div>
    <div class="subcommand-item">
      <span class="subcommand-name">subcmd</span>
      <span class="subcommand-desc">Description</span>
    </div>
  </div>

  <div class="command-channels">Channel: #specific-channel</div>
</div>
```

#### Channel card structure

```html
<div class="channel-card">
  <div class="channel-name">#channel-name</div>
  <div class="channel-description">Channel description text</div>
</div>
```

### Key CSS classes reference

#### Layout classes

- `.container` - Main content wrapper with grid layout
- `.sidebar-nav` - Left sidebar navigation (sticky on desktop)
- `.main-content` - Right side main content area
- `.mobile-menu-toggle` - Hamburger menu button (mobile only)
- `.sidebar-overlay` - Dark overlay when mobile menu is open

#### Navigation classes

- `.nav-header` - Navigation section header
- `.nav-list` - Main navigation list
- `.nav-section-header` - Section divider in nav (e.g., "Commands", "Channels")
- `.nav-sublist` - Nested navigation list
- `.nav-link` - Navigation link item
- `.nav-sublink` - Nested/indented navigation link

#### Header classes

- `.header-content` - Header wrapper with background and border
- `.header-title` - Title container
- `.intro` - Introduction text paragraph
- `.channel-highlight` - Highlighted channel names in intro text

#### Content organization classes

- `.category-section` - Wrapper for each command category
- `.category-title` - Category heading (e.g., "Weather Commands")
- `.anchor-link` - Linkable heading anchor
- `.commands-grid` - CSS Grid container for command cards
- `.channels-grid` - CSS Grid container for channel cards

#### Command card classes

- `.command-card` - Individual command card container
- `.command-header` - Command name and keywords section
- `.command-name` - Command name heading
- `.command-keywords` - Container for keyword badges
- `.keyword-badge` - Individual keyword/alias badge
- `.keyword-expand` - "+X more" expandable badge
- `.keyword-hidden` - Hidden keywords (shown on expand)
- `.command-description` - Command description paragraph
- `.command-usage` - Usage syntax box
- `.command-params` - Parameters section wrapper
- `.params-header` - "Parameters:" label
- `.param-item` - Individual parameter row
- `.param-name` - Parameter name
- `.param-desc` - Parameter description
- `.command-subcommands` - Sub-commands section wrapper
- `.subcommands-header` - "Sub-commands:" label
- `.subcommand-item` - Individual sub-command row
- `.subcommand-name` - Sub-command name
- `.subcommand-desc` - Sub-command description
- `.command-channels` - Channel restriction notice

#### Channel card classes

- `.channel-category` - Channel category wrapper
- `.channel-category-title` - Channel category heading
- `.channels-intro` - Introduction text for channels section
- `.channel-card` - Individual channel card
- `.channel-name` - Channel name (e.g., "#general")
- `.channel-description` - Channel description text

#### Utility classes

- `.atmosphere` - Background gradient effect layer
- `.grid-overlay` - Subtle grid pattern overlay
- `.hamburger` - Hamburger menu bar element

### Responsive breakpoints

The CSS includes two main responsive breakpoints:

- **1200px** - Switches to mobile layout, shows hamburger menu, sidebar becomes slide-out
- **768px** - Further mobile optimizations for small screens

Mobile behavior:
- Sidebar slides in from left when hamburger is clicked
- Overlay appears over main content
- Grid layouts adjust to single column
- Font sizes scale down slightly

### Tips for custom styles

1. **Start with variables** - Most visual changes can be achieved by only changing the CSS custom properties in `:root`

2. **Use the sample generator** - Run `--sample` with your `--embed-css` or `--link-css` flag to see your overrides on every built-in style

3. **Respect the structure** - The HTML structure and class names are semantic and should remain consistent

4. **Test mobile** - Always check your custom styles at mobile breakpoints (< 1200px, < 768px)

5. **Consider accessibility** - Ensure sufficient color contrast (WCAG AA: 4.5:1 for normal text, 3:1 for large text)

6. **Use monospace for code** - Keep `JetBrains Mono` or similar for `.keyword-badge`, `.command-usage code`, `.param-name`, etc.

7. **Preserve hover states** - Interactive elements should have clear hover/focus states for usability

8. **Match `!important` where needed** - The base CSS uses `!important` on many mobile-layout rules, and the brutalist, pixel, and minimalist styles use it heavily in their overrides. A custom rule only beats one of those if it's also `!important`.

9. **Some styles hard-code colors** - The gradient and brutalist styles set many colors directly instead of through the variables, so changing only the `:root` variables has less effect on them.

### Example style variations

#### Dark high-contrast
```css
:root {
    --bg-primary: #000000;
    --bg-card: #0a0a0a;
    --accent-blue: #00ffff;
    --accent-cyan: #00ff00;
    --text-primary: #ffffff;
    --border-subtle: rgba(255,255,255,0.2);
}
```

#### Light professional
```css
:root {
    --bg-primary: #ffffff;
    --bg-card: #f5f5f5;
    --accent-blue: #0066cc;
    --accent-cyan: #0088cc;
    --text-primary: #333333;
    --text-secondary: #666666;
    --border-subtle: rgba(0,0,0,0.1);
}
```

#### Warm earth tones
```css
:root {
    --bg-primary: #2c2416;
    --bg-card: #3d2f1f;
    --accent-blue: #d4a574;
    --accent-cyan: #c9a961;
    --accent-orange: #e6925b;
    --text-primary: #f4e8d8;
    --text-secondary: #b89968;
}
```

### JavaScript functionality

The generated HTML includes minimal JavaScript for:

1. **Mobile menu toggle** - Shows/hides sidebar navigation on mobile
2. **Keyword expansion** - Expands "+X more" keyword badges when clicked

Smooth scrolling to anchors comes from the CSS `scroll-behavior` property, not JavaScript.

These behaviors are built-in and don't require customization for basic styling changes.
