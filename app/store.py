"""Store persistente (JSON su disco) per storia stampe, filamento e statistiche.

File:
  data/history.json  — record per ogni stampa completata
  data/filament.json — inventario bobine + bobina attiva
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("elegoo.store")


class Store:
    def __init__(self, base_dir: str = "data"):
        self.dir = Path(base_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def _load(self, name: str, default: Any) -> Any:
        p = self.dir / name
        if not p.is_file():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _save(self, name: str, data: Any) -> None:
        p = self.dir / name
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # =================== STORIA STAMPE =================== #
    def add_print_record(self, filename: str, duration_s: int, filament_mm: float,
                         material: str, success: bool, error: Optional[str] = None) -> dict:
        """Registra una stampa completata (o fallita)."""
        with self._lock:
            hist = self._load("history.json", [])
            rec = {
                "id": len(hist) + 1,
                "ts": time.time(),
                "filename": filename,
                "duration_s": duration_s,
                "filament_mm": round(filament_mm, 1),
                "filament_g": self._mm_to_g(filament_mm, material),
                "material": material,
                "success": success,
                "error": error,
            }
            hist.append(rec)
            self._save("history.json", hist)
        log.info("Storia: stampa #%d salvata (%s, %.1f mm)", rec["id"], filename, filament_mm)
        return rec

    def get_history(self, limit: int = 100) -> list[dict]:
        return self._load("history.json", [])[-limit:]

    def get_stats(self) -> dict:
        hist = self._load("history.json", [])
        total = len(hist)
        ok = sum(1 for r in hist if r.get("success"))
        total_filament_mm = sum(r.get("filament_mm", 0) for r in hist)
        total_duration = sum(r.get("duration_s", 0) for r in hist)
        by_material: dict[str, int] = {}
        for r in hist:
            m = r.get("material", "?")
            by_material[m] = by_material.get(m, 0) + 1
        recent = hist[-10:] if hist else []
        return {
            "total_prints": total,
            "successful": ok,
            "failed": total - ok,
            "success_rate": round(ok / total * 100, 1) if total else None,
            "total_filament_mm": round(total_filament_mm, 0),
            "total_filament_m": round(total_filament_mm / 1000, 1),
            "total_filament_g": round(sum(r.get("filament_g", 0) for r in hist), 1),
            "total_print_time_h": round(total_duration / 3600, 1),
            "by_material": by_material,
            "recent": recent,
        }

    @staticmethod
    def _mm_to_g(mm: float, material: str) -> float:
        # 1.75mm filament: π×(1.75/2)² = 2.405 mm² → g = mm × 2.405 × densità
        densities = {"pla": 1.24, "petg": 1.27, "abs": 1.04, "tpu": 1.21, "asa": 1.07}
        d = densities.get((material or "").lower(), 1.25)
        return round(mm * 2.405 * d / 1000, 2)

    # =================== FILAMENTO =================== #
    def get_filament(self) -> dict:
        return self._load("filament.json", {"spools": [], "active": None})

    def set_active_spool(self, spool_id: Optional[int]) -> dict:
        with self._lock:
            data = self.get_filament()
            if spool_id is not None:
                if not any(s["id"] == spool_id for s in data.get("spools", [])):
                    raise ValueError(f"bobina {spool_id} non trovata")
            data["active"] = spool_id
            self._save("filament.json", data)
        return data

    def add_spool(self, material: str, color: str, weight_g: float, name: str = "") -> dict:
        with self._lock:
            data = self.get_filament()
            spool = {
                "id": max((s.get("id", 0) for s in data.get("spools", [])), default=0) + 1,
                "name": name or f"{material.upper()} {color}",
                "material": material.lower(),
                "color": color,
                "weight_g": weight_g,
                "remaining_g": weight_g,
                "added": time.time(),
            }
            data.setdefault("spools", []).append(spool)
            if data.get("active") is None:
                data["active"] = spool["id"]
            self._save("filament.json", data)
        log.info("Filamento: bobina aggiunta %s (%s, %dg)", spool["name"], material, weight_g)
        return spool

    def remove_spool(self, spool_id: int) -> dict:
        with self._lock:
            data = self.get_filament()
            data["spools"] = [s for s in data.get("spools", []) if s["id"] != spool_id]
            if data.get("active") == spool_id:
                data["active"] = None
            self._save("filament.json", data)
        return data

    def consume_filament(self, grams: float) -> Optional[dict]:
        """Deduci grammi dalla bobina attiva (chiamato a fine stampa)."""
        if grams <= 0:
            return None
        with self._lock:
            data = self.get_filament()
            active_id = data.get("active")
            for s in data.get("spools", []):
                if s["id"] == active_id:
                    s["remaining_g"] = max(0, round(s.get("remaining_g", s.get("weight_g", 0)) - grams, 1))
                    log.info("Filamento: -%.1fg dalla bobina '%s' (restano %.0fg)",
                             grams, s["name"], s["remaining_g"])
                    if s["remaining_g"] < 5:
                        log.warning("Filamento: bobina '%s' quasi esaurita (%.0fg)!", s["name"], s["remaining_g"])
                    break
            self._save("filament.json", data)
        return data

    def get_active_material(self) -> Optional[str]:
        data = self.get_filament()
        active_id = data.get("active")
        for s in data.get("spools", []):
            if s["id"] == active_id:
                return s.get("material")
        return None
