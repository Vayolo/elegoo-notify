"""elegoo-notify — punto di ingresso.

Collega tutti i moduli (WS connector, Telegram, progress manager,
scheduler, AI, MQTT, REST) e avvia il server uvicorn.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp
import uvicorn

from .config import load_config
from .events import EventBus
from .logging_setup import setup_logging
from .sdcp.printer_api import PrinterApi
from .sdcp.ws_connector import WsConnector
from .moonraker import MoonrakerApi, MoonrakerPoller
from .state import PrinterState
from .uploads import Uploader
from .webcam import Webcam
from .notify.telegram import TelegramNotifier
from .notify.progress_manager import ProgressManager
from .notify.scheduler import Scheduler
from .ai.monitor import AiMonitor
from .telegram_commands import TelegramCommandHandler
from .models import ModelStore, Slicer
from .api.ha_bridge import HaBridge
from .api.server import create_app

log = logging.getLogger("elegoo.main")


class AppContext:
    """Contenitore dei componenti con avvio/arresto ordinato."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.bus = EventBus()
        self.state = PrinterState()
        self.connector = WsConnector(cfg, self.bus)
        self.printer_api = PrinterApi(self.connector, cfg)
        self.started_ts = time.time()

        self.session: Optional[aiohttp.ClientSession] = None
        self.webcam: Optional[Webcam] = None
        self.telegram: Optional[TelegramNotifier] = None
        self.progress: Optional[ProgressManager] = None
        self.scheduler: Optional[Scheduler] = None
        self.ai: Optional[AiMonitor] = None
        self.ha: Optional[HaBridge] = None
        self.uploader: Optional[Uploader] = None
        self.models: Optional[ModelStore] = None
        self.slicer: Optional[Slicer] = None
        self.telegram_commands: Optional[TelegramCommandHandler] = None

        self.moonraker_api: Optional[MoonrakerApi] = None
        self.moonraker_poller: Optional[MoonrakerPoller] = None

        self._tasks: list[asyncio.Task] = []
        self._state_sub = None
        self._last_state_publish: float = 0.0

    # ------------------------------------------------------------------ #
    async def start(self, *, connect_printer: bool = True) -> None:
        cfg = self.cfg
        self.session = aiohttp.ClientSession()
        self.webcam = Webcam(cfg, self.session, cfg.printer["ip"])
        self.webcam.set_stream_failure_callback(self._enable_video_on_stream_failure)
        self.telegram = TelegramNotifier(cfg, self.session)
        self.uploader = Uploader(cfg, self.session)

        self.telegram.start()
        await self.webcam.start()

        self.progress = ProgressManager(cfg, self.bus, self.telegram, self.webcam,
                                         self.printer_api, self.state)
        self.progress.start()

        # Driver: "sdcp" (firmware stock) oppure "moonraker" (Klipper/COSMOS).
        # Con Moonraker: niente WS SDCP né scheduler (il poller guida lo stato),
        # e i comandi/upload vengono delegati all'API Moonraker.
        self.driver = str(cfg.printer.get("driver", "sdcp"))
        if self.driver == "moonraker":
            self.moonraker_api = MoonrakerApi(cfg, self.session)
            self.printer_api = PrinterApi(self.connector, cfg,
                                           moonraker=self.moonraker_api)
            self.uploader.moonraker = self.moonraker_api
            self.moonraker_poller = MoonrakerPoller(cfg, self.bus, self.moonraker_api)
            self.moonraker_poller.start()
            log.info("Driver stampante: Moonraker (Klipper/COSMOS) su %s",
                     self.moonraker_api.base)
        else:
            self.scheduler = Scheduler(cfg, self.bus, self.printer_api, self.connector)
            self.scheduler.start()

        # Modelli 3D + slicing on-the-go (PrusaSlicer CLI, opzionale)
        self.models = ModelStore(cfg)
        self.slicer = Slicer(cfg, self.models)

        async def _transfer_after_slice(job, gcode_path):
            if self.cfg.printer.get("driver", "sdcp") == "moonraker":
                await self.moonraker_api.upload(gcode_path.name, gcode_path.read_bytes())
            else:
                await self.uploader.transfer_to_printer(gcode_path)
        self.slicer.set_transfer_callback(_transfer_after_slice)

        self.telegram_commands = TelegramCommandHandler(
            cfg, self.state, self.printer_api, self.webcam, self.telegram,
            self.uploader, None)  # ai ref iniettato dopo la creazione dell'AI

        self.ai = AiMonitor(cfg, self.bus, self.telegram, self.webcam,
                            self.printer_api, self.state)
        self.ai.start()
        self.telegram_commands.ai = self.ai

        self._state_sub = self.bus.subscribe(
            "sdcp_status", "sdcp_attributes", "printer_connected", "printer_disconnected")
        self._state_sub.set_callback(self._on_sdcp_event)

        if cfg.mqtt.get("enabled"):
            self.ha = HaBridge(cfg, self.bus, self.state, self.printer_api,
                                ai_monitor=self.ai)
            self.ha.start()

        if connect_printer and self.driver != "moonraker":
            self._tasks.append(asyncio.create_task(self.connector.run(),
                                                   name="ws-connector"))

        if (cfg.telegram.get("notify_on_start_service")
                and self.telegram.configured):
            await self.telegram.notify("🚀 <b>elegoo-notify avviato</b>\n"
                                        "Servizio operativo.", kind="service_start")
        log.info("AppContext avviato (config: %s)", cfg.get("_source_path"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []
        for comp in (self.ai, self.scheduler, self.ha, self.moonraker_poller):
            if comp is not None:
                await comp.stop()
        if self.progress is not None:
            await self.progress.stop()
        if self.telegram is not None:
            await self.telegram.stop()
        if self.webcam is not None:
            await self.webcam.stop()
        await self.connector.close()
        if self._state_sub is not None:
            self._state_sub.close()
        if self.session is not None:
            await self.session.close()
        log.info("AppContext fermato")

    # ------------------------------------------------------------------ #
    # Wiring stato → bus
    # ------------------------------------------------------------------ #
    async def _on_sdcp_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        data = event.get("data") or {}
        publish_state = False
        try:
            if etype == "sdcp_status":
                events = self.state.update_from_status(data.get("payload") or {})
                for ev_type, ev_data in events:
                    self.bus.publish(ev_type, ev_data)
                publish_state = True
            elif etype == "sdcp_attributes":
                self.state.attributes = data.get("attributes") or {}
                publish_state = True
            elif etype == "printer_connected":
                self.state.connected = True
                publish_state = True
            elif etype == "printer_disconnected":
                self.state.connected = False
                publish_state = True
        except Exception:  # noqa: BLE001
            log.exception("Errore aggiornamento stato da evento %s", etype)
            return
        if publish_state:
            now = time.time()
            force = etype in ("printer_connected", "printer_disconnected")
            if force or now - self._last_state_publish >= 1.0:
                self._last_state_publish = now
                self.bus.publish("state_changed", self.state.snapshot())

    async def _enable_video_on_stream_failure(self) -> None:
        """Se lo stream MJPEG non parte, chiede alla stampante di attivarlo (Cmd 386)."""
        try:
            url = await self.printer_api.enable_video()
            if url:
                log.info("Stream video abilitato dalla stampante: %s", url)
        except Exception:  # noqa: BLE001
            pass


async def amain(cfg) -> None:
    ctx = AppContext(cfg)
    await ctx.start()
    app = create_app(ctx)
    srv_cfg = uvicorn.Config(
        app, host=cfg.service.get("host", "0.0.0.0"),
        port=int(cfg.service.get("port", 8766)),
        log_level="warning", access_log=False,
    )
    server = uvicorn.Server(srv_cfg)
    try:
        await server.serve()
    finally:
        await ctx.stop()


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.logging.get("level", "INFO"),
                  cfg.paths.get("logs", "data/logs"),
                  int(cfg.logging.get("max_bytes", 10485760)),
                  int(cfg.logging.get("backups", 5)))
    log.info("elegoo-notify in avvio (stampante: %s)", cfg.printer["ip"])
    try:
        asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
