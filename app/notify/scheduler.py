"""Scheduler: poll periodico dello stato (Cmd 0) + watchdog connessione.

La stampante pusha lo status quando cambia, ma il polling garantisce
freschezza anche dopo perdita silenziosa di messaggi.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("elegoo.scheduler")


class Scheduler:
    def __init__(self, cfg, bus, printer_api, connector):
        self.cfg = cfg
        self.bus = bus
        self.printer_api = printer_api
        self.connector = connector
        self.poll_seconds: float = max(2.0, float(cfg.printer.get("status_poll_seconds", 10)))
        self._task: asyncio.Task | None = None
        self._failures = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        # Attende la prima connessione
        while not self.connector.connected:
            await asyncio.sleep(1)
        while True:
            t0 = time.monotonic()
            try:
                await self.printer_api.refresh_status()
                self._failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._failures += 1
                log.debug("Poll status fallito (%d): %s", self._failures, e)
                if self._failures >= 3:
                    log.warning("Poll status fallito %d volte: forzo riconnessione WS",
                                self._failures)
                    await self.connector.force_reconnect()
                    self._failures = 0
                    await asyncio.sleep(5)
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.5, self.poll_seconds - elapsed))
