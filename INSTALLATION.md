# Installationsanleitung (Deutsch)

Diese Anleitung führt Schritt für Schritt durch die Installation des MeshCore-Bots
**mit deutscher Oberfläche, Repeater-Akku-Überwachung und Weboberfläche** auf einem
Linux-Rechner, z. B. einem Raspberry Pi.

Dauer: etwa 20–30 Minuten.

*Mod by [Mesh.Weserbergland.cc](https://mesh.weserbergland.cc) · basiert auf [agessaman/meshcore-bot](https://github.com/agessaman/meshcore-bot)*

---

## Inhalt

1. [Was du brauchst](#1-was-du-brauchst)
2. [Funkgerät vorbereiten](#2-funkgerät-vorbereiten)
3. [Rechner vorbereiten](#3-rechner-vorbereiten)
4. [Bot herunterladen](#4-bot-herunterladen)
5. [Bot als Dienst installieren](#5-bot-als-dienst-installieren)
6. [Konfiguration anpassen](#6-konfiguration-anpassen)
7. [Bot starten](#7-bot-starten)
8. [Weboberfläche öffnen](#8-weboberfläche-öffnen)
9. [Repeater-Akku-Überwachung einrichten](#9-repeater-akku-überwachung-einrichten)
10. [Testen](#10-testen)
11. [Aktualisieren](#11-aktualisieren)
12. [Fehlerbehebung](#12-fehlerbehebung)
13. [Andere Installationswege](#13-andere-installationswege)

---

## 1. Was du brauchst

| Was | Hinweis |
|-----|---------|
| **Linux-Rechner** | Raspberry Pi 3/4/5 mit Raspberry Pi OS, oder ein PC/Server mit Debian/Ubuntu. Läuft rund um die Uhr. |
| **Python 3.10 oder neuer** | Bei aktuellem Raspberry Pi OS (Bookworm) und Debian 12 schon dabei. Prüfen mit `python3 --version`. |
| **MeshCore-Funkgerät** | Mit **Companion-Firmware** (nicht Repeater- oder Room-Server-Firmware). Anschluss per USB, Bluetooth (BLE) oder WLAN (TCP). |
| **Netzwerkzugang** | Für die Installation (Internet) und die Weboberfläche (lokales Netz). |

---

## 2. Funkgerät vorbereiten

1. Flashe die **Companion-Firmware** auf das Funkgerät, z. B. mit dem Web-Flasher
   unter <https://flasher.meshcore.co.uk>. Wähle die Variante passend zum Anschluss:
   - **USB (Serial)** – empfohlen, am zuverlässigsten
   - **BLE** – per Bluetooth
   - **WiFi** – per Netzwerk (TCP)
2. Stelle die **Funkparameter** (Frequenz, Bandbreite, Spreading Factor, Coding Rate)
   so ein wie im lokalen Mesh üblich. Das geht mit der MeshCore-App oder später in
   der Weboberfläche unter *Einstellungen → Funkgerät*.
3. Schließe das Funkgerät per USB an den Rechner an (bei USB-Variante).

---

## 3. Rechner vorbereiten

Auf dem Rechner ein Terminal öffnen (oder per SSH anmelden) und die benötigten
Pakete installieren:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip rsync
```

Bei USB: prüfen, ob das Funkgerät erkannt wird:

```bash
ls /dev/serial/by-id/
```

Es erscheint ein Eintrag wie `usb-Heltec...-if00`. Den vollständigen Pfad
(`/dev/serial/by-id/usb-...`) brauchst du gleich in der Konfiguration. Er bleibt
auch nach einem Neustart gleich, anders als `/dev/ttyUSB0` oder `/dev/ttyACM0`.

---

## 4. Bot herunterladen

```bash
cd ~
git clone https://github.com/Techsuchti/meshcore-bot-Meshwbl.git meshcore-bot
cd meshcore-bot
```

---

## 5. Bot als Dienst installieren

Das Installationsskript richtet alles ein, damit der Bot automatisch beim Hochfahren
startet:

```bash
sudo ./install-service.sh
```

Das Skript:

- legt einen eigenen Systembenutzer `meshcore` an (mit Zugriff auf USB-Ports),
- kopiert den Bot nach `/opt/meshcore-bot`,
- erstellt eine Python-Umgebung mit allen Abhängigkeiten,
- legt die Konfiguration unter `/etc/meshcore-bot/config.ini` an,
- richtet den Dienst `meshcore-bot` ein.

Während der Installation fragt es nach zwei optionalen Paketen
(Schimpfwortfilter, erweiterte Ortssuche). Beides kannst du mit „nein“ überspringen.

**Wo liegt was?**

| Was | Ort |
|-----|-----|
| Programm | `/opt/meshcore-bot` |
| Konfiguration | `/etc/meshcore-bot/config.ini` |
| Datenbank | `/var/lib/meshcore-bot/meshcore_bot.db` |
| Logdateien | `/var/log/meshcore-bot/` |

---

## 6. Konfiguration anpassen

Konfiguration öffnen:

```bash
sudo nano /etc/meshcore-bot/config.ini
```

Die Datei ist lang und ausführlich kommentiert. Die folgenden Abschnitte musst du
anpassen. Mit `Strg+W` suchst du in nano nach einem Abschnitt (z. B. `[Connection]`).

### Verbindung zum Funkgerät

```ini
[Connection]
connection_type = serial
serial_port = /dev/serial/by-id/usb-DEIN-GERAET-if00
```

Für Bluetooth: `connection_type = ble` und optional `ble_device_name = ...`.
Für WLAN: `connection_type = tcp`, `hostname = 192.168.x.y` und `tcp_port = 5000`.

### Name und Kanäle

```ini
[Bot]
bot_name = MeinBot

[Channels]
monitor_channels = #meinkanal
```

`monitor_channels` sind die Kanäle, in denen der Bot zuhört und antwortet
(mehrere durch Komma trennen). Direktnachrichten beantwortet er immer.

> Bitte den Bot **nicht** im Kanal `Public` betreiben – das stört alle anderen
> im Mesh.

### Deutsche Sprache

```ini
[Localization]
language = de
```

Damit antwortet der Bot auf Deutsch, und auch die Weboberfläche ist deutsch.

### Weboberfläche

```ini
[Web_Viewer]
enabled = true
auto_start = true
host = 0.0.0.0
port = 8080
web_viewer_password = EinSicheresPasswort
```

- `auto_start = true` sorgt dafür, dass die Weboberfläche zusammen mit dem Bot startet.
- `host = 0.0.0.0` macht sie im ganzen lokalen Netz erreichbar.
- **Setze unbedingt ein eigenes Passwort**, sonst kann jeder im Netz alles ändern.

### Repeater-Akku-Überwachung einschalten

Diesen Abschnitt steht ganz unten in der Datei – `enabled` auf `true` setzen:

```ini
[RepeaterTelemetry_Service]
enabled = true
```

Alles Weitere (Repeater, Admins, Schwellwerte) stellst du später bequem in der
Weboberfläche ein.

### Admins für geschützte Befehle (optional)

Befehle wie `reload`, `repeater` oder `neighbors` dürfen nur Admins nutzen. Trage
dafür den vollständigen Public Key (64 Hex-Zeichen, steht in der MeshCore-App unter
den eigenen Einstellungen) ein:

```ini
[Admin_ACL]
admin_pubkeys = 1a2b3c4d...
```

Speichern mit `Strg+O`, `Enter`, beenden mit `Strg+X`.

**Konfiguration prüfen:**

```bash
sudo -u meshcore /opt/meshcore-bot/venv/bin/python /opt/meshcore-bot/meshcore_bot.py \
  --config /etc/meshcore-bot/config.ini --validate-config
```

Werden Fehler gemeldet, die genannte Stelle in der Konfiguration korrigieren.

---

## 7. Bot starten

```bash
sudo systemctl enable --now meshcore-bot
```

Status prüfen:

```bash
sudo systemctl status meshcore-bot
```

„active (running)“ bedeutet: Der Bot läuft. Das Protokoll live ansehen
(beenden mit `Strg+C`):

```bash
sudo journalctl -u meshcore-bot -f
```

Nach Änderungen an der `config.ini` den Bot neu starten:

```bash
sudo systemctl restart meshcore-bot
```

---

## 8. Weboberfläche öffnen

1. IP-Adresse des Rechners herausfinden:
   ```bash
   hostname -I
   ```
2. Im Browser (Handy, Tablet oder PC im selben Netz) öffnen:
   `http://<IP-Adresse>:8080`, z. B. `http://192.168.1.50:8080`
3. Mit dem Passwort aus `web_viewer_password` anmelden.

Im Menü oben findest du den Reiter **Anleitung** mit einer Übersicht über alle
Befehle und Seiten.

---

## 9. Repeater-Akku-Überwachung einrichten

1. In der Weboberfläche **Einstellungen → Repeater-Akku** öffnen.
2. **Repeater hinzufügen**: den Namen so eintragen, wie er in den Kontakten des
   Bots steht (die Seite schlägt bekannte Repeater vor), dazu das Passwort des
   Repeaters. Das **Gast-Passwort** reicht.
3. **Admins hinzufügen** (bis zu 5): Kontaktnamen, die bei niedrigem Akku eine
   MeshCore-Direktnachricht bekommen. Der Bot muss sie in seinen Kontakten haben.
4. Schwellwerte prüfen (Standard für Li-Ion: Warnung unter 3,5 V, kritisch unter 3,3 V).
5. **Speichern.** Die erste Abfrage startet etwa 2 Minuten nach dem Bot-Start, oder
   sofort über **Jetzt abfragen**.

Die Werte erscheinen in der Übersicht und als Verlaufsdiagramm. Im Mesh zeigt der
Befehl `akku` die aktuellen Spannungen.

Mehr dazu: [docs/repeater-telemetry.md](docs/repeater-telemetry.md)

---

## 10. Testen

Schicke dem Bot mit der MeshCore-App eine Direktnachricht oder schreibe in einen
seiner Kanäle:

| Nachricht | Erwartete Antwort |
|-----------|-------------------|
| `ping` | `Pong!` |
| `test` | Verbindungsinfos (Hops, Pfad) |
| `hilfe` | Liste der Befehle |
| `akku` | Akkuspannung der Repeater (sobald eingerichtet) |

Antwortet der Bot nicht, siehe [Fehlerbehebung](#12-fehlerbehebung).

---

## 11. Aktualisieren

```bash
cd ~/meshcore-bot
git pull
sudo ./install-service.sh --upgrade
sudo systemctl restart meshcore-bot
```

Das Upgrade behält Konfiguration, Datenbank und Logs.

Vorher eine Datenbank-Sicherung anlegen schadet nicht: in der Weboberfläche unter
**Einstellungen → Konfiguration → Datenbank-Sicherung → Jetzt sichern**.

---

## 12. Fehlerbehebung

**Protokoll ansehen** – die erste Anlaufstelle bei jedem Problem:

```bash
sudo journalctl -u meshcore-bot -n 100 --no-pager
```

oder in der Weboberfläche unter **Logs**.

| Problem | Lösung |
|---------|--------|
| `Permission denied` auf `/dev/tty...` | Der Dienstbenutzer braucht die Gruppe `dialout`: `sudo usermod -a -G dialout meshcore`, danach `sudo systemctl restart meshcore-bot`. |
| Funkgerät wird nicht gefunden | `ls /dev/serial/by-id/` prüfen und genau diesen Pfad als `serial_port` eintragen. Anderes USB-Kabel probieren (manche Kabel können nur laden). |
| Port belegt / „device busy“ | Ein anderes Programm (z. B. eine offene MeshCore-Web-App oder ein zweiter Bot) nutzt das Funkgerät. Dieses beenden. |
| Weboberfläche nicht erreichbar | `auto_start = true` und `host = 0.0.0.0` gesetzt? Firewall aktiv? Dann Port freigeben: `sudo ufw allow 8080/tcp`. |
| Bot antwortet nicht | Steht der Kanal in `monitor_channels`? Ist der Befehl unter *Einstellungen → Plugins* aktiv? Protokoll prüfen. |
| Antworten auf Englisch | `[Localization] language = de` gesetzt und Bot neu gestartet? |
| Repeater „antwortet nicht“ | Steht der Repeater in den Kontakten des Bots? Stimmt das Passwort? In Funkreichweite? Die Fehlermeldung erscheint in der Übersicht beim Überfahren von „Letzter Versuch“. |
| Admin bekommt keine Akku-Warnung | Der Name muss genau so in den Kontakten des Bots stehen. |
| Python zu alt | `python3 --version` muss 3.10 oder neuer zeigen. Sonst Betriebssystem aktualisieren. |

---

## 13. Andere Installationswege

### Zum Ausprobieren ohne Dienst

```bash
cd ~/meshcore-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.ini.example config.ini
nano config.ini          # wie in Schritt 6 anpassen
python meshcore_bot.py
```

Beenden mit `Strg+C`. Konfiguration und Datenbank liegen dann im Bot-Ordner.

### Docker

Siehe [docs/docker.md](docs/docker.md) (englisch).

### Deinstallieren

```bash
cd ~/meshcore-bot
sudo ./uninstall-service.sh
```

Konfiguration (`/etc/meshcore-bot`) und Datenbank (`/var/lib/meshcore-bot`) bleiben
dabei erhalten und müssen bei Bedarf von Hand gelöscht werden.

---

Weitere, ausführlichere Dokumentation (englisch) steht im Ordner [`docs/`](docs/).
