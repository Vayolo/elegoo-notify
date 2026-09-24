"""API REST + SSE + dashboard per elegoo-notify.

Endpoint:
  GET  /health              stato del servizio (sempre aperto)
  GET  /status               snapshot completo della stampante
  GET  /photo               snapshot JPEG fresco
  GET  /video               proxy dello stream MJPEG della stampante
  GET  /                    dashboard web
  POST /cmd/stop|pause|resume  comandi di stampa
  POST /print               avvia la stampa di un file caricato
  POST /upload              upload GCODE (multipart) → locale + stampante
  GET  /files               elenco GCODE (locali + stampante)
  GET  /api/events          SSE: eventi live per la dashboard
  POST /notify/test         invia una notifica Telegram di prova
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from ..sdcp.printer_api import PrinterCommandError
from ..uploads import UploadError

log = logging.getLogger("elegoo.api")

DASHBOARD_FILE = Path(__file__).resolve().parents[2] / "dashboard" / "index.html"
SSE_TIMEOUT = 20.0

security = HTTPBasic(auto_error=False)


def create_app(ctx) -> FastAPI:
    app = FastAPI(title="elegoo-notify", version="1.0.0",
                  docs_url=None, redoc_url=None, openapi_url=None)
    # vendor JS per la dashboard (three.js & co) — prima mancava il mount!
    vendor_dir = Path(__file__).resolve().parents[2] / "dashboard" / "vendor"
    if vendor_dir.is_dir():
        app.mount("/vendor", StaticFiles(directory=str(vendor_dir)), name="vendor")
    # asset statici della webui v2 (css/js/font/icone)
    assets_dir = Path(__file__).resolve().parents[2] / "dashboard" / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")
    cfg = ctx.cfg
    from ..store import Store
    store = Store(str(Path(cfg.paths.get("logs", "data/logs")).parent))
    ctx.store = store

    # ------------------------------------------------------------------ #
    # Auth (opzionale, da env DASHBOARD_USER / DASHBOARD_PASSWORD)
    # ------------------------------------------------------------------ #
    def require_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
        if not cfg.service.get("auth_enabled", False):
            return
        if credentials is None:
            raise HTTPException(401, "autenticazione richiesta",
                                headers={"WWW-Authenticate": 'Basic realm="elegoo-notify"'})
        ok_user = hmac.compare_digest(credentials.username, cfg.service.get("auth_user", ""))
        ok_pass = hmac.compare_digest(credentials.password, cfg.service.get("auth_password", ""))
        if not (ok_user and ok_pass):
            raise HTTPException(401, "credenziali non valide",
                                headers={"WWW-Authenticate": 'Basic realm="elegoo-notify"'})

    # ------------------------------------------------------------------ #
    # Endpoint servizio
    # ------------------------------------------------------------------ #
    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "connected": ctx.state.connected,
            "uptime_s": round(time.time() - ctx.started_ts, 1),
        }

    @app.get("/status", dependencies=[Depends(require_auth)])
    async def status() -> dict:
        snap = ctx.state.snapshot()
        snap["service"] = {
            "version": app.version,
            "uptime_s": round(time.time() - ctx.started_ts, 1),
            "ws_url": ctx.connector.current_url,
            "mainboard_id": ctx.connector.mainboard_id,
            "ai_enabled": ctx.ai.enabled if ctx.ai else False,
            "mqtt_enabled": bool(cfg.mqtt.get("enabled")),
            "telegram_configured": ctx.telegram.configured if ctx.telegram else False,
        }
        return snap

    # ------------------------------------------------------------------ #
    # Diagnostica AI (taratura: solo lettura, NESSUN comando alla stampante)
    # ------------------------------------------------------------------ #
    @app.get("/ai/metrics", dependencies=[Depends(require_auth)])
    async def ai_metrics() -> dict:
        if not ctx.ai or not ctx.ai.enabled:
            return {"enabled": False}
        return ctx.ai.status()

    @app.post("/ai/analyze_now", dependencies=[Depends(require_auth)])
    async def ai_analyze_now() -> dict:
        """Un'analisi forzata, diagnostica: rilevamenti + metriche del frame."""
        if not ctx.ai or not ctx.ai.enabled:
            return {"enabled": False}
        return await ctx.ai.analyze_once()

    @app.get("/photo", dependencies=[Depends(require_auth)])
    async def photo(roi: int = 0) -> Response:
        try:
            jpeg = await ctx.webcam.get_jpeg(max_age_s=5.0, wait_fresh=4.0)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f"webcam non disponibile: {e}") from e
        if roi and ctx.ai:
            import cv2 as _cv2
            img = _cv2.imdecode(np.frombuffer(jpeg, np.uint8), _cv2.IMREAD_COLOR)
            h, w = img.shape[:2]
            rx, ry, rw, rh = ctx.ai.roi
            p1 = (int(w * rx), int(h * ry))
            p2 = (int(w * (rx + rw)), int(h * (ry + rh)))
            _cv2.rectangle(img, p1, p2, (0, 0, 255), 2)
            ok, buf = _cv2.imencode(".jpg", img, [_cv2.IMWRITE_JPEG_QUALITY, 90])
            jpeg = buf.tobytes()
        return Response(content=jpeg, media_type="image/jpeg")

    @app.get("/video", dependencies=[Depends(require_auth)])
    async def video() -> StreamingResponse:
        """Proxy MJPEG: una sola connessione verso la stampante, N viewer."""
        queue = ctx.webcam.subscribe(queue_size=4)

        async def gen():
            try:
                while True:
                    # Nessun keepalive "vuoto": il client MJPEG di HA si
                    # inceppa sui frame a lunghezza zero. Se lo stream tarda,
                    # si chiude la connessione e il cliente si riconnette.
                    jpeg = await asyncio.wait_for(queue.get(), timeout=45)
                    header = (f"--foo\r\nContent-Type: image/jpeg\r\n"
                              f"Content-Length: {len(jpeg)}\r\n\r\n").encode()
                    yield header + jpeg + b"\r\n"
            except (asyncio.TimeoutError, TimeoutError):
                pass
            finally:
                ctx.webcam.unsubscribe(queue)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=--foo")

    @app.get("/sw.js")
    async def sw() -> FileResponse:
        swp = Path(__file__).resolve().parents[2] / "dashboard" / "sw.js"
        if not swp.is_file():
            raise HTTPException(404, "sw.js non trovato")
        return FileResponse(swp, media_type="application/javascript")

    @app.get("/", dependencies=[Depends(require_auth)])
    async def dashboard() -> FileResponse:
        if not DASHBOARD_FILE.is_file():
            raise HTTPException(404, "dashboard non trovata")
        return FileResponse(DASHBOARD_FILE)

    # ------------------------------------------------------------------ #
    # Comandi
    # ------------------------------------------------------------------ #
    async def _run_cmd(kind: str, action) -> dict:
        try:
            await action()
            ctx.bus.publish("remote_command", {"command": kind, "source": "rest"})
            return {"ok": True, "command": kind}
        except PrinterCommandError as e:
            return {"ok": False, "command": kind, "error": str(e)}
        except (ConnectionError, asyncio.TimeoutError, TimeoutError) as e:
            raise HTTPException(503, f"stampante non raggiungibile: {e}") from e

    @app.post("/cmd/stop", dependencies=[Depends(require_auth)])
    async def cmd_stop() -> dict:
        return await _run_cmd("stop", ctx.printer_api.stop_print)

    @app.post("/cmd/pause", dependencies=[Depends(require_auth)])
    async def cmd_pause() -> dict:
        return await _run_cmd("pause", ctx.printer_api.pause_print)

    @app.post("/cmd/resume", dependencies=[Depends(require_auth)])
    async def cmd_resume() -> dict:
        return await _run_cmd("resume", ctx.printer_api.resume_print)

    @app.post("/cmd/speed", dependencies=[Depends(require_auth)])
    async def cmd_speed(body: dict) -> dict:
        """Velocità di stampa: {"percent": 80} (50-150)."""
        try:
            pct = int(float(body.get("percent", 100)))
            if not 50 <= pct <= 150:
                raise ValueError("percent deve essere 50-150")
            await ctx.printer_api.set_print_speed(pct)
            ctx.bus.publish("remote_command", {"command": "speed", "value": pct})
            return {"ok": True, "command": "speed", "percent": pct}
        except (ConnectionError, asyncio.TimeoutError, TimeoutError) as e:
            raise HTTPException(503, f"stampante non raggiungibile: {e}") from e

    @app.post("/cmd/light", dependencies=[Depends(require_auth)])
    async def cmd_light(body: dict) -> dict:
        """Luce interna: {"on": true|false}."""
        try:
            on = bool(body.get("on", False))
            await ctx.printer_api.set_light(on)
            ctx.bus.publish("remote_command", {"command": "light", "value": on})
            return {"ok": True, "command": "light", "on": on}
        except PrinterCommandError as e:
            raise HTTPException(502, str(e)) from e
        except (ConnectionError, asyncio.TimeoutError, TimeoutError) as e:
            raise HTTPException(503, f"stampante non raggiungibile: {e}") from e

    @app.post("/print", dependencies=[Depends(require_auth)])
    async def start_print(body: dict) -> dict:
        filename = str(body.get("filename") or "").strip()
        if not filename:
            raise HTTPException(400, "campo 'filename' obbligatorio")
        try:
            ack = await ctx.printer_api.start_print(filename)
            if ack != 0 and not filename.startswith("/"):
                # secondo tentativo con il prefisso dello storage interno
                ack2 = await ctx.printer_api.start_print(f"/local/{filename}")
                if ack2 == 0:
                    filename = f"/local/{filename}"
                    ack = 0
            ok = ack == 0
            ctx.bus.publish("remote_command", {"command": "print", "filename": filename})
            return {"ok": ok, "command": "print", "filename": filename, "ack": ack}
        except (ConnectionError, asyncio.TimeoutError, TimeoutError) as e:
            raise HTTPException(503, f"stampante non raggiungibile: {e}") from e

    # ------------------------------------------------------------------ #
    # Upload / file
    # ------------------------------------------------------------------ #
    @app.post("/upload", dependencies=[Depends(require_auth)])
    async def upload_gcode(file: UploadFile, transfer: bool = True) -> dict:
        max_size = int(cfg.upload.get("max_size_mb", 500)) * 1024 * 1024
        data = bytearray()
        while chunk := await file.read(1024 * 1024):
            data.extend(chunk)
            if len(data) > max_size:
                raise HTTPException(413, "file troppo grande")
        if not data:
            raise HTTPException(400, "file vuoto")
        name = file.filename or "upload.gcode"
        transfer_flag = str(transfer).lower() not in ("0", "false", "no")
        try:
            result = await ctx.uploader.upload(name, bytes(data), transfer=transfer_flag)
        except UploadError as e:
            raise HTTPException(502, str(e)) from e
        return result

    @app.get("/files", dependencies=[Depends(require_auth)])
    async def files() -> dict:
        out: dict[str, Any] = {"local": ctx.uploader.list_local()}
        try:
            fl = await ctx.printer_api.file_list()
            out["printer"] = fl.get("FileList", [])
        except Exception as e:  # noqa: BLE001
            out["printer"] = None
            out["printer_error"] = str(e)
        return out

    # ------------------------------------------------------------------ #
    # SSE per la dashboard
    # ------------------------------------------------------------------ #
    @app.get("/api/events", dependencies=[Depends(require_auth)])
    async def sse() -> StreamingResponse:
        sub = ctx.bus.subscribe("state_changed", "print_*", "printer_error", "ai_alert",
                                "remote_command", "printer_connected", "printer_disconnected")

        async def gen():
            try:
                first = {"type": "state_changed", "data": ctx.state.snapshot(),
                         "ts": time.time()}
                yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(sub.queue.get(), timeout=SSE_TIMEOUT)
                    except (asyncio.TimeoutError, TimeoutError):
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            finally:
                sub.close()

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ #
    # Notifica di prova
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # Modelli 3D & slicing on-the-go
    # ------------------------------------------------------------------ #
    @app.get("/models", dependencies=[Depends(require_auth)])
    async def models_list() -> dict:
        return {"models": ctx.models.list()}

    @app.post("/models", dependencies=[Depends(require_auth)])
    async def models_upload(file: UploadFile) -> dict:
        max_size = int(cfg.models.get("max_size_mb", 200)) * 1024 * 1024
        data = bytearray()
        while chunk := await file.read(1024 * 1024):
            data.extend(chunk)
            if len(data) > max_size:
                raise HTTPException(413, "modello troppo grande")
        try:
            path = await asyncio.to_thread(ctx.models.save,
                                           file.filename or "model.stl",
                                           bytes(data))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "name": path.name, "size": path.stat().st_size}

    @app.get("/models/{name}", dependencies=[Depends(require_auth)])
    async def models_get(name: str):
        p = ctx.models.path(name)
        if p is None:
            raise HTTPException(404, "modello non trovato")
        media = {"stl": "model/stl", "3mf": "model/3mf",
                 "obj": "model/obj"}.get(p.suffix.lstrip(".").lower(), "application/octet-stream")
        return FileResponse(p, media_type=media, filename=p.name)

    @app.delete("/models/{name}", dependencies=[Depends(require_auth)])
    async def models_delete(name: str) -> dict:
        return {"ok": ctx.models.delete(name)}

    @app.post("/models/{name}/slice", dependencies=[Depends(require_auth)])
    async def models_slice(name: str, body: dict) -> dict:
        if not await ctx.slicer.check_available():
            raise HTTPException(501, "slicer non installato "
                                     f"({ctx.slicer.binary}): vedi README § Slicing")
        if ctx.models.path(name) is None:
            raise HTTPException(404, "modello non trovato")
        infill = body.get("infill")
        try:
            job = ctx.slicer.create_job(name,
                                        profile=body.get("profile", "standard"),
                                        material=body.get("material", "pla"),
                                        infill=int(infill) if infill is not None else None,
                                        supports=bool(body.get("supports", False)),
                                        transfer=bool(body.get("transfer", True)))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "job": job.public()}

    @app.get("/slice/profiles", dependencies=[Depends(require_auth)])
    async def slice_profiles() -> dict:
        from ..models import profiles_public, MATERIALS
        return {"profiles": profiles_public(), "materials": sorted(MATERIALS)}

    @app.get("/slice/jobs", dependencies=[Depends(require_auth)])
    async def slice_jobs() -> dict:
        return {"jobs": ctx.slicer.list_jobs(), "slicer_available": await ctx.slicer.check_available()}

    @app.get("/slice/jobs/{job_id}", dependencies=[Depends(require_auth)])
    async def slice_job(job_id: str) -> dict:
        job = ctx.slicer.job(job_id)
        if job is None:
            raise HTTPException(404, "job non trovato")
        return job.public()

    @app.get("/snapshots", dependencies=[Depends(require_auth)])
    async def snapshots_list() -> dict:
        """Elenco degli snapshot salvati (foto delle notifiche e degli alert AI)."""
        import os
        d = ctx.webcam.snapshot_dir
        out = []
        if d.is_dir():
            for f in sorted(d.glob("*.jpg"), key=lambda p: -p.stat().st_mtime)[:60]:
                st = f.stat()
                out.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
        return {"snapshots": out}

    @app.get("/snapshots/{name}", dependencies=[Depends(require_auth)])
    async def snapshots_get(name: str):
        """Serve uno snapshot (nome sanificato: nessun path traversal)."""
        safe = Path(name).name
        p = ctx.webcam.snapshot_dir / safe
        if not p.is_file() or p.suffix.lower() != ".jpg":
            raise HTTPException(404, "snapshot non trovato")
        return FileResponse(p, media_type="image/jpeg", filename=safe)

    @app.get("/history", dependencies=[Depends(require_auth)])
    async def history(limit: int = 100) -> dict:
        return {"prints": store.get_history(limit)}

    @app.get("/stats", dependencies=[Depends(require_auth)])
    async def stats() -> dict:
        return store.get_stats()

    @app.get("/filament", dependencies=[Depends(require_auth)])
    async def filament() -> dict:
        return store.get_filament()

    @app.post("/filament/spool", dependencies=[Depends(require_auth)])
    async def filament_add(body: dict) -> dict:
        try:
            spool = store.add_spool(
                material=str(body.get("material", "pla")).lower(),
                color=str(body.get("color", "#ccc")),
                weight_g=float(body.get("weight_g", 1000)),
                name=str(body.get("name", "")))
            return {"ok": True, "spool": spool}
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e)) from e

    @app.delete("/filament/spool/{spool_id}", dependencies=[Depends(require_auth)])
    async def filament_del(spool_id: int) -> dict:
        return store.remove_spool(spool_id)

    @app.post("/filament/active", dependencies=[Depends(require_auth)])
    async def filament_set(body: dict) -> dict:
        try:
            return store.set_active_spool(body.get("id"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/telegram/cmd", dependencies=[Depends(require_auth)])
    async def telegram_cmd(body: dict) -> dict:
        """Inoltro da Home Assistant di un comando Telegram (il polling sta su HA).
        L'evento telegram_command NON ha 'text': ha 'command' + 'args'."""
        text = str(body.get("text") or "").strip()
        if not text and body.get("command"):
            text = str(body["command"]).strip()
            args = body.get("args") or []
            if isinstance(args, list) and args:
                text += " " + " ".join(str(a) for a in args)
        asyncio.create_task(
            ctx.telegram_commands.handle(text, body.get("chat_id")))
        return {"ok": True, "queued": True}

    @app.post("/telegram/file", dependencies=[Depends(require_auth)])
    async def telegram_file(body: dict) -> dict:
        """GCODE inviato in chat Telegram (inoltrato da HA): download + upload."""
        asyncio.create_task(
            ctx.telegram_commands.handle_file(str(body.get("file_id", "")),
                                              str(body.get("file_name", "")),
                                              body.get("chat_id")))
        return {"ok": True, "queued": True}

    @app.post("/notify/test", dependencies=[Depends(require_auth)])
    async def notify_test(body: dict) -> dict:
        message = str(body.get("message") or "🔔 Notifica di prova da elegoo-notify")
        with_photo = bool(body.get("photo", False))
        photo: Optional[bytes] = None
        if with_photo:
            try:
                photo = await ctx.webcam.get_jpeg(max_age_s=5.0, wait_fresh=4.0)
            except Exception:  # noqa: BLE001
                photo = None
        await ctx.telegram.notify(message, photo=photo, kind="test")
        return {"ok": True, "queued": True, "with_photo": bool(photo)}

    return app
