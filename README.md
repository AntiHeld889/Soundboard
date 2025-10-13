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
