// Loads MapLibre GL for the mesh page's vector basemap.
//
// MapLibre GL 6 ships ES modules only, but the Leaflet bridge is a classic UMD
// script that reads a global `maplibregl` when it loads. So: import the module,
// publish it as that global, then load the bridge and announce `maplibre-ready`.
// Module scripts run after classic ones, so the page may already have drawn
// its map with the raster fallback; it switches when it hears the event.
import * as maplibregl from '../vendor/maplibre-gl/maplibre-gl.mjs';

window.maplibregl = maplibregl;

const bridge = document.createElement('script');
bridge.src = new URL('../vendor/maplibre-gl-leaflet/leaflet-maplibre-gl.js', import.meta.url).href;
bridge.onload = () => window.dispatchEvent(new Event('maplibre-ready'));
bridge.onerror = () => console.debug('MapLibre Leaflet bridge failed to load; keeping raster tiles');
document.head.appendChild(bridge);
