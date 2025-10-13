# Soundboard Setup auf Raspberry Pi (Bookworm)

Dieses Repository enthält das Skript `web_soundboard.py` für ein webbasiertes Soundboard mit optionaler Servo- und Live-Mikrofon-Unterstützung. Nach einer frischen Installation von Raspberry Pi OS (Bookworm) müssen folgende Komponenten eingerichtet werden:

## Systempakete installieren

```bash
sudo apt update
sudo apt install -y mpg123 alsa-utils sox libsox-fmt-alsa
```

Diese Pakete stellen die Audioausgabe über ALSA, MP3-Wiedergabe sowie die SoX-Effekte bereit, die das Skript nutzt.

## Python-Abhängigkeiten installieren

```bash
pip3 install --upgrade pip
pip3 install flask numpy pydub sounddevice pigpio
```

Die Bibliotheken werden für das Web-Interface (Flask), die Audiobearbeitung (NumPy, pydub), Live-Ein-/Ausgabe (sounddevice) sowie die Servo-Steuerung (pigpio) benötigt.

## pigpio-Dienst aktivieren

Damit die Servo-Steuerung funktioniert, muss der pigpio-Daemon automatisch gestartet werden:

```bash
sudo systemctl enable --now pigpio
```

## Skript starten

Nach der Installation aller Abhängigkeiten kann das Soundboard wie folgt gestartet werden:

```bash
python3 /opt/python/web_soundboard_fx.py
```

Das Web-Interface ist anschließend unter `http://<PI-IP>:8080` erreichbar.

> **Hinweis:** Passen Sie die Pfade `SOUND_DIR` und `CONFIG_PATH` im Skript oder über die Weboberfläche an Ihre Umgebung an.

## Einstellungen in der Weboberfläche

Über die Seiten `/settings` und `/live` erreichen Sie alle Konfigurationsoptionen des Soundboards. Jede Änderung wird im JSON-Config unter `CONFIG_PATH` gespeichert und beim nächsten Start automatisch geladen.

### Audioausgabe & Mixer

* **ALSA-Gerät auswählen (`/devices`, `/device`)** – Wählen Sie das Ausgabegerät (z. B. `plughw:1,0`). Bei Bedarf können Sie über `/info` den aktuell genutzten Index einsehen.
* **Lautstärke (`/volume`)** – Stellt über `amixer` die Mixer-Controls der ausgewählten Karte ein.

### Kategorien & Dateipfade

* **Sound-Verzeichnis & Konfigurationsdatei (`/paths`)** – Ändern Sie Speicherort der MP3s (`SOUND_DIR`) oder des Konfigurations-Files (`CONFIG_PATH`). Das Verzeichnis wird bei Bedarf automatisch angelegt.
* **Kategorien verwalten (`/categories`, `/file-category`)** – Legen Sie Kategorien an und verknüpfen Sie einzelne MP3-Dateien damit, um die Listenansicht zu filtern.

### Servo- und GPIO-Optionen

* **Servo-Trigger (`/sync`, `/angles`)** – Justieren Sie die Verzögerung (ms) zwischen Audio und Servo sowie die Winkel für geschlossenen und geöffneten Mund.
* **GPIO-Pins (`/gpio`)** – Definieren Sie, welcher Pin den Servo (`servo_gpio`) bzw. ein optionales Power- oder LED-Relais (`power_gpio`) ansteuert. `None` deaktiviert die jeweilige Funktion.

### Live-Mikrofon & Effekte

Auf der Seite `/live` konfigurieren Sie das Mikrofon-Streaming:

* **Moduswahl** – `normal` leitet das Mikrofon direkt durch, `fx` nutzt SoX-Effekte (Pitch, Reverb, Bass, Treble).
* **Samplerate, Blocksize & Geräte** – Für beide Modi lassen sich Eingangs- und Ausgangsgeräte (PortAudio/ALSA) sowie Latenz-relevante Parameter setzen.
* **Gain & Presets** – Einstellungen für Ein-/Ausgangsverstärkung, SoX-Puffer, Servo-Delay und Presets (z. B. „neutral“, „tief“, „roboter“).
* **Start/Stop** – Über `/live-start` und `/live-stop` lässt sich der Live-Prozess direkt aus der Oberfläche steuern. Status und Log werden kontinuierlich aktualisiert.

## HTTP-API

Alle Funktionen des Soundboards stehen zusätzlich als JSON-API zur Verfügung. Die wichtigsten Endpunkte sind:

### System & Status

* `GET /info` – Aktuelles ALSA-Gerät.
* `GET /devices` – Liste verfügbarer ALSA-Ausgänge.
* `GET /status` – Aktuell abgespielter Track, Laufzeit und Queue-Status.
* `GET /last-error`, `GET /last-cmd` – Diagnoseinformationen.

### Soundboard-Steuerung

* `POST /play` – Startet eine MP3-Datei. Payload: `{ "file": "<name>.mp3" }` plus optional `"category"` oder `"gain_db"`.
* `POST /stop` – Stoppt die Wiedergabe (optional `"all": true` für Hard-Stop).
* `POST /test-tone` – Spielt einen Sinuston (Frequenz/Level konfigurierbar).
* `POST /upload` – Lädt eine neue MP3-Datei in das Sound-Verzeichnis hoch.

### Konfiguration

* `POST /device` – Setzt ALSA-Gerät: `{ "alsa_device": "plughw:1,0" }`.
* `POST /volume` – Stellt Mixer-Werte: `{ "control": "PCM", "value": 80 }`.
* `POST /sync`, `POST /angles`, `POST /gpio`, `POST /paths` – Passen Servo-/GPIO-Parameter und Pfade an.
* `GET /app-config` – Liefert die gesamte, zusammengeführte Konfiguration.

### Kategorien & Dateien

* `GET /categories` – Gibt Kategorien sowie zugewiesene Dateien aus.
* `POST /categories` – `{ "name": "Jingles" }` erzeugt eine neue Kategorie.
* `POST /file-category` – Verknüpft Dateien: `{ "file": "intro.mp3", "categories": ["Jingles"] }`.

### Live-Mikrofon

* `GET /live-config`, `POST /live-config` – Lesen bzw. speichern Sie die Live-Einstellungen.
* `POST /live-start`, `POST /live-stop` – Starten oder beenden Sie den Live-Prozess.
* `GET /live-status`, `GET /live-log` – Überwachen Sie den aktuellen Zustand und die Logausgaben.

> **Tipp:** Alle POST-Endpunkte akzeptieren JSON-Bodies; Fehlermeldungen werden ebenfalls als JSON mit `error`-Feld zurückgegeben.
