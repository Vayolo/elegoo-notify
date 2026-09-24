"""Gestione materiali e profili di slicing: CRUD da web UI + storage file.

materials.json (slicer-profiles/...) → materiali built-in (read-only, da fonti reali)
data/custom_materials.json → materiali custom dell'utente (CRUD)
data/custom_profiles.json → profili di stampa custom dell'utente (CRUD)
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("elegoo.materials")


class MaterialManager:
    def __init__(self, cfg):
        self.cfg = cfg
        base = Path(cfg.paths.get("logs", "data/logs")).parent
        self.data_dir = base
        self.profiles_dir = Path(cfg.slicer.get(
            "profiles_dir", "slicer-profiles/centauri_carbon_cosmos"))
        # built-in: dal materials.json nei profili (dati Elegoo/OrcaSlicer reali)
        self.builtin_file = self.profiles_dir / "materials.json"
        self.custom_file = self.data_dir / "custom_materials.json"
        self.profiles_file = self.data_dir / "custom_profiles.json"
        self._ensure_dirs()

    def _ensure_dirs(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for f in (self.custom_file, self.profiles_file):
            if not f.exists():
                f.write_text("{}")

    # ============ MATERIALI ============ #
    def _load_builtin(self) -> dict:
        if self.builtin_file.is_file():
            try:
                return json.loads(self.builtin_file.read_text())
            except Exception:
                return {}
        return {}

    def _load_custom(self) -> dict:
        try:
            return json.loads(self.custom_file.read_text())
        except Exception:
            return {}

    def _save_custom(self, d: dict) -> None:
        self.custom_file.write_text(json.dumps(d, ensure_ascii=False, indent=2))

    def list_materials(self) -> list[dict]:
        """Unione built-in + custom, i custom sovrascrivono i built-in con lo stesso id."""
        all_m = {**self._load_builtin(), **self._load_custom()}
        out = []
        for mid, m in all_m.items():
            m["id"] = mid
            m["builtin"] = mid in self._load_builtin()
            out.append(m)
        out.sort(key=lambda x: (not x.get("builtin", False), x.get("name", "")))
        return out

    def get_material(self, mid: str) -> Optional[dict]:
        return self._load_custom().get(mid) or self._load_builtin().get(mid)

    def save_material(self, mid: str, data: dict) -> dict:
        """Crea o aggiorna un materiale (custom). I built-in non si modificano:
        viene creata una copia custom con lo stesso id."""
        data["id"] = mid
        data["updated"] = time.time()
        custom = self._load_custom()
        custom[mid] = data
        self._save_custom(custom)
        log.info("Materiale salvato: %s (nozzle=%s, bed=%s)", mid,
                 data.get("nozzle_temperature"), data.get("bed_temperature"))
        return data

    def delete_material(self, mid: str) -> bool:
        custom = self._load_custom()
        if mid in custom:
            del custom[mid]
            self._save_custom(custom)
            return True
        return False

    def to_ini(self, mid: str) -> Optional[str]:
        """Genera l'INI PrusaSlicer per il materiale."""
        m = self.get_material(mid)
        if not m:
            return None
        lines = [
            f"# {m.get('name', mid)} — {m.get('description', '')}",
            f"filament_type = {m.get('filament_type', mid.upper())}",
            f"first_layer_temperature = {m.get('nozzle_temperature_initial_layer', m.get('nozzle_temperature', 210))}",
            f"temperature = {m.get('nozzle_temperature', 210)}",
            f"first_layer_bed_temperature = {m.get('bed_temperature_initial_layer', m.get('bed_temperature', 60))}",
            f"bed_temperature = {m.get('bed_temperature', 60)}",
            f"fan_always_on = 1",
            f"cooling = 1",
            f"min_fan_speed = {m.get('fan_min_speed', 0)}",
            f"max_fan_speed = {m.get('fan_max_speed', 100)}",
            f"fan_below_layer_time = {m.get('fan_cooling_layer_time', 60)}",
            f"filament_max_volumetric_speed = {m.get('filament_max_volumetric_speed', 15)}",
            f"extrusion_multiplier = {m.get('filament_flow_ratio', 1.0)}",
            f"filament_density = {m.get('filament_density', 1.25)}",
            f"filament_cost = 25",
            f"filament_colour = {m.get('color', '#cccccc')}",
            f"retract_length = {m.get('retraction_length', 0.8)}",
            f"retract_speed = {m.get('retraction_speed', 40)}",
        ]
        return "\n".join(lines) + "\n"

    # ============ PROFILI DI STAMPA ============ #
    def list_profiles(self) -> list[dict]:
        """Profili built-in (da PRINT_PROPERTIES in models.py) + custom."""
        from ..models import PRINT_PROFILES
        custom = {}
        try:
            custom = json.loads(self.profiles_file.read_text())
        except Exception:
            pass
        out = []
        for pid, p in PRINT_PROFILES.items():
            d = dict(p)
            d["id"] = pid
            d["builtin"] = True
            d["editable"] = False
            out.append(d)
        for pid, p in custom.items():
            d = dict(p)
            d["id"] = pid
            d["builtin"] = False
            d["editable"] = True
            out.append(d)
        return out

    def get_profile(self, pid: str) -> Optional[dict]:
        for p in self.list_profiles():
            if p["id"] == pid:
                return p
        return None

    def save_profile(self, pid: str, data: dict) -> dict:
        data["id"] = pid
        data["updated"] = time.time()
        custom = {}
        try:
            custom = json.loads(self.profiles_file.read_text())
        except Exception:
            pass
        custom[pid] = data
        self.profiles_file.write_text(json.dumps(custom, ensure_ascii=False, indent=2))
        log.info("Profilo salvato: %s (layer=%s, walls=%s)", pid,
                 data.get("layer_height"), data.get("perimeters"))
        return data

    def delete_profile(self, pid: str) -> bool:
        try:
            custom = json.loads(self.profiles_file.read_text())
        except Exception:
            return False
        if pid in custom:
            del custom[pid]
            self.profiles_file.write_text(json.dumps(custom, ensure_ascii=False, indent=2))
            return True
        return False

    def profile_to_ini(self, pid: str) -> Optional[str]:
        """Genera l'INI override per un profilo custom."""
        p = None
        for pr in self.list_profiles():
            if pr["id"] == pid:
                p = pr
                break
        if not p:
            return None
        if p.get("builtin"):
            return None  # i built-in usano i file INI esistenti
        lines = [
            f"# Profilo custom: {p.get('name', pid)}",
            f"layer_height = {p.get('layer_height', 0.2)}",
            f"first_layer_height = {p.get('first_layer_height', 0.2)}",
            f"perimeters = {p.get('perimeters', 2)}",
            f"top_solid_layers = {p.get('top_solid_layers', 5)}",
            f"bottom_solid_layers = {p.get('bottom_solid_layers', 3)}",
            f"external_perimeter_speed = {p.get('external_perimeter_speed', 160)}",
            f"perimeter_speed = {p.get('perimeter_speed', 200)}",
            f"infill_speed = {p.get('infill_speed', 200)}",
        ]
        if "infill_density" in p:
            lines.append(f"infill_density = {p['infill_density']}")
        if "default_infill" in p:
            lines.append(f"infill_density = {p['default_infill']}")
        return "\n".join(lines) + "\n"
