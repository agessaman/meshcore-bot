# Radio reliability (zombie detection and radio-offline)

These features help when the serial/BLE/TCP link to the radio is misleadingly “up” or when outbound sends fail repeatedly.

## Zombie radio

A **zombie** connection means the transport is still open and you may still receive RF traffic, but the **firmware has stopped ACKing commands** (outbound operations time out with no response). Recovery is usually a **physical power cycle** of the radio; disconnecting USB or reconnecting BLE often does not help.

The bot probes health with periodic **`get_time()`** requests (`asyncio.wait_for` with a short timeout). Values are configured under `[Bot]`:

| Option | Meaning |
|--------|---------|
| `radio_probe_interval_seconds` | Seconds between probes; **clamped to 300–900** (5–15 minutes). |
| `radio_probe_fail_threshold` | Consecutive failed probes before declaring zombie state and logging CRITICAL. |
| `radio_zombie_alert_enabled` | If `true`, send an immediate alert email when zombie state is confirmed (SMTP must be set in web viewer notifications). |
| `radio_zombie_alert_email` | Comma-separated recipients; if empty, nightly maintenance recipients are used. |

Web UI metadata keys `zombie.alert_enabled` and `zombie.alert_email` override config when set (see web viewer API below).

While zombie state is active, the bot **suppresses outbound sends** from command handling and the scheduler until recovery.

## Radio-offline (send timeouts)

**Radio-offline** is different: the radio may still be responsive, but **outbound mesh sends are repeatedly timing out** at the scheduler level. Inbound packets may still arrive.

Detection is based on **consecutive failures** when the scheduler runs **scheduled channel messages** and **interval flood adverts** (the synchronous wrappers that call `future.result(timeout=60)` and record failures). It does **not** currently increment the offline counter from every interactive command reply path.

| Option | Meaning |
|--------|---------|
| `send_timeout_seconds` | Max seconds for `asyncio.wait_for` around `send_channel_message` in scheduled sends. |
| `radio_offline_threshold` | Consecutive scheduler send failures before entering offline state. |
| `radio_offline_alert_enabled` | Email alert when offline state is entered. |
| `radio_offline_alert_email` | Recipients; if empty, nightly recipients are used. |

While offline, the scheduler **suppresses** scheduled messages and interval adverts; **zombie** handling still applies to command sends. Clear the offline flag from the web viewer or after a successful send path as implemented.

## `radio_debug`

When `radio_debug = true` under `[Connection]` (or metadata `radio.debug` from the web UI), the bot enables verbose meshcore library logging. **Very noisy** — use for diagnosis, not routine production.

## Web viewer HTTP API (automation)

These JSON endpoints exist for dashboards and scripts; not all have a dedicated UI tab yet.

| Method | Path | Purpose |
|--------|------|---------|
| GET/POST | `/api/config/zombie-alert` | Read/write zombie alert email settings (metadata and optional `config.ini` write-back). |
| GET/POST | `/api/config/radio-debug` | Read/write `radio.debug` metadata and optional `config.ini` + reconnect queue. |
| POST | `/api/admin/zombie-recover` | Clear zombie flags after hardware recovery. |
| POST | `/api/admin/radio-offline-clear` | Clear radio-offline flags. |

POST requests from browser code should include `X-Requested-With: XMLHttpRequest` where the CSRF guard expects it.
