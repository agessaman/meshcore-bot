# Changelog

## 1.1.0

### Added

- First release of the MeshCore Bot add-on. The sidebar opens the web viewer through Home Assistant ingress, and the viewer port can be published for access outside Home Assistant.
- Options for the radio connection (serial, TCP, or Bluetooth Low Energy), bot name, log level, and web viewer password. Empty options leave `config.ini` alone; set options override the matching keys on each start.
- `config.ini` seeded from the example on first start, custom plugins and translations in `/config/local`, and the database and configuration backups under `/data`.
