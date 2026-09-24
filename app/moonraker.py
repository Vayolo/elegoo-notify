"""Driver Moonraker (Klipper/Kalico — COSMOS) per elegoo-notify.

Permette di usare elegoo-notify su una Centauri Carbon convertita a COSMOS
(o su QUALSIASI stampante Klipper) oltre che sul firmware stock via SDCP.

Architettura: il poller interroga gli oggetti Klipper (print_stats,
extruder, heater_bed, display_status) e li traduce in payload SDCP
SINTETICI (stessa forma dei push `sdcp/status` del firmware stock) che
vengono pubblicati sul bus come "sdcp_status" → TUTTO lo stack a valle
(eventi, notifiche, progressi, MQTT/HA, dashboard) funziona invariato.

Comandi mappati su Moonraker:
  pause/resume/stop → POST /printer/print/pause|resume|cancel
  start_print      → POST /printer/print/start {"filename"}
  velocità         → gcode M220 S<pct>
  luce interna     → gcode configurabile (default SET_PIN PIN=chamber_light
                     VALUE=1/0 — con COSMOS il pin esiste e il limite
                     "Ack=1 durante la stampa" del firmware stock SPARISCE)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import aiohttp

log = logging.getLogger("elegoo.moonraker")

# Stato Moonraper print_stats → codice PrintInfo.Status (nostro schema)
STATE_MAP = {
    "standby": 0, "initializing": 0, "shutdown": 0,
    "printing": 1,
    "paused": 6, "pausing": 5,
    "complete": 9,
    "cancelled": 8, "error": 8,  # error → il motivo va nel log
}


def synthetic_sdcp_status(objects: dict[str, Any],
                          light: Optional[bool]) -> dict[str, Any]:
    """Traduce gli oggetti Klipper in un payload SDCP-like
    (stessa forma del campo Status nei push sdcp/status)."""
    ps = objects.get("print_stats") or {}
    ext = objects.get("extruder") or {}
    bed = objects.get("heater_bed") or {}
    disp = objects.get("display_status") or {}
    state = str(ps.get("state") or "standby")
    print_status = STATE_MAP.get(state, 0)
    machine = 1 if print_status in (1, 5, 6, 10) else 0

    filename = ps.get("filename")
    job_active = print_status > 0 and print_status not in (8, 9)
    # il PrintInfo resta visibile anche negli stati terminali (9/8): la
    # derivazione del ciclo di vita (print_completed/failed) deve VEDERLI
    show_pi = job_active or print_status in (8, 9)
    progress = disp.get("progress")  # 0..1 (o None se non in stampa)
    print_duration = float(ps.get("print_duration") or 0.0)

    # percentuale: display_status.progress (0..1); ETA stimata dai tick
    reported = int(round(float(progress) * 100)) if progress else None
    total_ticks = 0
    current_ticks = int(print_duration)
    if progress and progress > 0.01:
        total_ticks = int(print_duration / progress)

    print_info = None
    if show_pi:
        print_info = {
            "Status": print_status,
            "Filename": filename or "",
            "TaskId": ps.get("job_id") or "",
            "ErrorNumber": 0,
            "CurrentTicks": current_ticks,
            "TotalTicks": total_ticks,
            "Progress": reported if reported is not None else 0,
            "CurrentLayer": 0, "TotalLayer": 0,
        }

    chamber = objects.get("temperature_sensor chamber") or {}
    status: dict[str, Any] = {
        "CurrentStatus": [machine],
        "TempOfNozzle": ext.get("temperature"),
        "TempTargetNozzle": ext.get("target"),
        "TempOfHotbed": bed.get("temperature"),
        "TempTargetHotbed": bed.get("target"),
        "TempOfBox": chamber.get("temperature"),
        "TempTargetBox": 0,
        "PrintSpeed": 100,
        "PrintInfo": print_info,
    }
    if light is not None:
        status["LightStatus"] = {"SecondLight": 1 if light else 0}
    # messa in cornice come push SDCP (campo Status)
    return status


class MoonrakerApi:
    """Chiamate REST Moonraker (Klipper host, es. COSMOS su :7125)."""

    def __init__(self, cfg, session: aiohttp.ClientSession):
        m = cfg.printer.get("moonraker", {})
        self.base = f"http://{cfg.printer['ip']}:{int(m.get('port', 7125))}"
        self.api_key = m.get("api_key") or ""
        # COSMOS espone la luce come [led case] (Klipper LED, dimmerabile):
        # SET_LED LED=case WHITE=0..1
        self.light_on_gcode = m.get("light_on_gcode", "SET_LED LED=case WHITE=1")
        self.light_off_gcode = m.get("light_off_gcode", "SET_LED LED=case WHITE=0")
        self.session = session
        self.light: Optional[bool] = None
        self._objects_cache: Optional[list] = None

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-Api-Key"] = self.api_key
        return h

    async def _post(self, path: str, payload: dict | None = None,
                    timeout: float = 10.0) -> dict[str, Any]:
        async with self.session.post(f"{self.base}{path}", json=payload or {},
                                     headers=self._headers(),
                                     timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            body = await r.json(content_type=None)
            if r.status >= 400:
                raise ConnectionError(f"Moonraker {path}: HTTP {r.status} {body}")
            return body

    # ------------------------------------------------------------------ #
    DEFAULT_OBJECTS = ["print_stats", "extruder", "heater_bed",
                       "display_status", "temperature_sensor chamber",
                       "led case"]

    async def query_objects(self) -> dict[str, Any]:
        """Interroga gli oggetti Klipper. Se un oggetto non esiste (es.
        'temperature_sensor chamber' su stampanti senza sensore camera),
        ripiega progressivamente finché trova un sottoinsieme valido."""
        objects = list(self._objects_cache or self.DEFAULT_OBJECTS)
        while objects:
            body = await self._post("/printer/objects/query",
                                    {"objects": {o: None for o in objects}})
            # Moonraker incapsula le risposte in {"result": {...}} (il test
            # con il fake che non wrappava ha mascherato questo bug!)
            payload = body.get("result") or body
            if "status" in payload:
                self._objects_cache = objects
                return payload["status"]
            # rimuove l'ultimo oggetto (i core vengono prima) e riprova
            if len(objects) == 1:
                raise ConnectionError(f"query oggetti Klipper fallita: {body}")
            objects = objects[:-1]
        return {}

    async def send_gcode(self, script: str) -> None:
        await self._post("/printer/gcode/script", {"script": script})

    async def pause(self) -> None:
        await self._post("/printer/print/pause")

    async def resume(self) -> None:
        await self._post("/printer/print/resume")

    async def cancel(self) -> None:
        await self._post("/printer/print/cancel")

    async def start(self, filename: str) -> None:
        await self._post("/printer/print/start", {"filename": filename})

    async def set_speed(self, pct: int) -> None:
        await self.send_gcode(f"M220 S{int(pct)}")

    async def set_light(self, on: bool) -> None:
        await self.send_gcode(self.light_on_gcode if on else self.light_off_gcode)
        self.light = bool(on)   # stato ottimistico: Klipper non lo riporta

    async def file_list(self) -> list[dict[str, Any]]:
        async with self.session.get(f"{self.base}/server/files/list",
                                    params={"root": "gcodes"},
                                    headers=self._headers(),
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
            return (await r.json(content_type=None)).get("files", [])

    async def upload(self, filename: str, data: bytes) -> dict[str, Any]:
        form = aiohttp.FormData()
        form.add_field("root", "gcodes")
        form.add_field("file", data, filename=filename,
                       content_type="application/octet-stream")
        # NB: NON passare _headers() (forza application/json e distrugge
        # il multipart); serve solo l'eventuale api key
        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        async with self.session.post(f"{self.base}/server/files/upload",
                                     data=form, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=300)) as r:
            body = await r.json(content_type=None)
            if r.status >= 400 or body.get("error"):
                raise ConnectionError(f"upload Moonraker: {body}")
            return body


class MoonrakerPoller:
    """Polla Moonraker e pubblica payload SDCP sintetici sul bus:
    tutto lo stack a valle (stato, eventi, notifiche, HA) resta invariato."""

    def __init__(self, cfg, bus, api: MoonrakerApi):
        self.cfg = cfg
        self.bus = bus
        self.api = api
        self.poll_seconds = max(2.0, float(cfg.printer.get("status_poll_seconds", 10)))
        self._task: Optional[asyncio.Task] = None
        self._connected_once = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
            log.info("Driver Moonraker attivo (poll ogni %.0fs)", self.poll_seconds)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                objects = await self.api.query_objects()
                if not self._connected_once:
                    self._connected_once = True
                    self.bus.publish("printer_connected", {"url": self.api.base})
                # luce REALE dall'oggetto Klipper [led case] (bianco > 0.05 = ON)
                led = objects.get("led case") or {}
                cd = led.get("color_data") or []
                if cd and isinstance(cd[0], (list, tuple)) and len(cd[0]) >= 4:
                    self.api.light = float(cd[0][3]) > 0.05
                failures = 0
                payload = synthetic_sdcp_status(objects, self.api.light)
                self.bus.publish("sdcp_status", {"payload": payload, "moonraker": True})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                failures += 1
                if self._connected_once and failures == 1:
                    # reset: alla prossima query riuscita va ripubblicato
                    # 'printer_connected' (es. restart di Klipper dopo
                    # SAVE_CONFIG durante la calibrazione COSMOS)
                    self._connected_once = False
                    self.bus.publish("printer_disconnected", {"reason": str(e)})
                log.debug("Poll Moonraker fallito (%d): %s", failures, e)
            await asyncio.sleep(self.poll_seconds)
