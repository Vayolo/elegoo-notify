#!/usr/bin/env python3
"""Test di accettazione #2 — AI alert → STOP automatico (fail-safe).

Avvia simulatore (MJPEG con anomalie iniettabili) + servizio con AI attiva
(sensibilità high, interval 0.5 s, consecutive 2):
  1. avvia una stampa e lascia stabilire le baseline per qualche secondo
  2. inietta l'anomalia "spaghetti" nei frame della webcam
  3. verifica: notifica CRITICA con foto PRIMA dello stop, stop ricevuto
     dalla stampante (Cmd 130), notifica di conferma DOPO l'azione
  4. verifica smoke detector con anomaly "smoke" (alert warning)

Uso:  python3 tests/run_ai_tests.py
Parametri di test utilizzati (documentati nel README § AI):
  ai.interval_seconds=0.5, sensitivity=high, consecutive_frames=2,
  auto_stop=true, cooldown_seconds=2.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from app.config import load_config  # noqa: E402
from app.main import AppContext  # noqa: E402
from tests.simulator import Simulator  # noqa: E402

SIM_PORT = 13031
BASE = f"http://127.0.0.1:{SIM_PORT}"

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    results.append((name, bool(cond), extra))
    line = f"  [{'PASS' if cond else 'FAIL'}] {name}"
    if not cond and extra:
        line += f"  -> {extra}"
    print(line, flush=True)


def tgrams(ctx: AppContext) -> list[dict]:
    return list(getattr(ctx.telegram, "sent_log", []))


async def wait_until(cond, timeout: float = 20.0, desc: str = "condizione") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.2)
    raise TimeoutError(desc)


def make_config():
    env = {"TELEGRAM_TOKEN": "TEST:TOKEN", "TELEGRAM_CHAT_ID": "12345",
           "TELEGRAM_DRYRUN": "1"}
    cfg = load_config(environ=env)
    cfg.printer.ip = "127.0.0.1"
    cfg.printer.ws_urls = [f"ws://127.0.0.1:{SIM_PORT}/websocket"]
    cfg.printer.http_port = SIM_PORT
    cfg.printer.status_poll_seconds = 1
    cfg.webcam.mode = "mjpeg"
    cfg.webcam.mjpeg_url = f"{BASE}/video"
    cfg.webcam.persistent_stream = True
    cfg.webcam.save_snapshots = False
    cfg.telegram.min_seconds_between_msgs = 0
    cfg.ai.enabled = True
    cfg.ai.interval_seconds = 0.5
    cfg.ai.auto_stop = True
    cfg.ai.only_while_printing = True
    cfg.ai.sensitivity = "high"
    cfg.ai.consecutive_frames = 2
    cfg.ai.cooldown_seconds = 2
    cfg.ai.warmup_samples = 2
    cfg.ai.spaghetti_min_layer = 0
    cfg.ai.spaghetti_baseline_factor = 1.0
    cfg.ai.layer_watch = {"enabled": False}
    cfg.mqtt.enabled = False
    cfg.paths.logs = str(ROOT / "tests/out/logs")
    cfg.paths.snapshots = str(ROOT / "tests/out/snapshots")
    cfg.paths.gcodes = str(ROOT / "tests/out/gcodes")
    return cfg


async def main() -> int:
    print("== Test accettazione AI (spaghetti → auto-stop, fail-safe) ==", flush=True)

    sim = Simulator(port=SIM_PORT)
    sim_runner = web.AppRunner(sim.build_app())
    await sim_runner.setup()
    site = web.TCPSite(sim_runner, "127.0.0.1", SIM_PORT)
    await site.start()

    cfg = make_config()
    ctx = AppContext(cfg)
    ctx.state.progress_throttle_s = 0.3
    await ctx.start()

    session = aiohttp.ClientSession()
    try:
        try:
            await wait_until(lambda: ctx.state.connected, 15, "connessione WS")
            check("WS: servizio connesso al simulatore", True)
        except TimeoutError:
            check("WS: servizio connesso al simulatore", False, "timeout")

        # --- 1) stampa + baseline AI stabile --- #
        async with session.post(f"{BASE}/_sim/start",
                                json={"filename": "ai_test.gcode"}) as r:
            await r.json()
        await wait_until(lambda: ctx.state.is_printing, 10, "printing")
        check("Stampa attiva prima dell'anomalia", ctx.state.is_printing)
        await asyncio.sleep(3.5)  # baseline EMA/sharpness (nessun alert atteso)
        pre_alerts = [k for k in tgrams(ctx) if k["kind"].startswith("ai_alert")]
        check("Nessun alert AI durante fase normale", len(pre_alerts) == 0,
              f"n={len(pre_alerts)}")

        # --- 2) anomalia SPAGHETTI --- #
        async with session.post(f"{BASE}/_sim/anomaly",
                                json={"type": "spaghetti"}) as r:
            await r.json()
        try:
            await wait_until(
                lambda: any(k["kind"] == "ai_alert" and k["critical"] for k in tgrams(ctx)),
                25, "alert critico spaghetti")
        except TimeoutError:
            pass
        alerts = [k for k in tgrams(ctx) if k["kind"] == "ai_alert" and k["critical"]]
        check("AI ALERT critico (spaghetti) emesso", len(alerts) >= 1,
              f"n={len(alerts)}")
        check("AI ALERT con FOTO dell'anomalia", bool(alerts and alerts[0]["has_photo"]))

        # --- 3) stop automatico eseguito --- #
        try:
            await wait_until(lambda: any(c["cmd"] == 130 for c in sim.commands),
                             15, "Cmd 130 (stop) ricevuto dal simulatore")
            check("STOP automatico: Cmd 130 ricevuto dalla stampante", True)
        except TimeoutError:
            check("STOP automatico: Cmd 130 ricevuto dalla stampante", False,
                  "nessuno stop nei comandi")

        # --- 4) notifica DOPO l'azione --- #
        try:
            await wait_until(lambda: any(k["kind"] == "ai_stop_done" for k in tgrams(ctx)),
                             15, "notifica post-stop")
        except TimeoutError:
            pass
        done = [k for k in tgrams(ctx) if k["kind"] == "ai_stop_done"]
        check("Notifica di conferma DOPO lo stop", len(done) >= 1, f"n={len(done)}")
        check("Conferma con foto aggiornata", bool(done and done[0]["has_photo"]))
        check("Ordine fail-safe (alert PRIMA della conferma)",
              bool(alerts and done and alerts[0]["ts"] <= done[0]["ts"]))

        # job fermo: nessuna stampa attiva
        try:
            await wait_until(lambda: not ctx.state.job_active, 10, "job chiuso")
        except TimeoutError:
            pass
        check("Stampa effettivamente fermata", not ctx.state.job_active)

        # --- 5) smoke detector (warning) --- #
        async with session.post(f"{BASE}/_sim/anomaly", json={"type": None}) as r:
            await r.json()
        async with session.post(f"{BASE}/_sim/start",
                                json={"filename": "smoke_test.gcode"}) as r:
            await r.json()
        await wait_until(lambda: ctx.state.is_printing, 10, "printing smoke job")
        await asyncio.sleep(2.5)  # baseline nitidezza/luminosità
        n_before = len([k for k in tgrams(ctx) if "smoke" in k["kind"]])
        async with session.post(f"{BASE}/_sim/anomaly",
                               json={"type": "smoke"}) as r:
            await r.json()
        try:
            await wait_until(
                lambda: len([k for k in tgrams(ctx) if "smoke" in k["kind"]]) > n_before,
                25, "alert smoke")
            check("AI ALERT smoke (warning) emesso", True)
        except TimeoutError:
            check("AI ALERT smoke (warning) emesso", False, "timeout")
        async with session.post(f"{BASE}/_sim/anomaly", json={"type": None}) as r:
            await r.json()
        async with session.post(f"{BASE}/_sim/stop") as r:
            await r.json()

    finally:
        await session.close()
        await ctx.stop()
        await sim_runner.cleanup()

    passed = sum(1 for _, c, _ in results if c)
    total = len(results)
    print(f"\n== RISULTATO: {passed}/{total} PASS ==")
    if passed == total:
        print("TEST AI/STOP: SUCCESSO")
        return 0
    print("TEST AI/STOP: FALLITO")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
