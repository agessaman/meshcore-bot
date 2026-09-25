# Repeater-Telemetrie (Akku-Überwachung)

Der Dienst `RepeaterTelemetry_Service` fragt in regelmäßigen Abständen den Status
eigener Repeater ab, speichert die Werte in der Bot-Datenbank und warnt, wenn
der Akku schwach wird oder ein Repeater nicht mehr antwortet.

## Ablauf

1. Für jeden konfigurierten Repeater meldet sich der Bot per Login an
   (Admin- oder Gast-Passwort).
2. Danach fordert er den Status an: Akkuspannung (mV), Laufzeit, Grundrauschen,
   RSSI, Paketzähler.
3. Jede Abfrage landet in der Tabelle `repeater_telemetry` (auch Fehlschläge).
4. Anhand der Schwellwerte wird eine Warnung verschickt, und zwar nur bei
   einer **Änderung** des Zustands (ok → Warnung → kritisch → wieder ok).
   Eine noch aktive Warnung wird optional nach `repeat_alert_hours` wiederholt.
5. Antwortet ein Repeater `offline_after_failures`-mal hintereinander nicht,
   kommt eine Offline-Meldung und später eine „wieder erreichbar“-Meldung.

Eine Hysterese (`hysteresis_mv`, Standard 100 mV) verhindert, dass ein Akku, der
genau um den Schwellwert pendelt, ständig Warnungen und Entwarnungen auslöst.
Repeater, die 0 mV melden (z. B. mit Netzteil), lösen keine Akku-Warnung aus.

## Konfiguration

```ini
[RepeaterTelemetry_Service]
enabled = true
repeaters = MeinRepeater:gastpasswort, ZweiterRepeater:pw
interval_minutes = 60
warn_mv = 3500
critical_mv = 3300
alert_channel = #admin
# alert_dm = Henne, Manuel
# telegram_chat_ids = 123456789
# discord_webhook_urls = https://discord.com/api/webhooks/...

[Akku_Command]
enabled = true
```

- `repeaters`: Kontaktname oder Public-Key-Präfix (mind. 4 Hex-Zeichen),
  danach `:passwort`. Der Repeater muss in den Kontakten des Bots stehen.
- Alle weiteren Optionen samt Standardwerten stehen in `config.ini.example`.
- Die Einstellungen lassen sich auch im Webinterface unter **Plugins** ändern.

Der Funkverkehr bleibt gering: pro Repeater und Durchlauf ein Login und eine
Statusabfrage, mit `delay_between_seconds` Pause zwischen den Repeatern.

## Befehl `akku`

| Eingabe | Antwort |
|---------|---------|
| `akku` | Eine Zeile pro Repeater: Spannung, Warnsymbol, Alter des Messwerts |
| `akku <name>` | Details zu einem Repeater (Laufzeit, RSSI, Erreichbarkeit) |
| `akku jetzt` | Startet sofort eine neue Abfrage |

Weil `akku jetzt` Funkverkehr erzeugt, kann man den Befehl in
`[Admin_ACL] admin_commands` aufnehmen, damit ihn nur Admins nutzen dürfen.

## Meldungstexte anpassen

Mit `template_warn`, `template_critical`, `template_recovered`,
`template_offline` und `template_online` lassen sich die Texte ändern.
Platzhalter: `{name}`, `{volt}` (Volt, z. B. `{volt:.2f}`), `{mv}`, `{fails}`.
