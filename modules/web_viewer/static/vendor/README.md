# Vendored front-end libraries

Served from here instead of a CDN so the web viewer works on a LAN with no internet access. Each file is the unmodified upstream build from npm (fetched through unpkg), except that the trailing `sourceMappingURL` comment is removed because the source maps aren't vendored.

| Directory | Package | Version | License |
|---|---|---|---|
| `leaflet/` | `leaflet` (`dist/leaflet.js`, `dist/leaflet.css`, `dist/images/`) | 1.9.4 | BSD-2-Clause |
| `maplibre-gl/` | `maplibre-gl` (`dist/maplibre-gl.js`, `dist/maplibre-gl.css`) | 5.6.0 | BSD-3-Clause |
| `maplibre-gl-leaflet/` | `@maplibre/maplibre-gl-leaflet` (`leaflet-maplibre-gl.js`) | 0.1.4 | ISC |
| `js-sha256/` | `js-sha256` (`build/sha256.min.js`) | 0.11.0 | MIT |
| `vis-network/` | `vis-network` (`standalone/umd/vis-network.min.js`, `styles/vis-network.min.css`) | 9.1.9 | Apache-2.0 or MIT |

The login page's fonts live in `../fonts/`: DM Sans and Outfit variable fonts, Latin subset, from `@fontsource-variable/dm-sans` and `@fontsource-variable/outfit` 5.3.0 (SIL Open Font License 1.1).

To upgrade, replace the files from the same package paths at the new version, strip the source-map comment, and update the version here and in the templates' comments.
