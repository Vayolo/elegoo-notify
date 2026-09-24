#!/usr/bin/env python3
"""Fake Moonraker server (Klipper/COSMOS) per test offline di elegoo-notify.

Implementa il sottoinsieme di API Moonraker usato dal driver:
  POST /printer/objects/query    stato (print_stats/extruder/heater_bed/display_status)
  POST /printer/print/start      avvia stampa {"filename": ...}
  POST /printer/print/pause|resume|cancel
  POST /printer/gcode/script     registra i gcode (luce, M220, …)
  POST /server/files/upload      upload GCODE
  GET  /server/files/list        elenco file
  GET  /video                    stream MJPEG (frame dal simulatore SDCP)
  Admin scenario: /_mr/setprogress | /_mr/complete | /_mr/state
"""
from __future__ import annotations

import json
import time
from typing import Any

import cv2
import numpy as np
from aiohttp import web, WSMsgType

from tests.simulator import Simulator  # riuso la generazione dei frame


class MoonrakerSim:
    def __init__(self, port: int = 18050):
        self.port = port
        self.state = "standby"
        self.filename: str | None = None
        self.progress = 0.0
        self.print_duration = 0.0
        self.job_id: str | None = None
        self.gcode_scripts: list[dict] = []
        self.uploads: list[str] = []
        self.calls: list[dict] = []   # log di ogni endpoint colpito
        self._frame_sim = Simulator()  # solo per i frame MJPEG
        self._t0 = time.time()

    # ------------------------------------------------------------------ #
    def _print_stats(self) -> dict[str, Any]:
        return {"state": self.state, "filename": self.filename or "",
                "print_duration": round(self.print_duration, 1),
                "job_id": self.job_id or ""}

    def build_app(self) -> web.Application:
        app = web.Application()

        async def body(request):
            try:
                return await request.json()
            except Exception:  # noqa: BLE001
                return {}

        async def h_query(request):
            self.calls.append({"ep": "objects/query", "ts": time.time()})
            return web.json_response({"result": {"status": {
                "print_stats": self._print_stats(),
                "extruder": {"temperature": 210.0 if self.state == "printing" else 24.0,
                             "target": 210.0 if self.state == "printing" else 0.0},
                "heater_bed": {"temperature": 60.0 if self.state == "printing" else 23.5,
                               "target": 60.0 if self.state == "printing" else 0.0},
                "display_status": {"progress": round(self.progress, 3)
                                    if self.state in ("printing", "paused") else 0.0},
                "temperature_sensor chamber": {"temperature": 31.5},
            }}})

        async def h_start(request):
            j = await body(request)
            self.calls.append({"ep": "print/start", "filename": j.get("filename"),
                              "ts": time.time()})
            self.filename = str(j.get("filename") or "job.gcode")
            self.job_id = f"mr-{int(time.time())}"
            self.state = "printing"
            self.progress = 0.01
            self.print_duration = 0.0
            return web.json_response({"result": "ok"})

        async def h_pause(request):
            self.calls.append({"ep": "print/pause", "ts": time.time()})
            self.state = "paused"
            return web.json_response({"result": "ok"})

        async def h_resume(request):
            self.calls.append({"ep": "print/resume", "ts": time.time()})
            self.state = "printing"
            return web.json_response({"result": "ok"})

        async def h_cancel(request):
            self.calls.append({"ep": "print/cancel", "ts": time.time()})
            self.state = "cancelled"
            return web.json_response({"result": "ok"})

        async def h_gcode(request):
            j = await body(request)
            self.gcode_scripts.append({"script": j.get("script", ""),
                                       "ts": time.time()})
            self.calls.append({"ep": "gcode", "script": j.get("script", ""),
                              "ts": time.time()})
            return web.json_response({"result": "ok"})

        async def h_upload(request):
            reader = await request.multipart()
            name = None
            async for part in reader:
                if part.filename:
                    name = part.filename
                    await part.read()  # scarta il contenuto
            self.uploads.append(name or "?")
            return web.json_response({"result": "ok", "files": [name or "?"]})

        async def h_list(request):
            return web.json_response({"files": [
                {"path": u, "size": 1024, "modified": time.time()}
                for u in self.uploads]})

        async def h_video(request):
            resp = web.StreamResponse(headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=--foo"})
            await resp.prepare(request)
            self._frame_sim.print_status = 1 if self.state == "printing" else 0
            self._frame_sim.current_ticks = int(self.progress * 36000)
            try:
                while True:
                    data = self._frame_sim.make_jpeg()
                    header = (b"--foo\r\nContent-Type: image/jpeg\r\n"
                              + f"Content-Length: {len(data)}\r\n\r\n".encode())
                    await resp.write(header + data + b"\r\n")
                    await resp.drain()
                    await __import__("asyncio").sleep(0.2)
            except (ConnectionResetError, __import__("asyncio").CancelledError):
                pass
            return resp

        # --- admin scenario ---
        async def h_setprogress(request):
            j = await body(request)
            self.progress = max(0.0, min(float(j.get("percent", 0)) / 100.0, 1.0))
            self.print_duration = self.progress * 600.0
            return web.json_response({"ok": True})

        async def h_complete(request):
            self.state = "complete"
            self.progress = 1.0
            self.print_duration = 600.0
            return web.json_response({"ok": True})

        async def h_state(request):
            return web.json_response({
                "state": self.state, "filename": self.filename,
                "progress": self.progress, "gcode": self.gcode_scripts,
                "uploads": self.uploads})

        app.router.add_post("/printer/objects/query", h_query)
        app.router.add_post("/printer/print/start", h_start)
        app.router.add_post("/printer/print/pause", h_pause)
        app.router.add_post("/printer/print/resume", h_resume)
        app.router.add_post("/printer/print/cancel", h_cancel)
        app.router.add_post("/printer/gcode/script", h_gcode)
        app.router.add_post("/server/files/upload", h_upload)
        app.router.add_get("/server/files/list", h_list)
        app.router.add_get("/video", h_video)
        app.router.add_post("/_mr/setprogress", h_setprogress)
        app.router.add_post("/_mr/complete", h_complete)
        app.router.add_get("/_mr/state", h_state)
        return app


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Fake Moonraker (Klipper)")
    ap.add_argument("--port", type=int, default=18050)
    args = ap.parse_args()
    sim = MoonrakerSim(port=args.port)
    print(f"Fake Moonraker su http://127.0.0.1:{args.port}")
    web.run_app(sim.build_app(), host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
