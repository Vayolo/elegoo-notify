"""Progress manager: decide QUANDO notificare,Telegram con testo e/o foto.

Notifiche gestite: start, progress (milestone %), time-based, complete,
failed, error, ai_alert. Il debounce/spam-control è delegato al
TelegramNotifier (min_seconds_between_msgs).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ..state import ERROR_STATUS_REASONS
from .telegram import TelegramNotifier

log = logging.getLogger("elegoo.progress")


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 0:
        return "0m"
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


class ProgressManager:
    def __init__(self, cfg, bus, telegram: TelegramNotifier, webcam, printer_api, state):
        self.cfg = cfg
        self.bus = bus
        self.telegram = telegram
        self.webcam = webcam
        self.printer_api = printer_api
        self.state = state
        self.sub = bus.subscribe("print_*", "printer_error", "sdcp_error", "ai_alert")

        t = cfg.telegram
        self.notify_on: set[str] = set(t.get("notify_on", []))
        self.photo_on: set[str] = set(t.get("photo_on", []))
        self.step: float = float(t.get("progress_step_percent", 10))
        self.interval_min: float = float(t.get("notify_interval_minutes", 30))

        # Tracciamento del job corrente
        self._job_filename: Optional[str] = None
        self._job_start_ts: Optional[float] = None
        self._last_milestone: int = 0
        self._last_time_notify: float = 0.0
        self._task: Optional[Any] = None

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self.sub.set_callback(self._on_event)

    async def stop(self) -> None:
        self.sub.close()

    # ------------------------------------------------------------------ #
    async def _on_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        data = event.get("data") or {}
        try:
            handler = getattr(self, f"_on_{etype}", None)
            if handler is not None:
                await handler(data)
        except Exception:  # noqa: BLE001
            log.exception("Errore nella gestione dell'evento %s", etype)

    def _maybe_photo(self, kind: str):
        if kind not in self.photo_on:
            return None
        return self.webcam

    async def _photo_bytes(self, kind: str) -> Optional[bytes]:
        """Foto per la notifica `kind`, None se la foto non è richiesta/disponibile.
        Mai bloccare una notifica per più di 8 s sulla webcam."""
        if kind not in self.photo_on:
            return None
        try:
            jpeg = await asyncio.wait_for(
                self.webcam.get_jpeg(max_age_s=10.0, wait_fresh=4.0), timeout=8.0)
            self.webcam.save_snapshot(jpeg, prefix=kind)
            return jpeg
        except Exception as e:  # noqa: BLE001
            log.warning("Foto per notifica '%s' non disponibile: %s", kind, e)
            return None

    # ------------------------------------------------------------------ #
    # Gestori eventi
    # ------------------------------------------------------------------ #
    async def _on_print_started(self, data: dict[str, Any]) -> None:
        if "start" not in self.notify_on:
            return
        self._job_filename = data.get("filename")
        self._job_start_ts = time.time()
        self._last_milestone = 0
        self._last_time_notify = time.time()
        photo = await self._photo_bytes("start")
        text = (f"🖨️ <b>Stampa iniziata</b>\n"
                f"File: {self._job_filename or 'sconosciuto'}")
        await self.telegram.notify(text, photo=photo, kind="start")

    async def _on_print_progress(self, data: dict[str, Any]) -> None:
        if self._job_start_ts is None:  # progress senza start visto: recupera
            self._job_start_ts = time.time()
            self._job_filename = data.get("filename") or self._job_filename
            self._last_time_notify = time.time()
        pct = data.get("percent")
        if pct is None:
            return

        # --- milestone percentuale ---
        milestone = int(pct // self.step)
        if ("progress" in self.notify_on and milestone > self._last_milestone and pct < 99.5):
            self._last_milestone = milestone
            photo = await self._photo_bytes("progress")
            eta = fmt_duration(data.get("time_remaining_s"))
            layers = ""
            if data.get("total_layers"):
                layers = f" · livello {data.get('current_layers', data.get('current_layer', 0))}/{data['total_layers']}"
            text = (f"📊 <b>Avanzamento {milestone * self.step:.0f}%</b>\n"
                    f"{pct:.1f}% — restano {eta}{layers}")
            silent = bool(self.cfg.telegram.get("silent_progress", True))
            await self.telegram.notify(text, photo=photo, kind="progress", silent=silent)

        # --- aggiornamento time-based ---
        if ("time" in self.notify_on and self._job_start_ts
                and time.time() - self._last_time_notify >= self.interval_min * 60):
            self._last_time_notify = time.time()
            elapsed = time.time() - self._job_start_ts
            eta = fmt_duration(data.get("time_remaining_s"))
            photo = await self._photo_bytes("time")
            text = (f"⏱️ <b>Aggiornamento</b> (ogni {self.interval_min:.0f} min)\n"
                    f"{pct:.1f}% — passati {fmt_duration(elapsed)}, restano {eta}")
            await self.telegram.notify(text, photo=photo, kind="time")

    async def _on_print_completed(self, data: dict[str, Any]) -> None:
        if "complete" not in self.notify_on:
            return
        photo = await self._photo_bytes("complete")
        duration = fmt_duration(data.get("duration_s"))
        text = (f"✅ <b>Stampa completata</b>\n"
                f"File: {data.get('filename') or self._job_filename or 'sconosciuto'}\n"
                f"Durata: {duration}")
        await self.telegram.notify(text, photo=photo, kind="complete")
        self._reset_job()

    async def _on_print_failed(self, data: dict[str, Any]) -> None:
        self._reset_job()
        if "failed" not in self.notify_on:
            return
        reason = await self._enrich_failure_reason(data)
        photo = await self._photo_bytes("failed")
        if data.get("stopped_by_user"):
            text = (f"⏹️ <b>Stampa fermata</b>\n"
                    f"File: {data.get('filename') or 'sconosciuto'}")
            await self.telegram.notify(text, photo=photo, kind="failed", silent=True)
        else:
            text = (f"❌ <b>Stampa FALLITA</b>\n"
                    f"File: {data.get('filename') or 'sconosciuto'}\n"
                    f"Motivo: {reason or 'sconosciuto'}")
            await self.telegram.notify(text, photo=photo, kind="failed", critical=True)

    async def _on_printer_error(self, data: dict[str, Any]) -> None:
        if "error" not in self.notify_on:
            return
        reason = data.get("reason") or str(data.get("error_number", "?"))
        photo = await self._photo_bytes("error")
        text = f"⚠️ <b>Errore stampante</b>\n{reason}"
        await self.telegram.notify(text, photo=photo, kind="error", critical=True)

    async def _on_sdcp_error(self, data: dict[str, Any]) -> None:
        msg = data.get("msg") or {}
        code = (msg.get("Data") or {}).get("Data", {}).get("ErrorCode", "?")
        if "error" not in self.notify_on:
            return
        text = f"⚠️ <b>Errore stampante (SDCP)</b>\nCodice: {code}"
        await self.telegram.notify(text, kind="error", critical=True)

    async def _on_ai_alert(self, data: dict[str, Any]) -> None:
        if "ai_alert" not in self.notify_on:
            return
        # Il messaggio pre/post stop lo gestisce AiMonitor; qui solo event log.
        log.info("Alert AI registrato: %s", data)

    # ------------------------------------------------------------------ #
    async def _enrich_failure_reason(self, data: dict[str, Any]) -> Optional[str]:
        """Recupera ErrorStatusReason dai dettagli storico (best-effort)."""
        task_id = data.get("task_id")
        if not task_id:
            return data.get("reason")
        try:
            details = await self.printer_api.task_details([task_id])
            items = details.get("HistoryDetailList") or []
            if items:
                code = int(items[0].get("ErrorStatusReason") or 0)
                if code:
                    return ERROR_STATUS_REASONS.get(code, f"motivo {code}")
        except Exception:  # noqa: BLE001
            log.debug("Recupero dettagli task %s fallito", task_id)
        return data.get("reason")

    def _reset_job(self) -> None:
        self._job_filename = None
        self._job_start_ts = None
        self._last_milestone = 0
