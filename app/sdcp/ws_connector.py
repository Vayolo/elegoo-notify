"""Connessione WebSocket alla stampante con failover, heartbeat e reconnect.

Prova in ordine gli URL in `printer.ws_urls` (il primo valido è
ws://IP:3030/websocket). Apprende il MainboardID dai messaggi in arrivo.
Pubblica sul bus: printer_connected, printer_disconnected, sdcp_status,
sdcp_attributes, sdcp_error, sdcp_notice, sdcp_response.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from typing import Any, Optional

import websockets

from . import protocol

log = logging.getLogger("elegoo.ws")


async def udp_discover(timeout: float = 2.0) -> Optional[dict[str, Any]]:
    """Discovery SDCP via UDP broadcast 'M99999' (porta 3000)."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    class Protocol(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):  # type: ignore[override]
            if not fut.done():
                try:
                    fut.set_result(json.loads(data.decode()))
                except Exception:
                    pass

        def error_received(self, exc):  # type: ignore[override]
            if not fut.done():
                fut.set_exception(exc)

    transport, _ = await loop.create_datagram_endpoint(
        Protocol, remote_addr=("255.255.255.255", 3000))
    try:
        transport.sendto(b"M99999")
        try:
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return None
    finally:
        transport.close()


class WsConnector:
    def __init__(self, cfg, bus):
        self.cfg = cfg
        self.bus = bus
        self.ws: Any = None
        self.connected: bool = False
        self.current_url: Optional[str] = None
        self.mainboard_id: Optional[str] = cfg.printer.get("mainboard_id") or None
        self._pending: dict[str, asyncio.Future] = {}
        self._last_incoming: float = 0.0
        self._hb_task: Optional[asyncio.Task] = None
        self._closed: bool = False
        self._wake_reconnect: Optional[asyncio.Event] = None
        self.heartbeat_interval: float = 10.0
        self.silence_timeout: float = max(35.0, 3 * float(cfg.printer.get("status_poll_seconds", 10)))

    # ------------------------------------------------------------------ #
    # Ciclo di vita
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Loop principale: connette, legge, riconnette con backoff."""
        backoff = 1.0
        max_backoff = float(self.cfg.printer.get("max_reconnect_backoff_seconds", 30))
        urls: list[str] = list(self.cfg.printer.get("ws_urls") or [])
        self._wake_reconnect = asyncio.Event()

        while not self._closed:
            try:
                if not urls:
                    raise ConnectionError("Nessun ws_urls configurato")
                last_err: Optional[Exception] = None
                for url in urls:
                    try:
                        self.ws = await websockets.connect(
                            url, ping_interval=20, ping_timeout=20,
                            close_timeout=3, max_size=8 * 1024 * 1024)
                        self.current_url = url
                        self.connected = True
                        backoff = 1.0
                        log.info("Connesso alla stampante: %s", url)
                        self.bus.publish("printer_connected", {"url": url})
                        await self._reader_loop()
                        break  # uscita pulita (self._closed)
                    except Exception as e:  # noqa: BLE001
                        last_err = e
                        self.connected = False
                        log.debug("Connessione a %s fallita: %s", url, e)
                        if self._closed:
                            return
                if self._closed:
                    return
                # Tutti gli URL hanno fallito o la connessione è caduta
                self.connected = False
                self.bus.publish("printer_disconnected", {"reason": str(last_err or "chiusura")})
                log.warning("Stampante disconnessa (%s): retry tra %.0fs",
                            last_err or " sconosciuto", backoff)
                self._wake_reconnect.clear()
                try:
                    await asyncio.wait_for(self._wake_reconnect.wait(), timeout=backoff)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
                backoff = min(backoff * 2, max_backoff)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("Errore inatteso nel loop di connessione")
                await asyncio.sleep(2)

    async def _reader_loop(self) -> None:
        """Legge i messaggi finché la connessione resta aperta."""
        self._last_incoming = time.monotonic()
        self._hb_task = asyncio.create_task(self._heartbeat_loop())
        try:
            async for message in self.ws:
                self._last_incoming = time.monotonic()
                await self._handle_message(message)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._cancel_heartbeat()
            if not self._closed:
                self.connected = False

    async def _heartbeat_loop(self) -> None:
        """Invia 'ping' testuale e chiude la connessione se resta muta."""
        while not self._closed:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                if self.ws is not None:
                    await self.ws.send("ping")
            except Exception:  # noqa: BLE001
                log.debug("Invio heartbeat fallito")
                continue
            silence = time.monotonic() - self._last_incoming
            if silence > self.silence_timeout:
                log.warning("Nessun messaggio dalla stampante da %.0fs: riconnessione", silence)
                asyncio.create_task(self.force_reconnect())
                return

    def _cancel_heartbeat(self) -> None:
        if self._hb_task is not None:
            self._hb_task.cancel()
            self._hb_task = None

    async def force_reconnect(self) -> None:
        """Forza la chiusura per innescare il ciclo di riconnessione."""
        try:
            if self.ws is not None:
                await self.ws.close()
        except Exception:  # noqa: BLE001
            pass
        if self._wake_reconnect is not None:
            self._wake_reconnect.set()

    async def close(self) -> None:
        self._closed = True
        self._cancel_heartbeat()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("connettore chiuso"))
        self._pending.clear()
        await self.force_reconnect()
        try:
            if self.ws is not None:
                await self.ws.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # Messaggi
    # ------------------------------------------------------------------ #
    async def _handle_message(self, text: str) -> None:
        if text in ("pong", "ping"):
            return
        msg = protocol.parse_message(text)
        if msg is None:
            log.debug("Messaggio WS non JSON: %.80s", text)
            return

        mid = protocol.extract_mainboard_id(msg)
        if mid:
            self.mainboard_id = mid

        kind = protocol.topic_kind(msg)
        if kind == "response":
            data = msg.get("Data") or {}
            request_id = data.get("RequestID")
            fut = self._pending.pop(str(request_id), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            self.bus.publish("sdcp_response", {"msg": msg})
        elif kind == "status":
            payload = protocol.status_payload(msg)
            self.bus.publish("sdcp_status", {"payload": payload, "raw": msg})
        elif kind == "attributes":
            attrs = msg.get("Attributes") or (msg.get("Data") or {}).get("Attributes") or msg
            self.bus.publish("sdcp_attributes", {"attributes": attrs})
        elif kind == "error":
            self.bus.publish("sdcp_error", {"msg": msg})
        elif kind == "notice":
            self.bus.publish("sdcp_notice", {"msg": msg})
        else:
            log.debug("Messaggio SDCP non gestito (topic=%s)", msg.get("Topic"))
        self.bus.publish("sdcp_message", {"msg": msg, "kind": kind})

    # ------------------------------------------------------------------ #
    # Richieste
    # ------------------------------------------------------------------ #
    async def send_request(self, cmd: int, data: dict[str, Any] | None = None,
                           timeout: float | None = None) -> dict[str, Any]:
        """Invia una richiesta SDCP e attende la risposta correlata."""
        if not self.connected or self.ws is None:
            raise ConnectionError("stampante non connessa")
        if timeout is None:
            timeout = float(self.cfg.printer.get("request_timeout_seconds", 8))
        mid = self.mainboard_id or ""
        request_id, payload = protocol.build_request(cmd, data, mid)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        try:
            await self.ws.send(payload)
            resp = await asyncio.wait_for(fut, timeout=timeout)
            return resp
        finally:
            self._pending.pop(request_id, None)
