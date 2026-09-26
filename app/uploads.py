"""Upload dei file GCODE: salvataggio locale + trasferimento alla stampante.

La stampante espone un'interfaccia HTTP:
  POST http://{ip}:{http_port}/uploadFile/upload
  multipart: S-File-MD5, Check=1, Offset=0, Uuid, TotalSize, File
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import aiohttp

log = logging.getLogger("elegoo.uploads")

ALLOWED_EXT = {".gcode", ".gco", ".g"}


class UploadError(Exception):
    pass


class Uploader:
    def __init__(self, cfg, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.session = session
        self.gcode_dir = Path(cfg.paths.get("gcodes", "data/gcodes"))
        self.max_size = int(cfg.upload.get("max_size_mb", 500)) * 1024 * 1024
        base = f"http://{cfg.printer['ip']}:{int(cfg.printer.get('http_port', 3030))}"
        self.upload_url = f"{base}/uploadFile/upload"
        self.moonraker = None   # MoonrakerApi iniettabile (driver moonraker)

    # ------------------------------------------------------------------ #
    def _sanitize(self, filename: str) -> str:
        name = Path(filename.replace("\\", "/")).name
        # sostituisci spazi e caratteri speciali con underscore (evita %20 sul printer)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip() or "print.gcode"
        name = re.sub(r"_+", "_", name)
        if not any(name.lower().endswith(ext) for ext in ALLOWED_EXT):
            name += ".gcode"
        return name

    async def save_local(self, filename: str, data: bytes) -> Path:
        """Salva il GCODE nel volume locale (validando nome e dimensione)."""
        if len(data) > self.max_size:
            raise UploadError(f"file troppo grande ({len(data) // (1024 * 1024)} MB, "
                              f"max {self.max_size // (1024 * 1024)} MB)")
        name = self._sanitize(filename)
        self.gcode_dir.mkdir(parents=True, exist_ok=True)
        path = self.gcode_dir / name
        path.write_bytes(data)
        log.info("GCODE salvato localmente: %s (%d B)", path, len(data))
        return path

    def list_local(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.gcode_dir.is_dir():
            for f in sorted(self.gcode_dir.glob("*.g*")):
                st = f.stat()
                out.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
        return out

    def md5_of_file(self, path: Path) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    async def transfer_to_printer(self, path: Path, retries: int = 2) -> dict[str, Any]:
        """Trasferisce il file alla stampante via HTTP multipart.

        Su Moonraker (Klipper/COSMOS) delega a /server/files/upload;
        su SDCP: invio a pacchetti da 1 MB con Offset crescente, Uuid
        identico per tutti i pacchetti, MD5 dell'INTERO file e TotalSize
        globale. (Un POST unico > ~1MB viene rifiutato con connection
        reset: verificato sul campo.)
        Ritorna {"success": bool, "code": str, "messages": ...}.
        """
        if self.moonraker is not None:
            await self.moonraker.upload(path.name, path.read_bytes())
            return {"success": True, "code": "000000", "messages": None}
        data = path.read_bytes()
        file_md5 = self.md5_of_file(path)
        CHUNK = 1024 * 1024
        last_err: Optional[str] = None
        for attempt in range(retries + 1):
            up_uuid = uuid.uuid4().hex
            try:
                off = 0
                while off < len(data):
                    piece = data[off:off + CHUNK]
                    form = aiohttp.FormData()
                    form.add_field("TotalSize", str(len(data)))
                    form.add_field("Uuid", up_uuid)
                    form.add_field("Offset", str(off))
                    form.add_field("Check", "1")
                    form.add_field("S-File-MD5", file_md5)
                    form.add_field("File", piece, filename=path.name,
                                   content_type="application/octet-stream")
                    timeout = aiohttp.ClientTimeout(
                        total=max(60, len(piece) // (128 * 1024) + 30))
                    async with self.session.post(self.upload_url, data=form,
                                                 timeout=timeout) as resp:
                        body: dict[str, Any] = {}
                        try:
                            body = await resp.json(content_type=None)
                        except Exception:  # noqa: BLE001
                            body = {"code": str(resp.status),
                                    "messages": "risposta non JSON"}
                        if (resp.status != 200
                                or body.get("code") != "000000"):
                            raise UploadError(
                                f"chunk@{off // 1024}KB HTTP {resp.status} "
                                f"code={body.get('code')} msg={body.get('messages')}")
                    off += CHUNK
                log.info("Upload su stampante riuscito: %s (%d KB in %d pacchetti)",
                         path.name, len(data) // 1024, (len(data) + CHUNK - 1) // CHUNK)
                return {"success": True, "code": "000000", "messages": None}
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
            if attempt < retries:
                log.warning("Upload fallito (%s), retry %d/%d", last_err,
                             attempt + 1, retries)
                await asyncio.sleep(1.5 * (attempt + 1))
        raise UploadError(f"upload verso la stampante fallito: {last_err}")

    async def upload(self, filename: str, data: bytes, transfer: bool = True) -> dict[str, Any]:
        """Pipeline completa: salva localmente e (opzionale) invia alla stampante."""
        path = await self.save_local(filename, data)
        result: dict[str, Any] = {
            "local_path": str(path),
            "filename": path.name,
            "size": len(data),
            "md5": self.md5_of_file(path),
            "transferred": False,
        }
        if transfer:
            t0 = time.time()
            await self.transfer_to_printer(path)
            result["transferred"] = True
            result["transfer_seconds"] = round(time.time() - t0, 1)
        return result
