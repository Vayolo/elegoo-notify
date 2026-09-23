"""Bridge MQTT per Home Assistant.

- Pubblica stato/temperatura/progresso su `{base}/...` (retained)
- Pubblica i payload di MQTT discovery (comparsi automaticamente in HA)
- Sottoscrive `{base}/cmd/#` per eseguire stop/pause/resume da HA
- Availability topic con Last Will ("offline") per tutte le entità
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiomqtt

log = logging.getLogger("elegoo.mqtt")

DEVICE = {
    "identifiers": ["elegoo_notify"],
    "name": "Elegoo Centauri Carbon",
    "manufacturer": "Elegoo",
    "model": "Centauri Carbon",
}


class HaBridge:
    def __init__(self, cfg, bus, state, printer_api, ai_monitor=None):
        self.ai_monitor = ai_monitor
        m = cfg.mqtt
        self.host = m.get("host")
        self.port = int(m.get("port", 1883))
        self.username = m.get("username") or None
        self.password = m.get("password") or None
        self.base: str = m.get("base_topic", "elegoo_notify")
        self.discovery_prefix: str = m.get("discovery_prefix", "homeassistant")
        self.discovery_enabled: bool = bool(m.get("discovery_enabled", True))
        self.bus = bus
        self.state = state
        self.printer_api = printer_api
        self._task: asyncio.Task | None = None
        self.client: aiomqtt.Client | None = None

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        backoff = 3.0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.host, port=self.port,
                    username=self.username, password=self.password,
                    identifier=f"elegoo-notify",
                    will=aiomqtt.Will(topic=f"{self.base}/status",
                                      payload=b"offline", retain=True),
                ) as client:
                    self.client = client
                    backoff = 3.0
                    log.info("MQTT connesso a %s:%d", self.host, self.port)
                    await client.publish(f"{self.base}/status", b"online", retain=True)
                    await client.subscribe(f"{self.base}/cmd/#")
                    await client.subscribe(f"{self.base}/set/#")  # speed + light
                    if self.discovery_enabled:
                        await self._publish_discovery(client)
                    await self._publish_full_state(client)

                    incoming = asyncio.create_task(self._handle_incoming(client))
                    bus_events = asyncio.create_task(self._handle_bus(client))
                    done, pending = await asyncio.wait(
                        [incoming, bus_events], return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    for t in done:
                        if t.exception() and not isinstance(t.exception(), asyncio.CancelledError):
                            raise t.exception()  # noqa: TRY201
            except asyncio.CancelledError:
                raise
            except aiomqtt.MqttError as e:
                log.warning("MQQT non disponibile (%s): retry tra %.0fs", e, backoff)
            except Exception:  # noqa: BLE001
                log.exception("Errore nel bridge MQTT")
            finally:
                self.client = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    # ------------------------------------------------------------------ #
    # Discovery (payload compatibili Home Assistant)
    # ------------------------------------------------------------------ #
    def _discovery_payloads(self) -> list[tuple[str, dict[str, Any]]]:
        base, prefix = self.base, self.discovery_prefix
        avail = f"{base}/status"

        def sensor(obj_id: str, name: str, state_topic: str, *,
                   unit: str | None = None, device_class: str | None = None,
                   icon: str | None = None) -> tuple[str, dict[str, Any]]:
            p: dict[str, Any] = {
                "name": name, "unique_id": f"elegoo_notify_{obj_id}",
                "state_topic": state_topic, "availability_topic": avail,
                "device": DEVICE,
            }
            if unit:
                p["unit_of_measurement"] = unit
            if device_class:
                p["device_class"] = device_class
            if icon:
                p["icon"] = icon
            return f"{prefix}/sensor/elegoo_notify/{obj_id}/config", p

        def binary_sensor(obj_id: str, name: str, state_topic: str, *,
                          device_class: str | None = None,
                          icon: str | None = None) -> tuple[str, dict[str, Any]]:
            p: dict[str, Any] = {
                "name": name, "unique_id": f"elegoo_notify_{obj_id}",
                "state_topic": state_topic, "availability_topic": avail,
                "payload_on": "ON", "payload_off": "OFF", "device": DEVICE,
            }
            if device_class:
                p["device_class"] = device_class
            if icon:
                p["icon"] = icon
            return f"{prefix}/binary_sensor/elegoo_notify/{obj_id}/config", p

        def button(obj_id: str, name: str, command_topic: str,
                   payload: str) -> tuple[str, dict[str, Any]]:
            return f"{prefix}/button/elegoo_notify/{obj_id}/config", {
                "name": name, "unique_id": f"elegoo_notify_{obj_id}",
                "command_topic": command_topic, "payload_press": payload,
                "availability_topic": avail, "device": DEVICE,
            }

        payloads: list[tuple[str, dict[str, Any]]] = [
            sensor("progress", "Avanzamento stampa", f"{base}/progress", unit="%", icon="mdi:progress-clock"),
            sensor("remaining", "Tempo rimanente", f"{base}/remaining", unit="min", icon="mdi:timer-sand"),
            sensor("nozzle_temp", "Temperatura ugello", f"{base}/temp/nozzle", unit="°C", device_class="temperature"),
            sensor("bed_temp", "Temperatura piatto", f"{base}/temp/bed", unit="°C", device_class="temperature"),
            sensor("chamber_temp", "Temperatura camera", f"{base}/temp/chamber", unit="°C", device_class="temperature"),
            sensor("state", "Stato stampante", f"{base}/state", icon="mdi:printer-3d"),
            sensor("filename", "File in stampa", f"{base}/filename", icon="mdi:file-document"),
            sensor("ai_risk", "Rischio AI", f"{base}/ai_risk", unit="%", icon="mdi:radar"),
            sensor("ai_alert", "Ultimo alert AI", f"{base}/ai_alert_last", icon="mdi:alert-outline"),
            sensor("elapsed", "Tempo trascorso", f"{base}/elapsed", unit="min", icon="mdi:timeline-clock"),
            sensor("speed", "Velocità stampa", f"{base}/speed", unit="%", icon="mdi:speedometer"),
            sensor("error", "Ultimo errore", f"{base}/error", icon="mdi:alert-circle"),
            binary_sensor("printing", "In stampa", f"{base}/printing", device_class="running"),
            button("stop", "Ferma stampa", f"{base}/cmd/stop", "stop"),
            button("pause", "Pausa stampa", f"{base}/cmd/pause", "pause"),
            button("resume", "Riprendi stampa", f"{base}/cmd/resume", "resume"),
        ]
        payloads.append((f"{prefix}/number/elegoo_notify/speed/config", {
            "name": "Velocità stampa", "unique_id": "elegoo_notify_speed",
            "state_topic": f"{base}/speed", "command_topic": f"{base}/set/speed",
            "min": 50, "max": 150, "step": 5,
            "availability_topic": avail, "device": DEVICE, "icon": "mdi:speedometer",
        }))
        payloads.append((f"{prefix}/light/elegoo_notify/light/config", {
            "name": "Luce interna", "unique_id": "elegoo_notify_light",
            "state_topic": f"{base}/light", "command_topic": f"{base}/set/light",
            "payload_on": "ON", "payload_off": "OFF",
            "availability_topic": avail, "device": DEVICE,
        }))
        return payloads

    async def _publish_discovery(self, client: aiomqtt.Client) -> None:
        for topic, payload in self._discovery_payloads():
            await client.publish(topic, json.dumps(payload), retain=True)
        log.info("MQTT discovery pubblicato (%d entità)", len(self._discovery_payloads()))

    # ------------------------------------------------------------------ #
    # Stato
    # ------------------------------------------------------------------ #
    def _state_messages(self) -> list[tuple[str, str]]:
        """(topic, payload). None → payload vuoto '' (stato unknown PULITO in HA:
        la stringa 'unknown' su sensori numerici genera errori di valore)."""
        s = self.state
        msgs: list[tuple[str, str]] = [
            (f"{self.base}/state", s.human_status()),
            (f"{self.base}/printing", "ON" if s.is_printing else "OFF"),
            (f"{self.base}/filename", s.filename or "none"),
        ]
        pct = s.percent
        msgs.append((f"{self.base}/progress", f"{pct:.1f}" if pct is not None else ""))
        rem = s.time_remaining_s
        msgs.append((f"{self.base}/remaining", str(rem // 60) if rem is not None else ""))
        snap = s.snapshot()
        el = snap.get("elapsed_s")
        msgs.append((f"{self.base}/elapsed", str(el // 60) if el else ""))
        msgs.append((f"{self.base}/speed", str(s.print_speed)))
        msgs.append((f"{self.base}/light", "ON" if s.light else ("OFF" if s.light is not None else "")))
        msgs.append((f"{self.base}/error", s.last_error or "none"))
        if self.ai_monitor is not None and self.ai_monitor.ml is not None:
            risk = float(self.ai_monitor.ml.last_result.get("score", 0.0)) * 100
            msgs.append((f"{self.base}/ai_risk", f"{risk:.1f}"))
        for key, topic in (("nozzle", "temp/nozzle"), ("hotbed", "temp/bed"), ("chamber", "temp/chamber")):
            v = s.temps.get(key)
            msgs.append((f"{self.base}/{topic}", f"{v:.1f}" if v is not None else ""))
        return msgs

    async def _publish_full_state(self, client: aiomqtt.Client) -> None:
        for topic, payload in self._state_messages():
            await client.publish(topic, payload, retain=True)
        await client.publish(f"{self.base}/json", json.dumps(self.state.snapshot()), retain=True)

    async def _handle_bus(self, client: aiomqtt.Client) -> None:
        """Eventi del bus → topic MQTT (con throttle)."""
        sub = self.bus.subscribe("state_changed", "print_*", "ai_alert",
                                 "printer_connected", "printer_disconnected")
        try:
            async for event in sub:
                if event["type"] == "state_changed":
                    for topic, payload in self._state_messages():
                        await client.publish(topic, payload, retain=True)
                elif event["type"] == "ai_alert":
                    d = event.get("data") or {}
                    await client.publish(
                        f"{self.base}/ai_alert_last", retain=True,
                        payload=f"{d.get('type', '?')} ({d.get('severity', '?')}, "
                                f"score {d.get('score', '?')})")
                elif event["type"] in ("print_started", "print_completed", "print_failed",
                                       "printer_error"):
                    await client.publish(
                        f"{self.base}/event",
                        json.dumps(event, ensure_ascii=False, default=str), retain=False)
        finally:
            sub.close()

    async def _handle_incoming(self, client: aiomqtt.Client) -> None:
        """Comandi da HA: {base}/cmd/stop|pause|resume e {base}/set/speed|light."""
        async for message in client.messages:
            topic = str(message.topic)
            raw = bytes(message.payload).decode(errors="replace").strip()
            payload = raw.lower()
            try:
                if topic.startswith(f"{self.base}/set/"):
                    setting = topic.removeprefix(f"{self.base}/set/").lower()
                    if setting == "speed":
                        await self.printer_api.set_print_speed(int(float(raw)))
                        log.info("Velocità impostata via MQTT: %s%%", raw)
                    elif setting == "light":
                        await self.printer_api.set_light(payload in ("on", "1", "true"))
                        log.info("Luce interna via MQTT: %s", payload)
                    self.bus.publish("remote_command", {"command": f"set_{setting}",
                                                        "value": raw, "source": "mqtt"})
                    continue
                cmd = topic.removeprefix(f"{self.base}/cmd/").lower() or payload
                if cmd == "stop":
                    await self.printer_api.stop_print()
                elif cmd == "pause":
                    await self.printer_api.pause_print()
                elif cmd == "resume":
                    await self.printer_api.resume_print()
                else:
                    log.debug("Comando MQTT ignorato: %s", cmd)
                    continue
                log.info("Comando MQTT eseguito: %s", cmd)
                self.bus.publish("remote_command", {"command": cmd, "source": "mqtt"})
            except Exception as e:  # noqa: BLE001
                log.error("Comando MQTT '%s' fallito: %s", topic, e)
