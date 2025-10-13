#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Raspberry Pi Web-Soundboard + Live-Mikro mit Stimmenverzerrer (SoX)

Features
- MP3-Soundboard mit Suche (schöne Liste)
- Output über ALSA / mpg123 (wählbares Gerät, z. B. plughw:1,0)
- Servo-Lippenbewegung (pigpio) anhand Hüllkurve aus MP3
- Optional Power/LED-GPIO (an beim Start, aus nach Ende)
- Lautstärkeregelung via amixer (Mixer auto-erkennung)
- Live-Mikrofon:
    * Normal: Mikro -> Lautsprecher (PortAudio/sounddevice)
    * FX:     Mikro -> SoX (Pitch/Reverb/Bass/Treble) -> ALSA Gerät
      + Servo-Delay zur Lippensynchronität
- Einstell-Seiten (Settings + Live) inkl. Speichern
- Pfade (SOUND_DIR, CONFIG_PATH) in UI anpassbar

Voraussetzungen
  sudo apt-get install -y mpg123 alsa-utils sox libsox-fmt-alsa
  pip3 install flask numpy pydub sounddevice pigpio
  sudo systemctl enable --now pigpio

Start
  python3 /opt/python/web_soundboard_fx.py
  Browser: http://<Pi-IP>:8080
"""

import os, sys, re, json, shlex, signal, subprocess, threading, time, argparse, copy
from collections import deque
from pathlib import Path

from flask import Flask, request, jsonify, render_template_string, abort

import numpy as np
from pydub import AudioSegment

# pigpio optional
try:
    import pigpio
    HAVE_PIGPIO = True
except Exception:
    HAVE_PIGPIO = False

# sounddevice optional (für Live)
try:
    import sounddevice as sd
    HAVE_SD = True
except Exception:
    HAVE_SD = False

# =========================
#   Konfiguration
# =========================
SOUND_DIR   = Path("/opt/python/sounds")
CONFIG_PATH = Path("/opt/python/web_soundboard_config.json")
HOST, PORT  = "0.0.0.0", 8080

SERVO_US_MIN = 500
SERVO_US_MAX = 2500

# Envelope (MP3 → Servo)
FRAME_MS          = 30
ATTACK_MS         = 40
RELEASE_MS        = 120
SILENCE_GATE_DBFS = -45
NORM_PERCENTILE   = 95

# mpg123 Start-Wartezeit
START_WAIT_MS     = 120

DEFAULT_CONFIG = {
    "alsa_device": "plughw:1,0",
    "alsa_card_index": 1,
    "mixer_candidates": ["Speaker", "PCM", "Master", "Headphone"],

    # Kategorien
    "categories": [],
    "file_categories": {},

    # Servo/Sync
    "sync_lead_ms": 180,
    "servo_gpio": 17,     # None = deaktiviert
    "power_gpio": 23,     # None = deaktiviert
    "closed_angle": 5,
    "open_angle": 65,

    # Live (Normal + FX)
    "live_config": {
        "mode": "fx",  # "normal" oder "fx"
        "normal": {
            "samplerate": 48000,
            "blocksize": 256,
            "input_device": None,
            "output_device": None,
            "input_gain_db": 0.0,
            "output_gain_db": 0.0,
            "ultra_low_latency": True
        },
        "fx": {
            "samplerate": 48000,
            "blocksize": 256,
            "input_device": None,     # PortAudio index
            "alsa_out": "plughw:1,0", # ALSA Zielgerät
            "fx_pitch_semitones": 0.0,
            "fx_reverb": 10.0,
            "fx_bass_db": 0.0,
            "fx_treble_db": 0.0,
            "sox_buffer_frames": 256,
            "ultra_low_latency": True,
            "servo_delay_ms": 180.0,
            "preset": "neutral"
        }
    }
}

GPIO_OPTIONS = [None, 2,3,4,5,6,7,8,9,10,11,12,13,16,17,18,19,20,21,22,23,24,25,26,27]

app = Flask(__name__)
play_lock    = threading.RLock()
current_proc = {"p": None, "file": None}
cfg          = copy.deepcopy(DEFAULT_CONFIG)
last_error   = {"msg": None, "ts": None}
last_cmd     = {"text": None}

# pigpio global
pi = None
servo_thread = {"t": None, "stop": None}

# Live Mic Prozess (Subprozess = dieses Skript mit --live)
live_proc = {"p": None, "mode": None, "args": None}
live_log  = deque(maxlen=600)

def now_str(): return time.strftime("%Y-%m-%d %H:%M:%S")

def set_last_error(msg):
    last_error["msg"] = msg
    last_error["ts"]  = now_str()
    print(f"[ERROR] {last_error['ts']} :: {msg}", file=sys.stderr)

def run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

def ensure_dirs():
    SOUND_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

def _normalize_gpio_in_cfg():
    for k in ("servo_gpio", "power_gpio"):
        v = cfg.get(k)
        if isinstance(v, str):
            t = v.strip().lower()
            if t in ("", "none", "null"): cfg[k] = None
            else:
                try: cfg[k] = int(t)
                except: cfg[k] = None

def _merge_defaults(d, defaults):
    if d is None: return defaults
    for k, v in defaults.items():
        if k not in d:
            d[k] = v
        elif isinstance(v, dict) and isinstance(d[k], dict):
            _merge_defaults(d[k], v)
    return d

def load_config():
    global cfg
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            set_last_error(f"Config lesen fehlgeschlagen ({CONFIG_PATH}): {e}")
    _normalize_gpio_in_cfg()
    cfg.setdefault("categories", [])
    cfg["file_categories"] = _normalized_assignment_map(cfg.get("file_categories", {}))
    cfg["live_config"] = _merge_defaults(cfg.get("live_config", {}), DEFAULT_CONFIG["live_config"])

def save_config():
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        set_last_error(f"Config speichern fehlgeschlagen ({CONFIG_PATH}): {e}")

def _coerce_categories(value):
    if isinstance(value, str):
        value = [value]
    elif isinstance(value, (set, tuple)):
        value = list(value)
    elif not isinstance(value, list):
        value = []
    result = []
    for cat in value:
        if not isinstance(cat, str):
            continue
        cat = cat.strip()
        if not cat or cat in result:
            continue
        result.append(cat)
    return result


def _normalized_assignment_map(raw):
    if not isinstance(raw, dict):
        return {}
    out = {}
    for fn, cats in raw.items():
        normed = _coerce_categories(cats)
        if normed:
            out[str(fn)] = normed
    return out


def list_mp3s():
    files = []
    assignments = _normalized_assignment_map(cfg.get("file_categories", {}))
    cfg["file_categories"] = assignments
    for pattern in ("*.mp3", "*.MP3"):
        for p in sorted(SOUND_DIR.glob(pattern)):
            cats = assignments.get(p.name, [])
            files.append({
                "name": p.stem.replace("_", " "),
                "file": p.name,
                "categories": cats,
                "category": cats[0] if cats else None,
            })
    return files

def resolve_file(fn):
    p = (SOUND_DIR / fn).resolve()
    if not str(p).startswith(str(SOUND_DIR.resolve())):
        abort(400, "Ungültiger Dateiname.")
    if not p.exists():
        abort(404, "Datei nicht gefunden.")
    return p

def aplay_list_devices():
    out = run(["aplay","-l"]).stdout or ""
    devices, cur_card = [], None
    for line in out.splitlines():
        if line.startswith("card"):
            parts = line.split()
            try:
                cur_card = int(parts[1].strip(":"))
            except: cur_card = None
        if "device" in line and cur_card is not None:
            m = re.search(r"device\s+(\d+):\s+([^\[]+)\[([^\]]+)\]", line)
            if m:
                dev = int(m.group(1))
                devices.append({
                    "card": cur_card,
                    "device": dev,
                    "label": f"plughw:{cur_card},{dev} – {m.group(2).strip()} ({m.group(3).strip()})",
                    "value": f"plughw:{cur_card},{dev}"
                })
    return devices, out

def device_to_card_index(dev):
    m = re.match(r".*?(\d+),(\d+)", dev or "")
    return int(m.group(1)) if m else cfg.get("alsa_card_index", 0)

def find_working_control(card_index, candidates):
    for ctl in candidates:
        r = run(["amixer","-c",str(card_index),"get",ctl])
        if r.returncode == 0 and "[" in r.stdout:
            return ctl
    r = run(["amixer","-c",str(card_index),"scontrols"])
    m = re.findall(r"Simple mixer control '([^']+)'", r.stdout or "")
    return m[0] if m else None

def parse_amixer_state(text):
    percents = re.findall(r"\[(\d{1,3})%\]", text or "")
    percent = int(percents[-1]) if percents else None
    muted = None
    if "[off]" in text: muted = True
    if "[on]" in text:  muted = False if muted is None else muted
    return percent, muted

def get_volume_state():
    ctl = find_working_control(cfg["alsa_card_index"], cfg["mixer_candidates"])
    if not ctl: return None, None, None
    r = run(["amixer","-c",str(cfg["alsa_card_index"]),"get",ctl])
    if r.returncode != 0: return None, None, ctl
    vol, muted = parse_amixer_state(r.stdout)
    return vol, muted, ctl

def set_volume(vol=None, toggle_mute=False):
    ctl = find_working_control(cfg["alsa_card_index"], cfg["mixer_candidates"])
    if not ctl: return None, None, None
    if vol is not None:
        run(["amixer","-c",str(cfg["alsa_card_index"]),"set",ctl,f"{int(max(0,min(100,vol)))}%"])
    if toggle_mute:
        run(["amixer","-c",str(cfg["alsa_card_index"]),"set",ctl,"toggle"])
    return get_volume_state()

# ===== Envelope / Servo =====
def angle_to_us(angle):
    angle = max(0.0, min(180.0, float(angle)))
    return int(SERVO_US_MIN + (SERVO_US_MAX - SERVO_US_MIN) * (angle / 180.0))

_envelope_cache = {}
def _env_key(path: Path):
    st = path.stat()
    return (str(path), int(st.st_mtime), st.st_size)

def dbfs(x): return 20*np.log10(np.maximum(x, 1e-12))

def compute_envelope(mp3_path: Path, frame_ms=FRAME_MS):
    key = _env_key(mp3_path)
    if key in _envelope_cache:
        return _envelope_cache[key]

    seg = AudioSegment.from_file(mp3_path)
    samples = np.array(seg.get_array_of_samples()).astype(np.float32)
    if seg.channels > 1:
        samples = samples.reshape((-1, seg.channels)).mean(axis=1)
    samples /= (1 << (8*seg.sample_width - 1))
    sr = seg.frame_rate

    frame_len = int(sr * frame_ms / 1000.0)
    n_frames = max(1, len(samples)//frame_len)
    rms = []
    for i in range(n_frames):
        w = samples[i*frame_len:(i+1)*frame_len]
        if len(w) == 0: break
        rms.append(np.sqrt(np.mean(w*w)))
    rms = np.array(rms)

    rms_db = dbfs(rms)
    rms[rms_db < SILENCE_GATE_DBFS] = 0.0

    ref = np.percentile(rms[rms>0], NORM_PERCENTILE) if np.any(rms>0) else 1.0
    if ref <= 0: ref = 1.0
    env = np.clip(rms/ref, 0, 1)

    atk_a = np.exp(-frame_ms / max(1, ATTACK_MS))
    rel_a = np.exp(-frame_ms / max(1, RELEASE_MS))
    smooth = np.zeros_like(env)
    y = 0.0
    for i, x in enumerate(env):
        if x > y: y = atk_a*y + (1-atk_a)*x
        else:     y = rel_a*y + (1-rel_a)*x
        smooth[i] = y

    times = np.arange(len(smooth)) * (frame_ms/1000.0)
    duration = len(samples)/sr

    _envelope_cache[key] = (times, smooth, duration)
    return times, smooth, duration

def is_servo_active():
    return HAVE_PIGPIO and (pi is not None) and pi.connected and (cfg.get("servo_gpio") is not None)

def power_on():
    if HAVE_PIGPIO and pi and pi.connected:
        pg = cfg.get("power_gpio")
        if pg is not None:
            try: pi.write(pg, 1)
            except Exception: pass

def power_off():
    if HAVE_PIGPIO and pi and pi.connected:
        pg = cfg.get("power_gpio")
        if pg is not None:
            try: pi.write(pg, 0)
            except Exception: pass

def servo_open_close_by_envelope(proc, mp3_path: Path, precomputed=None):
    if not is_servo_active():
        return
    if precomputed:
        times, env, duration = precomputed
    else:
        times, env, duration = compute_envelope(mp3_path)

    stop_evt = threading.Event()
    servo_thread["stop"] = stop_evt

    s_gpio = cfg["servo_gpio"]
    closed = float(cfg.get("closed_angle", 5))
    open_  = float(cfg.get("open_angle", 65))
    lead   = float(cfg.get("sync_lead_ms", 0)) / 1000.0

    def set_angle(a):
        pi.set_servo_pulsewidth(s_gpio, angle_to_us(a))

    def _runner():
        set_angle(closed); time.sleep(0.05)
        t0 = time.perf_counter() - lead
        for t, e in zip(times, env):
            if stop_evt.is_set(): break
            dt = (t0 + t) - time.perf_counter()
            if dt > 0: time.sleep(dt)
            a = closed + (open_ - closed) * float(e)
            set_angle(a)
        while not stop_evt.is_set():
            if proc.poll() is not None: break
            time.sleep(0.02)
        try:
            set_angle(closed); time.sleep(0.15)
            pi.set_servo_pulsewidth(s_gpio, 0)
        except Exception:
            pass
        power_off()

    t = threading.Thread(target=_runner, daemon=True)
    servo_thread["t"] = t
    t.start()

def stop_servo():
    if servo_thread["stop"]:
        servo_thread["stop"].set()
    t = servo_thread.get("t")
    if t and t.is_alive():
        t.join(timeout=1.0)
    if HAVE_PIGPIO and pi and pi.connected:
        s_gpio = cfg.get("servo_gpio")
        if s_gpio is not None:
            try:
                closed = float(cfg.get("closed_angle", 5))
                pi.set_servo_pulsewidth(s_gpio, angle_to_us(closed))
                time.sleep(0.1)
                pi.set_servo_pulsewidth(s_gpio, 0)
            except Exception:
                pass
    servo_thread["t"] = None
    servo_thread["stop"] = None
    power_off()

# ===== Playback Start/Stop =====
def start_play(path: Path):
    candidates = [cfg["alsa_device"], "plughw:1,0", "default", "plughw:0,0", "plughw:2,0", "plughw:3,0"]
    last_err = None
    for dev in [d for d in candidates if d]:
        try:
            cmd = ["mpg123","-q","-o","alsa","-a",dev,str(path)]
            last_cmd["text"] = " ".join(cmd)
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
            time.sleep(START_WAIT_MS/1000.0)
            if proc.poll() is None:
                print(f"[audio] using device: {dev}")
                print(f"[audio] cmd: {last_cmd['text']}")
                return proc
            last_err = f"mpg123 exited immediately on {dev} (code={proc.returncode})"
        except Exception as e:
            last_err = f"Exception for {dev}: {e}"
    raise RuntimeError(last_err or "Kein Ausgabegerät funktioniert (mpg123 endete sofort).")

def stop_current():
    with play_lock:
        p = current_proc.get("p")
        if p and p.poll() is None:
            try: p.terminate()
            except Exception: pass
        current_proc["p"] = None
        current_proc["file"] = None
        stop_servo()

# ======= HTML Pages =======
PAGE_INDEX = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Soundboard</title>
<script>
(function(){
  try{
    const stored = localStorage.getItem('theme');
    const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    const theme = stored || (prefersDark ? 'dark' : 'light');
    const root = document.documentElement;
    root.classList.toggle('dark', theme === 'dark');
    root.dataset.theme = theme;
  }catch(e){}
})();
</script>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  :root {
    --pad: 14px;
    --radius: 14px;
    --bg: #fafafa;
    --fg: #111;
    --card-bg: #ffffff;
    --card-alt: #f0f0f0;
    --border: #d8d8d8;
    --border-strong: #c0c0c0;
    --button-bg: #ffffff;
    --button-hover: #f0f0f0;
    --muted: #666666;
    --err: #ff5252;
    --ok: #2e7d32;
    --accent: #2e7d32;
  }
  .dark {
    --bg: #121212;
    --fg: #f1f1f1;
    --card-bg: #1e1e1e;
    --card-alt: #232323;
    --border: #3a3a3a;
    --border-strong: #4a4a4a;
    --button-bg: #1e1e1e;
    --button-hover: #2a2a2a;
    --muted: #a0a0a0;
    --err: #ff6b6b;
    --ok: #8bc34a;
    --accent: #81c784;
  }
  html, body { background: var(--bg); color: var(--fg); }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: clamp(12px, 4vw, 28px); transition: background 0.3s, color 0.3s; font-size: 16px; line-height: 1.5; }
  header { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; gap:12px; flex-wrap:wrap; }
  h1 { margin:0; font-size: clamp(22px, 5vw, 30px); }
  a.btn, button.btn { text-decoration:none; border:1px solid var(--border); padding:10px 14px; border-radius:var(--radius); color:var(--fg); background:var(--button-bg); cursor:pointer; transition:background 0.2s, border-color 0.2s; min-height:44px; }
  a.btn:hover, button.btn:hover { background:var(--button-hover); border-color:var(--border-strong); }
  button, input, select { font-size:15px; min-height:38px; }
  button { touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
  .toolbar { display:flex; gap:8px; align-items:center; margin:10px 0 12px; flex-wrap:wrap; }
  .toolbar button, .toolbar input, .toolbar select { font-size:15px; min-height:34px; padding:6px 10px; }
  input[type="search"] { padding:6px 10px; border:1px solid var(--border); border-radius:var(--radius); min-width:180px; width:min(300px,100%); background:var(--button-bg); color:var(--fg); margin:0; }
  input[type="search"]::placeholder { color: var(--muted); }
  input[type="text"], select { padding:10px 12px; border:1px solid var(--border); border-radius:var(--radius); background:var(--button-bg); color:var(--fg); width:auto; max-width:100%; }
  select { min-width:180px; }
  button { padding:8px 12px; border-radius:var(--radius); border:1px solid var(--border); background:var(--button-bg); color:var(--fg); cursor:pointer; transition:background 0.2s, border-color 0.2s; }
  button:hover { background:var(--button-hover); border-color:var(--border-strong); }
  .toolbar .group { display:flex; gap:6px; align-items:center; flex-wrap:wrap; flex:1 1 200px; }
  .toolbar .group.search-group { flex:1 1 220px; flex-wrap:nowrap; }
  .toolbar .group.search-group input[type="search"] { flex:1 1 auto; min-width:0; width:auto; }
  .toolbar .group.search-group button { flex:0 0 auto; width:auto; white-space:nowrap; }
  .list { display:flex; flex-direction:column; gap:8px; }
  .item { display:flex; justify-content:space-between; align-items:center; border:1px solid var(--border); border-radius:var(--radius); padding: var(--pad); background:var(--card-bg); transition:background 0.2s, border-color 0.2s; gap:16px; }
  .left { display:flex; flex-direction:column; gap:4px; }
  .name { font-weight:600; }
  .playing { outline:2px solid var(--accent); }
  .meta { font-size:12px; color:var(--muted); }
  .meta.ok { color:var(--ok); }
  .meta.err { color:var(--err); }
  .right { display:flex; gap:8px; align-items:center; flex-wrap:wrap; justify-content:flex-end; }
  .right .catSelect { min-width:200px; min-height:110px; width: min(320px, 100%); }
  .catLabel { font-size:12px; color:var(--muted); }
  .err { color:var(--err); font-weight:600; }
  @media (hover: none) {
    a.btn:hover, button.btn:hover, button:hover { background:var(--button-bg); border-color:var(--border); }
  }
  .toolbar.status { justify-content:space-between; gap:6px; }
  .toolbar.status .meta { flex:1 1 auto; }
  @media (max-width: 900px) {
    header { flex-direction:column; align-items:flex-start; }
    header > div { width:100%; justify-content:flex-start; flex-wrap:wrap; }
    .toolbar { flex-direction:column; align-items:stretch; }
    .toolbar.main-controls { row-gap:4px; column-gap:8px; }
    .toolbar .group { width:100%; flex:0 0 auto; }
    .toolbar .group > * { flex:1 1 auto; width:100%; }
    input[type="search"], select, button { width:100%; }
    .toolbar .group.search-group { flex-wrap:nowrap; }
    .toolbar .group.search-group > * { width:auto; flex:0 0 auto; }
    .toolbar .group.search-group input[type="search"] { flex:1 1 auto; width:auto; }
    .toolbar .group.search-group button { flex:0 0 auto; }
    .list { gap:12px; }
    .item { flex-direction:column; align-items:stretch; }
    .right { width:100%; justify-content:flex-start; }
    .right .catSelect { width:100%; }
  }
  @media (max-width: 520px) {
    body { margin: 12px 10px; }
  }
</style>
</head>
<body>
  <header>
    <h1>🎵 Raspberry Pi Soundboard</h1>
    <div style="display:flex;gap:8px;align-items:center">
      <button class="btn" id="themeToggle" type="button">🌙 Dunkel</button>
      <a class="btn" href="/live">🎙️ Live</a>
      <a class="btn" href="/settings">⚙️ Einstellungen</a>
    </div>
  </header>

  <div class="toolbar main-controls">
      <div class="group search-group">
        <input id="q" type="search" placeholder="Suche nach Titel/Datei …" autocomplete="off" />
        <button id="clearBtn" title="Suche löschen">✖</button>
      </div>
      <div class="group">
        <select id="catFilter">
          <option value="">Alle Kategorien</option>
          {% for cat in categories %}
            <option value="{{cat|e}}">{{cat}}</option>
          {% endfor %}
        </select>
      </div>
      <button id="stopBtn" title="Wiedergabe stoppen">⏹ Stop</button>
  </div>

  <div class="toolbar status">
      <span class="meta" id="meta"></span>
      <span class="meta" id="catMsg"></span>
  </div>

  <div id="list" class="list">
    {% for s in sounds %}
      <div class="item" data-name="{{s.name|e}}" data-file="{{s.file|e}}" data-category="{{ (s.category or '')|e }}" data-categories='{{ (s.categories or [])|tojson|e }}'>
        <div class="left">
          <div class="name">{{s.name}}</div>
          <div class="catLabel">{% if s.categories %}{{ s.categories|join(', ') }}{% else %}Keine Kategorie{% endif %}</div>
        </div>
        <div class="right">
          <select class="catSelect" data-file="{{s.file|e}}" multiple size="4">
            <option value="" {% if not s.categories %}selected{% endif %}>Keine Kategorie</option>
            {% for cat in categories %}
              <option value="{{cat|e}}" {% if s.categories and cat in s.categories %}selected{% endif %}>{{cat}}</option>
            {% endfor %}
          </select>
          <button class="playBtn" data-file="{{s.file|e}}">▶ Abspielen</button>
        </div>
      </div>
    {% endfor %}
  </div>

  <div class="toolbar" style="margin-top:14px;">
    <span class="meta">Ausgabegerät: <code id="devLabel"></code></span>
    <span class="meta err" id="errorLabel"></span>
  </div>

<script>
const themeToggle=document.getElementById('themeToggle');
const root=document.documentElement;

function setTheme(theme, store=true){
  root.classList.toggle('dark', theme === 'dark');
  root.dataset.theme = theme;
  if(store){
    try{ localStorage.setItem('theme', theme); }catch(e){}
  }
  if(themeToggle){ themeToggle.textContent = theme === 'dark' ? '☀️ Hell' : '🌙 Dunkel'; }
}

function initTheme(){
  let theme = 'light';
  try{
    const stored = localStorage.getItem('theme');
    if(stored){ theme = stored; }
    else if(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches){
      theme = 'dark';
    }
  }catch(e){}
  setTheme(theme, false);
}

initTheme();
if(themeToggle){
  themeToggle.addEventListener('click', ()=>{
    const next = root.classList.contains('dark') ? 'light' : 'dark';
    setTheme(next);
  });
}
if(window.matchMedia){
  try{
    const mm = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = (ev)=>{
      try{ if(localStorage.getItem('theme')) return; }catch(e){}
      setTheme(ev.matches ? 'dark' : 'light', false);
    };
    if(typeof mm.addEventListener === 'function') mm.addEventListener('change', handler);
    else if(typeof mm.addListener === 'function') mm.addListener(handler);
  }catch(e){}
}

const q=document.getElementById('q');
const clearBtn=document.getElementById('clearBtn');
const catFilter=document.getElementById('catFilter');
const catMsg=document.getElementById('catMsg');
const list=document.getElementById('list');
const meta=document.getElementById('meta');
const stopBtn=document.getElementById('stopBtn');
const errorLabel=document.getElementById('errorLabel');
const devLabel=document.getElementById('devLabel');

let categories={{ categories|tojson }};
let assignments={{ assignments|tojson }};
if(!Array.isArray(categories)) categories=[];
if(!assignments || typeof assignments!=='object') assignments={};

function normalizeCategoriesList(value){
  const out=[];
  if(Array.isArray(value)){
    for(const entry of value){
      if(typeof entry!=='string') continue;
      const trimmed=entry.trim();
      if(trimmed && !out.includes(trimmed)) out.push(trimmed);
    }
  }else if(typeof value==='string'){
    const trimmed=value.trim();
    if(trimmed) out.push(trimmed);
  }else if(value && typeof value==='object' && typeof value[Symbol.iterator]==='function'){
    for(const entry of value){
      if(typeof entry!=='string') continue;
      const trimmed=entry.trim();
      if(trimmed && !out.includes(trimmed)) out.push(trimmed);
    }
  }
  return out;
}

function normalizeAssignments(raw){
  const out={};
  if(!raw || typeof raw!=='object') return out;
  for(const [file, cats] of Object.entries(raw)){
    const normalized=normalizeCategoriesList(cats);
    if(normalized.length) out[file]=normalized;
  }
  return out;
}

function getItemCategories(el){
  if(!el) return [];
  const raw=el.dataset.categories;
  if(raw){
    try{ return normalizeCategoriesList(JSON.parse(raw)); }
    catch(e){}
  }
  return normalizeCategoriesList(el.dataset.category || []);
}

assignments=normalizeAssignments(assignments);

function norm(s){ return (s||'').toLowerCase().normalize('NFKD'); }
function setCatMsg(text, ok){
  if(!catMsg) return;
  if(!text){ catMsg.textContent=''; catMsg.className='meta'; return; }
  catMsg.textContent=text;
  catMsg.className='meta ' + (ok ? 'ok' : 'err');
}
function fillCategorySelect(select, selectedValues){
  if(!select) return;
  const selected=normalizeCategoriesList(selectedValues);
  while(select.options.length) select.remove(0);
  const noneOpt=document.createElement('option');
  noneOpt.value='';
  noneOpt.textContent='Keine Kategorie';
  noneOpt.selected = selected.length===0;
  select.appendChild(noneOpt);
  for(const cat of categories){
    const opt=document.createElement('option');
    opt.value=cat;
    opt.textContent=cat;
    opt.selected = selected.includes(cat);
    select.appendChild(opt);
  }
}
function applyFilter(){
  const items = list ? [...list.querySelectorAll('.item')] : [];
  const needle=norm(q ? q.value.trim() : '');
  const selectedCat = catFilter ? catFilter.value : '';
  let shown=0;
  for(const el of items){
    const hay=norm((el.dataset.name||'') + " " + (el.dataset.file||''));
    const matchesText=!needle || hay.includes(needle);
    const matchesCat=!selectedCat || getItemCategories(el).includes(selectedCat);
    const match=matchesText && matchesCat;
    el.style.display = match ? "" : "none";
    if(match) shown++;
  }
  if(meta) meta.textContent = shown + " / " + items.length + " sichtbar";
}
function updateCategoryUI(){
  const values = Object.values(assignments||{});
  for(const entry of values){
    const catList=normalizeCategoriesList(entry);
    for(const cat of catList){
      if(cat && !categories.includes(cat)) categories.push(cat);
    }
  }
  categories = categories.filter((v,i,self)=>self.indexOf(v)===i);
  categories.sort((a,b)=>a.localeCompare(b,'de',{sensitivity:'base'}));
  if(catFilter){
    const prev = catFilter.value;
    while(catFilter.options.length) catFilter.remove(0);
    const allOpt=document.createElement('option');
    allOpt.value=''; allOpt.textContent='Alle Kategorien';
    catFilter.appendChild(allOpt);
    for(const cat of categories){
      const opt=document.createElement('option');
      opt.value=cat; opt.textContent=cat; catFilter.appendChild(opt);
    }
    if(prev && categories.includes(prev)) catFilter.value=prev; else catFilter.value='';
  }
  document.querySelectorAll('.item').forEach(el=>{
    const file=el.dataset.file;
    const catList=normalizeCategoriesList(assignments[file]);
    el.dataset.category = catList.length?catList[0]:'';
    el.dataset.categories = JSON.stringify(catList);
    const label=el.querySelector('.catLabel');
    if(label) label.textContent = catList.length ? catList.join(', ') : 'Keine Kategorie';
    fillCategorySelect(el.querySelector('.catSelect'), catList);
  });
  applyFilter();
}
async function loadInfo(){
  try{ const r=await fetch('/info'); const j=await r.json();
       if(devLabel) devLabel.textContent = (j.alsa_device||"–") + (j.card_index!==undefined?(" (Karte "+j.card_index+")"):"");
  }catch(e){ if(devLabel) devLabel.textContent="unbekannt"; }
}
async function loadCategories(){
  try{
    const res=await fetch('/categories');
    const j=await res.json();
    if(!res.ok || !j.ok) throw new Error(j.error||'Kategorien konnten nicht geladen werden');
    categories=Array.isArray(j.categories)?j.categories:[];
    assignments=normalizeAssignments(j.assignments);
    setCatMsg('', true);
    updateCategoryUI();
  }catch(e){
    setCatMsg(e.message||'Kategorien konnten nicht geladen werden', false);
  }
}
function setBusy(isBusy, txt){
  if(txt && meta) meta.textContent=txt;
  document.querySelectorAll('button.playBtn').forEach(b=>b.disabled=isBusy);
  if(stopBtn) stopBtn.disabled = isBusy;
}
function markPlaying(file){
  document.querySelectorAll('.item').forEach(el=>{
    if(el.dataset.file===file) el.classList.add('playing'); else el.classList.remove('playing');
  });
}
async function play(file){
  if(!file) return;
  if(errorLabel) errorLabel.textContent="";
  setBusy(true, "Spiele: "+file);
  try{
    const res=await fetch('/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file})});
    const j=await res.json();
    if(!res.ok || !j.ok) throw new Error(j.error||'Fehler');
    setBusy(false, "Läuft: "+(j.now_playing||file)); markPlaying(j.now_playing||file);
  }catch(e){ setBusy(false,"Fehler: "+e.message); if(errorLabel) errorLabel.textContent=e.message; }
}
async function stop(){
  setBusy(true,"Stoppe…"); try{ await fetch('/stop',{method:'POST'});}catch(e){}
  setBusy(false,"Bereit"); markPlaying("__none__");
}
if(q) q.addEventListener('input', applyFilter);
if(clearBtn) clearBtn.addEventListener('click', ()=>{ if(q){ q.value=""; applyFilter(); q.focus(); } });
if(catFilter) catFilter.addEventListener('change', applyFilter);
if(stopBtn) stopBtn.addEventListener('click', stop);
if(list) list.addEventListener('click', (e)=>{ const btn=e.target.closest('.playBtn'); if(!btn) return; play(btn.dataset.file); });
if(list) list.addEventListener('change', async (e)=>{
  const sel=e.target.closest('.catSelect');
  if(!sel) return;
  const file=sel.dataset.file;
  const prev=normalizeCategoriesList(assignments[file]);
  try{
    let selectedValues=[...sel.options].filter(o=>o.selected).map(o=>o.value);
    if(selectedValues.includes('')) selectedValues=[];
    selectedValues=selectedValues.filter(v=>v);
    const res=await fetch('/file-category',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file, categories: selectedValues})});
    const j=await res.json();
    if(!res.ok || !j.ok) throw new Error(j.error||'Kategorie konnte nicht gespeichert werden');
    const updated=('categories' in j) ? normalizeCategoriesList(j.categories) : normalizeCategoriesList(j.category);
    if(updated.length) assignments[file]=updated; else delete assignments[file];
    setCatMsg('Kategorie gespeichert', true);
    updateCategoryUI();
  }catch(err){
    fillCategorySelect(sel, prev);
    setCatMsg(err.message||'Kategorie konnte nicht gespeichert werden', false);
    updateCategoryUI();
  }
});
window.addEventListener('DOMContentLoaded', ()=>{
  updateCategoryUI();
  loadInfo();
  loadCategories();
});
</script>
</body>
</html>
"""

PAGE_SETTINGS = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Einstellungen</title>
<script>
(function(){
  try{
    const stored = localStorage.getItem('theme');
    const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    const theme = stored || (prefersDark ? 'dark' : 'light');
    const root = document.documentElement;
    root.classList.toggle('dark', theme === 'dark');
    root.dataset.theme = theme;
  }catch(e){}
})();
</script>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  :root {
    --radius: 14px;
    --bg: #fafafa;
    --fg: #111;
    --card-bg: #ffffff;
    --card-alt: #f0f0f0;
    --border: #d8d8d8;
    --border-strong: #c0c0c0;
    --button-bg: #ffffff;
    --button-hover: #f0f0f0;
    --muted: #666666;
    --err: #ff5252;
    --ok: #2e7d32;
    --accent: #2e7d32;
  }
  .dark {
    --bg: #121212;
    --fg: #f1f1f1;
    --card-bg: #1e1e1e;
    --card-alt: #232323;
    --border: #3a3a3a;
    --border-strong: #4a4a4a;
    --button-bg: #1e1e1e;
    --button-hover: #2a2a2a;
    --muted: #a0a0a0;
    --err: #ff6b6b;
    --ok: #8bc34a;
    --accent: #81c784;
  }
  html, body { background: var(--bg); color: var(--fg); }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: clamp(12px, 4vw, 28px); transition: background 0.3s, color 0.3s; font-size: 16px; line-height: 1.5; }
  header { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; gap:12px; flex-wrap:wrap; }
  h1 { margin:0; font-size: clamp(22px, 5vw, 30px); }
  a.btn, button.btn { text-decoration:none; border:1px solid var(--border); padding:10px 14px; border-radius:var(--radius); background:var(--button-bg); color:var(--fg); cursor:pointer; transition:background 0.2s, border-color 0.2s; min-height:44px; }
  a.btn:hover, button.btn:hover { background:var(--button-hover); border-color:var(--border-strong); }
  button { border:1px solid var(--border); border-radius:var(--radius); background:var(--button-bg); color:var(--fg); padding:10px 14px; cursor:pointer; transition:background 0.2s, border-color 0.2s; min-height:44px; font-size:16px; touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
  button:hover { background:var(--button-hover); border-color:var(--border-strong); }
  .card { border:1px solid var(--border); border-radius:var(--radius); padding:16px; margin:12px 0; background:var(--card-bg); transition:background 0.2s, border-color 0.2s; }
  .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
  select, input[type="number"], input[type="text"] { padding:10px 12px; border:1px solid var(--border); border-radius:var(--radius); background:var(--button-bg); color:var(--fg); transition:background 0.2s, border-color 0.2s; font-size:16px; min-height:44px; width:auto; max-width:100%; }
  select:hover, button:hover, input[type="text"]:hover, input[type="number"]:hover { border-color:var(--border-strong); }
  input[type="range"] { accent-color: var(--accent); width:min(360px,100%); }
  button, input, select { font-size:16px; }
  .hint { font-size:12px; color:var(--muted); }
  pre { background:var(--card-alt); padding:12px; border-radius:var(--radius); overflow:auto; border:1px solid var(--border); transition:background 0.2s, border-color 0.2s; }
  .ok { color:var(--ok); }
  .err { color:var(--err); font-weight:600; }
  label { min-width: 200px; display:inline-block; }
  .category-list { list-style:none; margin:8px 0 0; padding:0; display:flex; flex-wrap:wrap; gap:8px; }
  .category-pill { display:inline-flex; align-items:center; gap:8px; padding:6px 10px; border-radius:999px; background:var(--card-alt); border:1px solid var(--border); }
  .category-pill span { font-size:14px; }
  .category-pill .tagDelete { min-height:0; padding:4px 8px; border-radius:999px; border:1px solid transparent; background:transparent; color:var(--muted); font-size:14px; line-height:1; }
  .category-pill .tagDelete:hover { color:var(--err); border-color:var(--border-strong); background:var(--button-hover); }
  .category-pill .tagDelete:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  @media (hover: none) {
    a.btn:hover, button.btn:hover, button:hover { background:var(--button-bg); border-color:var(--border); }
  }
  @media (max-width: 980px) {
    header { flex-direction:column; align-items:flex-start; }
    header > div { width:100%; display:flex; gap:8px; flex-wrap:wrap; }
    .row { flex-direction:column; align-items:stretch; }
    label { width:100%; min-width:0; }
    select, input[type="number"], input[type="text"], button { width:100%; }
    input[type="range"] { width:100%; }
  }
  @media (max-width: 520px) {
    body { margin: 12px 10px; }
  }
</style>
</head>
<body>
  <header>
    <h1>⚙️ Einstellungen</h1>
    <div style="display:flex;gap:8px;align-items:center">
      <button class="btn" id="themeToggle" type="button">🌙 Dunkel</button>
      <a class="btn" href="/">⬅️ Zurück</a>
      <a class="btn" href="/live">🎙️ Live</a>
    </div>
  </header>

  <div class="card">
    <h3>Ausgabegerät (ALSA / mpg123)</h3>
    <div class="row">
      <label for="devSel">Gerät:</label>
      <select id="devSel"></select>
      <button id="applyDev">Übernehmen</button>
      <button id="testTone">▶ Testton</button>
    </div>
    <p class="hint">mpg123 gibt direkt an ALSA: <code>-o alsa -a &lt;gerät&gt;</code> (z. B. <code>plughw:1,0</code> für USB).</p>
    <p class="hint">Aktuell: <code id="curDev"></code> | Mixer-Karte: <code id="curCard"></code></p>
    <div id="devMsg" class="hint"></div>
  </div>

  <div class="card">
    <h3>Lautstärke</h3>
    <div class="row">
      <input id="vol" type="range" min="0" max="100" step="1" />
      <strong id="volLabel">– %</strong>
      <button id="muteBtn">Mute</button>
      <span class="hint" id="ctlHint"></span>
    </div>
  </div>

  <div class="card">
    <h3>Synchronisation (Servo ↔ Ton)</h3>
    <div class="row">
      <label for="syncLead">Vorlauf (ms):</label>
      <input id="syncLead" type="number" min="0" max="500" step="10" />
      <button id="saveSync">Speichern</button>
      <span class="hint" id="syncMsg"></span>
    </div>
  </div>

  <div class="card">
    <h3>Winkel (Mund zu/auf)</h3>
    <div class="row">
      <label for="closedAngle">CLOSED_ANGLE (°):</label>
      <input id="closedAngle" type="number" min="0" max="180" step="1" />
    </div>
    <div class="row">
      <label for="openAngle">OPEN_ANGLE (°):</label>
      <input id="openAngle" type="number" min="0" max="180" step="1" />
    </div>
    <div class="row">
      <button id="saveAngles">Speichern</button>
      <span class="hint" id="anglesMsg"></span>
    </div>
  </div>

  <div class="card">
    <h3>GPIOs</h3>
    <div class="row">
      <label for="servoGpioSel">Servo GPIO (BCM):</label>
      <select id="servoGpioSel"></select>
    </div>
    <div class="row">
      <label for="powerGpioSel">LED/Power GPIO (BCM):</label>
      <select id="powerGpioSel"></select>
    </div>
    <div class="row">
      <button id="saveGpio">Speichern</button>
      <span class="hint" id="gpioMsg"></span>
    </div>
    <p class="hint">„None“ deaktiviert die jeweilige Funktion – dann werden nur MP3s abgespielt.</p>
  </div>

  <div class="card">
    <h3>Pfade</h3>
    <div class="row">
      <label for="soundDir">SOUND_DIR:</label>
      <input id="soundDir" type="text" placeholder="/opt/python/sounds" style="min-width:360px;" />
    </div>
    <div class="row">
      <label for="configPath">CONFIG_PATH:</label>
      <input id="configPath" type="text" placeholder="/opt/python/web_soundboard_config.json" style="min-width:360px;" />
    </div>
    <div class="row">
      <button id="savePaths">Speichern</button>
      <span class="hint" id="pathsMsg"></span>
    </div>
  </div>

  <div class="card">
    <h3>Kategorien</h3>
    <div class="row">
      <label for="newCategory">Neue Kategorie:</label>
      <input id="newCategory" type="text" placeholder="z. B. Jingles" />
      <button id="addCategoryBtn">➕ Hinzufügen</button>
    </div>
    <p class="hint" id="categoryListHint">Noch keine Kategorien vorhanden.</p>
    <ul class="category-list" id="categoryList" aria-live="polite"></ul>
    <div class="hint" id="categoryMsg"></div>
  </div>

  <div class="card">
    <h3>Letzter Fehler</h3>
    <pre id="lastErr"></pre>
  </div>

  <div class="card">
    <h3>Debug (aplay -l)</h3>
    <pre id="dbg"></pre>
  </div>

<script>
const themeToggle=document.getElementById('themeToggle');
const root=document.documentElement;

function setTheme(theme, store=true){
  root.classList.toggle('dark', theme === 'dark');
  root.dataset.theme = theme;
  if(store){
    try{ localStorage.setItem('theme', theme); }catch(e){}
  }
  if(themeToggle){ themeToggle.textContent = theme === 'dark' ? '☀️ Hell' : '🌙 Dunkel'; }
}

function initTheme(){
  let theme = 'light';
  try{
    const stored = localStorage.getItem('theme');
    if(stored){ theme = stored; }
    else if(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches){
      theme = 'dark';
    }
  }catch(e){}
  setTheme(theme, false);
}

initTheme();
if(themeToggle){
  themeToggle.addEventListener('click', ()=>{
    const next = root.classList.contains('dark') ? 'light' : 'dark';
    setTheme(next);
  });
}
if(window.matchMedia){
  try{
    const mm = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = (ev)=>{
      try{ if(localStorage.getItem('theme')) return; }catch(e){}
      setTheme(ev.matches ? 'dark' : 'light', false);
    };
    if(typeof mm.addEventListener === 'function') mm.addEventListener('change', handler);
    else if(typeof mm.addListener === 'function') mm.addListener(handler);
  }catch(e){}
}

async function fetchJSON(u, opt){ const r=await fetch(u, opt||{}); const j=await r.json(); if(!r.ok) throw new Error(j.error||'Fehler'); return j; }
const devSel=document.getElementById('devSel'), applyDev=document.getElementById('applyDev'), testTone=document.getElementById('testTone');
const curDev=document.getElementById('curDev'), curCard=document.getElementById('curCard'), vol=document.getElementById('vol'), volLabel=document.getElementById('volLabel'), muteBtn=document.getElementById('muteBtn'), ctlHint=document.getElementById('ctlHint');
const syncLead=document.getElementById('syncLead'), saveSync=document.getElementById('saveSync'), syncMsg=document.getElementById('syncMsg');
const closedAngle=document.getElementById('closedAngle'), openAngle=document.getElementById('openAngle'), saveAngles=document.getElementById('saveAngles'), anglesMsg=document.getElementById('anglesMsg');
const servoGpioSel=document.getElementById('servoGpioSel'), powerGpioSel=document.getElementById('powerGpioSel'), saveGpio=document.getElementById('saveGpio'), gpioMsg=document.getElementById('gpioMsg');
const soundDir=document.getElementById('soundDir'), configPath=document.getElementById('configPath'), savePaths=document.getElementById('savePaths'), pathsMsg=document.getElementById('pathsMsg');
const newCategory=document.getElementById('newCategory'), addCategoryBtn=document.getElementById('addCategoryBtn');
const categoryListHint=document.getElementById('categoryListHint'), categoryMsg=document.getElementById('categoryMsg'), categoryList=document.getElementById('categoryList');
const dbg=document.getElementById('dbg'), lastErr=document.getElementById('lastErr'), devMsg=document.getElementById('devMsg');
let currentCategories=[];

function sanitizeCategories(list){
  const out=[];
  if(Array.isArray(list)){
    for(const raw of list){
      if(typeof raw!== 'string') continue;
      const trimmed=raw.trim();
      if(!trimmed) continue;
      if(out.some(existing=>existing.toLowerCase()===trimmed.toLowerCase())) continue;
      out.push(trimmed);
    }
  }
  out.sort((a,b)=>a.localeCompare(b,'de',{sensitivity:'base'}));
  return out;
}

function renderCategoryList(){
  if(categoryListHint){
    if(!currentCategories.length){
      categoryListHint.textContent='Noch keine Kategorien vorhanden.';
    }else{
      categoryListHint.textContent='Vorhandene Kategorien ('+currentCategories.length+')';
    }
  }
  if(!categoryList) return;
  categoryList.innerHTML='';
  for(const cat of currentCategories){
    const li=document.createElement('li');
    li.className='category-pill';
    const label=document.createElement('span');
    label.textContent=cat;
    li.appendChild(label);
    const btn=document.createElement('button');
    btn.type='button';
    btn.className='tagDelete';
    btn.dataset.category=cat;
    btn.setAttribute('aria-label', `Kategorie "${cat}" löschen`);
    btn.textContent='✖';
    li.appendChild(btn);
    categoryList.appendChild(li);
  }
}

function setCategoryStatus(text, ok){
  if(!categoryMsg) return;
  if(!text){ categoryMsg.textContent=''; categoryMsg.className='hint'; return; }
  categoryMsg.textContent=text;
  if(ok === undefined){
    categoryMsg.className='hint';
  }else{
    categoryMsg.className='hint ' + (ok ? 'ok' : 'err');
  }
}

async function loadCategoriesCard(){
  if(!categoryListHint) return;
  try{
    const res=await fetch('/categories');
    const j=await res.json();
    if(!res.ok || !j.ok) throw new Error(j.error||'Kategorien konnten nicht geladen werden');
    currentCategories=sanitizeCategories(j.categories||[]);
    renderCategoryList();
    setCategoryStatus('');
  }catch(e){
    setCategoryStatus(e.message||'Kategorien konnten nicht geladen werden', false);
  }
}

async function deleteCategory(name, triggerBtn){
  if(!name) return;
  if(typeof window.confirm==='function'){
    const proceed = window.confirm(`Kategorie "${name}" wirklich löschen?\nZugeordnete Sounds verlieren diese Kategorie.`);
    if(!proceed) return;
  }
  if(triggerBtn) triggerBtn.disabled=true;
  setCategoryStatus('Kategorie wird gelöscht …');
  try{
    const res=await fetch(`/categories/${encodeURIComponent(name)}`, {method:'DELETE'});
    const j=await res.json();
    if(!res.ok || !j.ok) throw new Error(j.error||'Kategorie konnte nicht gelöscht werden');
    currentCategories=sanitizeCategories(j.categories||[]);
    renderCategoryList();
    setCategoryStatus(`Kategorie "${name}" gelöscht`, true);
  }catch(e){
    setCategoryStatus(e.message||'Kategorie konnte nicht gelöscht werden', false);
  }finally{
    if(triggerBtn) triggerBtn.disabled=false;
  }
}

if(categoryList){
  categoryList.addEventListener('click', (ev)=>{
    const btn = ev.target.closest('button.tagDelete');
    if(!btn || !btn.dataset.category) return;
    ev.preventDefault();
    deleteCategory(btn.dataset.category, btn);
  });
}

function fillGpioSelect(sel, value){ sel.innerHTML=""; const opts=[null,2,3,4,5,6,7,8,9,10,11,12,13,16,17,18,19,20,21,22,23,24,25,26,27]; for(const v of opts){ const o=document.createElement('option'); o.value=(v===null)?"None":String(v); o.textContent=(v===null)?"None":String(v); if((value===null&&v===null)||(value!==null&&String(value)===String(v))) o.selected=true; sel.appendChild(o);} }

async function loadDevices(){ const j=await fetchJSON('/devices'); devSel.innerHTML=""; for(const d of j.devices){ const o=document.createElement('option'); o.value=d.value; o.textContent=d.label; if(j.current && j.current.alsa_device===d.value) o.selected=true; devSel.appendChild(o);} curDev.textContent=(j.current&&j.current.alsa_device)||"–"; curCard.textContent=(j.current&&(""+j.current.alsa_card_index))||"–"; dbg.textContent=j.debug||""; }

applyDev.onclick=async()=>{ devMsg.textContent=""; try{ const j=await fetchJSON('/device',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({alsa_device:devSel.value})}); devMsg.textContent="Gerät übernommen: "+j.alsa_device+" (Karte "+j.alsa_card_index+")"; devMsg.className="hint ok"; await loadDevices(); await loadVol(); }catch(e){ devMsg.textContent=e.message; devMsg.className="hint err"; } };

testTone.onclick=async()=>{ devMsg.textContent="Testton…"; const r=await fetch('/test-tone',{method:'POST'}); const j=await r.json(); if(!j.ok){ devMsg.textContent=j.error||"Fehler"; devMsg.className="hint err"; }else{ devMsg.textContent=j.message||"ok"; devMsg.className="hint ok"; } };

async function loadVol(){ const r=await fetch('/volume'); const j=await r.json(); if(!r.ok){ alert(j.error||"Fehler Lautstärke"); return; } vol.value=j.volume??0; volLabel.textContent=(j.volume??0)+" %"; muteBtn.textContent=j.muted?"Unmute":"Mute"; ctlHint.textContent=j.control?("Mixer: "+j.control+" (Karte "+(j.card??"?")+")"):""; }
let debounce; vol.oninput=()=>{ volLabel.textContent=vol.value+" %"; clearTimeout(debounce); debounce=setTimeout(async()=>{ await fetchJSON('/volume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({volume:parseInt(vol.value,10)})}); },120); };
muteBtn.onclick=async()=>{ const j=await fetchJSON('/volume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({toggle_mute:true})}); muteBtn.textContent=j.muted?"Unmute":"Mute"; if(typeof j.volume==='number'){ vol.value=j.volume; volLabel.textContent=j.volume+" %"; } };

saveSync.onclick=async()=>{ try{ const val=parseInt(syncLead.value,10); const j=await fetchJSON('/sync',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sync_lead_ms:val})}); syncMsg.textContent="Vorlauf: "+j.sync_lead_ms+" ms"; syncMsg.className="hint ok"; }catch(e){ syncMsg.textContent=e.message; syncMsg.className="hint err"; } };
saveAngles.onclick=async()=>{ try{ const ca=parseInt(closedAngle.value,10), oa=parseInt(openAngle.value,10); const j=await fetchJSON('/angles',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({closed_angle:ca,open_angle:oa})}); anglesMsg.textContent="CLOSED="+j.closed_angle+"°, OPEN="+j.open_angle+"°"; anglesMsg.className="hint ok"; }catch(e){ anglesMsg.textContent=e.message; anglesMsg.className="hint err"; } };
saveGpio.onclick=async()=>{ function parse(v){ if(!v||v.toLowerCase()==="none") return null; const n=parseInt(v,10); return isNaN(n)?null:n; } try{ const j=await fetchJSON('/gpio',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({servo_gpio:parse(servoGpioSel.value),power_gpio:parse(powerGpioSel.value)})}); gpioMsg.textContent="Servo="+(j.servo_gpio===null?"None":j.servo_gpio)+", Power="+(j.power_gpio===null?"None":j.power_gpio); gpioMsg.className="hint ok"; }catch(e){ gpioMsg.textContent=e.message; gpioMsg.className="hint err"; } };
savePaths.onclick=async()=>{ try{ const body={}; if(soundDir.value.trim()) body.sound_dir=soundDir.value.trim(); if(configPath.value.trim()) body.config_path=configPath.value.trim(); const j=await fetchJSON('/paths',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); pathsMsg.textContent="SOUND_DIR="+j.sound_dir+" | CONFIG_PATH="+j.config_path; pathsMsg.className="hint ok"; }catch(e){ pathsMsg.textContent=e.message; pathsMsg.className="hint err"; } };

if(addCategoryBtn) addCategoryBtn.onclick=async()=>{
  if(!newCategory) return;
  const name=newCategory.value.trim();
  if(!name){ setCategoryStatus('Bitte Namen eingeben', false); newCategory.focus(); return; }
  try{
    const j=await fetchJSON('/categories',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
    const updated=Array.isArray(j.categories)?j.categories:[...currentCategories, name];
    currentCategories=sanitizeCategories(updated);
    renderCategoryList();
    newCategory.value='';
    setCategoryStatus(`Kategorie "${name}" erstellt`, true);
  }catch(e){
    setCategoryStatus(e.message||'Kategorie konnte nicht gespeichert werden', false);
  }
};
if(newCategory) newCategory.addEventListener('keydown', (ev)=>{ if(ev.key==='Enter'){ ev.preventDefault(); if(addCategoryBtn) addCategoryBtn.click(); } });

async function loadLastError(){ const j=await fetchJSON('/last-error'); lastErr.textContent=(j.ts?("["+j.ts+"] "):"")+(j.msg||"—"); }
async function loadAppConfig(){ const j=await fetchJSON('/app-config'); syncLead.value=j.sync_lead_ms??180; closedAngle.value=j.closed_angle??5; openAngle.value=j.open_angle??65; function fill(sel,val){ sel.innerHTML=""; const opts=[null,2,3,4,5,6,7,8,9,10,11,12,13,16,17,18,19,20,21,22,23,24,25,26,27]; for(const v of opts){ const o=document.createElement('option'); o.value=(v===null)?"None":String(v); o.textContent=o.value; if((val===null&&v===null)||(val!==null&&String(val)===String(v))) o.selected=true; sel.appendChild(o); } } fill(servoGpioSel,j.servo_gpio??null); fill(powerGpioSel,j.power_gpio??null); soundDir.value=j.sound_dir||""; configPath.value=j.config_path||""; }
window.addEventListener('DOMContentLoaded', async ()=>{ await loadDevices(); await loadVol(); await loadLastError(); await loadAppConfig(); await loadCategoriesCard(); });
</script>
</body>
</html>
"""

PAGE_LIVE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live Mikrofon</title>
<script>
(function(){
  try{
    const stored = localStorage.getItem('theme');
    const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    const theme = stored || (prefersDark ? 'dark' : 'light');
    const root = document.documentElement;
    root.classList.toggle('dark', theme === 'dark');
    root.dataset.theme = theme;
  }catch(e){}
})();
</script>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  :root {
    --radius: 14px;
    --bg: #fafafa;
    --fg: #111;
    --card-bg: #ffffff;
    --card-alt: #f0f0f0;
    --border: #d8d8d8;
    --border-strong: #c0c0c0;
    --button-bg: #ffffff;
    --button-hover: #f0f0f0;
    --muted: #666666;
    --err: #ff5252;
    --ok: #2e7d32;
    --accent: #2e7d32;
  }
  .dark {
    --bg: #121212;
    --fg: #f1f1f1;
    --card-bg: #1e1e1e;
    --card-alt: #232323;
    --border: #3a3a3a;
    --border-strong: #4a4a4a;
    --button-bg: #1e1e1e;
    --button-hover: #2a2a2a;
    --muted: #a0a0a0;
    --err: #ff6b6b;
    --ok: #8bc34a;
    --accent: #81c784;
  }
  html, body { background: var(--bg); color: var(--fg); }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: clamp(12px, 4vw, 28px); transition: background 0.3s, color 0.3s; font-size: 16px; line-height: 1.5; }
  header { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; gap:12px; flex-wrap:wrap; }
  h1 { margin:0; font-size: clamp(22px, 5vw, 30px); }
  a.btn, button.btn { text-decoration:none; border:1px solid var(--border); padding:10px 14px; border-radius:var(--radius); background:var(--button-bg); color:var(--fg); cursor:pointer; transition:background 0.2s, border-color 0.2s; min-height:44px; }
  a.btn:hover, button.btn:hover { background:var(--button-hover); border-color:var(--border-strong); }
  button { border:1px solid var(--border); border-radius:var(--radius); background:var(--button-bg); color:var(--fg); padding:10px 14px; cursor:pointer; transition:background 0.2s, border-color 0.2s; min-height:44px; font-size:16px; touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
  button:hover { background:var(--button-hover); border-color:var(--border-strong); }
  .card { border:1px solid var(--border); border-radius:var(--radius); padding:16px; margin:12px 0; background:var(--card-bg); transition:background 0.2s, border-color 0.2s; }
  .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
  select, input[type="number"], input[type="text"] { padding:10px 12px; border:1px solid var(--border); border-radius:var(--radius); background:var(--button-bg); color:var(--fg); transition:background 0.2s, border-color 0.2s; font-size:16px; min-height:44px; width:auto; max-width:100%; }
  select:hover, input[type="number"]:hover, input[type="text"]:hover { border-color:var(--border-strong); }
  .hint { font-size:12px; color:var(--muted); }
  pre { background:var(--card-alt); padding:12px; border-radius:var(--radius); overflow:auto; max-height:260px; border:1px solid var(--border); transition:background 0.2s, border-color 0.2s; }
  .ok { color:var(--ok); } .err { color:var(--err); font-weight:600; }
  label { min-width: 200px; display:inline-block; }
  @media (hover: none) {
    a.btn:hover, button.btn:hover, button:hover { background:var(--button-bg); border-color:var(--border); }
  }
  @media (max-width: 1024px) {
    header { flex-direction:column; align-items:flex-start; }
    header > div { width:100%; display:flex; gap:8px; flex-wrap:wrap; }
    .row { flex-direction:column; align-items:stretch; }
    label { width:100%; min-width:0; }
    select, input[type="number"], input[type="text"], button { width:100%; }
  }
  @media (max-width: 520px) {
    body { margin: 12px 10px; }
  }
</style>
</head>
<body>
  <header>
    <h1>🎙️ Live Mikrofon</h1>
    <div style="display:flex;gap:8px;align-items:center">
      <button class="btn" id="themeToggle" type="button">🌙 Dunkel</button>
      <a class="btn" href="/">⬅️ Sounds</a>
      <a class="btn" href="/settings">⚙️ Einstellungen</a>
    </div>
  </header>

  <div class="card">
    <h3>Modus</h3>
    <div class="row">
      <label>Wähle:</label>
      <select id="modeSel">
        <option value="normal">Normal (direkt)</option>
        <option value="fx">Stimmen-Verzerrer (SoX)</option>
      </select>
      <button id="saveModeBtn">Modus speichern</button>
      <span id="modeMsg" class="hint"></span>
    </div>
  </div>

  <div class="card" id="normalCard">
    <h3>Normaler Betrieb</h3>
    <div class="row">
      <label>Mikro (Input-Gerät):</label>
      <select id="inDev"></select>
    </div>
    <div class="row">
      <label>Output (Lautsprecher/Kopfhörer):</label>
      <select id="outDev"></select>
    </div>
    <div class="row">
      <label>Samplerate:</label><select id="sr"></select>
      <label>Blocksize:</label><select id="bs"></select>
      <label>Ultra-Low-Latency:</label><select id="ull"></select>
    </div>
    <div class="row">
      <label>Input-Gain (dB):</label><select id="inGain"></select>
      <label>Output-Gain (dB):</label><select id="outGain"></select>
    </div>
    <div class="row">
      <button id="saveNormalBtn">Einstellungen speichern</button>
      <span id="normalMsg" class="hint"></span>
    </div>
  </div>

  <div class="card" id="fxCard" style="display:none">
    <h3>Verzerrer (SoX)</h3>
    <div class="row">
      <label>Mikro (Input-Gerät):</label>
      <select id="inDevFx"></select>
    </div>
    <div class="row">
      <label>ALSA Ausgabegerät:</label>
      <select id="alsaOut"></select>
      <span class="hint">entspricht `aplay -l` → plughw:X,Y</span>
    </div>

    <div class="row">
      <label>Preset:</label>
      <select id="presetSel">
        <option value="neutral">Neutral</option>
        <option value="daemon">Dämon</option>
        <option value="monster">Monster</option>
        <option value="cave">Höhlengeist</option>
        <option value="helium">Helium</option>
        <option value="funky">Funky</option>
        <option value="whisper">Flüstergeist</option>
      </select>
      <button id="applyPresetBtn">Preset anwenden</button>
      <span class="hint">Preset setzt Pitch/Hall/Bass/Treble – danach feinjustieren.</span>
    </div>

    <div class="row">
      <label>Samplerate:</label><select id="srFx"></select>
      <label>Blocksize:</label><select id="bsFx"></select>
      <label>SoX Buffer (Frames):</label><select id="soxBuf"></select>
      <label>Ultra-Low-Latency:</label><select id="ullFx"></select>
    </div>
    <div class="row">
      <label>Pitch (Halbtöne):</label><select id="pitch"></select>
      <label>Reverb:</label><select id="reverb"></select>
      <label>Bass (dB):</label><select id="bass"></select>
      <label>Treble (dB):</label><select id="treble"></select>
    </div>
    <div class="row">
      <label>Servo-Delay (ms):</label><select id="servoDelay"></select>
      <span class="hint">typisch 120–220 ms</span>
    </div>
    <div class="row">
      <button id="saveFxBtn">Einstellungen speichern</button>
      <span id="fxMsg" class="hint"></span>
    </div>
  </div>

  <div class="card">
    <div class="row">
      <button id="startBtn">▶ Start</button>
      <button id="stopBtn">⏹ Stop</button>
      <span id="status" class="hint"></span>
    </div>
    <div class="row"><pre id="logBox"></pre></div>
  </div>

<script>
const themeToggle=document.getElementById('themeToggle');
const root=document.documentElement;

function setTheme(theme, store=true){
  root.classList.toggle('dark', theme === 'dark');
  root.dataset.theme = theme;
  if(store){
    try{ localStorage.setItem('theme', theme); }catch(e){}
  }
  if(themeToggle){ themeToggle.textContent = theme === 'dark' ? '☀️ Hell' : '🌙 Dunkel'; }
}

function initTheme(){
  let theme = 'light';
  try{
    const stored = localStorage.getItem('theme');
    if(stored){ theme = stored; }
    else if(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches){
      theme = 'dark';
    }
  }catch(e){}
  setTheme(theme, false);
}

initTheme();
if(themeToggle){
  themeToggle.addEventListener('click', ()=>{
    const next = root.classList.contains('dark') ? 'light' : 'dark';
    setTheme(next);
  });
}
if(window.matchMedia){
  try{
    const mm = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = (ev)=>{
      try{ if(localStorage.getItem('theme')) return; }catch(e){}
      setTheme(ev.matches ? 'dark' : 'light', false);
    };
    if(typeof mm.addEventListener === 'function') mm.addEventListener('change', handler);
    else if(typeof mm.addListener === 'function') mm.addListener(handler);
  }catch(e){}
}

function selFill(sel, values, stringify=(v)=>String(v), label=(v)=>String(v), selectedVal=null){
  sel.innerHTML = "";
  for(const v of values){
    const o = document.createElement('option');
    o.value = stringify(v);
    o.textContent = label(v);
    if(selectedVal!==null && String(selectedVal)===String(o.value)) o.selected = true;
    sel.appendChild(o);
  }
}
function showMode(){
  const m=document.getElementById('modeSel').value;
  document.getElementById('normalCard').style.display = (m==='normal')?'block':'none';
  document.getElementById('fxCard').style.display = (m==='fx')?'block':'none';
}
document.getElementById('modeSel').addEventListener('change', showMode);

async function jget(u){ const r=await fetch(u); const j=await r.json(); if(!r.ok) throw new Error(j.error||'Fehler'); return j; }
async function jpost(u,body){ const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})}); const j=await r.json(); if(!r.ok) throw new Error(j.error||'Fehler'); return j; }

async function loadDevs(){
  const pa=await jget('/pa-devices');
  const alsa=await jget('/devices');
  const inSel = document.getElementById('inDev'), outSel = document.getElementById('outDev');
  const inFx  = document.getElementById('inDevFx'), alsaOut = document.getElementById('alsaOut');
  inSel.innerHTML=outSel.innerHTML=inFx.innerHTML=alsaOut.innerHTML="";
  for(const d of pa.inputs){ const o=document.createElement('option'); o.value=d.index; o.textContent=`[${d.index}] ${d.name} (${d.api})`; inSel.appendChild(o); inFx.appendChild(o.cloneNode(true)); }
  for(const d of pa.outputs){ const o=document.createElement('option'); o.value=d.index; o.textContent=`[${d.index}] ${d.name} (${d.api})`; outSel.appendChild(o); }
  for(const d of alsa.devices){ const o=document.createElement('option'); o.value=d.value; o.textContent=d.label; alsaOut.appendChild(o); }
}

function fillDropdownSets(lc){
  // Normal
  selFill(document.getElementById('sr'), [32000,44100,48000,88200,96000], String, String, lc.normal.samplerate);
  selFill(document.getElementById('bs'), [32,64,96,128,160,192,256,384,512,1024], String, String, lc.normal.blocksize);
  selFill(document.getElementById('ull'), [{v:"0",t:"aus"},{v:"1",t:"an"}], x=>x.v, x=>x.t, lc.normal.ultra_low_latency?"1":"0");
  const dbrange=[]; for(let i=-12;i<=12;i++) dbrange.push(i);
  selFill(document.getElementById('inGain'), dbrange, String, v=>v+" dB", Math.round(lc.normal.input_gain_db));
  selFill(document.getElementById('outGain'), dbrange, String, v=>v+" dB", Math.round(lc.normal.output_gain_db));

  // FX
  selFill(document.getElementById('srFx'), [32000,44100,48000,88200,96000], String, String, lc.fx.samplerate);
  selFill(document.getElementById('bsFx'), [32,64,96,128,160,192,256,384,512,1024], String, String, lc.fx.blocksize);
  selFill(document.getElementById('soxBuf'), [64,96,128,160,192,256,384,512,768,1024,1536,2048], String, String, lc.fx.sox_buffer_frames);
  selFill(document.getElementById('ullFx'), [{v:"0",t:"aus"},{v:"1",t:"an"}], x=>x.v, x=>x.t, lc.fx.ultra_low_latency?"1":"0");
  const pitch=[]; for(let i=-12;i<=12;i++) pitch.push(i);
  selFill(document.getElementById('pitch'), pitch, String, v=>v+" Halbton", Math.round(lc.fx.fx_pitch_semitones));
  const reverb=[0,5,8,10,12,15,18,25,35,45];
  selFill(document.getElementById('reverb'), reverb, String, v=>v, Math.round(lc.fx.fx_reverb));
  const dbrange2=[]; for(let i=-12;i<=12;i++) dbrange2.push(i);
  selFill(document.getElementById('bass'), dbrange2, String, v=>v+" dB", Math.round(lc.fx.fx_bass_db));
  selFill(document.getElementById('treble'), dbrange2, String, v=>v+" dB", Math.round(lc.fx.fx_treble_db));
  const sDel=[]; for(let i=0;i<=400;i+=10) sDel.push(i);
  selFill(document.getElementById('servoDelay'), sDel, String, v=>v, Math.round(lc.fx.servo_delay_ms||0));

  document.getElementById('modeSel').value = lc.mode || "fx";
  document.getElementById('presetSel').value = lc.fx.preset || "neutral";
}

const PRESETS = {
  neutral: { pitch: 0,   reverb: 0,  bass: 0,  treble: 0 },
  daemon:  { pitch: -8,  reverb: 18, bass: 6,  treble: -2 },
  monster: { pitch: -12, reverb: 12, bass: 9,  treble: -3 },
  cave:    { pitch: -4,  reverb: 35, bass: 2,  treble: -4 },
  helium:  { pitch: +7,  reverb: 8,  bass: -3, treble: +5 },
  funky:   { pitch: +3,  reverb: 10, bass: 2,  treble: +6 },
  whisper: { pitch: 0,   reverb: 45, bass: -6, treble: 2 }
};
function applyPreset(name){
  const p = PRESETS[name] || PRESETS.neutral;
  document.getElementById('pitch').value = String(p.pitch);
  document.getElementById('reverb').value = String(p.reverb);
  document.getElementById('bass').value = String(p.bass);
  document.getElementById('treble').value = String(p.treble);
}
document.getElementById('applyPresetBtn').addEventListener('click', ()=>{
  const name = document.getElementById('presetSel').value;
  applyPreset(name);
});

async function loadLiveConfig(){
  const lc=(await jget('/live-config')).live_config;
  await loadDevs();
  const inSel=document.getElementById('inDev'), outSel=document.getElementById('outDev'), inFx=document.getElementById('inDevFx'), alsaOut=document.getElementById('alsaOut');
  if(lc.normal.input_device!==null){ for(const o of inSel.options){ if(String(o.value)===String(lc.normal.input_device)) o.selected=true; } }
  if(lc.normal.output_device!==null){ for(const o of outSel.options){ if(String(o.value)===String(lc.normal.output_device)) o.selected=true; } }
  if(lc.fx.input_device!==null){ for(const o of inFx.options){ if(String(o.value)===String(lc.fx.input_device)) o.selected=true; } }
  for(const o of alsaOut.options){ if(String(o.value)===String(lc.fx.alsa_out)) o.selected=true; }
  fillDropdownSets(lc);
  showMode();
}

async function refreshStatus(){
  try{ const j=await jget('/live-status'); document.getElementById('status').textContent = j.running ? `läuft (PID ${j.pid}, Modus ${j.mode})` : 'bereit';
  }catch(e){ document.getElementById('status').textContent='Status: Fehler'; }
}
async function refreshLog(){
  try{ const j=await jget('/live-log'); document.getElementById('logBox').textContent=(j.log||[]).join("\\n");
  }catch(e){}
}

document.getElementById('saveModeBtn').onclick = async ()=>{
  try{
    const mode = document.getElementById('modeSel').value;
    const j = await jpost('/live-config', { mode });
    document.getElementById('modeMsg').textContent='Modus gespeichert'; document.getElementById('modeMsg').className='hint ok';
  }catch(e){ document.getElementById('modeMsg').textContent=e.message; document.getElementById('modeMsg').className='hint err'; }
};
document.getElementById('saveNormalBtn').onclick = async ()=>{
  try{
    const body = {
      normal: {
        samplerate: parseInt(document.getElementById('sr').value,10),
        blocksize:  parseInt(document.getElementById('bs').value,10),
        input_device: parseInt(document.getElementById('inDev').value,10),
        output_device: parseInt(document.getElementById('outDev').value,10),
        input_gain_db: parseFloat(document.getElementById('inGain').value),
        output_gain_db: parseFloat(document.getElementById('outGain').value),
        ultra_low_latency: (document.getElementById('ull').value==='1')
      }
    };
    await jpost('/live-config', body);
    document.getElementById('normalMsg').textContent='Gespeichert'; document.getElementById('normalMsg').className='hint ok';
  }catch(e){ document.getElementById('normalMsg').textContent=e.message; document.getElementById('normalMsg').className='hint err'; }
};
document.getElementById('saveFxBtn').onclick = async ()=>{
  try{
    const body = {
      fx: {
        samplerate: parseInt(document.getElementById('srFx').value,10),
        blocksize:  parseInt(document.getElementById('bsFx').value,10),
        input_device: parseInt(document.getElementById('inDevFx').value,10),
        alsa_out: document.getElementById('alsaOut').value,
        fx_pitch_semitones: parseFloat(document.getElementById('pitch').value),
        fx_reverb: parseFloat(document.getElementById('reverb').value),
        fx_bass_db: parseFloat(document.getElementById('bass').value),
        fx_treble_db: parseFloat(document.getElementById('treble').value),
        sox_buffer_frames: parseInt(document.getElementById('soxBuf').value,10),
        ultra_low_latency: (document.getElementById('ullFx').value==='1'),
        servo_delay_ms: parseFloat(document.getElementById('servoDelay').value),
        preset: document.getElementById('presetSel').value
      }
    };
    await jpost('/live-config', body);
    document.getElementById('fxMsg').textContent='Gespeichert'; document.getElementById('fxMsg').className='hint ok';
  }catch(e){ document.getElementById('fxMsg').textContent=e.message; document.getElementById('fxMsg').className='hint err'; }
};

document.getElementById('startBtn').onclick = async ()=>{
  try{
    const mode = document.getElementById('modeSel').value;
    if(mode==='normal'){
      const body={
        mode:'normal',
        samplerate: parseInt(document.getElementById('sr').value,10),
        blocksize:  parseInt(document.getElementById('bs').value,10),
        input_device: parseInt(document.getElementById('inDev').value,10),
        output_device: parseInt(document.getElementById('outDev').value,10),
        input_gain_db: parseFloat(document.getElementById('inGain').value),
        output_gain_db: parseFloat(document.getElementById('outGain').value),
        ultra_low_latency: document.getElementById('ull').value==='1'
      };
      await jpost('/live-start', body);
    }else{
      const body={
        mode:'fx',
        samplerate: parseInt(document.getElementById('srFx').value,10),
        blocksize:  parseInt(document.getElementById('bsFx').value,10),
        input_device: parseInt(document.getElementById('inDevFx').value,10),
        alsa_out: document.getElementById('alsaOut').value,
        fx_pitch_semitones: parseFloat(document.getElementById('pitch').value),
        fx_reverb: parseFloat(document.getElementById('reverb').value),
        fx_bass_db: parseFloat(document.getElementById('bass').value),
        fx_treble_db: parseFloat(document.getElementById('treble').value),
        sox_buffer_frames: parseInt(document.getElementById('soxBuf').value,10),
        ultra_low_latency: document.getElementById('ullFx').value==='1',
        servo_delay_ms: parseFloat(document.getElementById('servoDelay').value)
      };
      await jpost('/live-start', body);
    }
    await refreshStatus(); setTimeout(refreshLog, 300);
  }catch(e){ alert(e.message); }
};
document.getElementById('stopBtn').onclick = async ()=>{ try{ await jpost('/live-stop', {}); await refreshStatus(); }catch(e){ alert(e.message);} };

window.addEventListener('DOMContentLoaded', async ()=>{
  await loadLiveConfig();
  await refreshStatus(); await refreshLog();
  setInterval(refreshStatus,1500); setInterval(refreshLog,1500);
});
</script>
</body>
</html>
"""

# ===== Routes: Pages =====
@app.get("/")
def index():
    sounds = list_mp3s()
    assignments = {s["file"]: s["categories"] for s in sounds if s.get("categories")}
    return render_template_string(
        PAGE_INDEX,
        sounds=sounds,
        categories=cfg.get("categories", []),
        assignments=assignments
    )

@app.get("/settings")
def settings():
    return render_template_string(PAGE_SETTINGS)

@app.get("/live")
def live_page():
    if not HAVE_SD:
        return "sounddevice (PortAudio) ist nicht installiert. Bitte: pip3 install sounddevice", 500
    return render_template_string(PAGE_LIVE)

# ===== Routes: Info/Devices/Volume/Errors/Config/Paths =====
@app.get("/info")
def info():
    return jsonify(alsa_device=cfg["alsa_device"], card_index=cfg["alsa_card_index"])

@app.get("/devices")
def devices_get():
    devs, raw = aplay_list_devices()
    device_list = [{"label":"default (System)", "value":"default"}] + devs
    return jsonify(ok=True, devices=device_list,
                   current={"alsa_device":cfg["alsa_device"], "alsa_card_index":cfg["alsa_card_index"]},
                   debug=raw)

@app.post("/device")
def device_post():
    data = request.get_json(silent=True) or {}
    alsa = data.get("alsa_device") or request.form.get("alsa_device")
    if not alsa:
        return jsonify(error="Parameter 'alsa_device' fehlt"), 400
    cfg["alsa_device"] = alsa
    cfg["alsa_card_index"] = device_to_card_index(alsa)
    save_config()
    return jsonify(ok=True, alsa_device=cfg["alsa_device"], alsa_card_index=cfg["alsa_card_index"])

@app.get("/volume")
def volume_get():
    v, m, ctl = get_volume_state()
    if v is None and m is None and ctl is None:
        return jsonify(error=f"Kein Mixer-Control gefunden (Karte {cfg['alsa_card_index']})."), 500
    return jsonify(ok=True, volume=v, muted=m, control=ctl, card=cfg["alsa_card_index"])

@app.post("/volume")
def volume_post():
    data = request.get_json(silent=True) or {}
    v = data.get("volume") or request.form.get("volume")
    toggle = data.get("toggle_mute") or request.form.get("toggle_mute")
    if isinstance(toggle, str): toggle = toggle.lower() in ("1","true","yes","on")
    toggle = bool(toggle)
    if v is not None:
        try: v = int(v)
        except: return jsonify(error="Ungültiger volume-Wert"), 400
    nv, nm, ctl = set_volume(vol=v, toggle_mute=toggle)
    if nv is None and nm is None and ctl is None:
        return jsonify(error="Mixer konnte nicht gesetzt/gelesen werden."), 500
    return jsonify(ok=True, volume=nv, muted=nm, control=ctl, card=cfg["alsa_card_index"])

@app.get("/last-error")
def last_error_get():
    return jsonify(ts=last_error.get("ts"), msg=last_error.get("msg"))

@app.get("/last-cmd")
def last_cmd_get():
    return jsonify(cmd=last_cmd.get("text"))

@app.get("/app-config")
def app_config_get():
    return jsonify(sync_lead_ms=cfg.get("sync_lead_ms"),
                   servo_gpio=cfg.get("servo_gpio"),
                   power_gpio=cfg.get("power_gpio"),
                   closed_angle=cfg.get("closed_angle"),
                   open_angle=cfg.get("open_angle"),
                   pigpio_connected=bool(HAVE_PIGPIO and pi and pi.connected),
                   gpio_options=GPIO_OPTIONS,
                   sound_dir=str(SOUND_DIR),
                   config_path=str(CONFIG_PATH),
                   alsa_device=cfg.get("alsa_device"))

@app.post("/sync")
def sync_post():
    data = request.get_json(silent=True) or {}
    try:
        val = int(data.get("sync_lead_ms"))
        if val < 0 or val > 1000: raise ValueError()
    except Exception:
        return jsonify(error="sync_lead_ms muss 0..1000 (ms) sein"), 400
    cfg["sync_lead_ms"] = val
    save_config()
    return jsonify(ok=True, sync_lead_ms=cfg["sync_lead_ms"])

@app.post("/angles")
def angles_post():
    data = request.get_json(silent=True) or {}
    try:
        ca = int(data.get("closed_angle"))
        oa = int(data.get("open_angle"))
        if not (0 <= ca <= 180 and 0 <= oa <= 180): raise ValueError()
        if oa < ca: return jsonify(error="OPEN_ANGLE muss ≥ CLOSED_ANGLE sein"), 400
    except Exception:
        return jsonify(error="Bitte Winkel 0..180 übermitteln"), 400
    cfg["closed_angle"] = ca
    cfg["open_angle"]   = oa
    save_config()
    return jsonify(ok=True, closed_angle=ca, open_angle=oa)

def _apply_gpio_runtime():
    if not (HAVE_PIGPIO and pi and pi.connected): return
    stop_servo()
    s = cfg.get("servo_gpio")
    if s is not None:
        try: pi.set_mode(s, pigpio.OUTPUT); pi.set_servo_pulsewidth(s, 0)
        except Exception: pass
    p = cfg.get("power_gpio")
    if p is not None:
        try: pi.set_mode(p, pigpio.OUTPUT); pi.write(p, 0)
        except Exception: pass

@app.post("/gpio")
def gpio_post():
    data = request.get_json(silent=True) or {}
    def parse_gpio(v):
        if v is None: return None
        if isinstance(v, str):
            t = v.strip().lower()
            if t in ("", "none", "null"): return None
            try: return int(t)
            except: return None
        if isinstance(v, (int, float)): return int(v)
        return None
    cfg["servo_gpio"] = parse_gpio(data.get("servo_gpio"))
    cfg["power_gpio"] = parse_gpio(data.get("power_gpio"))
    save_config()
    _apply_gpio_runtime()
    return jsonify(ok=True, servo_gpio=cfg["servo_gpio"], power_gpio=cfg["power_gpio"])

@app.post("/paths")
def paths_post():
    global SOUND_DIR, CONFIG_PATH
    data = request.get_json(silent=True) or {}
    new_sd = data.get("sound_dir")
    new_cp = data.get("config_path")
    if new_sd:
        try:
            nd = Path(new_sd).expanduser().resolve()
            nd.mkdir(parents=True, exist_ok=True)
            SOUND_DIR = nd
        except Exception as e:
            return jsonify(error=f"SOUND_DIR ungültig/nicht anlegbar: {e}"), 400
    if new_cp:
        try:
            npth = Path(new_cp).expanduser().resolve()
            npth.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH = npth
        except Exception as e:
            return jsonify(error=f"CONFIG_PATH ungültig: {e}"), 400
    save_config(); ensure_dirs()
    return jsonify(ok=True, sound_dir=str(SOUND_DIR), config_path=str(CONFIG_PATH))

def _normalize_category_name(name):
    if name is None:
        return None
    if isinstance(name, str):
        name = name.strip()
        return name or None
    return None

def _ensure_category_structures():
    cfg.setdefault("categories", [])
    cfg["file_categories"] = _normalized_assignment_map(cfg.get("file_categories", {}))

@app.get("/categories")
def categories_get():
    _ensure_category_structures()
    existing_files = {p.name for p in SOUND_DIR.glob("*.mp3")} | {p.name for p in SOUND_DIR.glob("*.MP3")}
    assignments = {}
    removed = []
    for fn, cats in cfg["file_categories"].items():
        if fn in existing_files:
            if cats:
                assignments[fn] = cats
        else:
            removed.append(fn)
    if removed:
        for key in removed:
            cfg["file_categories"].pop(key, None)
        save_config()
    return jsonify(ok=True, categories=cfg["categories"], assignments=assignments)

@app.post("/categories")
def categories_post():
    _ensure_category_structures()
    data = request.get_json(silent=True) or {}
    name = _normalize_category_name(data.get("name"))
    if not name:
        return jsonify(error="Name der Kategorie fehlt"), 400
    if any(name.lower() == c.lower() for c in cfg["categories"]):
        return jsonify(error="Kategorie existiert bereits"), 400
    cfg["categories"].append(name)
    save_config()
    return jsonify(ok=True, categories=cfg["categories"], assignments=cfg["file_categories"])

@app.delete("/categories/<path:name>")
def categories_delete(name):
    _ensure_category_structures()
    cname = _normalize_category_name(name)
    if not cname:
        return jsonify(error="Kategorie nicht gefunden"), 404
    target = None
    for existing in cfg["categories"]:
        if existing.lower() == cname.lower():
            target = existing
            break
    if target is None:
        return jsonify(error="Kategorie nicht gefunden"), 404

    target_lower = target.lower()
    cfg["categories"] = [c for c in cfg["categories"] if c.lower() != target_lower]

    for fn, cats in list(cfg["file_categories"].items()):
        if not isinstance(cats, list):
            continue
        remaining = [c for c in cats if c.lower() != target_lower]
        if remaining:
            cfg["file_categories"][fn] = remaining
        else:
            cfg["file_categories"].pop(fn, None)

    save_config()
    return jsonify(ok=True, categories=cfg["categories"], assignments=cfg["file_categories"], removed=target)

@app.post("/file-category")
def file_category_post():
    _ensure_category_structures()
    data = request.get_json(silent=True) or {}
    fn = data.get("file")
    if not fn:
        return jsonify(error="Parameter 'file' fehlt"), 400
    resolve_file(fn)
    raw_cats = data.get("categories", None)
    if raw_cats is None:
        raw_single = data.get("category", None)
        if isinstance(raw_single, list):
            raw_cats = raw_single
        elif raw_single is None:
            raw_cats = []
        else:
            raw_cats = [raw_single]
    elif isinstance(raw_cats, str):
        raw_cats = [raw_cats]
    elif not isinstance(raw_cats, (list, tuple, set)):
        return jsonify(error="Ungültiges Format für Kategorien"), 400

    normalized = []
    for cat in raw_cats:
        cname = _normalize_category_name(cat)
        if not cname:
            continue
        if not any(cname == c for c in cfg["categories"]):
            return jsonify(error=f"Unbekannte Kategorie: {cname}"), 400
        if cname not in normalized:
            normalized.append(cname)

    if normalized:
        cfg["file_categories"][fn] = normalized
    else:
        cfg["file_categories"].pop(fn, None)
    save_config()
    cats = cfg["file_categories"].get(fn, [])
    return jsonify(ok=True, file=fn, categories=cats, category=cats[0] if cats else None)

# ===== PortAudio Devices =====
@app.get("/pa-devices")
def pa_devices():
    if not HAVE_SD:
        return jsonify(error="sounddevice nicht installiert"), 500
    inputs, outputs = [], []
    try:
        devs = sd.query_devices()
        for idx, d in enumerate(devs):
            api = sd.query_hostapis(d["hostapi"])["name"]
            ent = {"index": idx, "name": d["name"], "api": api, "in": d["max_input_channels"], "out": d["max_output_channels"]}
            if d["max_input_channels"] > 0: inputs.append(ent)
            if d["max_output_channels"] > 0: outputs.append(ent)
    except Exception as e:
        return jsonify(error=f"Geräteliste fehlgeschlagen: {e}"), 500
    return jsonify(ok=True, inputs=inputs, outputs=outputs)

# ===== Live Config (Normal + FX) =====
@app.get("/live-config")
def live_config_get():
    return jsonify(live_config=cfg["live_config"])

@app.post("/live-config")
def live_config_post():
    data = request.get_json(silent=True) or {}
    lc = cfg["live_config"]

    if "mode" in data:
        if data["mode"] in ("normal","fx"):
            lc["mode"] = data["mode"]
        else:
            return jsonify(error="mode muss 'normal' oder 'fx' sein"), 400

    if "normal" in data and isinstance(data["normal"], dict):
        n = data["normal"]
        for k in ("samplerate","blocksize","input_device","output_device"):
            if k in n and n[k] is not None:
                try: lc["normal"][k] = int(n[k])
                except: return jsonify(error=f"normal.{k} ungültig"), 400
        for k in ("input_gain_db","output_gain_db"):
            if k in n and n[k] is not None:
                try: lc["normal"][k] = float(n[k])
                except: return jsonify(error=f"normal.{k} ungültig"), 400
        if "ultra_low_latency" in n:
            lc["normal"]["ultra_low_latency"] = bool(n["ultra_low_latency"])

    if "fx" in data and isinstance(data["fx"], dict):
        f = data["fx"]
        for k in ("samplerate","blocksize","input_device","sox_buffer_frames"):
            if k in f and f[k] is not None:
                try: lc["fx"][k] = int(f[k])
                except: return jsonify(error=f"fx.{k} ungültig"), 400
        for k in ("fx_pitch_semitones","fx_reverb","fx_bass_db","fx_treble_db","servo_delay_ms"):
            if k in f and f[k] is not None:
                try: lc["fx"][k] = float(f[k])
                except: return jsonify(error=f"fx.{k} ungültig"), 400
        if "alsa_out" in f and f["alsa_out"]:
            lc["fx"]["alsa_out"] = str(f["alsa_out"])
        if "ultra_low_latency" in f:
            lc["fx"]["ultra_low_latency"] = bool(f["ultra_low_latency"])
        if "preset" in f:
            lc["fx"]["preset"] = str(f["preset"])

    save_config()
    return jsonify(ok=True, live_config=lc)

# ===== Live Prozess =====
def _live_running():
    p = live_proc.get("p")
    return p is not None and p.poll() is None

@app.get("/live-status")
def live_status():
    p = live_proc.get("p")
    return jsonify(running=_live_running(), pid=(p.pid if p else None), mode=live_proc.get("mode"))

@app.get("/live-log")
def live_log_get():
    return jsonify(log=list(live_log))

@app.post("/live-stop")
def live_stop():
    p = live_proc.get("p")
    if p and p.poll() is None:
        try: p.terminate()
        except Exception: pass
        try: p.wait(timeout=1.5)
        except Exception:
            try: p.kill()
            except Exception: pass
    live_proc["p"] = None
    live_proc["mode"] = None
    live_proc["args"] = None
    live_log.clear()
    power_off()
    return jsonify(ok=True, stopped=True)

@app.post("/live-start")
def live_start():
    if not HAVE_SD:
        return jsonify(error="sounddevice nicht installiert"), 500
    if _live_running():
        return jsonify(error="Live läuft bereits – zuerst stoppen"), 409
    data = request.get_json(silent=True) or {}
    mode = data.get("mode") or cfg["live_config"]["mode"]
    if mode not in ("normal","fx"):
        return jsonify(error="mode muss 'normal' oder 'fx' sein"), 400

    base_args = ["python3", str(Path(__file__).resolve()), "--live",
                 "--closed_angle", str(cfg.get("closed_angle",5)),
                 "--open_angle",   str(cfg.get("open_angle",65)),
                 "--servo_gpio",   str(cfg.get("servo_gpio")) if cfg.get("servo_gpio") is not None else "None",
                 "--power_gpio",   str(cfg.get("power_gpio")) if cfg.get("power_gpio") is not None else "None"
                 ]

    if mode == "normal":
        N = cfg["live_config"]["normal"]
        samplerate = int(data.get("samplerate", N["samplerate"]))
        blocksize  = int(data.get("blocksize",  N["blocksize"]))
        input_dev  = int(data.get("input_device",  N["input_device"] if N["input_device"] is not None else 0))
        output_dev = int(data.get("output_device", N["output_device"] if N["output_device"] is not None else 0))
        in_gain    = float(data.get("input_gain_db",  N["input_gain_db"]))
        out_gain   = float(data.get("output_gain_db", N["output_gain_db"]))
        ull        = bool(data.get("ultra_low_latency", N["ultra_low_latency"]))
        base_args += ["--samplerate", str(samplerate),
                      "--blocksize",  str(blocksize),
                      "--input_device",  str(input_dev),
                      "--output_device", str(output_dev),
                      "--input_gain_db",  str(in_gain),
                      "--output_gain_db", str(out_gain),
                      "--mode", "normal"]
        if ull: base_args += ["--ultra_low_latency"]
    else:
        F = cfg["live_config"]["fx"]
        samplerate = int(data.get("samplerate", F["samplerate"]))
        blocksize  = int(data.get("blocksize",  F["blocksize"]))
        input_dev  = int(data.get("input_device",  F["input_device"] if F["input_device"] is not None else 0))
        alsa_out   = str(data.get("alsa_out", F["alsa_out"] or cfg.get("alsa_device","plughw:1,0")))
        pitch      = float(data.get("fx_pitch_semitones", F["fx_pitch_semitones"]))
        reverb     = float(data.get("fx_reverb",          F["fx_reverb"]))
        bass       = float(data.get("fx_bass_db",         F["fx_bass_db"]))
        treble     = float(data.get("fx_treble_db",       F["fx_treble_db"]))
        soxbuf     = int(data.get("sox_buffer_frames",    F["sox_buffer_frames"]))
        ull        = bool(data.get("ultra_low_latency",   F["ultra_low_latency"]))
        sdelay     = float(data.get("servo_delay_ms",     F["servo_delay_ms"]))
        base_args += [
            "--samplerate", str(samplerate),
            "--blocksize",  str(blocksize),
            "--input_device",  str(input_dev),
            "--alsa_out", alsa_out,
            "--fx_pitch_semitones", str(pitch),
            "--fx_reverb", str(reverb),
            "--fx_bass_db", str(bass),
            "--fx_treble_db", str(treble),
            "--sox_buffer_frames", str(soxbuf),
            "--servo_delay_ms", str(sdelay),
            "--mode", "fx"
        ]
        if ull: base_args += ["--ultra_low_latency"]

    try:
        p = subprocess.Popen(base_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, universal_newlines=True)
        live_proc["p"] = p
        live_proc["mode"] = mode
        live_proc["args"] = base_args
        live_log.append(" ".join(shlex.quote(a) for a in base_args))
        def _reader():
            for line in p.stdout:
                live_log.append(line.rstrip())
        threading.Thread(target=_reader, daemon=True).start()
        return jsonify(ok=True, pid=p.pid, mode=mode)
    except Exception as e:
        return jsonify(error=f"Start fehlgeschlagen: {e}"), 500

# ===== Playback Routes =====
def _get_filename_from_request():
    data = request.get_json(silent=True) or {}
    return data.get("file") or request.form.get("file") or request.args.get("file")

@app.post("/play")
def play():
    fn = _get_filename_from_request()
    if not fn:
        set_last_error("Parameter 'file' fehlt")
        return jsonify(error="Parameter 'file' fehlt."), 400
    try:
        path = resolve_file(fn)
        try:
            precomputed = compute_envelope(path)
        except Exception as e:
            precomputed = None
            set_last_error(f"Envelope-Berechnung: {e}")

        with play_lock:
            stop_current()
            power_on()
            pr = start_play(path)
            current_proc["p"] = pr
            current_proc["file"] = path.name
            try:
                servo_open_close_by_envelope(pr, path, precomputed=precomputed)
            except Exception as e:
                set_last_error(f"Servo/Envelope: {e}")
        return jsonify(ok=True, now_playing=path.name)
    except Exception as e:
        set_last_error(str(e))
        power_off()
        return jsonify(error=f"Play fehlgeschlagen: {e}"), 500

@app.post("/stop")
def stop():
    with play_lock:
        stop_current()
    return jsonify(ok=True, stopped=True)

@app.get("/status")
def status():
    with play_lock:
        p = current_proc.get("p")
        if p and p.poll() is None:
            return jsonify(playing=True, file=current_proc.get("file"))
    return jsonify(playing=False)

@app.post("/test-tone")
def test_tone():
    try:
        dev = cfg["alsa_device"] if cfg["alsa_device"] != "default" else None
        args = ["speaker-test", "-t", "sine", "-f", "440", "-l", "1"]
        if dev: args.extend(["-D", dev])
        proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=5)
        if proc.returncode == 0:
            return jsonify(ok=True, message="Testton ausgegeben (speaker-test).")
        return jsonify(ok=False, error="speaker-test meldete Fehler."), 200
    except Exception as e:
        return jsonify(ok=False, error=f"speaker-test nicht verfügbar/fehlgeschlagen: {e}"), 200

# ===== Live-Subprozess =====
def angle_to_us_local(angle, mn=SERVO_US_MIN, mx=SERVO_US_MAX):
    angle = float(max(0.0, min(180.0, angle)))
    return int(mn + (mx - mn) * (angle/180.0))

def build_sox_cmd(alsa_out, samplerate, sox_buffer_frames, fx):
    # Ausgabe-Typ ALSA, Zielgerät als Name (z. B. plughw:1,0)
    cmd = [
        "sox", "-V1", "-q",
        "--buffer", str(max(64, int(sox_buffer_frames))),
        "-t","raw","-e","floating-point","-b","32","-L","-c","1","-r", str(int(samplerate)), "-",
        "-t","alsa", str(alsa_out)
    ] + fx
    return cmd

def live_main(argv):
    if not HAVE_SD:
        print("sounddevice nicht installiert (pip3 install sounddevice)", file=sys.stderr)
        sys.exit(1)

    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["normal","fx"], default="normal")
    p.add_argument("--samplerate", type=int, default=48000)
    p.add_argument("--blocksize",  type=int, default=256)
    p.add_argument("--input_device",  type=int, default=None)
    p.add_argument("--output_device", type=int, default=None)
    p.add_argument("--input_gain_db", type=float, default=0.0)
    p.add_argument("--output_gain_db", type=float, default=0.0)
    p.add_argument("--ultra_low_latency", action="store_true")

    # FX/SoX
    p.add_argument("--alsa_out", type=str, default=None)
    p.add_argument("--fx_pitch_semitones", type=float, default=0.0)
    p.add_argument("--fx_reverb", type=float, default=0.0)
    p.add_argument("--fx_bass_db", type=float, default=0.0)
    p.add_argument("--fx_treble_db", type=float, default=0.0)
    p.add_argument("--sox_buffer_frames", type=int, default=256)
    p.add_argument("--servo_delay_ms", type=float, default=0.0)

    # Servo/GPIO
    p.add_argument("--servo_gpio", type=str, default="None")
    p.add_argument("--power_gpio", type=str, default="None")
    p.add_argument("--closed_angle", type=float, default=5.0)
    p.add_argument("--open_angle",   type=float, default=65.0)

    args = p.parse_args(argv)

    # pigpio init
    pi_local = None
    servo_gpio = None if str(args.servo_gpio).lower() in ("none","null","") else int(args.servo_gpio)
    power_gpio = None if str(args.power_gpio).lower() in ("none","null","") else int(args.power_gpio)
    if servo_gpio is not None or power_gpio is not None:
        try:
            pi_local = pigpio.pi()
            if not pi_local.connected:
                print("pigpio nicht verbunden – sudo systemctl start pigpio", file=sys.stderr); sys.exit(2)
            if power_gpio is not None:
                pi_local.set_mode(power_gpio, pigpio.OUTPUT); pi_local.write(power_gpio, 1)
            if servo_gpio is not None:
                pi_local.set_mode(servo_gpio, pigpio.OUTPUT); pi_local.set_servo_pulsewidth(servo_gpio, angle_to_us_local(args.closed_angle))
        except Exception as e:
            print(f"GPIO init Fehler: {e}", file=sys.stderr)
            pi_local = None

    # Hüllkurven-Glättung abhängig von blocksize
    frame_ms = 1000.0 * args.blocksize / args.samplerate
    atk_a = np.exp(-frame_ms / max(1.0, ATTACK_MS))
    rel_a = np.exp(-frame_ms / max(1.0, RELEASE_MS))
    hist = deque(maxlen=max(1, int(2.5 * args.samplerate / args.blocksize)))
    y = 0.0

    def set_angle(a):
        if pi_local and servo_gpio is not None:
            pi_local.set_servo_pulsewidth(servo_gpio, angle_to_us_local(a))

    def close_out():
        try:
            if pi_local:
                if servo_gpio is not None:
                    pi_local.set_servo_pulsewidth(servo_gpio, angle_to_us_local(args.closed_angle)); time.sleep(0.1)
                    pi_local.set_servo_pulsewidth(servo_gpio, 0)
                if power_gpio is not None:
                    pi_local.write(power_gpio, 0)
        except Exception: pass
        try:
            if pi_local: pi_local.stop()
        except Exception: pass

    in_gain  = float(10.0 ** (args.input_gain_db  / 20.0))
    out_gain = float(10.0 ** (args.output_gain_db / 20.0))

    # Servo-Delay in Blöcken (für FX-Pfad)
    delay_blocks = int(round(max(0.0, args.servo_delay_ms) * args.samplerate / args.blocksize / 1000.0))
    q_delay = deque(maxlen=delay_blocks if delay_blocks>0 else 1)

    # SoX vorbereiten (nur Modus fx)
    sox_proc = None
    if args.mode == "fx":
        if not args.alsa_out:
            print("Mit Modus fx bitte --alsa_out angeben (z.B. plughw:1,0).", file=sys.stderr); close_out(); sys.exit(3)
        cents = args.fx_pitch_semitones * 100.0
        fx = []
        if abs(cents) > 0.01: fx += ["pitch", f"{cents:.1f}"]
        if args.fx_reverb > 0.0:   fx += ["reverb", f"{args.fx_reverb:.1f}"]
        if abs(args.fx_bass_db) > 0.01:   fx += ["bass", f"{args.fx_bass_db:+.1f}"]
        if abs(args.fx_treble_db) > 0.01: fx += ["treble", f"{args.fx_treble_db:+.1f}"]

        cmd = build_sox_cmd(args.alsa_out, args.samplerate, args.sox_buffer_frames, fx)
        try:
            sox_proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, text=False
            )
            print("SoX:", " ".join(shlex.quote(c) for c in cmd))
        except FileNotFoundError:
            print("SoX fehlt: sudo apt-get install -y sox libsox-fmt-alsa", file=sys.stderr); close_out(); sys.exit(4)

    def process_block(mono_block):
        nonlocal y
        rms = float(np.sqrt(np.mean(mono_block * mono_block) + 1e-20))
        hist.append(rms)
        level_db = 20.0 * np.log10(max(rms, 1e-12))
        if level_db < SILENCE_GATE_DBFS:
            x = 0.0
        else:
            arr = np.array(hist, dtype=np.float64)
            ref = float(np.percentile(arr[arr>0], NORM_PERCENTILE)) if np.any(arr>0) else 1.0
            if ref <= 0: ref = 1.0
            x = max(0.0, min(1.0, rms/ref))
        y = atk_a*y + (1.0-atk_a)*x if x>y else rel_a*y + (1.0-rel_a)*x
        angle = float(args.closed_angle) + (float(args.open_angle) - float(args.closed_angle)) * y
        set_angle(angle)

    def callback(indata, outdata, frames, time_info, status):
        if status: print(status, file=sys.stderr)
        block = indata
        mono = block.mean(axis=1).astype(np.float32) if block.ndim > 1 else block.astype(np.float32)
        mono = np.clip(mono * in_gain, -1.0, 1.0)

        if args.mode == "fx":
            if sox_proc is not None and sox_proc.stdin:
                try:
                    sox_proc.stdin.write(mono.tobytes(order="C"))
                except BrokenPipeError:
                    pass
        else:
            # Direkte Ausgabe über PortAudio
            if outdata is not None:
                out_buf = np.clip(mono * out_gain, -1.0, 1.0)
                if outdata.ndim == 1: outdata[:] = out_buf
                else:
                    outdata[:,0] = out_buf
                    if outdata.shape[1] > 1: outdata[:,1] = out_buf

        # Servo-Delay (nur sinnvoll bei FX)
        if args.mode == "fx" and delay_blocks > 0:
            q_delay.append(mono)
            if len(q_delay) == q_delay.maxlen:
                process_block(q_delay[0])
        else:
            process_block(mono)

    latency_kw = {"latency": "low"} if args.ultra_low_latency else {}
    try:
        in_channels = 1
        if args.input_device is not None:
            try:
                info_in = sd.query_devices(args.input_device)
                max_in = int(info_in.get("max_input_channels", 0))
                if max_in < 1:
                    print(f"Ausgewähltes Input-Gerät (# {args.input_device}) besitzt keine Eingangskanäle.", file=sys.stderr)
                    sys.exit(11)
            except Exception as e:
                print(f"Warnung: Eingabegerät #{args.input_device} konnte nicht abgefragt werden ({e}). Nutze Mono (1 Kanal).", file=sys.stderr)

        if args.mode == "fx":
            stream = sd.InputStream(samplerate=args.samplerate, blocksize=args.blocksize, dtype="float32",
                                    channels=in_channels, device=args.input_device,
                                    callback=lambda indata, frames, time_info, status: callback(indata, None, frames, time_info, status),
                                    **latency_kw)
        else:
            out_channels = 1
            if args.output_device is not None:
                try:
                    info_out = sd.query_devices(args.output_device)
                    max_out = int(info_out.get("max_output_channels", 0))
                    if max_out < 1:
                        print(f"Ausgewähltes Output-Gerät (# {args.output_device}) besitzt keine Ausgangskanäle.", file=sys.stderr)
                        sys.exit(12)
                    out_channels = 2 if max_out >= 2 else 1
                except Exception as e:
                    print(f"Warnung: Ausgabegerät #{args.output_device} konnte nicht abgefragt werden ({e}). Nutze Mono (1 Kanal).", file=sys.stderr)
            stream = sd.Stream(samplerate=args.samplerate, blocksize=args.blocksize, dtype="float32",
                               channels=(in_channels, out_channels), device=(args.input_device, args.output_device),
                               callback=callback, **latency_kw)
        print(f"Live gestartet (Modus {args.mode}). Strg+C zum Beenden.")
        with stream:
            try:
                signal.pause()
            except AttributeError:
                while True: time.sleep(1)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print("Fehler:", e, file=sys.stderr)
        sys.exit(10)
    finally:
        try:
            if sox_proc is not None:
                try:
                    if sox_proc.stdin: sox_proc.stdin.close()
                except Exception: pass
                try: sox_proc.terminate(); sox_proc.wait(timeout=1.0)
                except Exception:
                    try: sox_proc.kill()
                    except Exception: pass
        except Exception: pass
        close_out()

# ===== Main =====
if __name__ == "__main__":
    if "--live" in sys.argv:
        argv = [a for a in sys.argv[1:] if a != "--live"]
        live_main(argv)
        sys.exit(0)

    ensure_dirs()
    load_config()
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
    cfg["live_config"] = _merge_defaults(cfg.get("live_config", {}), DEFAULT_CONFIG["live_config"])

    # pigpio global verbinden
    if HAVE_PIGPIO:
        try:
            pi = pigpio.pi()
            if pi.connected:
                s = cfg.get("servo_gpio")
                p = cfg.get("power_gpio")
                if s is not None:
                    pi.set_mode(s, pigpio.OUTPUT); pi.set_servo_pulsewidth(s, 0)
                if p is not None:
                    pi.set_mode(p, pigpio.OUTPUT); pi.write(p, 0)
            else:
                pi = None
        except Exception as e:
            print(f"pigpio init Fehler: {e}", file=sys.stderr)
            pi = None

    print(f"Soundboard läuft auf http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False)
