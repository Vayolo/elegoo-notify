#!/usr/bin/env python3
"""Test di accettazione #1 — eventi WS → notifiche Telegram + REST.

Avvia simulatore + servizio in-process (Telegram in DRY-RUN, no rete):
  1. avvio stampa        → notifica start  con FOTO
  2. salto al 35%        → notifica milestone 30% con FOTO
  3. completamento       → notifica complete con FOTO
  4. secondo job fallito → notifica failed CRITICA
  5. messaggio sdcp/error → notifica error
  6. REST: /status, /cmd/pause|resume, /upload (MD5), /print, /cmd/stop, /photo

Uso:  python3 tests/run_ws_tests.py   (dalla root del repo)
Esito: elenco PASS/FAIL + exit code 0/1.
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
import uvicorn  # noqa: E402

from app.config import load_config  # noqa: E402
from app.main import AppContext  # noqa: E402
from app.api.server import create_app  # noqa: E402
from tests.simulator import Simulator  # noqa: E402

SIM_PORT = 13030
API_PORT = 18766
BASE = f"http://127.0.0.1:{SIM_PORT}"
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


async def wait_until(cond, timeout: float = 15.0, desc: str = "condizione") -> None:
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
    cfg.printer.driver = "sdcp"  # isola dal config.json di produzione
    cfg.printer.ws_urls = [f"ws://127.0.0.1:{SIM_PORT}/websocket"]
    cfg.printer.http_port = SIM_PORT
    cfg.printer.status_poll_seconds = 1
    cfg.webcam.mode = "mjpeg"
    cfg.webcam.mjpeg_url = f"{BASE}/video"
    cfg.webcam.persistent_stream = True
    cfg.webcam.save_snapshots = False
    cfg.telegram.min_seconds_between_msgs = 0
    cfg.telegram.progress_step_percent = 10
    cfg.ai.enabled = False
    cfg.mqtt.enabled = False
    cfg.service.port = API_PORT
    cfg.paths.logs = str(ROOT / "tests/out/logs")
    cfg.paths.snapshots = str(ROOT / "tests/out/snapshots")
    cfg.paths.gcodes = str(ROOT / "tests/out/gcodes")
    return cfg


async def main() -> int:
    print("== Test accettazione WS/Telegram/REST (dry-run) ==", flush=True)

    sim = Simulator(port=SIM_PORT)
    sim_runner = web.AppRunner(sim.build_app())
    await sim_runner.setup()
    site = web.TCPSite(sim_runner, "127.0.0.1", SIM_PORT)
    await site.start()

    cfg = make_config()
    ctx = AppContext(cfg)
    ctx.state.progress_throttle_s = 0.3   # accelerazione per il test
    ctx.state.temp_throttle_s = 0.3
    await ctx.start()

    uv = uvicorn.Server(uvicorn.Config(create_app(ctx), host="127.0.0.1",
                                        port=API_PORT, log_level="error"))
    uv_task = asyncio.create_task(uv.serve())

    session = aiohttp.ClientSession()
    try:
        # --- connessione --- #
        try:
            await wait_until(lambda: ctx.state.connected, 15, "connessione WS")
            check("WS: servizio connesso alla stampante simulata", True)
        except TimeoutError:
            check("WS: servizio connesso alla stampante simulata", False, "timeout")

        # --- 1) START --- #
        async with session.post(f"{BASE}/_sim/start",
                                json={"filename": "test_cube.gcode"}) as r:
            await r.json()
        try:
            await wait_until(lambda: ctx.state.is_printing, 10, "stato printing")
            await wait_until(lambda: any(k["kind"] == "start" for k in tgrams(ctx)),
                             10, "notifica start")
        except TimeoutError:
            pass
        starts = [k for k in tgrams(ctx) if k["kind"] == "start"]
        check("Notifica START ricevuta", len(starts) == 1, f"n={len(starts)}")
        check("Notifica START con FOTO (MJPEG)", bool(starts and starts[0]["has_photo"]))

        # --- 2) PROGRESS 35% → milestone 30% --- #
        async with session.post(f"{BASE}/_sim/setprogress", json={"percent": 35}) as r:
            await r.json()
        try:
            await wait_until(lambda: any(k["kind"] == "progress" for k in tgrams(ctx)),
                             10, "notifica milestone")
        except TimeoutError:
            pass
        progs = [k for k in tgrams(ctx) if k["kind"] == "progress"]
        check("Notifica PROGRESS milestone 30% ricevuta", len(progs) >= 1, f"n={len(progs)}")
        check("Notifica PROGRESS con FOTO", bool(progs and progs[-1]["has_photo"]))
        check("Contenuto milestone corretto (30%)",
              bool(progs and any("30%" in k["text"] for k in progs)))

        # --- 3) COMPLETE --- #
        async with session.post(f"{BASE}/_sim/complete") as r:
            await r.json()
        try:
            await wait_until(lambda: any(k["kind"] == "complete" for k in tgrams(ctx)),
                             10, "notifica complete")
        except TimeoutError:
            pass
        comps = [k for k in tgrams(ctx) if k["kind"] == "complete"]
        check("Notifica COMPLETE ricevuta", len(comps) == 1, f"n={len(comps)}")
        check("Notifica COMPLETE con FOTO", bool(comps and comps[0]["has_photo"]))

        # --- 4) FAILED (secondo job) --- #
        async with session.post(f"{BASE}/_sim/start",
                                json={"filename": "fail_test.gcode"}) as r:
            await r.json()
        try:
            await wait_until(lambda: ctx.state.is_printing, 10, "secondo job printing")
        except TimeoutError:
            pass
        async with session.post(f"{BASE}/_sim/fail",
                                json={"error": 2, "reason": 19}) as r:
            await r.json()
        try:
            await wait_until(lambda: any(k["kind"] == "failed" for k in tgrams(ctx)),
                             12, "notifica failed")
        except TimeoutError:
            pass
        fails = [k for k in tgrams(ctx) if k["kind"] == "failed"]
        check("Notifica FAILED ricevuta", len(fails) == 1, f"n={len(fails)}")
        check("Notifica FAILED critica", bool(fails and fails[0]["critical"]))
        check("Testo FALLITA presente", bool(fails and "FALLITA" in fails[0]["text"]))

        # --- 5) SDCP ERROR --- #
        async with session.post(f"{BASE}/_sim/error") as r:
            await r.json()
        try:
            await wait_until(lambda: any(k["kind"] == "error" for k in tgrams(ctx)),
                             10, "notifica error")
        except TimeoutError:
            pass
        errs = [k for k in tgrams(ctx) if k["kind"] == "error"]
        check("Notifica ERRORE SDCP ricevuta", len(errs) >= 1, f"n={len(errs)}")

        # --- 6) REST --- #
        async with session.get(f"{API}/status") as r:
            st = await r.json()
            st_status = r.status
        check("REST /status risponde connesso",
              st_status == 200 and st.get("connected") is True,
              f"status={st_status} body={st.get('status')}")

        async with session.post(f"{API}/cmd/pause") as r:
            j = await r.json()
        check("REST /cmd/pause eseguito sulla stampante",
              j.get("ok") is True and any(c["cmd"] == 129 for c in sim.commands), f"{j}")

        async with session.post(f"{API}/cmd/resume") as r:
            j = await r.json()
        check("REST /cmd/resume eseguito sulla stampante",
              j.get("ok") is True and any(c["cmd"] == 131 for c in sim.commands), f"{j}")

        gcode = b"; test gcode\nG28\nG1 X10 Y10 F3000\n"
        form = aiohttp.FormData()
        form.add_field("file", gcode, filename="rest_upload.gcode",
                       content_type="application/octet-stream")
        async with session.post(f"{API}/upload", data=form) as r:
            up = await r.json()
            up_status = r.status
        check("REST /upload → GCODE salvato e trasferito",
              up_status == 200 and up.get("transferred") is True,
              f"status={up_status} body={up}")
        check("Upload: MD5 verificato dalla stampante",
              any(u.get("ok") and u.get("name") == "rest_upload.gcode" for u in sim.uploads))

        async with session.post(f"{API}/print",
                                json={"filename": "test_cube.gcode"}) as r:
            pj = await r.json()
            pj_status = r.status
        check("REST /print avvia la stampa",
              pj_status == 200 and pj.get("ok") is True, f"status={pj_status} {pj}")
        try:
            await wait_until(lambda: ctx.state.is_printing, 10, "stampa da /print")
        except TimeoutError:
            pass
        check("Stampa da /print attiva sullo stato", ctx.state.is_printing)

        # velocità in stampa: accettata (Cmd 403)
        async with session.post(f"{API}/cmd/speed", json={"percent": 80}) as r:
            j = await r.json()
        check("REST /cmd/speed imposta la velocità (anche in stampa)",
              r.status == 200 and j.get("ok") is True and any(
                  c["cmd"] == 403 and c["data"].get("PrintSpeedPct") == 80
                  for c in sim.commands), f"{j}")
        # luce DURANTE la stampa: il firmware rifiuta (limitazione reale Centauri)
        async with session.post(f"{API}/cmd/light", json={"on": True}) as r:
            j = await r.json()
        check("Luce in stampa RIFIUTATA dal firmware (Ack=1, come il reale)",
              r.status == 502 and "rifiutato" in (j.get("detail") or ""),
              f"status={r.status} {j}")
        # pre-hook: start_print ha acceso la luce PRIMA del Cmd 128
        idx_light = next((i for i, c in enumerate(sim.commands)
                          if c["cmd"] == 403 and isinstance(c["data"].get("LightStatus"), dict)), None)
        idx_start = next((i for i, c in enumerate(sim.commands) if c["cmd"] == 128), None)
        check("Pre-hook: luce accesa PRIMA dello start print",
              idx_light is not None and idx_start is not None and idx_light < idx_start,
              f"light@{idx_light} start@{idx_start}")

        async with session.post(f"{API}/cmd/stop") as r:
            j = await r.json()
        check("REST /cmd/stop eseguito sulla stampante",
              j.get("ok") is True and any(c["cmd"] == 130 for c in sim.commands), f"{j}")
        try:
            await wait_until(lambda: not ctx.state.job_active, 10, "fine job")
        except TimeoutError:
            pass
        check("Stato coerente post-stop (non in job)", not ctx.state.job_active)

        # luce a stampante ferma: accettata
        async with session.post(f"{API}/cmd/light", json={"on": False}) as r:
            j = await r.json()
        check("Luce a stampante FERMA accettata",
              r.status == 200 and j.get("ok") is True and sim.light == 0, f"{j}")

        async with session.get(f"{API}/photo") as r:
            photo_ok = r.status == 200 and len(await r.read()) > 500
        check("REST /photo restituisce un JPEG", photo_ok)

    finally:
        await session.close()
        uv_task.cancel()
        try:
            await uv_task
        except (asyncio.CancelledError, Exception):
            pass
        await ctx.stop()
        await sim_runner.cleanup()

    passed = sum(1 for _, c, _ in results if c)
    total = len(results)
    print(f"\n== RISULTATO: {passed}/{total} PASS ==")
    if passed == total:
        print("TEST WS/REST: SUCCESSO")
        return 0
    print("TEST WS/REST: FALLITO")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
