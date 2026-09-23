"""Monitor AI: campiona frame dalla webcam e applica i rilevatori CV.

Fail-safe auto-stop (config `ai.auto_stop`, default ON):
  1. notifica CRITICA all'admin con foto dell'anomalia  ("prima")
  2. invia lo stop alla stampante (Cmd 130)
  3. notifica di conferma con foto aggiornata            ("dopo")

Per disabilitare l'AI (server sotto carico): `ai.enabled: false`
nel config, oppure `ai.interval_seconds` più alto.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import cv2
import numpy as np

from ..state import PrinterState
from ..notify.telegram import TelegramNotifier
from .detectors import Detection, FrameAnalyzer
from .layer_watch import LayerWatch

log = logging.getLogger("elegoo.ai")

ALERT_TITLES = {
    "spaghetti": "Spaghetti / caos di filamento",
    "layer_shift": "Shift di layer",
    "detach": "Distacco della stampa dal piatto",
    "breakage": "Pezzo rotto durante la stampa",
    "runout": "Filamento esaurito / ugello intasato",
    "smoke": "Fumo / annebbiamento",
}


class AiMonitor:
    def __init__(self, cfg, bus, telegram: TelegramNotifier, webcam, printer_api, state: PrinterState):
        ai = cfg.ai
        self.enabled: bool = bool(ai.get("enabled", True))
        self.interval: float = max(1.0, float(ai.get("interval_seconds", 4)))
        self.auto_stop: bool = bool(ai.get("auto_stop", True))
        self.only_while_printing: bool = bool(ai.get("only_while_printing", True))
        self.consecutive: int = max(1, int(ai.get("consecutive_frames", 2)))
        self.cooldown: float = float(ai.get("cooldown_seconds", 300))
        self.detectors: dict[str, dict[str, Any]] = ai.get("detectors", {})
        self.spaghetti_min_layer: int = int(ai.get("spaghetti_min_layer", 7))
        self._spag_factor: float = float(ai.get("spaghetti_baseline_factor", 3.0))
        self.roi: tuple = tuple(ai.get("roi") or (0.03, 0.33, 0.94, 0.64))
        self.bus = bus
        self.telegram = telegram
        self.webcam = webcam
        self.printer_api = printer_api
        self.state = state
        self.sensitivity: str = ai.get("sensitivity", "medium")
        self.warmup_samples: int = int(ai.get("warmup_samples", 8))
        self.analyzer = FrameAnalyzer(self.sensitivity, roi=self.roi,
                                      warmup_samples=self.warmup_samples,
                                      spag_baseline_factor=self._spag_factor)
        lw = ai.get("layer_watch", {})
        self.layer_watch = LayerWatch(
            min_layers=int(lw.get("min_layers", 7)),
            deviance_lag=int(lw.get("deviance_lag", 5)),
            nozzle_mask=bool(lw.get("nozzle_mask", True)),
            thresholds=lw.get("thresholds"))
        self.layer_watch_enabled = bool(lw.get("enabled", True))
        self.layer_frame_delay: float = float(lw.get("frame_delay_s", 1.5))
        self._last_layer_seen: Optional[int] = None
        self._layer_pending: Optional[asyncio.Task] = None
        self._counters: dict[str, int] = {}
        self._last_alert: dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None
        self._stopped_alerts: set[str] = set()

        # Detector ML (stack PrintGuard, GPL-2.0): fallback silenzioso a CV
        ml_cfg = ai.get("ml", {})
        self.ml_threshold: float = float(ml_cfg.get("threshold", 0.6))
        self._ml_warmup_left: int = int(ml_cfg.get("warmup_samples", 8))
        self.ml: Optional[Any] = None
        if bool(ml_cfg.get("enabled", True)):
            try:
                from .ml_detector import MlDetector
                crop = ml_cfg.get("crop") or self.roi
                self.ml = MlDetector(ml_cfg.get("model_path", "models/encoder_float32.onnx"),
                                     ml_cfg.get("prototypes_path", "models/prototypes.json"),
                                     crop=crop)
                log.info("Detector ML attivo (soglia %.2f)", self.ml_threshold)
            except Exception as e:  # noqa: BLE001
                log.warning("Detector ML non disponibile (%s): solo stack CV", e)
                self.ml = None

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._run())
            log.info("AI monitor attivo (interval=%.1fs, auto_stop=%s)",
                     self.interval, self.auto_stop)
        elif not self.enabled:
            log.info("AI monitor DISABILITATO da config")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    # ------------------------------------------------------------------ #
    async def _run(self) -> None:
        was_printing = False
        while True:
            try:
                await asyncio.sleep(self.interval)
                printing_now = self.state.is_printing
                if printing_now and not was_printing:
                    # nuova stampa: baseline pulite per i rilevatori
                    self.analyzer = FrameAnalyzer(self.sensitivity, roi=self.roi,
                                                  warmup_samples=self.warmup_samples,
                                                  spag_baseline_factor=self._spag_factor)
                    self._ml_warmup_left = 8
                    self.layer_watch.reset()
                    self._last_layer_seen = None
                    self._counters.clear()
                    self._stopped_alerts.clear()
                    log.info("AI: nuova stampa rilevata, baseline azzerate")
                was_printing = printing_now
                if self.only_while_printing and not printing_now:
                    continue
                sample = await self._get_frame()
                if sample is None:
                    continue
                frame, jpeg = sample
                detections = self.analyzer.analyze(frame)
                # Detector ML (PrintGuard): score per-frame del modello
                if self.ml is not None:
                    self._ml_warmup_left -= 1
                if self.ml is not None and self._ml_warmup_left < 0:
                    try:
                        ml_res = self.ml.score_frame(frame)
                        if ml_res["score"] > self.ml_threshold:
                            detections.append(Detection(
                                "ml_defect", float(ml_res["score"]),
                                f"il modello ML classifica il frame come stampa in difetto "
                                f"(score {ml_res['score']:.2f} > {self.ml_threshold:.2f}, "
                                f"pred {ml_res['prediction']})"))
                    except Exception as e:  # noqa: BLE001
                        log.debug("Inferenza ML fallita: %s", e)
                self._update_counters(detections)
                for det in detections:
                    # gate strutturale: nei primi layer skirt/brim/pareti fini
                    # sembrano spaghetti — come 3DPrintSaviour, niente allerte
                    if (det.type == "spaghetti"
                            and (self.state.current_layer or 0) < self.spaghetti_min_layer):
                        continue
                    await self._maybe_fire(det, jpeg)
                # LayerWatch: un frame per layer (score/deviance NRMSE)
                if self.layer_watch_enabled:
                    layer = self.state.current_layer
                    if layer and layer != self._last_layer_seen:
                        self._last_layer_seen = layer
                        if (self._layer_pending is None
                                or self._layer_pending.done()):
                            self._layer_pending = asyncio.create_task(
                                self._capture_layer(layer))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Errore nel loop AI monitor")
                await asyncio.sleep(self.interval)

    async def _get_frame(self) -> Optional[tuple[np.ndarray, bytes]]:
        """Frame dalla webcam: JPEG bytes → ndarray BGR. None se assente."""
        try:
            jpeg = await self.webcam.get_jpeg(max_age_s=self.interval * 4 + 5)
            frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                return None
            return frame, jpeg
        except Exception as e:  # noqa: BLE001
            log.debug("Frame non disponibile per l'AI: %s", e)
            return None

    async def _capture_layer(self, layer: int) -> None:
        """Cattura il frame del layer appena cambiato e valuta score/deviance."""
        try:
            await asyncio.sleep(self.layer_frame_delay)
            sample = await self._get_frame()
            if sample is None:
                return
            frame, jpeg = sample
            det = self.layer_watch.observe(layer, frame)
            if det is None:
                return
            for typ, fired in (("detach", det.detach), ("breakage", det.breakage),
                               ("runout", det.runout)):
                if fired:
                    desc = {"detach": f"oggetto mancante/sparito dal piatto "
                                      f"(score {det.score:.2f}, devianza {det.deviance:.2f})",
                            "breakage": f"pezzo staccatosi dall'oggetto "
                                        f"(score {det.score:.2f}→Δ, devianza {det.deviance:.2f})",
                            "runout": "la stampa non cresce da più layer "
                                      "(filamento esaurito o ugello intasato)"}[typ]
                    await self._maybe_fire(
                        Detection(typ, max(det.score, det.deviance), desc), jpeg,
                        force=True)  # già confermato per costruzione (1/layer)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Errore nella cattura layer %d", layer)

    def _update_counters(self, detections: list[Detection]) -> None:
        seen = {d.type for d in detections}
        for t in seen:
            self._counters[t] = self._counters.get(t, 0) + 1
        for t in list(self._counters):
            if t not in seen:
                self._counters[t] = 0

    async def _maybe_fire(self, det: Detection, jpeg: bytes,
                          force: bool = False) -> None:
        cfg_det = self.detectors.get(det.type, {})
        if not cfg_det.get("enabled", True):
            return
        severity = str(cfg_det.get("severity", "warning")).lower()
        count = self._counters.get(det.type, 0)
        if count < self.consecutive and not force:
            return
        now = time.time()
        if now - self._last_alert.get(det.type, 0) < self.cooldown:
            return
        self._last_alert[det.type] = now
        self._counters[det.type] = 0

        title = ALERT_TITLES.get(det.type, det.type)
        log.warning("AI ALERT [%s/%s]: %s (score %.2f, %d/%d conferme)",
                    det.type, severity, det.description, det.score, count, self.consecutive)

        # Snapshot persistente + evento per dashboard/MQTT
        saved = self.webcam.save_snapshot(jpeg, prefix=f"ai_{det.type}") if jpeg else None
        self.bus.publish("ai_alert", {
            "type": det.type, "severity": severity, "score": round(det.score, 3),
            "description": det.description, "snapshot": saved,
        })

        if severity == "critical" and self.auto_stop and det.type not in self._stopped_alerts:
            self._stopped_alerts.add(det.type)
            await self._critical_with_stop(title, det, jpeg)
        else:
            text = (f"🤖 <b>Alert AI: {title}</b>\n"
                    f"Anomalia rilevata: {det.description}\n"
                    f"Conferme consecutive: {count}")
            await self.telegram.notify(text, photo=jpeg, kind=f"ai_alert_{det.type}")

    # ------------------------------------------------------------------ #
    # Diagnostica per taratura (NESSUN comando verso la stampante)
    # ------------------------------------------------------------------ #
    async def analyze_once(self) -> dict:
        """Un'analisi forzata, puramente diagnostica: restituisce rilevamenti
        e metriche. Non tocca contatori/notifiche né invia comandi."""
        sample = await self._get_frame()
        if sample is None:
            return {"error": "frame non disponibile dalla webcam"}
        frame, jpeg = sample
        detections = self.analyzer.analyze(frame)
        from dataclasses import asdict
        ml_res = self.ml.score_frame(frame) if self.ml is not None else None
        return {
            "detections": [asdict(d) for d in detections],
            "ml": ml_res,
            "metrics": self.analyzer.last_metrics,
            "counters": dict(self._counters),
            "cooldowns": {k: round(time.time() - v, 0) for k, v in self._last_alert.items()},
            "auto_stop": self.auto_stop,
            "roi": list(self.roi),
        }

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval,
            "auto_stop": self.auto_stop,
            "sensitivity": self.sensitivity,
            "consecutive_frames": self.consecutive,
            "cooldown_seconds": self.cooldown,
            "detectors": self.detectors,
            "roi": list(self.roi),
            "ml": ({"enabled": True,
                    "threshold": self.ml_threshold,
                    **self.ml.last_result} if self.ml is not None
                   else {"enabled": False, "reason": "modello/onnxruntime non disponibile"}),
            "last_metrics": self.analyzer.last_metrics,
            "layer_watch": self.layer_watch.status() if self.layer_watch_enabled else None,
            "counters": dict(self._counters),
        }

    # ------------------------------------------------------------------ #
    # Fail-safe: notifica PRIMA, stop, notifica DOPO
    # ------------------------------------------------------------------ #
    async def _critical_with_stop(self, title: str, det: Detection, jpeg: bytes) -> None:
        # 1) Notifica PRIMA dell'azione
        text = (f"🚨 <b>AI ALERT CRITICO: {title}</b>\n"
                f"Anomalia: {det.description}\n"
                f"STOP automatico della stampa in corso…")
        await self.telegram.notify(text, photo=jpeg, kind="ai_alert", critical=True)

        # 2) Azione di stop
        stop_ok = True
        stop_err: str | None = None
        try:
            await self.printer_api.stop_print()
        except Exception as e:  # noqa: BLE001
            stop_ok = False
            stop_err = str(e)
            log.error("STOP automatico fallito: %s", e)

        await asyncio.sleep(3)

        # 3) Notifica DOPO l'azione, con foto aggiornata
        fresh: bytes | None = None
        try:
            fresh = await self.webcam.get_jpeg(max_age_s=2.0, wait_fresh=5.0)
            self.webcam.save_snapshot(fresh, prefix=f"ai_{det.type}_after")
        except Exception:  # noqa: BLE001
            fresh = None
        if stop_ok:
            text = (f"🛑 <b>Stampa fermata</b> (alert AI: {title})\n"
                    f"Verifica fisicamente la stampante prima di riavviare.")
        else:
            text = (f"🚨 <b>STOP AUTOMATICO FALLITO</b> (alert AI: {title})\n"
                    f"Errore: {stop_err}\n"
                    f"INTERVENI MANUALMENTE!")
        await self.telegram.notify(text, photo=fresh or jpeg, kind="ai_stop_done", critical=True)
