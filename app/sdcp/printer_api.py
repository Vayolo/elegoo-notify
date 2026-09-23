"""API ad alto livello verso la stampante (comandi SDCP + upload HTTP)."""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import protocol
from .ws_connector import WsConnector

log = logging.getLogger("elegoo.printer_api")


class PrinterCommandError(Exception):
    def __init__(self, cmd: int, ack: int, message: str):
        self.cmd = cmd
        self.ack = ack
        super().__init__(f"Cmd {protocol.CMD_NAMES.get(cmd, cmd)}: {message} (ack={ack})")


class PrinterApi:
    def __init__(self, connector: WsConnector, cfg):
        self.connector = connector
        self.cfg = cfg

    # ------------------------------------------------------------------ #
    # Helper
    # ------------------------------------------------------------------ #
    async def _request(self, cmd: int, data: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self.connector.send_request(cmd, data)
        return protocol.response_payload(resp)

    async def _request_checked(self, cmd: int, data: dict[str, Any] | None = None,
                                ack_map: dict[int, str] | None = None) -> dict[str, Any]:
        resp = await self.connector.send_request(cmd, data)
        payload = protocol.response_payload(resp)
        ack = protocol.response_ack(resp)
        if ack is not None and ack != 0:
            msg = (ack_map or protocol.PRINT_CTRL_ACK).get(ack, f"errore {ack}")
            raise PrinterCommandError(cmd, ack, msg)
        return payload

    # ------------------------------------------------------------------ #
    # Informazioni
    # ------------------------------------------------------------------ #
    async def refresh_status(self) -> dict[str, Any]:
        """Cmd 0: chiede alla stampante di ripushare lo stato."""
        return await self._request(protocol.CMD_STATUS_REFRESH)

    async def get_attributes(self) -> dict[str, Any]:
        return await self._request(protocol.CMD_ATTRIBUTES)

    async def file_list(self, url: str = "/local/") -> dict[str, Any]:
        return await self._request(protocol.CMD_FILE_LIST, {"Url": url})

    async def history_list(self) -> dict[str, Any]:
        return await self._request(protocol.CMD_HISTORY_LIST)

    async def task_details(self, task_ids: list[str]) -> dict[str, Any]:
        return await self._request(protocol.CMD_HISTORY_DETAIL, {"Id": task_ids})

    # ------------------------------------------------------------------ #
    # Controllo stampa
    # ------------------------------------------------------------------ #
    async def start_print(self, filename: str, start_layer: int = 0) -> int:
        """Cmd 128. Ritorna l'ack (0 = ok)."""
        resp = await self.connector.send_request(
            protocol.CMD_START_PRINT, {"Filename": filename, "StartLayer": start_layer})
        ack = protocol.response_ack(resp)
        return 0 if ack is None else int(ack)

    async def pause_print(self) -> None:
        await self._request_checked(protocol.CMD_PAUSE_PRINT, ack_map=protocol.PRINT_CTRL_ACK)

    async def resume_print(self) -> None:
        await self._request_checked(protocol.CMD_CONTINUE_PRINT, ack_map=protocol.PRINT_CTRL_ACK)

    async def stop_print(self) -> None:
        await self._request_checked(protocol.CMD_STOP_PRINT, ack_map=protocol.PRINT_CTRL_ACK)

    # ------------------------------------------------------------------ #
    # Video
    # ------------------------------------------------------------------ #
    async def enable_video(self) -> Optional[str]:
        """Cmd 386: attiva lo stream e ritorna la VideoUrl (se presente)."""
        payload = await self._request_checked(
            protocol.CMD_VIDEO_STREAM, {"Enable": 1}, ack_map=protocol.VIDEO_ACK)
        return payload.get("VideoUrl")

    async def disable_video(self) -> None:
        await self._request_checked(
            protocol.CMD_VIDEO_STREAM, {"Enable": 0}, ack_map=protocol.VIDEO_ACK)

    async def set_print_speed(self, pct: int) -> dict[str, Any]:
        return await self._request(protocol.CMD_SET_CONFIG, {"PrintSpeedPct": int(pct)})

    async def set_fan_speed(self, model: int | None = None, auxiliary: int | None = None,
                            box: int | None = None) -> dict[str, Any]:
        target: dict[str, int] = {}
        if model is not None:
            target["ModelFan"] = int(model)
        if auxiliary is not None:
            target["AuxiliaryFan"] = int(auxiliary)
        if box is not None:
            target["BoxFan"] = int(box)
        return await self._request(protocol.CMD_SET_CONFIG, {"TargetFanSpeed": target})
