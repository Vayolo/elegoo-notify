# elegoo-notify

[![License: GPL v2](https://img.shields.io/badge/License-GPLv2-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](requirements.txt)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](Dockerfile)
[![Home Assistant](https://img.shields.io/badge/Home_Assistant-MQTT%20discovery-41BDF5?logo=homeassistant&logoColor=white)](#-home-assistant)
[![Tests](https://img.shields.io/badge/test-47%2F47%20PASS-brightgreen)](#-test)

**Monitoraggio, notifiche Telegram, rilevamento difetti con AI e controllo remoto
per la Elegoo Centauri Carbon** — production-ready, Celeron-friendly, tutto self-hosted.

> 🤖 Rilevamento difetti a doppio strato: **modello ML ShuffleNetV2** (stack
> [PrintGuard](https://github.com/oliverbravery/PrintGuard), GPL-2.0 — ~5 MB, CPU)
> + euristiche OpenCV (spaghetti, layer shift, fumo, detach per-layer ispirate a
> [3DPrintSaviour](https://github.com/Manicben/3DPrintSaviour) e
> [PrintSight](https://github.com/bossman-lab/printsight)).

- 🖨️ Connessione **WebSocket** alla stampante con riconnessione automatica
- 📸 **Telegram**: testo + foto ad alta qualità su inizio/avanzamento/completamento/errori
- 🤖 **AI leggera** (solo OpenCV, CPU-friendly): spaghetti, layer shift, distacco, fumo — con **STOP automatico** fail-safe
- 🌐 **API REST**: status, stop/pausa/riprendi, upload GCODE, avvio stampa
- 🏠 **Home Assistant**: MQTT discovery (sensori + pulsanti) e comandi via REST
- 📊 **Dashboard web** minimale (SSE): stato live, progresso, grafico temperature, webcam
- 🖥️ Pensato per CPU modeste (Celeron): nessun modello ML, solo frame differencing

> Testato con: stampante a `192.168.1.56` (WS `:3030`, MJPEG `:3031`).

---

## Indice
1. [Architettura](#architettura)
2. [Installazione rapida](#installazione-rapida)
3. [Configurare Telegram (bot + chat_id)](#configurare-telegram)
4. [Sicurezza: token e accesso remoto](#sicurezza)
5. [Configurazione (config.json)](#configurazione)
6. [Uso: API REST e comandi](#api-rest)
7. [Home Assistant (MQTT)](#home-assistant)
8. [AI: come funziona, taratura e disattivazione](#ai)
9. [Test di accettazione](#test)
10. [Troubleshooting](#troubleshooting)

---

## Architettura

```
                    ┌────────────────────────── elegoo-notify (docker, host net) ──────────────────────────┐
Centauri Carbon     │                                                                                     │
  WS :3030 ◄────────┤  wsConnector ─┬─► state ─► EventBus ─┬─► progressManager ─► telegramNotifier ─► 📱     │
  (SDCP v3)         │  (failover,   │   (eventi          ├─► aiMonitor (OpenCV) ──► auto-stop fail-safe      │
  MJPEG :3031 ◄─────┤   heartbeat,  │    derivati)        ├─► haBridge (MQTT) ─────► Home Assistant         │
  HTTP upload ◄──────┤   reconnect)  │                     └─► REST+SSE (:8766) ───► Dashboard              │
                    │  printerApi ──┴─ Cmd 0/128/129/130/131/258/386…                                              │
                    │  webcam ── una sola connessione MJPEG, fan-out a: AI, snapshot Telegram, dashboard         │
                    └─────────────────────────────────────────────────────────────────────────────────────────────┘
```

Moduli (`app/`): `config`, `events` (bus), `state` (derivazione eventi),
`sdcp/{protocol,ws_connector,printer_api}`, `webcam`, `uploads`,
`notify/{telegram,progress_manager,scheduler}`, `ai/{detectors,monitor}`,
`api/{server,ha_bridge}`, `main`.

Gli **eventi non esistono** nel protocollo SDCP: vengono **derivati** dalle
transizioni di `PrintInfo.Status` (0→1 = start, 9 = complete, 8+errore = fail…).
Esempi completi di payload in [`docs/examples.md`](docs/examples.md).

---

## Installazione rapida

Prerequisiti: Docker + docker compose v2, stampante raggiungibile in LAN.

```bash
unzip elegoo-notify.zip && cd elegoo-notify
./install.sh          # crea .env/config.json, build e avvio, health check
```

oppure manualmente:

```bash
cp .env.example .env        # poi inserisci token/chat_id
cp config.json.example config.json
mkdir -p data/logs data/snapshots data/gcodes
docker compose up --build -d
```

Verifiche:

```bash
curl http://127.0.0.1:8766/health    # {"status":"ok","connected":true,...}
curl http://127.0.0.1:8766/status    # snapshot completo (temperatura, progresso…)
docker logs -f elegoo-notify        # log (anche in data/logs/elegoo-notify.log)
```

Dashboard: **http://127.0.0.1:8766/**

> Il container usa `network_mode: host`: la porta `8766` è esposta direttamente
> dalla macchina e la stampante è raggiungibile senza routing Docker.

### systemd (avvio al boot)

```bash
sudo cp systemd/elegoo-notify.service /etc/systemd/system/
# sistema il percorso in WorkingDirectory se diverso da /opt/elegoo-notify
sudo systemctl daemon-reload
sudo systemctl enable --now elegoo-notify
```

---

## Configurare Telegram

### 1) Creare il bot (se ne usi uno dedicato)
1. Su Telegram cerca **@BotFather** → `/newbot` → scegli nome e username.
2. Copia il **token** (`123456:AAE…`).

> Questo progetto può **riusare un bot esistente** (es. quello di Home
> Assistant): il servizio è solo mittente (`sendMessage`/`sendPhoto`), non
> fa polling di `getUpdates`, quindi non collide con altri client.

### 2) Ottenere il chat_id
1. Scrivi un messaggio al tuo bot.
2. Apri `https://api.telegram.org/bot<TOKEN>/getUpdates` nel browser.
3. Cerca `"chat":{"id":123456789,…}` → quel numero è il `TELEGRAM_CHAT_ID`.

### 3) Inserire i valori

```bash
nano .env
# TELEGRAM_TOKEN=123456:AAE-il-tuo-token
# TELEGRAM_CHAT_ID=123456789
```

Prova immediata (senza stampare nulla):

```bash
curl -X POST http://127.0.0.1:8766/notify/test -d '{"message":"prova"}'
curl -X POST http://127.0.0.1:8766/notify/test -d '{"photo":true}'
```

**Qualità foto**: di default si usa `sendPhoto` (anteprima in chat).
Per il file **originale lossless** imposta in `config.json`:
`"telegram": {"photo_mode": "document"}`.

**Anti-spam**: `min_seconds_between_msgs` (default 45 s) distanzia i messaggi
non critici; start/complete/error/AI sono **critici** e bypassano il debounce.
Il rate limit 429 di Telegram è gestito automaticamente (retry dopo il periodo indicato).

---

## Sicurezza

- **Mai token nel repo**: `.env` è in `.gitignore`; usa Docker secrets se preferisci:
  ```yaml
  # docker-compose.yml (alternativa a env_file)
  secrets:
    tg_token:
      file: ./secrets/tg_token.txt
  services:
    elegoo-notify:
      environment:
        TELEGRAM_TOKEN_FILE: /run/secrets/tg_token   # (leggi e popola ELEGOO_CONFIG/TOKEN)
  ```
- **API/Dashboard protette**: imposta `DASHBOARD_USER` e `DASHBOARD_PASSWORD`
  in `.env` → HTTP Basic Auth su tutti gli endpoint (tranne `/health`).
- **Accesso remoto**: usa una VPN mesh — **NordVPN Meshnet** o **Tailscale** —
  e collega il telefono al servizio via IP mesh (es. `http://100.x.y.z:8766`).
  Evita port-forward sul router: il servizio non cifra il traffico di per sé.
- La stampante non esce dalla LAN: tutte le connessioni sono in locale.

---

## Configurazione

`config.json` (creato dall'example, già impostato per 192.168.1.56). Sezioni principali:

| Chiave | Default | Note |
|---|---|---|
| `printer.ip` | `192.168.1.56` | IP della stampante |
| `printer.ws_urls` | ws://IP:3030/websocket, /ws, :3333, /printer/ws | candidati in ordine (3030 è quello reale) |
| `printer.status_poll_seconds` | 10 | poll Cmd 0 di sicurezza |
| `service.port` | 8766 | porta REST/dashboard |
| `telegram.progress_step_percent` | 10 | notifica ogni N% |
| `telegram.notify_interval_minutes` | 30 | notifica time-based |
| `telegram.min_seconds_between_msgs` | 45 | debounce |
| `telegram.notify_on` / `photo_on` | vedi example | quali notifiche / con foto |
| `webcam.mode` | `mjpeg` | `mjpeg` (:3031/video) o `snapshot` (URL JPEG) |
| `webcam.save_snapshots` | true | salva le foto in data/snapshots |
| `ai.enabled` | true | **disattiva l'AI se il server è sotto carico** |
| `ai.interval_seconds` | 4 | 3–5 s consigliato (1 frame ogni N secondi) |
| `ai.auto_stop` | true | stop automatico su alert critico |
| `mqtt.*` | 192.168.1.50:1883 | broker per HA (vedi sotto) |

Override da env: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_DRYRUN`,
`DASHBOARD_USER/PASSWORD`, `LOG_LEVEL`, `ELEGOO_PRINTER_IP`, `ELEGOO_CONFIG`.

Dopo ogni modifica: `docker compose restart elegoo-notify`.

---

## API REST

| Endpoint | Descrizione |
|---|---|
| `GET /health` | stato servizio |
| `GET /status` | stato completo stampante (JSON) |
| `GET /photo` | snapshot JPEG fresco |
| `GET /video` | proxy MJPEG della webcam |
| `POST /cmd/stop` `POST /cmd/pause` `POST /cmd/resume` | comandi stampa |
| `POST /print` | `{"filename": "x.gcode"}` avvia la stampa |
| `POST /upload` | multipart `file`: salva in data/gcodes + trasmette alla stampante (MD5) |
| `GET /files` | elenco GCODE locali e della stampante |
| `GET /api/events` | SSE (eventi live per la dashboard) |
| `POST /notify/test` | notifica Telegram di prova (opz. `"photo":true`) |

Esempi:

```bash
curl -X POST http://127.0.0.1:8766/cmd/pause
curl -F "file=@benchy.gcode" http://127.0.0.1:8766/upload
curl -X POST http://127.0.0.1:8766/print -H 'Content-Type: application/json' -d '{"filename":"benchy.gcode"}'
```

---

## Home Assistant

Il servizio pubblica via **MQTT discovery**: le entità compaiono
automaticamente in HA (broker già usato da HA: `192.168.1.50:1883`).

**16 entità** (device "Elegoo Centauri Carbon"):

| Entità HA | Descrizione |
|---|---|
| `sensor.elegoo_centauri_carbon_avanzamento_stampa` | % di avanzamento |
| `sensor.elegoo_centauri_carbon_tempo_rimanente` / `_tempo_trascorso` | min rimanenti / trascorsi |
| `sensor.elegoo_centauri_carbon_temperatura_ugello` / `_piatto` / `_camera` | °C |
| `sensor.elegoo_centauri_carbon_stato_stampante` / `_file_in_stampa` / `_velocita_stampa` | stato, file, velocità |
| `sensor.elegoo_centauri_carbon_rischio_ai` | rischio ML 0-100% |
| `sensor.elegoo_centauri_carbon_ultimo_alert_ai` / `_ultimo_errore` | ultimo alert / errore |
| `binary_sensor.elegoo_centauri_carbon_in_stampa` | ON durante la stampa |
| `button.elegoo_centauri_carbon_ferma_stampa` / `_pausa_stampa` / `_riprendi_stampa` | comandi |

**Dashboard dedicata "Stampante 3D"** (`http://192.168.1.50:8123/stampante-3d`):
gauge avanzamento, webcam live, temperature, comandi, rischio AI, storico —
YAML in `/config/dashboards/stampante3d.yaml` (rif. repo `dashboards/`).

**Webcam in HA**: config entry MJPEG `camera.stampante_3d` che punta al
fan-out del servizio (`:8766/video`) → nessun conflitto col limite di
1 stream della Centauri (la camera esistente `camera.3d_cam` puntava
direttamente alla stampante e compete per lo slot: usare la nuova).

**Comandi da HA**: pulsanti MQTT (sopra), topic `elegoo_notify/cmd/#`, o
`rest_command` verso `http://192.168.1.50:8766/cmd/*`.

Automazione HA d'esempio:

```yaml
automation:
  - alias: Stampa 3D quasi finita
    trigger:
      - platform: numeric_state
        entity_id: sensor.elegoo_centauri_carbon_avanzamento_stampa
        above: 95
    action:
      - service: notify.mobile_telefono
        data:
          message: "Stampa 3D al 95%!"
```

---

## Comandi Telegram (via Home Assistant)

Il polling del bot appartiene ad HA (`telegram_bot`); HA inoltra tutto al
servizio, e la logica vive qui (risposte spedite direttamente dal nostro
stack, foto comprese). Automazioni già pronte: `telegram_elegoo_cmd` e
`telegram_elegoo_upload_gcode` (repo `hass/`).

| Comando | Azione |
|---|---|
| `/status` `/stato` | stato completo **con foto** |
| `/foto` | ultimo frame webcam |
| `/ai` | metriche detector (ML score, CV, layer) |
| `/pause` `/pausa` · `/resume` `/riprendi` | pausa/riprendi |
| `/stop` | **ferma** (conferma: `/stop conferma`) |
| `/file` | elenco GCODE (stampante + locali) |
| `/stampa nome.gcode` | avvia stampa (conferma a 2 passaggi) |
| `/upload` | istruzioni: invia il .gcode in chat |

**Invio file**: allega un `.gcode` in chat → scarico via `getFile`,
salvataggio in `data/gcodes`, trasferimento MD5 alla stampante, risposta
con conferma → `/stampa nomefile.gcode` per avviare.

Sicurezza: doppio filtro chat (allowed_chat_ids di HA + `TELEGRAM_CHAT_ID`
del servizio); comandi distruttivi con conferma a 2 passaggi (TTL 120 s).

---

## AI

**Stack v3 = ML PrintGuard + CV ibrida + LayerWatch**

Il rilevamento primario ora è il **modello di PrintGuard**
(https://github.com/oliverbravery/PrintGuard, **GPL-2.0**, vedi
`LICENSE-NOTICE`): encoder **ShuffleNetV2-x1.0** (~5 MB, ONNX) →
embedding 1024-d → prototipi success/failure → **score 0-1** di difetto.
CPU-only, 1 inferenza ogni 4 s: carico trascurabile (Celeron ok).
Soglia `ai.ml.threshold` (default 0.6). Sul telaio sano della Centauri
lo score reale è ~0.07 (margine 8×): falsi positivi praticamente nulli.

Fallback automatico: se onnxruntime/modello mancano → solo stack CV.

**Stack v2** — zero ML pesante, tutto OpenCV, metodologie da tre progetti
open source studiati sul campo:

| Riferimento | Cosa abbiamo preso |
|---|---|
| [3DPrintSaviour](https://github.com/Manicben/3DPrintSaviour) | analisi ANCORATA AI LAYER (non al tempo): score/deviance NRMSE, nessun verdetto nei primi layer, detach/breakage/runout |
| [PrintSight](https://github.com/bossman-lab/printsight) | detector STATICo di stringing (niente frame-diff → la testa in movimento non produce falsi): morfologia `thresh − erode` + thinness P²/4πA + Hough |
| [PrintGuard](https://github.com/oliverbravery/PrintGuard) | filosofia: soglia tunabile, persistenza del difetto, cooldown, "guarda solo mentre stampa" |

### Architettura del rilevamento

1. **ROI configurabile** che esclude la fascia alta del frame dove vivono
   gantry e testa di stampa (`ai.roi`, calibrato sulla webcam reale:
   il movimento del carro è tutto in y 0.00–0.31)
2. **FrameAnalyzer** (1 frame ogni 4 s, warm-up 8 campioni):
   - **spaghetti** (critico): strutture sottili SOLO nel vicinato dell'oggetto
     (lo skirt non conta), con **soglia adattiva**: ogni pezzo ha la sua
     normalità di dettagli fini → si allerta solo sopra `baseline × 3`
     (o soglia assoluta). Gate: mai sotto il layer 7 (`spaghetti_min_layer`)
   - **layer_shift** (warning): segmenti diagonali anomali rispetto
     all'orientamento DOMINANTE della scena (la webcam è in prospettiva,
     mai rispetto a 0°/90° assoluti)
   - **smoke** (warning): cadita di nitidezza + variazione luminosità,
     con baseline adattive
3. **LayerWatch** (metodologia 3DPrintSaviour — un frame per layer):
   - **score** = NRMSE(layer N, N−1), **deviance** = NRMSE(N, N−5)
     calcolati sulla REGIONE DELL'OGGETTO (soglia di segmentazione calibrata
     sul frame di riferimento e riusata fissa: se l'oggetto sparisce, il
     verdetto è decisivo → score 1.5)
   - **detach** = score > 1.0 ∧ deviance > 1.0 (o silhouette mancante)
   - **breakage** = |Δscore| > 0.2 ∧ |Δdeviance| > 0.2
   - **runout** = stampa piatta da ≥6 layer consecutivi (**sperimentale,
     disattivato di default**: attivalo in `ai.detectors` dopo taratura)

**Fail-safe auto-stop** (`ai.auto_stop`, nel deploy attuale OFF su tua
richiesta — nessuno stop senza il tuo OK): 1) notifica critica con foto
*prima*, 2) `Cmd 130`, 3) notifica di conferma *dopo* (se fallisce:
"INTERVENI MANUALMENTE!").

### Taratura con i dati veri (sessione detection)

Il servizio espone la diagnostica per tarare le soglie sulla TUA webcam:

```bash
curl http://127.0.0.1:8766/ai/metrics        # metriche live + storia layer
curl -X POST http://127.0.0.1:8766/ai/analyze_now   # analisi forzata, diagnostica
curl "http://127.0.0.1:8766/photo?roi=1"    # snapshot con rettangolo ROI
```

In `ai/metrics` trovi: `spaghetti_severity` vs `spaghetti_baseline` e la
soglia effettiva, lo storico score/deviance per layer, i contatori.
Dati osservati sulla Centauri reale (layer 30–43): score normali 0.03–0.13,
deviance 0.05–0.13, baseline spaghetti ~0.10 su un pezzo "cutout" a pareti
sottili (assorbita senza alcun falso positivo in stampa reale).

**Parametri di test** (usati da `tests/run_ai_tests.py`):
`interval=0.5, sensitivity=high, consecutive=2, warmup=2,
spaghetti_min_layer=0, baseline_factor=1.0` + anomalie sintetiche
(spaghetti = polilinee sottili attorno all'ugello, smoke = haze sfocato,
detach = oggetto rimosso dal frame).

**Disattivare l'AI** (server sotto carico): `"ai": {"enabled": false}`.
Solo lo stop automatico: `"ai": {"auto_stop": false}`.


Tutti i test girano **offline** (Telegram in dry-run) con la stampante
simulata `tests/simulator.py`.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python3 tests/run_ws_tests.py     # start/progress/complete/error → notifiche+foto, REST, upload, print (22 check)
python3 tests/run_ai_tests.py     # spaghetti → alert critico + auto-stop fail-safe; smoke → warning (11 check)
python3 tests/run_layer_tests.py # LayerWatch: score/deviance NRMSE per layer → detach (10 check)
```

I messaggi "Telegram" vengono registrati in `tests/out/logs/telegram_dryrun.jsonl`.

Simulatore standalone (per prova manuale della dashboard/API):

```bash
python3 tests/simulator.py --port 13030
# poi: ELEGOO_PRINTER_IP=127.0.0.1 ... a config.json → ws://127.0.0.1:13030
curl -X POST http://127.0.0.1:13030/_sim/start -d '{"filename":"demo.gcode"}' -H 'Content-Type: application/json'
curl -X POST http://127.0.0.1:13030/_sim/setprogress -d '{"percent":50}' -H 'Content-Type: application/json'
```

Payload WS/risposte della stampante per test offline: [`docs/examples.md`](docs/examples.md).

---

## Troubleshooting

| Problema | Causa/rimedio |
|---|---|
| `/health` ok ma `connected:false` | Stampante spenta o WS irraggiungibile: controlla `curl -s http://192.168.1.56:3030` e i log (`ws_connector`). Il servizio riprova da solo con backoff. |
| Nessuna notifica Telegram | `TELEGRAM_TOKEN/CHAT_ID` mancanti in `.env`; prova `POST /notify/test`. In dry-run (`TELEGRAM_DRYRUN=1`) niente esce: controlla il jsonl. |
| Notifiche foto senza immagine | Webcam occupata (limite 1 stream sulla Centauri): chiudi altre app (Elegoo/Elegoo-Link) o `webcam.persistent_stream: false`. |
| Foto nere/vecchie | `webcam.mode` errato: su Centauri usare `mjpeg` (`http://IP:3031/video`); `/webcam.jpg` NON esiste. |
| `/cmd/*` restituisce errore | Stampante occupata (ack=1) o non connessa; `PrinterCommandError` riporta il codice. |
| Upload fallito | Verificare MD5/rete: il simulatore esegue la stessa verifica. Il file resta comunque in `data/gcodes`. |
| MQTT: entità assenti in HA | Broker non raggiungibile (`mqtt.host`), o discovery disattivato; controllare `docker logs` riga "MQTT connesso". |
| CPU alta | AI troppo frequente: alza `ai.interval_seconds` o `ai.enabled:false`. OpenCV carica ~30-60 ms/frame a 480px. |
| Porta 8766 occupata | cambia `service.port` in config.json. |
| Log non ruotano | verificare permessi `data/logs` (`chown -R` dell'utente). |

Reset completo: `docker compose down && rm -rf data/logs/* data/snapshots/*`.

---

## Struttura del repo

```
elegoo-notify/
├── app/                 # servizio (vedi Architettura)
├── dashboard/index.html # UI minimale (SSE, vanilla JS)
├── models/              # encoder ONNX + prototipi PrintGuard (GPL-2.0)
├── hass/                # configurazioni Home Assistant (dashboard, automazioni)
├── tests/               # simulatore + 4 suite di accettazione
├── docs/examples.md     # payload SDCP per test offline
├── config.json.example  # valori per 192.168.1.56
├── .env.example         # token Telegram (DA completare)
├── Dockerfile · docker-compose.yml · install.sh
└── systemd/elegoo-notify.service
```

---

## Licenza

**GPL-2.0-only** — vedi [LICENSE](LICENSE) e [LICENSE-NOTICE](LICENSE-NOTICE).
Il detector ML (`app/ai/ml_detector.py`, `models/`) deriva da
[PrintGuard](https://github.com/oliverbravery/PrintGuard) di Oliver Bravery
(GPL-2.0): distribuendo questo software, l'intero lavoro derivato va rilasciato
sotto GPL-2.0 con attribuzione.
