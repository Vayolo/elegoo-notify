#!/usr/bin/env python3
"""Simulatore di Elegoo Centauri Carbon (SDCP v3) per test offline.

Espone su UNA porta (default 13030):
  - WebSocket SDCP su /websocket, /ws e /  (stessi payload della docs)
  - GET  /video                stream MJPEG (frame sintetici, anomalie injectable)
  - POST /uploadFile/upload    upload GCODE multipart (verifica MD5)
  - API di controllo scenario:
      POST /_sim/start {"filename": "x.gcode"}
      POST /_sim/setprogress {"percent": 35}
      POST /_sim/complete
      POST /_sim/fail {"error": 2, "reason": 19}
      POST /_sim/error          → invia un messaggio sdcp/error
      POST /_sim/anomaly {"type": "spaghetti"|"smoke"|null}
      POST /_sim/pause | /_sim/resume | /_sim/stop
      GET  /_sim/state | /_sim/commands | /_sim/uploads

Uso standalone:  python3 tests/simulator.py --port 13030
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import time
import uuid
from typing import Any

import numpy as np
import cv2
from aiohttp import web, WSMsgType

CMD_NAMES = {0: "status", 1: "attributes", 128: "start_print", 129: "pause",
             130: "stop", 131: "resume", 258: "file_list", 320: "history",
             321: "task_details", 386: "video", 387: "timelapse"}


class Simulator:
    def __init__(self, port: int = 13030):
        self.port = port
        self.mid = "SIM0001CENTAURI"
        self.clients: set = set()

        self.print_status = 0
        self.prev_status = 0
        self.filename: str | None = None
        self.task_id: str | None = None
        self.total_ticks = 36000
        self.current_ticks = 0
        self.total_layers = 120
        self.current_layer = 0
        self.error_number = 0
        self.error_reason = 0
        self.temp_nozzle = 24.0
        self.temp_bed = 23.0
        self.temp_chamber = 24.5
        self.anomaly: str | None = None

        self.commands: list[dict] = []
        self.uploads: list[dict] = []
        self._pusher_task: asyncio.Task | None = None
        self._job_counter = 0

    # ------------------------------------------------------------------ #
    # Messaggi SDCP
    # ------------------------------------------------------------------ #
    def status_msg(self) -> dict:
        machine = 1 if self.print_status in (1, 2, 3, 4, 5, 6, 10) else 0
        print_info = None
        if self.print_status != 0:
            print_info = {
                "Status": self.print_status,
                "CurrentLayer": self.current_layer,
                "TotalLayer": self.total_layers,
                "CurrentTicks": self.current_ticks,
                "TotalTicks": self.total_ticks,
                "Filename": self.filename or "",
                "ErrorNumber": self.error_number,
                "TaskId": self.task_id or "",
                "PrintSpeed": 100,
            }
        hot = self.print_status in (1, 5, 6, 10)
        return {
            "Id": str(uuid.uuid4()),
            "Status": {
                "CurrentStatus": [machine],
                "PreviousStatus": self.prev_status,
                "TempOfNozzle": round(self.temp_nozzle, 1),
                "TempTargetNozzle": 210 if hot else 0,
                "TempOfHotbed": round(self.temp_bed, 1),
                "TempTargetHotbed": 60 if hot else 0,
                "TempOfBox": round(self.temp_chamber, 1),
                "TempTargetBox": 0,
                "CurrenCoord": f"150.5,75.2,{10.8 + self.current_layer}",
                "CurrentFanSpeed": {"ModelFan": 80, "ModeFan": 80, "AuxiliaryFan": 50, "BoxFan": 0},
                "LightStatus": {"SecondLight": 1},
                "ZOffset": 0.0,
                "PrintSpeed": 100,
                "PrintInfo": print_info,
            },
            "MainboardID": self.mid,
            "TimeStamp": int(time.time()),
            "Topic": f"sdcp/status/{self.mid}",
        }

    def attrs_msg(self) -> dict:
        return {
            "Id": str(uuid.uuid4()),
            "Attributes": {
                "Name": "Centauri Carbon (SIM)", "MachineName": "Centauri Carbon",
                "BrandName": "Elegoo", "ProtocolVersion": "V3.0.0",
                "FirmwareVersion": "V1.1.0-SIM", "XYZsize": "256x256x256",
                "MainboardIP": "127.0.0.1", "MainboardID": self.mid,
                "NumberOfVideoStreamConnected": len(self.clients),
                "MaximumVideoStreamAllowed": 1, "NetworkStatus": "wlan",
                "UsbDiskStatus": 0,
                "Capabilities": ["FILE_TRANSFER", "PRINT_CONTROL", "VIDEO_STREAM"],
                "SupportFileType": ["GCODE"], "CameraStatus": 1,
                "RemainingMemory": 8 * 1024 * 1024 * 1024,
            },
            "MainboardID": self.mid,
            "TimeStamp": int(time.time()),
            "Topic": f"sdcp/attributes/{self.mid}",
        }

    def response_msg(self, req: dict, data: dict) -> dict:
        inner = req.get("Data") or {}
        return {
            "Id": str(uuid.uuid4()),
            "Data": {
                "Cmd": inner.get("Cmd"),
                "Data": data,
                "RequestID": inner.get("RequestID"),
                "MainboardID": self.mid,
                "TimeStamp": int(time.time()),
            },
            "Topic": f"sdcp/response/{self.mid}",
        }

    # ------------------------------------------------------------------ #
    # Invii
    # ------------------------------------------------------------------ #
    async def _push(self, ws, msg: dict) -> None:
        try:
            await ws.send_str(json.dumps(msg))
        except (ConnectionResetError, RuntimeError):
            self.clients.discard(ws)

    async def _broadcast(self, msg: dict) -> None:
        for ws in list(self.clients):
            await self._push(ws, msg)

    async def push_status(self) -> None:
        await self._broadcast(self.status_msg())

    # ------------------------------------------------------------------ #
    # Comandi / scenari
    # ------------------------------------------------------------------ #
    async def api_start_print(self, filename: str) -> int:
        if self.print_status in (1, 2, 3, 4, 10):
            return 1  # busy
        self._job_counter += 1
        self.filename = filename
        self.task_id = f"task-{self._job_counter:04d}"
        self.error_number = 0
        self.error_reason = 0
        self.current_ticks = 0
        self.current_layer = 0
        self.print_status = 10
        await self.push_status()
        asyncio.create_task(self._begin_printing())
        return 0

    async def _begin_printing(self) -> None:
        await asyncio.sleep(0.5)
        if self.print_status == 10:
            self.print_status = 1
            await self.push_status()
            if self._pusher_task is None or self._pusher_task.done():
                self._pusher_task = asyncio.create_task(self._pusher_loop())

    async def _pusher_loop(self) -> None:
        """Avanza tick/temperature e pusha lo status mentre la stampa è attiva."""
        try:
            while self.print_status in (1, 5, 6, 10):
                if self.print_status == 1:
                    self.current_ticks = min(self.current_ticks + 5, self.total_ticks)
                    self.current_layer = int(self.total_layers * self.current_ticks / max(self.total_ticks, 1))
                target_n, target_b = (210.0, 60.0) if self.print_status in (1, 5, 6, 10) else (25.0, 24.0)
                self.temp_nozzle += (target_n - self.temp_nozzle) * 0.06
                self.temp_bed += (target_b - self.temp_bed) * 0.06
                self.temp_chamber = 24.0 + (self.print_status in (1, 5, 6, 10)) * 8 * (self.current_ticks / max(self.total_ticks, 1))
                self.prev_status = self.print_status
                await self.push_status()
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    async def api_pause(self) -> None:
        if self.print_status in (1, 10):
            self.print_status = 6
            await self.push_status()

    async def api_resume(self) -> None:
        if self.print_status == 6:
            self.print_status = 1
            await self.push_status()

    async def api_stop(self) -> None:
        if self.print_status in (1, 5, 6, 10):
            self.print_status = 8
            await self.push_status()

    async def api_set_progress(self, percent: float) -> None:
        pct = max(0.0, min(float(percent), 100.0))
        self.current_ticks = int(self.total_ticks * pct / 100)
        self.current_layer = int(self.total_layers * pct / 100)
        await self.push_status()

    async def api_complete(self) -> None:
        self.current_ticks = self.total_ticks
        self.current_layer = self.total_layers
        self.print_status = 9
        await self.push_status()

    async def api_fail(self, error: int = 2, reason: int = 19) -> None:
        self.error_number = error
        self.error_reason = reason
        self.print_status = 8
        await self.push_status()

    async def api_send_error(self) -> None:
        msg = {
            "Id": str(uuid.uuid4()),
            "Data": {"Data": {"ErrorCode": 1}, "MainboardID": self.mid,
                     "TimeStamp": int(time.time())},
            "Topic": f"sdcp/error/{self.mid}",
        }
        await self._broadcast(msg)

    # ------------------------------------------------------------------ #
    # WebSocket handler
    # ------------------------------------------------------------------ #
    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.clients.add(ws)
        await self._push(ws, self.attrs_msg())
        await self._push(ws, self.status_msg())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if msg.data == "ping":
                        await ws.send_str("pong")
                        continue
                    try:
                        req = json.loads(msg.data)
                    except ValueError:
                        continue
                    await self._on_request(ws, req)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            self.clients.discard(ws)
        return ws

    async def _on_request(self, ws, req: dict) -> None:
        inner = req.get("Data") or {}
        cmd = inner.get("Cmd")
        payload = inner.get("Data") or {}
        self.commands.append({"cmd": cmd, "name": CMD_NAMES.get(cmd, str(cmd)),
                              "ts": time.time(), "data": payload})
        if cmd == 0:
            await self._push(ws, self.status_msg())
            await self._push(ws, self.response_msg(req, {"Ack": 0}))
        elif cmd == 1:
            await self._push(ws, self.attrs_msg())
            await self._push(ws, self.response_msg(req, {"Ack": 0}))
        elif cmd == 128:
            ack = await self.api_start_print(str(payload.get("Filename") or "job.gcode"))
            await self._push(ws, self.response_msg(req, {"Ack": ack}))
        elif cmd == 129:
            await self.api_pause()
            await self._push(ws, self.response_msg(req, {"Ack": 0}))
        elif cmd == 130:
            await self.api_stop()
            await self._push(ws, self.response_msg(req, {"Ack": 0}))
        elif cmd == 131:
            await self.api_resume()
            await self._push(ws, self.response_msg(req, {"Ack": 0}))
        elif cmd == 258:
            await self._push(ws, self.response_msg(req, {"Ack": 0, "FileList": [
                {"name": "/local/test_cube.gcode", "usedSize": 1024, "totalSize": 1024,
                 "storageType": 0, "type": 1}]}))
        elif cmd == 320:
            await self._push(ws, self.response_msg(req, {"Ack": 0, "HistoryData": [self.task_id or "task-0001"]}))
        elif cmd == 321:
            await self._push(ws, self.response_msg(req, {"Ack": 0, "HistoryDetailList": [{
                "TaskName": self.filename, "TaskId": self.task_id,
                "BeginTime": int(time.time()) - 1000, "EndTime": int(time.time()),
                "TaskStatus": 2, "AlreadyPrintLayer": self.current_layer,
                "ErrorStatusReason": self.error_reason}]}))
        elif cmd == 386:
            data = {"Ack": 0}
            if payload.get("Enable"):
                data["VideoUrl"] = f"http://127.0.0.1:{self.port}/video"
            await self._push(ws, self.response_msg(req, data))
        else:
            await self._push(ws, self.response_msg(req, {"Ack": 0}))

    # ------------------------------------------------------------------ #
    # HTTP: upload + MJPEG + admin
    # ------------------------------------------------------------------ #
    async def handle_upload(self, request: web.Request) -> web.Response:
        fields: dict[str, str] = {}
        file_name: str | None = None
        file_data = bytearray()
        try:
            reader = await request.multipart()
            async for part in reader:
                if part.filename:
                    file_name = part.filename
                    while True:
                        chunk = await part.read_chunk()
                        if not chunk:
                            break
                        file_data.extend(chunk)
                else:
                    fields[part.name] = (await part.text()).strip()
        except Exception:  # noqa: BLE001
            return web.json_response({"code": "111111", "success": False,
                                      "messages": "multipart malformato"})
        md5 = hashlib.md5(bytes(file_data)).hexdigest()
        ok = bool(file_name) and fields.get("S-File-MD5") == md5
        self.uploads.append({"name": file_name, "size": len(file_data), "md5": md5,
                             "ok": ok, "uuid": fields.get("Uuid"), "ts": time.time()})
        if ok:
            return web.json_response({"code": "000000", "messages": None, "data": {}, "success": True})
        return web.json_response({"code": "111111",
                                  "messages": [{"field": "common_field", "message": -4}],
                                  "data": None, "success": False})

    async def handle_video(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={
            "Content-Type": "multipart/x-mixed-replace; boundary=--foo"})
        await resp.prepare(request)
        try:
            while True:
                data = self.make_jpeg()
                header = (b"--foo\r\nContent-Type: image/jpeg\r\nContent-Length: "
                          + str(len(data)).encode() + b"\r\n\r\n")
                await resp.write(header + data + b"\r\n")
                await resp.drain()
                await asyncio.sleep(0.2)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    # ------------------------------------------------------------------ #
    # Frame sintetici
    # ------------------------------------------------------------------ #
    def make_jpeg(self) -> bytes:
        h, w = 270, 480
        img = np.full((h, w, 3), (28, 26, 24), np.uint8)
        cv2.rectangle(img, (60, 150), (420, 250), (45, 42, 40), -1)  # piatto scuro (come la Centauri reale)
        # skirt perimetrale (come da stampa reale): DEVE essere ignorato dall'AI
        cv2.rectangle(img, (150, 140), (330, 260), (120, 115, 110), 1)
        show_object = self.print_status != 0 and self.anomaly != "detach"
        if show_object:
            pct = min(self.current_ticks / max(self.total_ticks, 1), 1.0)
            hh = int(12 + 80 * pct)
            cv2.rectangle(img, (200, 250 - hh), (280, 250), (200, 195, 190), -1)
            if self.print_status in (1, 5, 6, 10):
                cv2.circle(img, (240, 250 - hh - 8), 6, (245, 245, 245), -1)  # ugello
        if self.anomaly == "spaghetti":
            # spaghetti REALISTICI: polilinee sottili attorno all'oggetto
            rng = random.Random(int(time.time() * 1000))
            for _ in range(36):
                x, y = rng.randint(194, 286), rng.randint(224, 256)
                pts = []
                for _ in range(6):
                    pts.append([x, y])
                    x += rng.randint(-9, 9)
                    y += rng.randint(-8, 8)
                cv2.polylines(img, [np.array(pts)], False, (235, 230, 225), 2, cv2.LINE_AA)
        # rumore di scena (texture: baseline realistica per il rilevatore smoke)
        noise = np.random.default_rng().integers(-6, 7, img.shape, dtype=np.int16)
        img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        if self.anomaly == "smoke":
            haze = np.full_like(img, 95)
            img = cv2.addWeighted(img, 0.45, haze, 0.55, 0)
            img = cv2.GaussianBlur(img, (23, 23), 0)  # il fumo sfuma il rumore
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return jpg.tobytes()

    # ------------------------------------------------------------------ #
    # Admin HTTP
    # ------------------------------------------------------------------ #
    def build_app(self) -> web.Application:
        app = web.Application()

        async def body(request):
            try:
                return await request.json()
            except Exception:  # noqa: BLE001
                return {}

        async def h_start(request):
            j = await body(request)
            ack = await self.api_start_print(str(j.get("filename") or "job.gcode"))
            return web.json_response({"ok": ack == 0, "ack": ack})

        async def h_setprogress(request):
            j = await body(request)
            await self.api_set_progress(float(j.get("percent", 0)))
            return web.json_response({"ok": True})

        async def h_complete(request):
            await self.api_complete()
            return web.json_response({"ok": True})

        async def h_fail(request):
            j = await body(request)
            await self.api_fail(int(j.get("error", 2)), int(j.get("reason", 19)))
            return web.json_response({"ok": True})

        async def h_error(request):
            await self.api_send_error()
            return web.json_response({"ok": True})

        async def h_anomaly(request):
            j = await body(request)
            self.anomaly = j.get("type") or None
            return web.json_response({"ok": True, "anomaly": self.anomaly})

        async def h_pause(request):
            await self.api_pause()
            return web.json_response({"ok": True})

        async def h_resume(request):
            await self.api_resume()
            return web.json_response({"ok": True})

        async def h_stop(request):
            await self.api_stop()
            return web.json_response({"ok": True})

        async def h_state(request):
            return web.json_response({
                "print_status": self.print_status, "filename": self.filename,
                "ticks": self.current_ticks, "total_ticks": self.total_ticks,
                "error_number": self.error_number, "anomaly": self.anomaly,
                "clients": len(self.clients)})

        async def h_commands(request):
            return web.json_response(self.commands)

        async def h_uploads(request):
            return web.json_response(self.uploads)

        app.router.add_get("/websocket", self.handle_ws)
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_get("/", self.handle_ws)
        app.router.add_get("/video", self.handle_video)
        app.router.add_post("/uploadFile/upload", self.handle_upload)
        app.router.add_post("/_sim/start", h_start)
        app.router.add_post("/_sim/setprogress", h_setprogress)
        app.router.add_post("/_sim/complete", h_complete)
        app.router.add_post("/_sim/fail", h_fail)
        app.router.add_post("/_sim/error", h_error)
        app.router.add_post("/_sim/anomaly", h_anomaly)
        app.router.add_post("/_sim/pause", h_pause)
        app.router.add_post("/_sim/resume", h_resume)
        app.router.add_post("/_sim/stop", h_stop)
        app.router.add_get("/_sim/state", h_state)
        app.router.add_get("/_sim/commands", h_commands)
        app.router.add_get("/_sim/uploads", h_uploads)
        return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Simulatore Centauri Carbon SDCP")
    ap.add_argument("--port", type=int, default=13030)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    sim = Simulator(port=args.port)
    print(f"Simulatore SDCP su http://{args.host}:{args.port}  (WS: /websocket)")
    web.run_app(sim.build_app(), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
