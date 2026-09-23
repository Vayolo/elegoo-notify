# Integrazione Home Assistant — come l'abbiamo configurata sul server

Già applicato su questo host (HA 192.168.1.50:8123):

1. **`configuration.yaml`** — aggiunto:
   - `lovelace.dashboards` → `stampante-3d` (YAML, file `dashboards/stampante3d.yaml`)
   - `rest_command.elegoo_telegram_cmd` / `.elegoo_telegram_file`
     (inoltro comandi/file Telegram verso `http://127.0.0.1:8766/telegram/*`)
2. **Camera**: config entry MJPEG `camera.stampante_3d` →
   `http://192.168.1.50:8766/video` (fan-out del servizio, NON la
   webcam diretta: la Centauri ammette un solo stream). Creata via UI o
   REST flow `{"handler":"mjpeg","name":"Stampante 3D",...}`.
3. **`automations.yaml`** — le due automazioni in
   `automations-telegram-stampante.yaml` (comandi + upload GCODE).

Su una NUOVA installazione: copia `dashboards/stampante3d.yaml` in
`/config/dashboards/`, aggiungi i blocchi a `configuration.yaml`,
crea la camera via UI (Impostazioni → Dispositivi → Aggiungi → MJPEG),
incolla le automazioni, riavvia HA.
