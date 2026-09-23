"""Client Telegram: coda unica, debounce, gestione rate-limit (429), dry-run.

- I messaggi critici (errori, alert AI) bypassano il debounce
  `min_seconds_between_msgs` ma restano serializzati (1 invio alla volta).
- Foto: `sendPhoto` (anteprima in chat) oppure `sendDocument`
  (qualità originale, lossless) secondo `telegram.photo_mode`.
- `TELEGRAM_DRYRUN=1`: nessuna chiamata di rete, tutto registrato in
  data/logs/telegram_dryrun.jsonl (usato dai test di accettazione).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import aiohttp

log = logging.getLogger("elegoo.telegram")


@dataclass
class NotifyJob:
    text: str
    photo: Optional[bytes] = None
    caption: Optional[str] = None
    critical: bool = False
    silent: bool = False
    kind: str = "message"  # etichetta per log/test: start/progress/complete/...


class TelegramNotifier:
    def __init__(self, cfg, session: aiohttp.ClientSession):
        tcfg = cfg.telegram
        self.token: Optional[str] = tcfg.get("token")
        self.chat_id: Optional[str] = tcfg.get("chat_id")
        self.api_base: str = tcfg.get("api_base", "https://api.telegram.org")
        self.photo_mode: str = tcfg.get("photo_mode", "photo")
        self.min_gap: float = float(tcfg.get("min_seconds_between_msgs", 45))
        self.dry_run: bool = bool(tcfg.get("dry_run", False))
        self.dry_run_path = Path(cfg.paths.get("logs", "data/logs")) / "telegram_dryrun.jsonl"
        self.session = session
        # coda con PRIORITÀ: i job critici (comandi interattivi, errori,
        # alert) saltano il debounce e non aspettano mai i non-critici
        self._jobs: deque[NotifyJob] = deque()
        self._jobs_event = asyncio.Event()
        self.sent_log: list[dict[str, Any]] = []  # per i test
        self._worker: Optional[asyncio.Task] = None
        self._last_normal_send: float = 0.0

    # ------------------------------------------------------------------ #
    @property
    def configured(self) -> bool:
        return self.dry_run or bool(self.token and self.chat_id)

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._worker = None

    # ------------------------------------------------------------------ #
    # API pubblica
    # ------------------------------------------------------------------ #
    async def notify(self, text: str, *, photo: bytes | None = None,
                     critical: bool = False, silent: bool = False,
                     kind: str = "message") -> None:
        """Accoda una notifica. `photo` (JPEG bytes) opzionale: se presente
        viene inviata come foto con `text` come caption."""
        job = NotifyJob(text=text, photo=photo, critical=critical, silent=silent, kind=kind)
        if not self.configured:
            log.debug("Telegram non configurato, notifica scartata: %s", text[:60])
            return
        self._jobs.append(job)
        self._jobs_event.set()

    # ------------------------------------------------------------------ #
    # Worker
    # ------------------------------------------------------------------ #
    async def _run(self) -> None:
        while True:
            try:
                if not self._jobs:
                    self._jobs_event.clear()
                    await self._jobs_event.wait()
                    continue
                # 1) i CRITICI passano sempre per primi
                job = None
                for i, j in enumerate(self._jobs):
                    if j.critical:
                        job = self._jobs[i]
                        del self._jobs[i]
                        break
                if job is None:
                    job = self._jobs.popleft()
                # 2) debounce SOLO per i non-critici, a fette da 0.5 s:
                #    un critico in coda viene preso dopo al mezzo secondo
                if not job.critical:
                    wait = self._last_normal_send + self.min_gap - time.monotonic()
                    if wait > 0:
                        self._jobs.appendleft(job)
                        await asyncio.sleep(min(wait, 0.5))
                        continue
                await self._send_with_retry(job)
                if not job.critical:
                    self._last_normal_send = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Invio notifica Telegram fallito")

    async def _send_with_retry(self, job: NotifyJob, max_retries: int = 3) -> None:
        for attempt in range(max_retries):
            try:
                await self._send(job)
                return
            except _RateLimited as e:
                wait = e.retry_after or 5
                log.warning("Rate limit Telegram: attesa %ss", wait)
                await asyncio.sleep(wait)
            except aiohttp.ClientError as e:
                log.warning("Errore di rete Telegram (%s), retry %d/%d", e, attempt + 1, max_retries)
                await asyncio.sleep(2 * (attempt + 1))
        log.error("Notifica scartata dopo %d tentativi: %s", max_retries, job.text[:80])

    # ------------------------------------------------------------------ #
    # Invio effettivo
    # ------------------------------------------------------------------ #
    async def _send(self, job: NotifyJob) -> None:
        if self.dry_run:
            self._record_dry_run(job)
            log.info("[DRY-RUN] %s: %s", job.kind, (job.caption or job.text).replace("\n", " ")[:100])
            return
        if not self.token or not self.chat_id:
            raise RuntimeError("token/chat_id Telegram mancanti")
        url = f"{self.api_base}/bot{self.token}"
        if job.photo:
            method = "sendPhoto" if self.photo_mode == "photo" else "sendDocument"
            caption = (job.caption or job.text)[:1024]
            form = aiohttp.FormData()
            form.add_field("chat_id", str(self.chat_id))
            form.add_field("caption", caption)
            form.add_field("parse_mode", "HTML")
            form.add_field("disable_notification", "true" if job.silent else "false")
            form.add_field(method.replace("send", "").lower(), job.photo,
                           filename="snapshot.jpg", content_type="image/jpeg")
            try:
                await self._api_call(f"{url}/{method}", data=form, expect_json=True)
            except aiohttp.ClientError as e:
                if "can't parse entities" in str(e):
                    # HTML malformato (es. '<' nel testo): retry in plain
                    form = aiohttp.FormData()
                    form.add_field("chat_id", str(self.chat_id))
                    form.add_field("caption", _strip_html(caption))
                    form.add_field("disable_notification", "true" if job.silent else "false")
                    form.add_field(method.replace("send", "").lower(), job.photo,
                                   filename="snapshot.jpg", content_type="image/jpeg")
                    await self._api_call(f"{url}/{method}", data=form, expect_json=True)
                else:
                    raise
        else:
            payload = {"chat_id": self.chat_id, "text": job.text,
                       "parse_mode": "HTML",
                       "disable_notification": bool(job.silent)}
            try:
                await self._api_call(f"{url}/sendMessage", json=payload, expect_json=True)
            except aiohttp.ClientError as e:
                if "can't parse entities" in str(e):
                    payload["text"] = _strip_html(job.text)
                    del payload["parse_mode"]
                    await self._api_call(f"{url}/sendMessage", json=payload, expect_json=True)
                else:
                    raise
        log.info("Telegram %s inviata: %s", "foto" if job.photo else "notifica", job.kind)

    async def get_file(self, file_id: str) -> bytes:
        """Scarica un file Telegram via getFile (indipendente dal polling,
        che appartiene a Home Assistant: nessun conflitto)."""
        if self.dry_run or not self.token:
            raise RuntimeError("getFile non disponibile (dry-run/token assente)")
        url = f"{self.api_base}/bot{self.token}"
        timeout = aiohttp.ClientTimeout(total=120)
        async with self.session.get(f"{url}/getFile",
                                    params={"file_id": file_id}, timeout=timeout) as r:
            body = await r.json(content_type=None)
            if r.status != 200 or not body.get("ok"):
                raise RuntimeError(f"getFile: {body.get('description', r.status)}")
            path = body["result"]["file_path"]
        file_url = f"{self.api_base}/file/bot{self.token}/{path}"
        async with self.session.get(file_url, timeout=aiohttp.ClientTimeout(total=300)) as r:
            if r.status != 200:
                raise RuntimeError(f"download file: HTTP {r.status}")
            return await r.read()

    async def _api_call(self, url: str, *, data=None, json=None, expect_json=False) -> dict:
        timeout = aiohttp.ClientTimeout(total=30)
        async with self.session.post(url, data=data, json=json, timeout=timeout) as resp:
            body: dict[str, Any] = {}
            try:
                body = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                body = {"ok": resp.status == 200}
            if resp.status == 429:
                retry = body.get("parameters", {}).get("retry_after", 5)
                raise _RateLimited(retry)
            if resp.status != 200 or not body.get("ok", False):
                desc = body.get("description", f"HTTP {resp.status}")
                raise aiohttp.ClientError(f"Telegram: {desc}")
            return body

    def _record_dry_run(self, job: NotifyJob) -> None:
        record = {
            "ts": time.time(), "kind": job.kind, "critical": job.critical,
            "silent": job.silent, "has_photo": bool(job.photo),
            "photo_bytes": len(job.photo) if job.photo else 0,
            "text": job.text, "caption": job.caption,
        }
        self.sent_log.append(record)
        try:
            self.dry_run_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.dry_run_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


def _strip_html(text: str) -> str:
    """Rimuove i tag HTML (fallback quando Telegram rifiuta il parse)."""
    import re
    return re.sub(r"</?[a-z][a-z0-9]*[^>]*>", "", text)


class _RateLimited(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = int(retry_after)
        super().__init__(f"429: retry dopo {retry_after}s")
