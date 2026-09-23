"""Webcam: singola connessione MJPEG verso la stampante con fan-out.

La Centauri Carbon permette UN solo stream video attivo
(MaximumVideoStreamAllowed=1): il modulo mantiene una sola connessione
e ridistribuisce i frame a più consumatori (AI, dashboard, snapshot).
In alternativa supporta la modalità "snapshot" (URL che restituisce JPEG).
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

import aiohttp

log = logging.getLogger("elegoo.webcam")

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


class WebcamError(Exception):
    pass


class Webcam:
    def __init__(self, cfg, session: aiohttp.ClientSession, printer_ip: str):
        self.cfg = cfg
        self.session = session
        self.ip = printer_ip
        self.mode: str = cfg.webcam.get("mode", "mjpeg")
        self.mjpeg_url: str = cfg.webcam["mjpeg_url"]
        self.snapshot_url: str = cfg.webcam["snapshot_url"]
        self.timeout_s: float = float(cfg.webcam.get("timeout_seconds", 10))
        self.retries: int = int(cfg.webcam.get("retries", 2))
        self.persistent: bool = bool(cfg.webcam.get("persistent_stream", True))
        self.save_snapshots: bool = bool(cfg.webcam.get("save_snapshots", True))
        self.retention: int = int(cfg.webcam.get("snapshot_retention", 300))
        self.snapshot_dir = Path(cfg.paths.get("snapshots", "data/snapshots"))

        self.latest_jpeg: Optional[bytes] = None
        self.latest_ts: float = 0.0
        self.stream_running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._subs: list[asyncio.Queue] = []
        self._on_stream_failure = None  # callback async opzionale (es. enable_video)
        self._backoff: float = 2.0

    # ------------------------------------------------------------------ #
    # Avvio / arresto
    # ------------------------------------------------------------------ #
    def set_stream_failure_callback(self, cb) -> None:
        self._on_stream_failure = cb

    async def start(self) -> None:
        if self.mode == "mjpeg" and self.persistent and self._task is None:
            self._task = asyncio.create_task(self._reader_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self.stream_running = False

    # ------------------------------------------------------------------ #
    # Reader MJPEG persistente
    # ------------------------------------------------------------------ #
    async def _reader_loop(self) -> None:
        max_backoff = 30.0
        while True:
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_read=self.timeout_s)
                async with self.session.get(self.mjpeg_url, timeout=timeout) as resp:
                    if resp.status != 200:
                        raise WebcamError(f"MJPEG HTTP {resp.status}")
                    self.stream_running = True
                    self._backoff = 2.0
                    log.info("Stream webcam connesso: %s", self.mjpeg_url)
                    await self._read_stream(resp)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("Stream webcam non disponibile: %s", e)
            finally:
                self.stream_running = False
            # stream caduto: chiedi alla stampante di attivarlo, poi riprova
            if self._on_stream_failure is not None:
                try:
                    await self._on_stream_failure()
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, max_backoff)

    async def _read_stream(self, resp) -> None:
        """Scansiona il multipart alla ricerca di JPEG (marcatori SOI/EOI)."""
        buf = bytearray()
        async for chunk in resp.content.iter_any():
            buf.extend(chunk)
            while True:
                soi = buf.find(JPEG_SOI)
                if soi < 0:
                    # conserva gli ultimi byte per non perdere un SOI spezzato
                    if len(buf) > 4:
                        del buf[:-4]
                    break
                eoi = buf.find(JPEG_EOI, soi + 2)
                if eoi < 0:
                    # frame incompleto: attendi altri chunk
                    if len(buf) > 8 * 1024 * 1024:
                        del buf[:soi + 2]
                    break
                jpeg = bytes(buf[soi:eoi + 2])
                del buf[:eoi + 2]
                self._publish_frame(jpeg)

    def _publish_frame(self, jpeg: bytes) -> None:
        self.latest_jpeg = jpeg
        self.latest_ts = time.time()
        for q in list(self._subs):
            try:
                q.put_nowait(jpeg)
            except asyncio.QueueFull:
                try:  # drop-oldest: il dashboard resta fluido
                    q.get_nowait()
                    q.put_nowait(jpeg)
                except Exception:  # noqa: BLE001
                    pass

    def subscribe(self, queue_size: int = 8) -> asyncio.Queue:
        """Coda di frame JPEG per i consumatori (es. proxy /video)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    # ------------------------------------------------------------------ #
    # Acquisizione frame
    # ------------------------------------------------------------------ #
    async def get_jpeg(self, max_age_s: float = 8.0, wait_fresh: float = 0.0) -> bytes:
        """Ultimo frame JPEG. In modalità snapshot fa una GET con retry."""
        if self.mode == "mjpeg":
            if self.latest_jpeg is not None:
                age = time.time() - self.latest_ts
                if age <= max_age_s:
                    return self.latest_jpeg
                if wait_fresh > 0 and self.stream_running:
                    # attende il prossimo frame (max wait_fresh secondi)
                    try:
                        return await asyncio.wait_for(self._wait_next(), timeout=wait_fresh)
                    except (asyncio.TimeoutError, TimeoutError):
                        return self.latest_jpeg  # meglio vecchio che niente
            if not self.persistent or not self.stream_running:
                # fetch singolo frame dallo stream
                jpeg = await self._fetch_single_mjpeg()
                if jpeg is not None:
                    return jpeg
            if self.latest_jpeg is not None:
                return self.latest_jpeg
            raise WebcamError("nessun frame disponibile dallo stream MJPEG")
        return await self._fetch_snapshot()

    async def _wait_next(self) -> bytes:
        q = self.subscribe(queue_size=1)
        try:
            return await q.get()
        finally:
            self.unsubscribe(q)

    async def _fetch_single_mjpeg(self) -> Optional[bytes]:
        """Apre lo stream, legge UN frame e chiude (occupa lo slot solo per un attimo)."""
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_s)
            async with self.session.get(self.mjpeg_url, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                buf = bytearray()
                while len(buf) < 12 * 1024 * 1024:
                    chunk = await resp.content.readany()
                    if not chunk:
                        return None
                    buf.extend(chunk)
                    soi = buf.find(JPEG_SOI)
                    if soi >= 0:
                        eoi = buf.find(JPEG_EOI, soi + 2)
                        if eoi >= 0:
                            return bytes(buf[soi:eoi + 2])
        except Exception:  # noqa: BLE001
            return None
        return None

    async def _fetch_snapshot(self) -> bytes:
        """GET dell'URL snapshot con timeout e retry."""
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=self.timeout_s)
                async with self.session.get(self.snapshot_url, timeout=timeout) as resp:
                    if resp.status != 200:
                        raise WebcamError(f"snapshot HTTP {resp.status}")
                    data = await resp.read()
                    if len(data) < 4 or JPEG_SOI not in data[:2]:
                        raise WebcamError("risposta snapshot non JPEG")
                    self.latest_jpeg = data
                    self.latest_ts = time.time()
                    return data
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < self.retries:
                    await asyncio.sleep(0.7 * (attempt + 1))
        raise WebcamError(f"snapshot non disponibile: {last_err}")

    # ------------------------------------------------------------------ #
    # Salvataggio snapshot (persistenza)
    # ------------------------------------------------------------------ #
    def save_snapshot(self, jpeg: bytes, prefix: str = "snap") -> Optional[str]:
        if not self.save_snapshots:
            return None
        try:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            name = time.strftime(f"%Y%m%d_%H%M%S_{prefix}.jpg")
            path = self.snapshot_dir / name
            path.write_bytes(jpeg)
            self._prune()
            return str(path)
        except OSError as e:
            log.warning("Salvataggio snapshot fallito: %s", e)
            return None

    def _prune(self) -> None:
        try:
            files = sorted(self.snapshot_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
            for f in files[:-self.retention] if len(files) > self.retention else []:
                f.unlink(missing_ok=True)
        except OSError:
            pass
