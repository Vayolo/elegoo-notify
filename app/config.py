"""Caricamento configurazione: config.json + default + override da ambiente.

Precedenza (dal più basso al più alto):
  1. DEFAULTS (in questo file, uguali a config.json.example)
  2. config.json (o percorso da env ELEGOO_CONFIG)
  3. Variabili d'ambiente / file .env (token, credenziali, ecc.)
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "printer": {
        "ip": "192.168.1.56",
        "driver": "sdcp",   # "sdcp" (firmware stock) | "moonraker" (Klipper/COSMOS)
        "moonraker": {"port": 80, "api_key": "",
                      "light_on_gcode": "SET_LED LED=case WHITE=1",
                      "light_off_gcode": "SET_LED LED=case WHITE=0"},
        "ws_urls": [
            "ws://{ip}:3030/websocket",
            "ws://{ip}:3030/ws",
            "ws://{ip}:3030/",
            "ws://{ip}:3333",
            "ws://{ip}/printer/ws",
        ],
        "http_port": 3030,
        "mainboard_id": None,
        "discovery_enabled": False,
        "light_on_print_start": True,
        "status_poll_seconds": 10,
        "request_timeout_seconds": 8,
        "max_reconnect_backoff_seconds": 30,
    },
    "service": {"host": "0.0.0.0", "port": 8766, "auth_enabled": False,
                "public_url": "http://192.168.1.50:8766"},
    "telegram": {
        "api_base": "https://api.telegram.org",
        "photo_mode": "photo",
        "progress_step_percent": 10,
        "notify_interval_minutes": 30,
        "min_seconds_between_msgs": 45,
        "silent_progress": True,
        "notify_on": ["start", "progress", "time", "complete", "failed", "error", "ai_alert"],
        "photo_on": ["start", "progress", "complete", "failed", "error", "ai_alert"],
        "notify_on_start_service": False,
    },
    "webcam": {
        "mode": "mjpeg",
        "mjpeg_url": "http://{ip}:3031/video",
        "snapshot_url": "http://{ip}/webcam.jpg",
        "timeout_seconds": 10,
        "retries": 2,
        "persistent_stream": True,
        "save_snapshots": True,
        "snapshot_retention": 300,
    },
    "ai": {
        "enabled": True,
        "interval_seconds": 4,
        "auto_stop": True,
        "roi": [0.03, 0.33, 0.94, 0.64],
        "only_while_printing": True,
        "sensitivity": "medium",
        "consecutive_frames": 2,
        "cooldown_seconds": 300,
        "quiet_minutes": 10,
        "warmup_samples": 8,
        "spaghetti_min_layer": 7,
        "spaghetti_baseline_factor": 3.0,
        "ml": {
            "enabled": True,
            "model_path": "models/encoder_float32.onnx",
            "prototypes_path": "models/prototypes.json",
            "threshold": 0.6,
            "consecutive": 3,
            "crop": None,   # None = full frame (COME ADDESTRATO). Il crop confonde il modello!
        },
        "layer_watch": {
            "enabled": True,
            "min_layers": 7,
            "deviance_lag": 5,
            "frame_delay_s": 1.5,
            "nozzle_mask": True,
            "thresholds": {"detach": 1.0, "breakage_diff": 0.2, "runout": 0.02, "runout_consecutive": 6},
        },
        "detectors": {
            "spaghetti": {"enabled": True, "severity": "critical"},
            "layer_shift": {"enabled": True, "severity": "warning"},
            "detach": {"enabled": True, "severity": "warning"},
            "breakage": {"enabled": True, "severity": "warning"},
            "runout": {"enabled": False, "severity": "warning"},  # sperimentale: attivalo dopo taratura
            "smoke": {"enabled": True, "severity": "warning"},
            "ml_defect": {"enabled": True, "severity": "critical"},
        },
    },
    "mqtt": {
        "enabled": True,
        "host": "192.168.1.50",
        "port": 1883,
        "username": "",
        "password": "",
        "base_topic": "elegoo_notify",
        "discovery_prefix": "homeassistant",
        "discovery_enabled": True,
    },
    "paths": {"logs": "data/logs", "snapshots": "data/snapshots",
              "gcodes": "data/gcodes", "models": "data/models"},
    "logging": {"level": "INFO", "max_bytes": 10485760, "backups": 5},
    "upload": {"max_size_mb": 500},
    "models": {"max_size_mb": 200},
    "slicer": {
        "path": "prusa-slicer",
        "profiles_dir": "slicer-profiles/centauri_carbon",
        "timeout_seconds": 1800,
    },
}


class AttrDict(dict):
    """Dict con accesso ai campi come attributi: cfg.printer.ip."""

    def __getattr__(self, item: str) -> Any:
        try:
            v = self[item]
        except KeyError as e:  # pragma: no cover
            raise KeyError(f"Chiave di configurazione mancante: {item}") from e
        if isinstance(v, dict) and not isinstance(v, AttrDict):
            v = AttrDict(v)
            self[item] = v
        return v

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _apply_env(cfg: dict, environ: dict[str, str] | None = None) -> dict:
    env = environ if environ is not None else dict(os.environ)

    # Token/chat Telegram (semplicità: metti in telegram.* implicito)
    tok = env.get("TELEGRAM_TOKEN")
    chat = env.get("TELEGRAM_CHAT_ID")
    if tok:
        cfg["telegram"]["token"] = tok
    if chat:
        cfg["telegram"]["chat_id"] = str(chat)
    if env.get("TELEGRAM_DRYRUN", "").lower() in ("1", "true", "yes"):
        cfg["telegram"]["dry_run"] = True

    user = env.get("DASHBOARD_USER", "").strip()
    pwd = env.get("DASHBOARD_PASSWORD", "").strip()
    if user and pwd:
        cfg["service"]["auth_enabled"] = True
        cfg["service"]["auth_user"] = user
        cfg["service"]["auth_password"] = pwd

    if env.get("LOG_LEVEL"):
        cfg["logging"]["level"] = env["LOG_LEVEL"].upper()
    if env.get("ELEGOO_PRINTER_IP"):
        cfg["printer"]["ip"] = env["ELEGOO_PRINTER_IP"]
    return cfg


def _resolve_templates(cfg: dict) -> dict:
    """Sostituisce {ip} negli URL con l'IP della stampante."""
    ip = cfg["printer"]["ip"]
    for section, key in (("printer", "ws_urls"), ("webcam", "mjpeg_url"), ("webcam", "snapshot_url")):
        val = cfg[section].get(key)
        if val is None:
            continue
        if isinstance(val, list):
            cfg[section][key] = [u.format(ip=ip) for u in val]
        else:
            cfg[section][key] = val.format(ip=ip)
    return cfg


def load_config(path: str | None = None, environ: dict[str, str] | None = None) -> AttrDict:
    """Carica la configurazione con default, file JSON e override env."""
    cfg_path = None
    candidates = [path] if path else []
    env_path = (environ or {}).get("ELEGOO_CONFIG") or os.environ.get("ELEGOO_CONFIG")
    if env_path:
        candidates.append(env_path)
    candidates.append("config.json")

    user_cfg: dict = {}
    for c in candidates:
        if c and Path(c).is_file():
            with open(c, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            cfg_path = c
            break

    merged = _deep_merge(DEFAULTS, user_cfg)
    merged = _apply_env(merged, environ)
    merged = _resolve_templates(merged)
    merged["_source_path"] = str(cfg_path) if cfg_path else "(defaults)"
    return AttrDict(merged)
