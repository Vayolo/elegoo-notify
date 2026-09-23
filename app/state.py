"""Stato condiviso della stampante + derivazione eventi semantici.

L'SDCP non invia eventi "print_started": tutto va derivato dalle
transizioni di `PrintInfo.Status` nei push `sdcp/status`.
"""
from __future__ import annotations

import time
from typing import Any, Optional

# Codici PrintInfo.Status (SDCP): i codici 0-10 sono documentati, ma il
# firmware REALE della Centauri Carbon usa anche codici non documentati
# (es. 13 = printing, osservato sul campo). Strategia robusta: tutto ciò che
# è > 0 e non terminale (8/9) è considerato "job attivo".
PRINT_STATUS_NAMES = {
    0: "idle", 1: "printing", 2: "dropping", 3: "exposing", 4: "lifting",
    5: "pausing", 6: "paused", 7: "stopping", 8: "stopped", 9: "complete",
    10: "file_checking", 13: "printing",
}
MACHINE_STATUS_NAMES = {0: "idle", 1: "printing", 2: "transferring", 3: "calibrating", 4: "testing"}

TERMINAL_PRINT_STATES = {8, 9}   # stopped / complete
PAUSED_STATES = {5, 6}           # pausing / paused (codici documentati)

ERROR_NUMBER_REASONS = {
    1: "Verifica MD5 del file fallita",
    2: "Lettura del file di stampa fallita",
    3: "Risoluzione del file non valida",
    4: "Formato file non supportato",
    5: "File non compatibile con il modello di stampante",
}

ERROR_STATUS_REASONS = {
    0: "Nessun errore",
    1: "Errore temperatura (ugello/piatto)",
    3: "Filamento esaurito",
    6: "Filamento inceppato",
    7: "Livellamento automatico fallito",
    12: "Chiavetta USB rimossa durante la stampa",
    13: "Errore homing asse X",
    14: "Errore homing asse Z",
    17: "Errore homing generico",
    18: "Distacco della stampa dal piatto",
    19: "Eccezione durante la stampa",
    20: "Movimento motori anormale",
    23: "Errore homing asse Y",
    24: "Errore file G-code",
    25: "Errore connessione telecamera",
    26: "Errore di rete",
    27: "Connessione al server fallita",
    28: "Disconnessione app durante la stampa",
    33: "Sensore temperatura ugello offline",
    34: "Sensore temperatura piatto offline",
}

TEMP_KEYS = {
    "nozzle": ("TempOfNozzle", "TempTargetNozzle"),
    "hotbed": ("TempOfHotbed", "TempTargetHotbed"),
    "chamber": ("TempOfBox", "TempTargetBox"),
}


def _is_job_active(status: Optional[int]) -> bool:
    """Job di stampa in corso (qualsiasi codice attivo, incluso 13 reale)."""
    return status is not None and status > 0 and status not in TERMINAL_PRINT_STATES


def _is_motion_active(status: Optional[int]) -> bool:
    """Movimenti in corso (per pausa/riprendi; codici noti non-terminale)."""
    return _is_job_active(status)


def _first(msg: dict, *keys: str, default: Any = None) -> Any:
    """Recupera il primo campo presente tra i nomi alternativi (gestisce
    le variazioni di annidamento e i refusi di battitura dell'SDCP)."""
    candidates = [msg, msg.get("Data") if isinstance(msg.get("Data"), dict) else None,
                  msg.get("Status") if isinstance(msg.get("Status"), dict) else None,
                  msg.get("last_status") if isinstance(msg.get("last_status"), dict) else None]
    for k in keys:
        for c in candidates:
            if isinstance(c, dict) and k in c:
                return c[k]
    return default


class PrinterState:
    """Contiene lo stato normalizzato e deriva gli eventi del ciclo di stampa."""

    def __init__(self) -> None:
        self.connected: bool = False
        self.last_ws_message_ts: float = 0.0
        self.last_status_ts: float = 0.0
        self.machine_status: Optional[int] = None
        self.print_status: Optional[int] = None
        self.filename: Optional[str] = None
        self.task_id: Optional[str] = None
        self.current_ticks: int = 0
        self.total_ticks: int = 0
        self.current_layer: int = 0
        self.total_layers: int = 0
        self.error_number: int = 0
        self.reported_progress: Optional[int] = None
        self.print_speed: int = 100
        self.light: Optional[bool] = None
        self.temps: dict[str, float] = {}
        self.coords: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.attributes: dict[str, Any] = {}
        self._job_started_ts: Optional[float] = None
        self._last_temp_emit: float = 0.0
        self._progress_emit_ts: float = 0.0
        self._last_progress_pct: float = -1.0
        self._last_error_emitted: int = 0
        self._terminal_emitted: bool = False
        self._started_emitted: bool = False
        self.last_error: Optional[str] = None
        self.progress_throttle_s: float = 5.0
        self.temp_throttle_s: float = 5.0

    # ------------------------------------------------------------------ #
    # Aggiornamento da push/risposte SDCP
    # ------------------------------------------------------------------ #
    def update_from_status(self, status: dict[str, Any]) -> list[tuple[str, dict]]:
        """Aggiorna lo stato e restituisce gli eventi derivati [(type, data)]."""
        events: list[tuple[str, dict]] = []
        now = time.time()
        self.last_status_ts = now

        prev_print = self.print_status
        prev_machine = self.machine_status

        machine = _first(status, "CurrentStatus")
        if isinstance(machine, list) and machine:
            self.machine_status = int(machine[0])
        elif isinstance(machine, (int, float)):
            self.machine_status = int(machine)

        print_info = _first(status, "PrintInfo")
        if isinstance(print_info, dict):
            ps = print_info.get("Status")
            if ps is not None:
                self.print_status = int(ps)
            self.filename = print_info.get("Filename") or self.filename
            self.task_id = print_info.get("TaskId") or self.task_id
            self.current_ticks = int(print_info.get("CurrentTicks") or 0)
            self.total_ticks = int(print_info.get("TotalTicks") or 0)
            self.current_layer = int(print_info.get("CurrentLayer") or 0)
            self.total_layers = int(print_info.get("TotalLayer") or 0)
            self.error_number = int(print_info.get("ErrorNumber") or 0)
            prog = print_info.get("Progress")
            self.reported_progress = int(prog) if prog is not None else None
            for key in ("PrintSpeed", "PrintSpeedPct"):
                if print_info.get(key) is not None:
                    self.print_speed = int(print_info[key])
                    break
        else:
            # PrintInfo assente => stampante in idle
            self.print_status = 0

        temps_changed = False
        for name, (cur_key, tgt_key) in TEMP_KEYS.items():
            cur = _first(status, cur_key)
            tgt = _first(status, tgt_key)
            old = self.temps.get(name, 0.0)
            new = float(cur) if cur is not None else None
            if new is not None and abs(new - old) > 0.3:
                temps_changed = True
            self.temps[name] = new
            self.temps[f"{name}_target"] = float(tgt) if tgt is not None else None

        light = _first(status, "LightStatus")
        if isinstance(light, dict):
            try:
                self.light = bool(int(light.get("SecondLight", 0)))
            except (ValueError, TypeError):
                pass

        coord_raw = _first(status, "CurrenCoord", "CurrentCoord")
        if isinstance(coord_raw, str):
            try:
                self.coords = tuple(float(v) for v in coord_raw.split(",")[:3])
            except ValueError:
                pass

        # ---- Derivazione eventi ---- #
        events.extend(self._derive_lifecycle(prev_print, prev_machine))

        # temperature_update: throttled e solo se cambiate
        if temps_changed and now - self._last_temp_emit > self.temp_throttle_s:
            self._last_temp_emit = now
            events.append(("temperature_update", {"temps": dict(self.temps)}))

        # progress: throttled se la percentuale è cambiata
        pct = self.percent
        if pct is not None and pct != self._last_progress_pct and now - self._progress_emit_ts > self.progress_throttle_s:
            self._progress_emit_ts = now
            self._last_progress_pct = pct
            events.append(("print_progress", {
                "percent": pct,
                "time_remaining_s": self.time_remaining_s,
                "filename": self.filename,
                "current_layer": self.current_layer,
                "total_layers": self.total_layers,
            }))
        return events

    def _derive_lifecycle(self, prev_print: Optional[int], prev_machine: Optional[int]) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        cur = self.print_status

        # Ingresso in un job: da stato non-job a qualunque stato attivo
        # (0/idle → 10 file_checking → 1 printing, o direttamente 1)
        if _is_job_active(cur) and not _is_job_active(prev_print):
            self._job_started_ts = time.time()
            self._terminal_emitted = False
            self._started_emitted = True
            self._last_error_emitted = 0
            events.append(("print_started", {
                "filename": self.filename, "task_id": self.task_id,
                "total_ticks": self.total_ticks,
            }))
        elif cur in {1, 13} and not self._started_emitted and _is_job_active(prev_print):
            # push persi (es. riconnessione a stampa in corso): recovery
            self._job_started_ts = self._job_started_ts or time.time()
            self._terminal_emitted = False
            self._started_emitted = True
            events.append(("print_started", {
                "filename": self.filename, "task_id": self.task_id,
                "total_ticks": self.total_ticks,
            }))

        if cur == 1 and prev_print in {5, 6}:
            events.append(("print_resumed", {"filename": self.filename}))

        if cur in PAUSED_STATES and _is_motion_active(prev_print) and prev_print not in PAUSED_STATES:
            events.append(("print_paused", {"filename": self.filename}))

        # Errore segnalato mentre la stampa è attiva
        if self.error_number != 0 and self.error_number != self._last_error_emitted:
            self._last_error_emitted = self.error_number
            reason = ERROR_NUMBER_REASONS.get(self.error_number, f"codice {self.error_number}")
            self.last_error = reason
            events.append(("printer_error", {
                "error_number": self.error_number, "reason": reason,
                "filename": self.filename,
            }))

        if cur == 9 and not self._terminal_emitted:
            self._terminal_emitted = True
            self._started_emitted = False
            duration_s = None
            if self._job_started_ts:
                duration_s = int(time.time() - self._job_started_ts)
            events.append(("print_completed", {
                "filename": self.filename, "task_id": self.task_id,
                "duration_s": duration_s, "layer": self.current_layer,
            }))
            self._reset_job()

        if cur == 8 and _is_job_active(prev_print) and not self._terminal_emitted:
            self._terminal_emitted = True
            self._started_emitted = False
            reason = ERROR_NUMBER_REASONS.get(self.error_number, "")
            events.append(("print_failed", {
                "filename": self.filename, "task_id": self.task_id,
                "error_number": self.error_number, "reason": reason,
                "stopped_by_user": self.error_number == 0,
            }))
            self._reset_job()

        if cur == 0:
            self._started_emitted = False

        # Fallback: machine printing senza PrintInfo (status incompleto)
        if (cur is None and prev_machine != 1 and self.machine_status == 1
                and not _is_job_active(prev_print) and not self._started_emitted):
            events.append(("print_started", {"filename": None, "task_id": None, "total_ticks": 0}))
            self._job_started_ts = time.time()
            self._terminal_emitted = False
            self._started_emitted = True
        return events

    def _reset_job(self) -> None:
        self._job_started_ts = None
        self.current_ticks = 0
        self.total_ticks = 0
        self.current_layer = 0
        self.total_layers = 0

    # ------------------------------------------------------------------ #
    # Helpers di lettura
    # ------------------------------------------------------------------ #
    @property
    def percent(self) -> Optional[float]:
        # La Centauri riporta direttamente "Progress" (int %); fallback sui tick
        if self.reported_progress is not None:
            return float(min(max(self.reported_progress, 0.0), 100.0))
        if self.total_ticks and self.total_ticks > 0:
            return round(min(self.current_ticks / self.total_ticks * 100.0, 100.0), 1)
        return None

    @property
    def time_remaining_s(self) -> Optional[int]:
        # I tick SDCP sono secondi (es. TotalTicks 36000 = 10 h)
        if self.total_ticks > 0 and self.job_active:
            return max(int(self.total_ticks - self.current_ticks), 0)
        return None

    @property
    def is_printing(self) -> bool:
        return self.job_active

    @property
    def job_active(self) -> bool:
        return _is_job_active(self.print_status)

    def human_status(self) -> str:
        if not self.connected:
            return "disconnessa"
        if self.print_status is not None:
            return PRINT_STATUS_NAMES.get(self.print_status, f"sconosciuto({self.print_status})")
        if self.machine_status is not None:
            return MACHINE_STATUS_NAMES.get(self.machine_status, "sconosciuta")
        return "sconosciuta"

    def snapshot(self) -> dict[str, Any]:
        """Stato serializzabile per REST/SSE/MQTT."""
        return {
            "connected": self.connected,
            "last_status_age_s": round(time.time() - self.last_status_ts, 1) if self.last_status_ts else None,
            "machine_status": self.machine_status,
            "machine_status_str": MACHINE_STATUS_NAMES.get(self.machine_status) if self.machine_status is not None else None,
            "print_status": self.print_status,
            "print_status_str": PRINT_STATUS_NAMES.get(self.print_status) if self.print_status is not None else None,
            "status": self.human_status(),
            "is_printing": self.is_printing,
            "job_active": self.job_active,
            "filename": self.filename,
            "task_id": self.task_id,
            "percent": self.percent,
            "time_remaining_s": self.time_remaining_s,
            "current_ticks": self.current_ticks,
            "total_ticks": self.total_ticks,
            "current_layer": self.current_layer,
            "total_layers": self.total_layers,
            "error_number": self.error_number,
            "last_error": self.last_error,
            "print_speed": self.print_speed,
            "light": self.light,
            "elapsed_s": (int(time.time() - self._job_started_ts)
                          if self._job_started_ts else None),
            "temps": dict(self.temps),
            "coords": list(self.coords),
            "attributes": dict(self.attributes),
        }
