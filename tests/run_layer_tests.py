#!/usr/bin/env python3
"""Test di accettazione #3 — LayerWatch (score/deviance NRMSE per layer).

Verifica la metodologia 3DPrintSaviour adattata:
  1. stampa normale che avanza per layer → score/deviance registrati,
     NESSUN alert (lo skirt perimetrale è presente: deve essere ignorato)
  2. l'oggetto SPARISCE (anomalia "detach") al layer successivo →
     score e devianza esplodono → alert detach (warning)
  3. auto_stop disattivo → NESSUN comando stop verso la stampante

Uso:  python3 tests/run_layer_tests.py
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

SIM_PORT = 13032
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
    ai = cfg.ai
    ai.enabled = True
    ai.interval_seconds = 0.3
    ai.only_while_printing = True
    ai.warmup_samples = 2
    ai.auto_stop = False            # in questo test: MAI stop
    ai.sensitivity = "medium"
    ai.consecutive_frames = 2
    ai.cooldown_seconds = 1
    ai.layer_watch = {"enabled": True, "min_layers": 3, "deviance_lag": 2,
                      "frame_delay_s": 0.2, "nozzle_mask": False,
                      "thresholds": {"detach": 1.0, "breakage_diff": 0.2, "runout": 0.2}}
    ai.detectors = {
        # detector frame-based disattivi: test focalizzato sul LayerWatch
        "spaghetti": {"enabled": False, "severity": "critical"},
        "layer_shift": {"enabled": False, "severity": "warning"},
        "detach": {"enabled": True, "severity": "warning"},
        "breakage": {"enabled": True, "severity": "warning"},
        "runout": {"enabled": True, "severity": "warning"},
        "smoke": {"enabled": False, "severity": "warning"},
    }
    cfg.mqtt.enabled = False
    cfg.paths.logs = str(ROOT / "tests/out/logs")
    cfg.paths.snapshots = str(ROOT / "tests/out/snapshots")
    cfg.paths.gcodes = str(ROOT / "tests/out/gcodes")
    return cfg


async def main() -> int:
    print("== Test accettazione LayerWatch (NRMSE per layer, no auto-stop) ==", flush=True)

    sim = Simulator(port=SIM_PORT)
    sim_runner = web.AppRunner(sim.build_app())
    await sim_runner.setup()
    site = web.TCPSite(sim_runner, "127.0.0.1", SIM_PORT)
    await site.start()

    cfg = make_config()
    ctx = AppContext(cfg)
    await ctx.start()

    session = aiohttp.ClientSession()
    try:
        await wait_until(lambda: ctx.state.connected, 15, "connessione WS")
        check("WS: servizio connesso al simulatore", True)
        async with session.post(f"{BASE}/_sim/start",
                                json={"filename": "layer_test.gcode"}) as r:
            await r.json()
        await wait_until(lambda: ctx.state.is_printing, 10, "printing")
        check("Stampa attiva", ctx.state.is_printing)

        # --- 1) layer normali: avanza e raccoglie score senza alert --- #
        async with session.post(f"{BASE}/_sim/setprogress", json={"percent": 20}) as r:
            await r.json()
        await asyncio.sleep(1.2)
        async with session.post(f"{BASE}/_sim/setprogress", json={"percent": 40}) as r:
            await r.json()
        await asyncio.sleep(1.2)
        async with session.post(f"{BASE}/_sim/setprogress", json={"percent": 60}) as r:
            await r.json()
        await asyncio.sleep(1.5)
        lw = ctx.ai.layer_watch
        check("LayerWatch: ≥3 layer campionati", len(lw.scores) >= 3,
              f"samples={len(lw.scores)}")
        if lw.scores:
            check("LayerWatch: score normali finiti", max(lw.scores) < 1.0,
                  f"scores={[round(s,2) for s in lw.scores]}")
        alerts_normal = [k for k in tgrams(ctx) if k["kind"].startswith("ai_alert")]
        check("Nessun alert in stampa normale (skirt ignorato)",
              len(alerts_normal) == 0, f"n={len(alerts_normal)}")

        # --- 2) l'oggetto sparisce → score/deviance esplodono → detach --- #
        n_before = len(tgrams(ctx))
        async with session.post(f"{BASE}/_sim/anomaly",
                                json={"type": "detach"}) as r:
            await r.json()
        async with session.post(f"{BASE}/_sim/setprogress", json={"percent": 80}) as r:
            await r.json()
        try:
            await wait_until(
                lambda: any("detach" in k["kind"] for k in tgrams(ctx)[n_before:]),
                20, "alert detach")
            check("LayerWatch: alert DETACH emesso", True)
        except TimeoutError:
            check("LayerWatch: alert DETACH emesso", False,
                  f"scores={[round(s,2) for s in lw.scores]}")
        det = [k for k in tgrams(ctx) if "detach" in k["kind"]]
        if det:
            check("Alert detach NON critico (warning)", not det[-1]["critical"])
            check("Alert detach con FOTO", det[-1]["has_photo"])
        check("Score post-detach elevato (> 1.0)", bool(lw.scores and lw.scores[-1] > 1.0),
              f"last={lw.scores[-1] if lw.scores else None}")

        # --- 3) auto_stop disattivo: NESSUN comando stop --- #
        await asyncio.sleep(1.0)
        stops = [c for c in sim.commands if c["cmd"] == 130]
        check("Auto-stop OFF: nessuno stop inviato", len(stops) == 0)

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
        print("TEST LAYERWATCH: SUCCESSO")
        return 0
    print("TEST LAYERWATCH: FALLITO")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
