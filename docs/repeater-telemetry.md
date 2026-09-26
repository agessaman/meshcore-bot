# Repeater-Telemetrie (Akku-Überwachung)

Der Dienst `RepeaterTelemetry_Service` fragt in regelmäßigen Abständen den Status
eigener Repeater ab, speichert die Werte in der Bot-Datenbank und warnt bis zu
**5 Admins per MeshCore-Direktnachricht**, wenn der Akku schwach wird oder ein
Repeater nicht mehr antwortet. Eingestellt wird alles bequem im Webinterface
auf der Seite **Repeater-Akku**.

## Einrichten

1. In der `config.ini` den Dienst einmalig aktivieren und den Bot neu starten:

   ```ini
   [RepeaterTelemetry_Service]
   enabled = true

   [Web_Viewer]
   enabled = true
   auto_start = true
   host = 0.0.0.0
   web_viewer_password = EinSicheresPasswort
   ```

2. Im Webinterface **Settings → Repeater-Akku** öffnen.
3. Repeater eintragen (Name wie in den Kontakten des Bots, dazu das Gast- oder
   Admin-Passwort des Repeaters).
4. Bis zu 5 Admins eintragen. Das sind Kontaktnamen, der Bot muss sie also
   in seinen Kontakten haben.
5. **Speichern**. Der Bot übernimmt die Änderungen nach wenigen Sekunden,
   ein Neustart ist nicht nötig.

## Die Seite „Repeater-Akku“

- **Übersicht:** Pro Repeater die letzte Spannung, der Zustand (ok / niedrig /
  kritisch / nicht erreichbar), das Alter der Messung, die Laufzeit, der RSSI
  und der letzte Abfrageversuch. Mit der Maus über „Letzter Versuch“ siehst du
  die Fehlermeldung.
- **Verlauf:** Diagramm der Akkuspannung über 24 Stunden bis 90 Tage, mit
  Warn- und Kritisch-Linie. Zum Umschalten eine Zeile der Übersicht anklicken.
- **Jetzt abfragen:** Startet sofort einen Abfragedurchlauf.
- **Einstellungen:** Repeater, Admins, Schwellwerte, Abfrageintervall,
  Wiederholung von Warnungen, optionaler Zusatzkanal, Aufbewahrungsdauer,
  Meldungstexte und Pause-Schalter.
- **Auf config.ini zurücksetzen:** Verwirft die im Webinterface gespeicherten
  Werte. Danach gelten wieder die Werte aus der `config.ini`.

Passwörter werden nie an den Browser geschickt. Ein leeres Passwortfeld lässt
das gespeicherte Passwort unverändert.

## Ablauf und Warnungen

1. Pro Durchlauf meldet sich der Bot an jedem Repeater an und fragt den Status
   ab. Zwischen zwei Repeatern wartet er `delay_between_seconds`, damit wenig
   Funkverkehr auf einmal entsteht.
2. Jede Abfrage wird in der Tabelle `repeater_telemetry` gespeichert, auch
   fehlgeschlagene.
3. Eine Warnung geht nur raus, wenn sich der **Zustand ändert**
   (ok → niedrig → kritisch → wieder ok). Ist eine Warnung noch aktiv, wird sie
   nach `repeat_alert_hours` wiederholt (0 = nie).
4. Antwortet ein Repeater `offline_after_failures`-mal hintereinander nicht,
   kommt eine Offline-Meldung und später eine „wieder erreichbar“-Meldung.
5. Ist das Funkgerät des Bots gerade nicht verbunden, fällt der Durchlauf aus.
   Das löst keine falschen Offline-Meldungen aus.

Die Hysterese (`hysteresis_mv`, Standard 100 mV) verhindert, dass ein Akku, der
genau um den Schwellwert pendelt, ständig Warnung und Entwarnung auslöst.
Repeater, die 0 mV melden (z. B. mit Netzteil), lösen keine Akku-Warnung aus.

Die Warnungen gehen per Direktnachricht an die Admins, optional zusätzlich in
einen Kanal (`alert_channel`) und an Telegram/Discord (`telegram_chat_ids`,
`discord_webhook_urls` in der `config.ini`). Mit „Nicht ins Mesh senden“ gehen
sie nur an Telegram/Discord.

## Einstellungen in der config.ini

Alle Werte lassen sich auch in der `config.ini` setzen. Die Werte aus dem
Webinterface haben aber Vorrang. Die vollständige Liste mit Standardwerten steht
in `config.ini.example` im Abschnitt `[RepeaterTelemetry_Service]`.

```ini
[RepeaterTelemetry_Service]
enabled = true
repeaters = MeinRepeater:gastpasswort, ZweiterRepeater:pw
admins = Manuel, Henne M
warn_mv = 3500
critical_mv = 3300
interval_minutes = 60
```

## Befehl `akku`

| Eingabe | Antwort |
|---------|---------|
| `akku` | Eine Zeile pro Repeater: Spannung, Warnsymbol, Alter des Messwerts |
| `akku <name>` | Details zu einem Repeater (Laufzeit, RSSI, Erreichbarkeit) |
| `akku jetzt` | Startet sofort eine neue Abfrage |

Weil `akku jetzt` Funkverkehr erzeugt, kann man den Befehl in
`[Admin_ACL] admin_commands` aufnehmen, damit ihn nur Admins nutzen dürfen.

## Meldungstexte anpassen

Im Webinterface unter „Meldungstexte anpassen“ oder in der `config.ini` mit
`template_warn`, `template_critical`, `template_recovered`, `template_offline`
und `template_online`. Platzhalter: `{name}`, `{volt}` (Volt, z. B.
`{volt:.2f}`), `{mv}`, `{fails}`.
