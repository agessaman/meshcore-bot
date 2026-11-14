// Copy the web viewer's front-end libraries from node_modules into
// modules/web_viewer/static/, at the versions pinned in package.json.
//
//   cd scripts/web-vendor && npm run vendor
//
// Every file is the unmodified npm build except that a trailing
// sourceMappingURL comment is removed, since the source maps aren't vendored.
// Each destination directory is owned by this script: it is emptied before the
// copy, so a file dropped upstream doesn't linger. The version table in
// static/vendor/README.md is regenerated from the installed packages.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATIC = path.resolve(HERE, '../../modules/web_viewer/static');
const README = path.join(STATIC, 'vendor/README.md');

// package -> { destination file under static/: source file in the package }
const LIBRARIES = {
  bootstrap: {
    'vendor/bootstrap/bootstrap.min.css': 'dist/css/bootstrap.min.css',
    'vendor/bootstrap/bootstrap.bundle.min.js': 'dist/js/bootstrap.bundle.min.js',
    'vendor/bootstrap/LICENSE': 'LICENSE',
  },
  // The CSS lists a .ttf after each .woff2; every browser the viewer supports
  // takes the woff2, so the .ttf files are left out.
  '@fortawesome/fontawesome-free': {
    'vendor/fontawesome/css/all.min.css': 'css/all.min.css',
    'vendor/fontawesome/webfonts/fa-brands-400.woff2': 'webfonts/fa-brands-400.woff2',
    'vendor/fontawesome/webfonts/fa-regular-400.woff2': 'webfonts/fa-regular-400.woff2',
    'vendor/fontawesome/webfonts/fa-solid-900.woff2': 'webfonts/fa-solid-900.woff2',
    'vendor/fontawesome/webfonts/fa-v4compatibility.woff2': 'webfonts/fa-v4compatibility.woff2',
    'vendor/fontawesome/LICENSE.txt': 'LICENSE.txt',
  },
  'socket.io-client': {
    'vendor/socket.io/socket.io.min.js': 'dist/socket.io.min.js',
    'vendor/socket.io/LICENSE': 'LICENSE',
  },
  // chart.umd.js is already minified; there is no .min.js in the package.
  'chart.js': {
    'vendor/chart.js/chart.umd.js': 'dist/chart.umd.js',
    'vendor/chart.js/LICENSE.md': 'LICENSE.md',
  },
  leaflet: {
    'vendor/leaflet/leaflet.js': 'dist/leaflet.js',
    'vendor/leaflet/leaflet.css': 'dist/leaflet.css',
    'vendor/leaflet/images/layers.png': 'dist/images/layers.png',
    'vendor/leaflet/images/layers-2x.png': 'dist/images/layers-2x.png',
    'vendor/leaflet/images/marker-icon.png': 'dist/images/marker-icon.png',
    'vendor/leaflet/images/marker-icon-2x.png': 'dist/images/marker-icon-2x.png',
    'vendor/leaflet/images/marker-shadow.png': 'dist/images/marker-shadow.png',
    'vendor/leaflet/LICENSE': 'LICENSE',
  },
  'maplibre-gl': {
    'vendor/maplibre-gl/maplibre-gl.js': 'dist/maplibre-gl.js',
    'vendor/maplibre-gl/maplibre-gl.css': 'dist/maplibre-gl.css',
    'vendor/maplibre-gl/LICENSE.txt': 'LICENSE.txt',
  },
  '@maplibre/maplibre-gl-leaflet': {
    'vendor/maplibre-gl-leaflet/leaflet-maplibre-gl.js': 'leaflet-maplibre-gl.js',
    'vendor/maplibre-gl-leaflet/LICENSE': 'LICENSE',
  },
  'vis-network': {
    'vendor/vis-network/vis-network.min.js': 'standalone/umd/vis-network.min.js',
    'vendor/vis-network/vis-network.min.css': 'styles/vis-network.min.css',
    'vendor/vis-network/LICENSE-MIT': 'LICENSE-MIT',
  },
  'js-sha256': {
    'vendor/js-sha256/sha256.min.js': 'build/sha256.min.js',
    'vendor/js-sha256/LICENSE.txt': 'LICENSE.txt',
  },
  // The login page's fonts: variable, Latin subset.
  '@fontsource-variable/dm-sans': {
    'fonts/dm-sans-latin-opsz-normal.woff2': 'files/dm-sans-latin-opsz-normal.woff2',
    'fonts/LICENSE-DM-Sans.txt': 'LICENSE',
  },
  '@fontsource-variable/outfit': {
    'fonts/outfit-latin-wght-normal.woff2': 'files/outfit-latin-wght-normal.woff2',
    'fonts/LICENSE-Outfit.txt': 'LICENSE',
  },
};

const SOURCE_MAP_COMMENT = /(?:\/\/# sourceMappingURL=\S+|\/\*# sourceMappingURL=\S+ \*\/)\s*$/;

function packageDir(name) {
  return path.join(HERE, 'node_modules', name);
}

function readManifest(name) {
  return JSON.parse(fs.readFileSync(path.join(packageDir(name), 'package.json'), 'utf8'));
}

function topDir(dest) {
  const parts = dest.split('/');
  return parts[0] === 'vendor' ? `vendor/${parts[1]}` : parts[0];
}

const pinned = JSON.parse(fs.readFileSync(path.join(HERE, 'package.json'), 'utf8')).dependencies;
for (const name of Object.keys(LIBRARIES)) {
  if (!(name in pinned)) throw new Error(`${name} is not a dependency in package.json`);
  const installed = readManifest(name).version;
  if (installed !== pinned[name]) {
    throw new Error(`${name} ${installed} is installed but package.json pins ${pinned[name]}; run npm ci`);
  }
}
for (const name of Object.keys(pinned)) {
  if (!(name in LIBRARIES)) throw new Error(`${name} is in package.json but has no files listed in sync.mjs`);
}

const owned = new Set(Object.values(LIBRARIES).flatMap((files) => Object.keys(files).map(topDir)));
for (const dir of owned) fs.rmSync(path.join(STATIC, dir), { recursive: true, force: true });

for (const [name, files] of Object.entries(LIBRARIES)) {
  for (const [dest, src] of Object.entries(files)) {
    const out = path.join(STATIC, dest);
    fs.mkdirSync(path.dirname(out), { recursive: true });
    const from = path.join(packageDir(name), src);
    if (/\.(js|css)$/.test(src)) {
      fs.writeFileSync(out, fs.readFileSync(from, 'utf8').replace(SOURCE_MAP_COMMENT, ''));
    } else {
      fs.copyFileSync(from, out);
    }
  }
}

const rows = Object.entries(LIBRARIES).map(([name, files]) => {
  const { version, license } = readManifest(name);
  const dirs = [...new Set(Object.keys(files).map(topDir))].map((d) => `\`${d}/\``).join(', ');
  return `| \`${name}\` | ${version} | ${license} | ${dirs} |`;
});
const table = [
  '<!-- vendor-table:start (generated by scripts/web-vendor/sync.mjs) -->',
  '| Package | Version | License | Served from `static/` |',
  '|---|---|---|---|',
  ...rows,
  '<!-- vendor-table:end -->',
].join('\n');
const readme = fs.readFileSync(README, 'utf8');
const marked = /<!-- vendor-table:start[^>]*-->[\s\S]*?<!-- vendor-table:end -->/;
if (!marked.test(readme)) throw new Error(`${README} has no vendor-table markers`);
fs.writeFileSync(README, readme.replace(marked, table));

console.log(`Vendored ${Object.keys(LIBRARIES).length} packages into ${path.relative(process.cwd(), STATIC)}/`);
