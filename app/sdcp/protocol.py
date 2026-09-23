"""Protocollo SDCP v3 (Smart Device Control Protocol) - Elegoo Centauri Carbon.

Riferimenti: https://docs.opencentauri.cc/software/api/
Note importanti:
  * i topic sono sdcp/{request|response|status|attributes|error|notice}/{MainboardID}
  * heartbeat testuale "ping"/"pong"
  * alcuni nomi campo contengono refusi di battitura (CurrenCoord,
    RelaseFilmState, MaximumCloudSDCPSercicesAllowed): vanno usati così.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

# Comandi SDCP
CMD_STATUS_REFRESH = 0
CMD_ATTRIBUTES = 1
CMD_START_PRINT = 128
CMD_PAUSE_PRINT = 129
CMD_STOP_PRINT = 130
CMD_CONTINUE_PRINT = 131
CMD_STOP_FEEDING = 132
CMD_SKIP_PREHEAT = 133
CMD_TERMINATE_TRANSFER = 255
CMD_SET_NAME = 192
CMD_SET_CONFIG = 403
CMD_FILE_LIST = 258
CMD_FILE_DELETE = 259
CMD_HISTORY_LIST = 320
CMD_HISTORY_DETAIL = 321
CMD_VIDEO_STREAM = 386
CMD_TIMELAPSE = 387

CMD_NAMES = {
    0: "status_refresh", 1: "attributes", 128: "start_print", 129: "pause",
    130: "stop", 131: "continue", 132: "stop_feeding", 133: "skip_preheat",
    255: "terminate_transfer", 192: "set_name", 403: "set_config",
    258: "file_list", 259: "file_delete", 320: "history_list",
    321: "history_detail", 386: "video_stream", 387: "timelapse",
}

TOPIC_REQUEST = "sdcp/request/"
TOPIC_RESPONSE = "sdcp/response/"
TOPIC_STATUS = "sdcp/status/"
TOPIC_ATTRIBUTES = "sdcp/attributes/"
TOPIC_ERROR = "sdcp/error/"
TOPIC_NOTICE = "sdcp/notice/"

# Codici di errore dei comandi di stampa
PRINT_CTRL_ACK = {
    0: "OK", 1: "stampante occupata", 2: "file non trovato",
    3: "verifica MD5 fallita", 4: "errore I/O file",
    5: "risoluzione non valida", 6: "formato sconosciuto", 7: "modello sconosciuto",
}

FILE_TRANSFER_ACK = {0: "OK", 1: "nessun transfer in corso", 2: "verifica in corso", 3: "non trovato"}

VIDEO_ACK = {0: "OK", 1: "limite stream raggiunto", 2: "telecamera assente", 3: "errore sconosciuto"}

SDCP_FROM_PC = 0


def new_id() -> str:
    return str(uuid.uuid4())


def build_request(cmd: int, data: dict[str, Any] | None, mainboard_id: str) -> tuple[str, str]:
    """Costruisce il messaggio di richiesta SDCP. Ritorna (request_id, json_text)."""
    request_id = new_id()
    envelope = {
        "Id": new_id(),
        "Data": {
            "Cmd": cmd,
            "Data": data or {},
            "RequestID": request_id,
            "MainboardID": mainboard_id or "",
            "TimeStamp": int(time.time()),
            "From": SDCP_FROM_PC,
        },
        "Topic": f"{TOPIC_REQUEST}{mainboard_id or ''}",
    }
    return request_id, json.dumps(envelope, ensure_ascii=False)


def parse_message(text: str) -> Optional[dict[str, Any]]:
    """Parsoa un frame WS testuale: 'pong' → None, JSON → dict."""
    if not text or text == "pong" or text == "ping":
        return None
    try:
        msg = json.loads(text)
        return msg if isinstance(msg, dict) else None
    except (ValueError, TypeError):
        return None


def topic_kind(msg: dict[str, Any]) -> Optional[str]:
    """Ritorna il tipo di topic ('status', 'response', ...) o None."""
    topic = msg.get("Topic", "")
    for prefix in (TOPIC_STATUS, TOPIC_RESPONSE, TOPIC_ATTRIBUTES, TOPIC_ERROR,
                   TOPIC_NOTICE, TOPIC_REQUEST):
        if isinstance(topic, str) and topic.startswith(prefix):
            return prefix.removeprefix("sdcp/").rstrip("/")
    return None


def extract_mainboard_id(msg: dict[str, Any]) -> Optional[str]:
    """Ricava il MainboardID da qualunque messaggio (campo o topic)."""
    mid = msg.get("MainboardID")
    data = msg.get("Data")
    if mid is None and isinstance(data, dict):
        mid = data.get("MainboardID")
    if mid is None:
        topic = msg.get("Topic", "")
        if isinstance(topic, str) and topic.startswith("sdcp/"):
            tail = topic.split("/", 2)
            if len(tail) == 3 and tail[2]:
                return tail[2]
        return None
    return str(mid) if mid is not None else None


def response_ack(msg: dict[str, Any]) -> Optional[int]:
    """Estrae il codice Ack dalla risposta a un comando."""
    data = msg.get("Data")
    if not isinstance(data, dict):
        return None
    inner = data.get("Data")
    if isinstance(inner, dict) and "Ack" in inner:
        try:
            return int(inner["Ack"])
        except (ValueError, TypeError):
            return None
    return None


def response_payload(msg: dict[str, Any]) -> dict[str, Any]:
    """Payload Data.Data di una risposta (dict vuoto se assente)."""
    data = msg.get("Data")
    if isinstance(data, dict) and isinstance(data.get("Data"), dict):
        return data["Data"]
    return {}


def status_payload(msg: dict[str, Any]) -> dict[str, Any]:
    """Payload Status: il push può avere il dict direttamente o annidato."""
    for key in ("Status", "Data"):
        v = msg.get(key)
        if isinstance(v, dict) and ("TempOfNozzle" in v or "PrintInfo" in v or "CurrentStatus" in v):
            return v
    if "TempOfNozzle" in msg or "PrintInfo" in msg or "CurrentStatus" in msg:
        return msg
    return {}
