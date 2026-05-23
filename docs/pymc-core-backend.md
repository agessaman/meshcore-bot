# pyMC Core Backend

The bot can run against `pyMC_core` CompanionRadio instead of `meshcore_py` by setting:

```ini
[Connection]
connection_type = pymc
pymc_radio_type = kiss-modem
pymc_serial_port = /dev/ttyUSB0
pymc_baudrate = 115200
pymc_identity_file = pymc_identity.key
```

The v1 backend supports MeshCore KISS modem firmware through `KissModemWrapper`. The backend lazy-loads `pymc_core`; install it normally or, for local development, install the checkout:

```bash
pip install -e /Users/adam/pymc_core
```

If that local checkout exists, the bot also adds `/Users/adam/pymc_core/src` as a fallback import path when the pyMC backend is selected.

## Identity

The bot uses a local pyMC identity file. If `pymc_identity_file` does not exist, the backend creates a 32-byte seed, writes it as hex, and sets file permissions to `0600` where supported. Reusing the same file keeps the bot's public key stable across restarts.

## Contacts

pyMC contacts are hydrated from the bot database table `complete_contact_tracking` at startup. Incoming adverts update both the in-memory `CompanionRadio` contact store and the database. The pyMC backend does not use firmware contact slots, so repeater/contact purge pressure is not applied to this backend.

The table does not persist every pyMC `ContactStore` field yet. Basic name, public key, type, location, path, and advert packet data are stored; firmware-style contact replay features may need a future schema extension if stricter persistence is required.

## Radio Settings

Common KISS modem settings:

```ini
pymc_frequency = 910525000
pymc_bandwidth = 62500
pymc_spreading_factor = 7
pymc_coding_rate = 5
pymc_tx_power = 22
pymc_tx_delay_ms = 50
pymc_kiss_full_duplex = false
pymc_lbt_enabled = false
```

`pymc_sync_word` is intentionally not used for KISS modem configuration because the MeshCore KISS modem firmware controls sync word behavior.

## Manual Smoke Test

1. Start with `config-pymc.ini` or set `connection_type = pymc`.
2. Confirm startup logs show `Connecting to pyMC KISS modem`.
3. Confirm an identity file is created or loaded.
4. Send a startup advert and verify another node hears it.
5. Send `ping` to the bot by DM and verify the bot replies.
6. Send `ping` on a configured channel and verify the bot replies on that channel.
7. Wait for an advert from another node, restart the bot, and confirm the contact is loaded from `complete_contact_tracking`.
8. Exercise web viewer radio actions for stats, path hash mode, TX power, and radio params where supported.

## Feature Notes

The backend maps existing bot commands to `CompanionRadio` where possible: DMs, channel sends, adverts, channels, contact CRUD, stats, radio params, path hash mode, custom variables, status, telemetry, and trace send. If pyMC_core lacks a direct equivalent for a specific firmware command, the backend returns a structured error instead of shelling out to `meshcore-cli`.
