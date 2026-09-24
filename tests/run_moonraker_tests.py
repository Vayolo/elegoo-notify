#!/usr/bin/env python3
"""Test di accettazione #5 — driver Moonraker (Klipper/COSMOS).

Dimostra che elegoo-notify funziona IDENTICO su una stampante convertita
a Klipper/COSMOS: il poller Moonraker produce payload SDCP sintetici e
tutto lo stack (stato, eventi, notifiche Telegram con foto, REST, HA
topic) lavora invariato.

Uso:  python3 tests/run_moonraker_tests.py
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
from app.api.server import create_app  # noqa: E402
from tests.moonraker_sim import MoonrakerSim  # noqa: E402

MR_PORT = 18050
MR = f"http://127.0.0.1:{MR_PORT}"
API_PORT = 18767
API = f"http://127.0.0.1:{API_PORT}"

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
    cfg.printer.driver = "moonraker"
    cfg.printer.moonraker = {"port": MR_PORT, "api_key": "",
                             "light_on_gcode": "SET_LED LED=case WHITE=1",
                             "light_off_gcode": "SET_LED LED=case WHITE=0"}
    cfg.printer.status_poll_seconds = 1
    cfg.webcam.mode = "mjpeg"
    cfg.webcam.mjpeg_url = f"{MR}/video"
    cfg.webcam.save_snapshots = False
    cfg.telegram.min_seconds_between_msgs = 0
    cfg.ai.enabled = False
    cfg.mqtt.enabled = False
    cfg.service.port = API_PORT
    cfg.paths.logs = str(ROOT / "tests/out/logs")
    cfg.paths.snapshots = str(ROOT / "tests/out/snapshots")
    cfg.paths.gcodes = str(ROOT / "tests/out/gcodes")
    return cfg


async def main() -> int:
    print("== Test driver Moonraker (Klipper/COSMOS) ==", flush=True)

    sim = MoonrakerSim(port=MR_PORT)
    runner = web.AppRunner(sim.build_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", MR_PORT).start()

    cfg = make_config()
    ctx = AppContext(cfg)
    ctx.state.progress_throttle_s = 0.3
    await ctx.start()

    import uvicorn
    uv = uvicorn.Server(uvicorn.Config(create_app(ctx), host="127.0.0.1",
                                        port=API_PORT, log_level="error"))
    uv_task = asyncio.create_task(uv.serve())

    session = aiohttp.ClientSession()
    try:
        # 1) connessione via POLLER Moonraker (nessun WS SDCP)
        try:
            await wait_until(lambda: ctx.state.connected, 15, "connessione Moonraker")
            check("Moonraker: servizio connesso (poller, no WS SDCP)", True)
        except TimeoutError:
            check("Moonraker: servizio connesso (poller, no WS SDCP)", False, "timeout")

        # 2) start via REST → pre-hook luce → notifica START con foto
        async with session.post(f"{API}/print",
                                json={"filename": "klipper_test.gcode"}) as r:
            pj = await r.json()
        check("REST /print su Moonraker accettato",
              r.status == 200 and pj.get("ok") is True, f"{pj}")
        await wait_until(lambda: ctx.state.is_printing, 10, "stato printing")
        check("Stato 'printing' propagato (print_stats→SDCP sintetico)",
              ctx.state.is_printing)
        light_gcode_idx = next((i for i, g in enumerate(sim.gcode_scripts)
                                if g["script"].upper().startswith("SET_LED") and "WHITE=1" in g["script"].upper()), None)
        start_idx = next((i for i, c in enumerate(sim.calls)
                          if c["ep"] == "print/start"), None)
        check("Pre-hook luce: gcode SET_LED PRIMA dello start (su COSMOS la luce funziona sempre)",
              light_gcode_idx is not None and start_idx is not None
              and sim.gcode_scripts[light_gcode_idx]["ts"] < sim.calls[start_idx]["ts"])
        try:
            await wait_until(lambda: any(k["kind"] == "start" for k in tgrams(ctx)),
                             10, "notifica start")
            starts = [k for k in tgrams(ctx) if k["kind"] == "start"]
            check("Notifica START con FOTO (pipeline invariata)",
                  bool(starts and starts[0]["has_photo"]))
        except TimeoutError:
            check("Notifica START con FOTO (pipeline invariata)", False, "timeout")

        # 3) progresso: percentuale da display_status.progress
        async with session.post(f"{MR}/_mr/setprogress",
                                json={"percent": 35}) as r:
            await r.json()
        try:
            await wait_until(lambda: any(k["kind"] == "progress"
                                         for k in tgrams(ctx)), 15, "milestone 30%")
            check("Milestone 30% notificata (35% via Moonraker)", True)
        except TimeoutError:
            check("Milestone 30% notificata (35% via Moonraker)", False, "timeout")
        check("Temperatura ugello mappata (extruder→TempOfNozzle)",
              (ctx.state.temps.get("nozzle") or 0) > 100)

        # 4) comandi: pause/resume
        async with session.post(f"{API}/cmd/pause") as r:
            await r.json()
        await wait_until(lambda: any(c["ep"] == "print/pause" for c in sim.calls),
                         8, "pause Moonraker")
        check("REST /cmd/pause → /printer/print/pause", True)
        async with session.post(f"{API}/cmd/resume") as r:
            await r.json()
        await wait_until(lambda: any(c["ep"] == "print/resume" for c in sim.calls),
                         8, "resume Moonraker")
        check("REST /cmd/resume → /printer/print/resume", True)

        # 5) luce: gcode su COSMOS + stato ottimistico propagato
        async with session.post(f"{API}/cmd/light",
                                json={"on": True}) as r:
            j = await r.json()
        check("Luce via Moonraker: gcode SET_LED accettato (niente Ack=1!)",
              r.status == 200 and j.get("ok") is True
              and any("SET_LED" in g["script"] for g in sim.gcode_scripts), f"{j}")

        # 6) velocità: M220
        async with session.post(f"{API}/cmd/speed",
                                json={"percent": 80}) as r:
            j = await r.json()
        check("Velocità via Moonraker → gcode M220 S80",
              r.status == 200 and any("M220 S80" in g["script"]
                                      for g in sim.gcode_scripts), f"{j}")

        # 7) upload + elenco file
        form = aiohttp.FormData()
        form.add_field("file", b"G28\nG1 X10\n", filename="mr_upload.gcode",
                       content_type="application/octet-stream")
        async with session.post(f"{API}/upload", data=form) as r:
            up = await r.json()
        check("Upload GCODE via /server/files/upload (gcodes)",
              r.status == 200 and up.get("transferred") is True
              and "mr_upload.gcode" in sim.uploads, f"{up}")

        # 8) completamento → notifica
        async with session.post(f"{MR}/_mr/complete") as r:
            await r.json()
        try:
            await wait_until(lambda: any(k["kind"] == "complete"
                                         for k in tgrams(ctx)), 15, "notifica complete")
            comps = [k for k in tgrams(ctx) if k["kind"] == "complete"]
            check("Notifica COMPLETE (print_stats state=complete→9)", bool(comps))
        except TimeoutError:
            check("Notifica COMPLETE (print_stats state=complete→9)", False, "timeout")

    finally:
        await session.close()
        uv_task.cancel()
        try:
            await uv_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await ctx.stop()
        await runner.cleanup()

    passed = sum(1 for _, c, _ in results if c)
    total = len(results)
    print(f"\n== RISULTATO: {passed}/{total} PASS ==")
    if passed == total:
        print("TEST MOONRAKER: SUCCESSO")
        return 0
    print("TEST MOONRAKER: FALLITO")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
