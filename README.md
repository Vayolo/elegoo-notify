# elegoo-notify

[![License: GPL v2](https://img.shields.io/badge/License-GPLv2-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](requirements.txt)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](Dockerfile)
[![Tests](https://img.shields.io/badge/tests-75%20checks-green)](#testing)

**Self-hosted monitoring, Telegram notifications, AI print-failure detection and
remote control for the Elegoo Centauri Carbon 3D printer.**

Single Docker container, runs on modest hardware (a Celeron is fine), no cloud
services, no subscriptions. Your printer, your camera frames, your data.

---

## What you get

- **Real-time printer link** — native SDCP v3 WebSocket connection with
  automatic reconnection, heartbeat and multi-URL failover.
- **Telegram notifications with photos** — print started / progress milestones /
  time-based updates / completed / failed, each with a high-quality snapshot
  from the printer camera. Rate-limit aware, debounced, critical messages
  (errors, AI alerts) always go through immediately.
- **Interactive Telegram commands** — ask for status with a photo, change print
  speed, toggle the chamber light, list files, start a print, or send a GCODE
  file directly in the chat; destructive actions require a two-step confirm.
- **AI print-failure detection** — a two-layer stack:
  - a small ONNX neural network (ShuffleNetV2 encoder + prototypes, from the
    [PrintGuard](https://github.com/oliverbravery/PrintGuard) project) scoring
    every camera frame 0–1, and
  - pure-OpenCV heuristics (static stringing detector, layer-shift, smoke,
    per-layer NRMSE analysis inspired by
    [3DPrintSaviour](https://github.com/Manicben/3DPrintSaviour)).
- **Optional auto-stop** — on a critical AI alert the service notifies you
  *before* acting, sends the stop command, then confirms *after* — with photos.
  Disabled by default: you decide when to arm it.
- **Home Assistant integration** — 18 entities via MQTT discovery (progress,
  temperatures, AI risk, buttons, a settable speed number and a light), a
  ready-made Lovelace dashboard, and a camera that feeds from the service
  fan-out.
- **Web dashboard** — lightweight SSE page with live state, progress bar,
  temperature chart and webcam feed. No build tools, one HTML file.
- **REST API** — status, commands, GCODE upload, snapshots, SSE event stream.

## How it works

```
            Elegoo Centauri Carbon                elegoo-notify (Docker, host network)
 ┌────────────────────────────────┐   ┌───────────────────────────────────────────────────┐
 │ WS  :3030  SDCP v3 control     │◄──┤ sdcp.ws_connector  failover + heartbeat + retry  │
 │ MJPEG :3031  camera (1 stream) │◄──┤ webcam             single stream → fan-out to    │
 │ HTTP :3030  /uploadFile/upload │◄──┤ uploads            AI / Telegram / HA / dashboard │
 └────────────────────────────────┘   ├───────────────────────────────────────────────────┤
                                      │ state + event bus  derives lifecycle events from  │
                                      │                    PrintInfo.Status transitions:  │
                                      │                    0→10/13 = started, 9 = done,  │
                                      │                    8+error = failed, 13 = active │
                                      ├───────────────────────────────────────────────────┤
                                      │ notify.progress_manager → telegram (photos)       │
                                      │ ai.monitor  → ML score + CV detectors            │
                                      │             → LayerWatch (per-layer NRMSE)       │
                                      │ api.server  → REST + SSE + dashboard (:8766)      │
                                      │ api.ha_bridge → MQTT discovery (18 entities)      │
                                      └───────────────────────────────────────────────────┘
```

**Key design points**

- The printer does **not** send "print started" events: the service derives the
  full lifecycle from SDCP status transitions (it also handles the undocumented
  real-firmware status code `13` = printing, and the native `Progress` field).
- The Centauri allows **one camera client at a time**: the service opens a
  single MJPEG connection and re-distributes frames to every consumer (AI
  detector, Telegram photos, HA camera, dashboard).
- The AI stack is **CPU-only**: the ML model is ~5 MB and runs once every 4 s;
  the CV heuristics are tuned to reject the moving print head (ROI excludes the
  gantry area) and to learn each print's own "normal" (adaptive baselines).
  On a healthy Centauri print the ML score sits around **0.07** against a 0.6
  alert threshold — false positives are practically ruled out, and zero were
  observed across real prints.

## Requirements

- Docker + Docker Compose v2
- An Elegoo Centauri Carbon reachable on your LAN (default `192.168.1.56`)
- Python 3.11+ **only if** you run the tests/simulator outside Docker
- ~150 MB RAM, negligible CPU (AI adds ~30–60 ms per frame at 480px)

Python dependencies (installed inside the image, see `requirements.txt`):
`fastapi`, `uvicorn`, `websockets`, `aiohttp`, `aiomqtt`, `opencv-python-headless`,
`numpy`, `onnxruntime` (optional at runtime — the service falls back to the CV
stack if the model or the library is missing).

## Quick start

```bash
git clone https://github.com/Vayolo/elegoo-notify.git
cd elegoo-notify

# 1) Telegram credentials
cp .env.example .env
nano .env                 # set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID (see below)

# 2) Configuration
cp config.json.example config.json
nano config.json          # adjust printer IP / MQTT broker / AI options

# 3) (optional but recommended) download the ML detector models
python3 scripts/download_models.py

# 4) Build & run
docker compose up --build -d
curl http://127.0.0.1:8766/health
```

Open the dashboard at `http://<host>:8766/`. Install as a service with the
provided `systemd/elegoo-notify.service` (adjust `WorkingDirectory`, then
`systemctl enable --now elegoo-notify`).

## Configuration

All settings live in `config.json` (a fully commented starting point is
`config.json.example`); secrets live in `.env`. Environment variables always
override the JSON file.

### Environment variables (`.env`)

| Variable | Purpose |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Your chat id (see the [Telegram](#telegram) section) |
| `TELEGRAM_DRYRUN` | `1` = never call the Telegram API; log messages to `data/logs/telegram_dryrun.jsonl` instead (used by the test suite) |
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD` | Enable HTTP Basic Auth on all endpoints (except `/health`) |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `ELEGOO_PRINTER_IP` | Override the printer IP |
| `ELEGOO_CONFIG` | Path to an alternative config file |

### `config.json` reference

| Key | Default | Description |
|---|---|---|
| `printer.ip` | `192.168.1.56` | Printer address |
| `printer.ws_urls` | `ws://{ip}:3030/websocket`, `/ws`, … | WebSocket candidates, tried in order (the first is the real SDCP endpoint; `:3333` and `/printer/ws` are kept as fallbacks) |
| `printer.http_port` | `3030` | Port for the GCODE upload endpoint |
| `printer.status_poll_seconds` | `10` | Safety status poll (Cmd 0) on top of the printer's own pushes |
| `printer.request_timeout_seconds` | `8` | Timeout for SDCP requests |
| `printer.max_reconnect_backoff_seconds` | `30` | Cap for the exponential reconnect backoff |
| `service.port` | `8766` | REST API / dashboard port |
| `service.public_url` | `http://192.168.1.50:8766` | Base URL used in the Telegram `/link` command |
| `telegram.photo_mode` | `photo` | `photo` (inline preview) or `document` (lossless original) |
| `telegram.progress_step_percent` | `10` | Notify every N% of progress |
| `telegram.notify_interval_minutes` | `30` | Time-based progress notifications |
| `telegram.min_seconds_between_msgs` | `45` | Debounce between non-critical messages |
| `telegram.notify_on` / `photo_on` | see example | Which events notify, and which include a photo |
| `webcam.mode` | `mjpeg` | `mjpeg` (Centauri stream at `:3031/video`) or `snapshot` (any URL returning a JPEG) |
| `webcam.persistent_stream` | `true` | Keep one MJPEG connection open and fan it out (recommended) |
| `webcam.save_snapshots` | `true` | Persist notification photos to `data/snapshots` |
| `ai.enabled` | `true` | Master switch — turn the whole AI stack off with one flag |
| `ai.interval_seconds` | `4` | One analysed frame every N seconds (3–5 recommended) |
| `ai.auto_stop` | `true` | Stop the print on critical alerts (fail-safe: notify before + after). Arm it only after tuning |
| `ai.sensitivity` | `medium` | `low` / `medium` / `high` presets for the CV thresholds |
| `ai.consecutive_frames` | `2` | Confirmations required before a CV alert fires |
| `ai.cooldown_seconds` | `300` | Per-detector alert cooldown |
| `ai.roi` | `[0.03, 0.33, 0.94, 0.64]` | Analysis region (x, y, w, h fractions) — excludes the gantry/head area. Verify with `/photo?roi=1` |
| `ai.warmup_samples` | `8` | Ignore the first N frames after (re)start |
| `ai.spaghetti_min_layer` | `7` | No stringing alerts before layer N (skirt/brim/purge look like stringing) |
| `ai.spaghetti_baseline_factor` | `3.0` | Adaptive threshold: alert only above `baseline × factor` — each print learns its own "normal" |
| `ai.ml.*` | see below | ML detector (model path, `threshold` 0.6, warmup) |
| `ai.layer_watch.*` | see below | Per-layer NRMSE analysis (`min_layers` 7, `deviance_lag` 5, `thresholds`) |
| `ai.detectors.*` | see example | Enable/disable + severity per detector (`spaghetti`, `layer_shift`, `detach`, `breakage`, `runout`, `smoke`, `ml_defect`) |
| `mqtt.enabled` | `true` | Home Assistant MQTT discovery |
| `mqtt.host` / `mqtt.port` | `192.168.1.50` / `1883` | Your broker (the same one Home Assistant uses) |
| `mqtt.base_topic` | `elegoo_notify` | Topic root; commands listened on `elegoo_notify/set/#` and `elegoo_notify/cmd/#` |
| `upload.max_size_mb` | `500` | GCODE upload size cap |

## Telegram

### One-time setup

1. Create a bot with [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. Send any message to your new bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read the `chat.id`.
3. Put both values in `.env`.

The service only **sends** through the Bot API, so it can share a bot with
Home Assistant's `telegram_bot` (which owns the polling) without conflicts —
that is exactly how the command forwarding below works.

### Interactive commands

| Command | Action |
|---|---|
| `/status` (or `/stato`) | Full status **with photo** |
| `/foto` | Latest webcam frame |
| `/ai` | Detector metrics (ML score, CV severities, per-layer history) |
| `/pause` `/resume` | Pause / resume the print |
| `/stop` | Stop the print — requires `/stop conferma` within 2 minutes |
| `/velocita 80` (or `/speed`) | Set print speed, 50–150% |
| `/luce on` / `/luce off` | Chamber light on/off — see the [firmware note](#firmware-quirk-light-during-print) below |
| `/link` | Clickable links to dashboard, webcam stream, AI metrics |
| `/file` | GCODE list (printer + local) |
| `/stampa name.gcode` | Start a print — requires a second confirming message |
| `/upload` | How to load a file: just attach a `.gcode` in the chat |

Sending a `.gcode` file directly in the chat makes the service download it
(via `getFile`, which does not interfere with HA's polling), store it in
`data/gcodes`, transfer it to the printer (with MD5 verification) and reply
with the result.

### Wiring the commands through Home Assistant

Home Assistant's `telegram_bot` integration owns `getUpdates`, so commands are
forwarded by two small automations to the service, which handles everything
and replies on its own. Ready-to-paste files are in
[`hass/`](hass/README-ha.md): `rest_command` snippets, the forwarding
automations, and the Lovelace dashboard.

### Firmware quirk: light during print

The Centauri Carbon firmware **rejects remote light commands while a print
is running** (SDCP `Cmd 403` with `LightStatus` returns `Ack=1`, "busy" —
verified on the real printer; the full SDCP spec has no other light command).
Consequences:

- `light.turn_on` from HA/Telegram during a print is refused: the service
  surfaces a clear error instead of failing silently.
- For prints started **through elegoo-notify** (`/stampa`, REST `/print`,
  HA `button`) the service turns the light on **before** starting the job
  (idle = accepted) via `printer.light_on_print_start` (default `true`).
- The off path (5 min after print end, printer idle) always works.
- For prints started from the printer screen the light can be toggled from
  the printer's own display.

## Home Assistant

Point the service at the MQTT broker Home Assistant already uses and the
entities appear automatically (MQTT discovery). Device: **Elegoo Centauri
Carbon**.

| Entity | Description |
|---|---|
| `sensor.elegoo_centauri_carbon_avanzamento_stampa` | Progress % |
| `sensor.elegoo_centauri_carbon_tempo_rimanente` / `_tempo_trascorso` | Remaining / elapsed (min) |
| `sensor.elegoo_centauri_carbon_temperatura_ugello` / `_piatto` / `_camera` | Nozzle / bed / chamber °C |
| `sensor.elegoo_centauri_carbon_stato_stampante` / `_file_in_stampa` / `_velocita_stampa` | State, file, speed |
| `sensor.elegoo_centauri_carbon_rischio_ai` | ML risk 0–100% |
| `sensor.elegoo_centauri_carbon_ultimo_alert_ai` / `_ultimo_errore` | Last AI alert / last error |
| `binary_sensor.elegoo_centauri_carbon_in_stampa` | ON while printing |
| `button.elegoo_centauri_carbon_ferma_stampa` / `_pausa_stampa` / `_riprendi_stampa` | Stop / pause / resume |
| `number.elegoo_centauri_carbon_velocita_stampa` | Settable speed 50–150% |
| `light.elegoo_centauri_carbon_luce_interna` | Chamber light |

An example automation in `hass/automations-luce-stampa.yaml` turns the
chamber light on when a print starts and off 5 minutes after it ends.

A ready-made dashboard (gauge, camera, temperatures, commands, AI risk,
history and links) is provided in [`hass/dashboards/stampante3d.yaml`]
(hass/dashboards/stampante3d.yaml). For the camera, create an **MJPEG** camera
pointing at `http://<host>:8766/video` (the service fan-out) — *not* directly
at the printer, which accepts a single camera client.

## Klipper / COSMOS support (dual driver)

elegoo-notify speaks **two printer dialects**, selected with `printer.driver`:

| | `"sdcp"` (default) | `"moonraker"` |
|---|---|---|
| Firmware | Stock Elegoo Centauri Carbon | [COSMOS](https://docs.opencentauri.cc/klipper-conversion/cosmos/cosmos/) (Klipper/Kalico) or any Klipper host |
| Transport | SDCP v3 WebSocket `:3030` + HTTP upload | Moonraker REST `:7125` |
| Light control | refused during print (firmware quirk) | **always works** (SET_PIN gcode) |
| Speed | Cmd 403 `PrintSpeedPct` | `M220` |
| Status | printer pushes → events derived | poller translates `print_stats`/objects into SDCP-like payloads → **the whole stack downstream is unchanged** |

Switch by setting in `config.json`:

```json
"printer": {"ip": "192.168.1.56", "driver": "moonraker",
            "moonraker": {"port": 7125, "api_key": "",
                          "light_on_gcode": "SET_PIN PIN=chamber_light VALUE=1",
                          "light_off_gcode": "SET_PIN PIN=chamber_light VALUE=0"}}
```

**Is COSMOS worth it?** For tinkerers: full Klipper ecosystem (bed mesh in the
webUI, input shaper, adaptive meshing, CANVAS/AFC multi-material, exhaust fan,
no cloud phoning home) — and with this driver elegoo-notify keeps every
feature, notifications and AI included. Caveats: the stock mainboard has very
little headroom (no extra plugins), first boot flashes toolhead/bed boards,
and the Elegoo app/cloud stop working. If you only need monitoring, the stock
firmware + this service is already a complete solution.

## 3D models & slicing on the go

The dashboard has a built-in **STL viewer** (three.js, vendored, works on LAN)
and a slicing pipeline: upload a model → inspect it in 3D → slice → send to
the printer → start, all from the browser.

- `POST /models` (multipart `.stl`/`.3mf`/`.obj`) → stored in `data/models`
- `GET /models` / `GET /models/{name}` (served to the viewer) / `DELETE`
- `POST /models/{name}/slice` — body:
  `{"material": "pla"|"petg", "layer_height": 0.2, "infill": 15,
    "supports": false, "transfer": true}`
  → async job (one at a time — modest CPUs deserve mercy): status on
  `GET /slice/jobs[/{id}]`. On success the GCODE lands in `data/gcodes`
  and (if `transfer`) is MD5-uploaded to the printer, ready for
  `POST /print` or the Telegram `/stampa` command.
- Engine: **PrusaSlicer 2.8.1** CLI (AGPL-3.0, see LICENSE-NOTICE) bundled
  in the Docker image. Profiles in `slicer-profiles/centauri_carbon/` are
  derived from the **official Elegoo Centauri Carbon profiles** shipped in
  OrcaSlicer (`resources/profiles/Elegoo`): the real start/end g-code
  (M729 nozzle clean, M6211, M83 relative extrusion, prime lines,
  M749 shutdown sequence) and the official speeds (outer 160 / inner 200 /
  infill 200 / solid 250 mm/s, travel 500, first layer 50), PLA 210/60,
  PETG 240/70.
- Not installed? Every slicing endpoint degrades gracefully (501) and the
  rest of the service keeps working.
- Expect a benchy in ~2-6 min on a Celeron-class CPU; big models queue.

## REST API

| Endpoint | Description |
|---|---|
| `GET /health` | Service health (no auth) |
| `GET /status` | Full printer state (JSON) |
| `GET /photo` | Fresh camera snapshot; add `?roi=1` to overlay the AI region |
| `GET /video` | MJPEG proxy of the printer camera |
| `POST /cmd/stop` `/cmd/pause` `/cmd/resume` | Print control |
| `POST /cmd/speed` | `{"percent": 80}` — set print speed |
| `POST /cmd/light` | `{"on": true}` — chamber light |
| `POST /upload` | Multipart GCODE → local store + printer transfer (MD5) |
| `POST /print` | `{"filename": "x.gcode"}` — start a print |
| `GET /files` | GCODE files on printer and local store |
| `GET /api/events` | SSE live event stream (used by the dashboard) |
| `GET /ai/metrics` | Live detector metrics + per-layer history |
| `POST /ai/analyze_now` | Diagnostic: one forced analysis, returns detections + metrics |
| `POST /notify/test` | `{"message": "…"}` / `{"photo": true}` — test the Telegram path |

## AI detection in depth

**Layer 1 — ML detector (primary).** A ShuffleNetV2-x1.0 encoder (~5 MB, ONNX,
CPU) maps each frame to a 1024-d embedding; a nearest-prototype classifier
compares it against "success" and "failure" prototypes and produces a 0–1
defect score (0.5 = decision boundary). Threshold: `ai.ml.threshold` (0.6).
The preprocessing/classification/scoring are faithful ports of PrintGuard's
`vision.py` (GPL-2.0, see [LICENSE-NOTICE](LICENSE-NOTICE)).

**Layer 2 — CV heuristics (fallback + complementary).**

- **Spaghetti/stringing** — *static* detector (no frame differencing, so the
  moving head produces no noise): thin structures isolated with adaptive
  thresholding + morphology (`thresh − erode`), thinness-filtered contours and
  non-horizontal Hough lines, evaluated **only around the print object** (the
  skirt is masked out). An adaptive baseline learns each part's normal amount
  of thin detail and alerts only above `baseline × 3`.
- **Layer shift** — diagonal line segments that break the scene's dominant
  orientation (the webcam sees the bed in perspective, so nothing is compared
  to absolute 0°/90°).
- **Smoke** — sharpness drop (Laplacian variance) + brightness shift in the
  upper region, with slow-adapting baselines.
- **LayerWatch** (one frame **per layer**, 3DPrintSaviour method) — NRMSE
  between layer N and N−1 (*score*) and N−5 (*deviance*), computed on the
  object region with a segmentation threshold fixed on the reference frame:
  if the object silhouette disappears the verdict is immediate. Verdicts:
  `detach` (score & deviance > 1.0), `breakage` (Δ > 0.2 both), `runout`
  (flat for 6+ layers — experimental, off by default). No verdicts before
  layer 7.

**Fail-safe auto-stop** (`ai.auto_stop`): 1) critical notification with the
anomaly photo *before* acting → 2) `Cmd 130` stop → 3) confirmation *after*,
with a fresh photo. If the stop fails you get an explicit
"INTERVENI MANUALMENTE!" message.

### Integrating the ML model

Model binaries are **not** in this repository (licence and size). One command
fetches them from the upstream project:

```bash
python3 scripts/download_models.py   # → models/encoder_float32.onnx, prototypes.json, metadata.json
```

Restart the service and check `docker logs` for `Modello ML caricato`
("ML model loaded"), or `curl :8766/ai/metrics`. Without the models the
service simply runs the CV stack.

### Tuning on your own camera

```bash
curl http://127.0.0.1:8766/ai/metrics        # ML score, CV severities vs baselines, layer history
curl -X POST http://127.0.0.1:8766/ai/analyze_now
curl "http://127.0.0.1:8766/photo?roi=1"     # check the ROI rectangle on a live frame
```

Values observed on a real Centauri Carbon: ML score ~0.07 on healthy prints,
per-layer scores 0.03–0.13, stringing baseline absorbs thin-walled parts with
zero false positives. If your camera view differs, adjust `ai.roi` and the
sensitivity preset.

## Testing

Four offline acceptance suites (Telegram in dry-run, printer simulated by
`tests/simulator.py`, no hardware needed):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python3 tests/run_ws_tests.py      # 24 checks: events → notifications+photos, REST, upload, print
python3 tests/run_ai_tests.py     # 11 checks: spaghetti → critical alert → auto-stop fail-safe, smoke
python3 tests/run_layer_tests.py  # 10 checks: LayerWatch score/deviance → detach
python3 tests/run_ml_test.py      # 4 checks: ML loads, real healthy frame scores low
python3 tests/run_moonraker_tests.py  # 13 checks: full stack on a Klipper/COSMOS printer
python3 tests/run_slicer_tests.py   # 11 checks: models upload/view/slice pipeline (+ real slice if prusa-slicer installed)
```

The simulator implements the real SDCP payloads (see [docs/examples.md]
(docs/examples.md)) including anomaly injection for AI testing.

## Security notes

- Secrets only in `.env` (git-ignored); `.env.example` contains placeholders.
- Enable `DASHBOARD_USER`/`DASHBOARD_PASSWORD` to protect API and dashboard.
- For remote access use a VPN (Tailscale, NordVPN Meshnet, …); do **not**
  port-forward the service.
- Telegram commands are double-gated (HA `allowed_chat_ids` **and** the
  service's own chat check); destructive commands need a two-step confirm.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `connected: false` in `/health` | Printer off or busy: the service retries with backoff; check `docker logs` |
| No Telegram messages | Check token/chat in `.env`; try `POST /notify/test` |
| No photos | Another client holds the only camera slot (Elegoo app?); set `webcam.persistent_stream: false` or close it |
| MQTT entities missing in HA | Broker unreachable or discovery disabled — look for `MQTT connesso` in logs |
| AI uses too much CPU | Raise `ai.interval_seconds` (10–15 s) or set `ai.enabled: false` |
| Port 8766 busy | Change `service.port` |

## Credits

- **[PrintGuard](https://github.com/oliverbravery/PrintGuard)** by Oliver Bravery
  (GPL-2.0) — the ML detector: model, prototypes and scoring.
- **[3DPrintSaviour](https://github.com/Manicben/3DPrintSaviour)** (archived) —
  the per-layer NRMSE methodology.
- **[PrintSight](https://github.com/bossman-lab/printsight)** (MIT) — the static
  thin-structure stringing heuristics.
- **[OpenCentauri](https://docs.opencentauri.cc)** — community documentation of
  the Centauri Carbon SDCP API.

## License

**GPL-2.0-only** — see [LICENSE](LICENSE) and [LICENSE-NOTICE](LICENSE-NOTICE).
The ML detector (`app/ai/ml_detector.py` + models) derives from PrintGuard
(GPL-2.0): if you redistribute this software, the whole derived work must be
released under GPL-2.0 with attribution.
