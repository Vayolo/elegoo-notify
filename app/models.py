"""Modelli 3D e slicing on-the-go (PrusaSlicer CLI).

- ModelStore: caricamento/elenco/servizio di file .stl/.3mf/.obj in data/models
- Slicer: coda di job (uno alla volta: CPU server modesta) che invoca il
  binario prusa-slicer con i profili della Centauri Carbon (slicer-profiles/)
  e produce GCODE in data/gcodes, con trasferimento opzionale alla stampante.

Il binario è opzionale: se assente gli endpoint rispondono 501 e il resto
del servizio funziona (graceful degradation, come per il modello ML).

CLI PrusaSlicer (verificata con --help):
  prusa-slicer --load printer.ini --load filament.ini --load print.ini \
               --load override.ini --slice model.stl -o out.gcode
L'override.ini (generato per job) imposta layer/infill/supports richiesti.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("elegoo.models")

ALLOWED_EXT = {".stl", ".3mf", ".obj"}
MATERIALS = {"pla": "filament_pla.ini", "petg": "filament_petg.ini"}
# Altri materiali (abs, asa, tpu, custom...) via MaterialManager (app/api/materials.py)

# Profili di stampa: preset UFFICIALI "Elegoo Centauri Carbon 0.4 nozzle"
# (OrcaSlicer resources/profiles/Elegoo). "file" è un INI caricato DOPO
# print.ini (le chiavi definite sovrascrivono il base Standard 0.20).
PRINT_PROFILES: dict[str, dict] = {
    "standard": {
        "file": None, "name": "Standard 0.20mm", "layer_height": 0.20,
        "default_infill": 15, "default": True,
        "description": "Uso generale, 2 pareti — profilo ufficiale Elegoo CC"},
    "optimal": {
        "file": "print_optimal_016.ini", "name": "Optimal 0.16mm",
        "layer_height": 0.16, "default_infill": 15,
        "description": "Dettaglio più fine, tempo di stampa medio"},
    "fine": {
        "file": "print_fine_012.ini", "name": "Fine 0.12mm",
        "layer_height": 0.12, "default_infill": 15,
        "description": "Dettaglio massimo, 3 pareti, stampa lenta"},
    "strength": {
        "file": "print_strength_020.ini", "name": "Strength 0.20mm",
        "layer_height": 0.20, "default_infill": 20,
        "description": "6 pareti, top 6, bottom 5, outer 120 — parti solide"},
    "draft": {
        "file": "print_draft_024.ini", "name": "Draft 0.24mm",
        "layer_height": 0.24, "default_infill": 10,
        "description": "Più veloce, qualità ridotta"},
    "extra_draft": {
        "file": "print_draft_028.ini", "name": "Extra Draft 0.28mm",
        "layer_height": 0.28, "default_infill": 10,
        "description": "Massima velocità, per prototipi"},
}


def profiles_public() -> list[dict]:
    return [{"id": pid, "name": v["name"], "layer_height": v["layer_height"],
             "default_infill": v["default_infill"], "default": v.get("default", False),
             "description": v["description"]}
            for pid, v in PRINT_PROFILES.items()]


def _sanitize(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    return re.sub(r"[^A-Za-z0-9._\- ]", "_", name).strip() or "model.stl"


class ModelStore:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dir = Path(cfg.paths.get("models", "data/models"))
        self.max_size = int(cfg.models.get("max_size_mb", 200)) * 1024 * 1024
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    def save(self, filename: str, data: bytes) -> Path:
        name = _sanitize(filename)
        if Path(name).suffix.lower() not in ALLOWED_EXT:
            raise ValueError(f"estensione non valida (uso: {', '.join(sorted(ALLOWED_EXT))})")
        if len(data) > self.max_size:
            raise ValueError(f"file troppo grande (max {self.max_size // (1024 * 1024)} MB)")
        if not data:
            raise ValueError("file vuoto")
        path = self.dir / name
        path.write_bytes(data)
        log.info("Modello caricato: %s (%d KB)", name, len(data) // 1024)
        return path

    def list(self) -> list[dict[str, Any]]:
        out = []
        for f in sorted(self.dir.iterdir(), key=lambda p: -p.stat().st_mtime):
            if f.is_file() and f.suffix.lower() in ALLOWED_EXT:
                st = f.stat()
                out.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
        return out

    def path(self, name: str) -> Optional[Path]:
        p = self.dir / _sanitize(name)
        return p if p.is_file() else None

    def delete(self, name: str) -> bool:
        p = self.path(name)
        if p is None:
            return False
        p.unlink()
        return True


@dataclass
class SliceJob:
    id: str
    model: str
    profile: str = "standard"
    material: str = "pla"
    infill: Optional[int] = None      # None = default del profilo
    supports: bool = False
    transfer: bool = True
    rotate_x: float = 0.0              # gradi, orientamento del pezzo
    rotate_y: float = 0.0
    rotate_z: float = 0.0
    state: str = "queued"          # queued | running | done | error
    created: float = field(default_factory=time.time)
    finished: Optional[float] = None
    log_tail: str = ""
    gcode: Optional[str] = None
    error: Optional[str] = None
    duration_s: Optional[float] = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "model": self.model, "state": self.state,
                "profile": self.profile,
                "material": self.material, "infill": self.infill,
                "supports": self.supports,
                "transfer": self.transfer,
                "rotate_x": self.rotate_x, "rotate_y": self.rotate_y,
                "rotate_z": self.rotate_z, "gcode": self.gcode,
                "duration_s": self.duration_s, "error": self.error,
                "log_tail": self.log_tail[-800:]}


class Slicer:
    """Coda di slicing: un job alla volta (subprocess prusa-slicer)."""

    def __init__(self, cfg, model_store: ModelStore, matman=None):
        s = cfg.slicer
        self._matman = matman
        self.cfg = cfg
        self.binary: str = s.get("path", "prusa-slicer")
        self.profiles = Path(s.get("profiles_dir", "slicer-profiles/centauri_carbon"))
        self.timeout: float = float(s.get("timeout_seconds", 1800))
        self.available: Optional[bool] = None   # None = non verificato
        self.store = model_store
        self.jobs: dict[str, SliceJob] = {}
        self._queue: list[SliceJob] = []
        self._worker: Optional[asyncio.Task] = None
        self._on_done = None   # callback async(job) per il trasferimento

    def set_transfer_callback(self, cb) -> None:
        self._on_done = cb

    # ------------------------------------------------------------------ #
    async def check_available(self) -> bool:
        """Il binario esiste? (cache del risultato)"""
        if self.available is None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.binary, "--help",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=15)
                    self.available = proc.returncode == 0
                except (asyncio.TimeoutError, TimeoutError):
                    proc.kill()
                    self.available = False
            except (FileNotFoundError, PermissionError, OSError):
                self.available = False
            log.info("Slicer %s: %s", self.binary,
                     "disponibile" if self.available else "NON disponibile (endpoint 501)")
        return self.available

    # ------------------------------------------------------------------ #
    def create_job(self, model: str, **params) -> SliceJob:
        profile = str(params.get("profile", "standard")).lower()
        if profile not in PRINT_PROFILES:
            raise ValueError(f"profilo non valido: {profile} "
                             f"(disponibili: {', '.join(PRINT_PROFILES)})")
        prof = PRINT_PROFILES[profile]
        raw_infill = params.get("infill")
        infill = int(raw_infill) if raw_infill is not None else prof["default_infill"]
        job = SliceJob(id=uuid.uuid4().hex[:12], model=model,
                       profile=profile,
                       material=str(params.get("material", "pla")).lower(),
                       infill=infill,
                       supports=bool(params.get("supports", False)),
                       transfer=bool(params.get("transfer", True)),
                       rotate_x=float(params.get("rotate_x", 0) or 0),
                       rotate_y=float(params.get("rotate_y", 0) or 0),
                       rotate_z=float(params.get("rotate_z", 0) or 0))
        valid = set(MATERIALS.keys())
        if self._matman:
            valid |= {m["id"] for m in self._matman.list_materials()}
        if job.material not in valid:
            raise ValueError(f"materiale non valido: {job.material} (disponibili: {', '.join(sorted(valid))})")
        if not 0 <= job.infill <= 100:
            raise ValueError("infill fuori range (0-100)")
        self.jobs[job.id] = job
        self._queue.append(job)
        self._ensure_worker()
        log.info("Job slicing %s: %s profilo=%s %s infill=%d%% supports=%s",
                 job.id, model, profile, job.material, job.infill, job.supports)
        return job

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_queue())

    async def _run_queue(self) -> None:
        while self._queue:
            job = self._queue.pop(0)
            job.state = "running"
            try:
                await self._slice(job)
                job.state = "done"
            except Exception as e:  # noqa: BLE001
                job.state = "error"
                job.error = str(e)[:500]
                log.exception("Slicing %s fallito", job.id)
            finally:
                job.finished = time.time()
                job.duration_s = round(job.finished - job.created, 1)

    # ------------------------------------------------------------------ #
    def _override_ini(self, job: SliceJob) -> Path:
        """INI per-job con infill/supports (si applica DOPO i preset)."""
        lines = [
            f"infill_density = {job.infill}",
            f"support_material = {1 if job.supports else 0}",
            "support_material_buildplate_only = 1",
            f"; job {job.id} profilo={job.profile}",
        ]
        p = self.profiles / f"job_{job.id}.ini"
        p.write_text("\n".join(lines) + "\n")
        return p

    async def _slice(self, job: SliceJob) -> None:
        model_path = self.store.path(job.model)
        if model_path is None:
            raise FileNotFoundError(f"modello {job.model} non trovato")
        if not await self.check_available():
            raise RuntimeError(f"slicer non installato ({self.binary})")

        gcode_dir = Path(self.cfg.paths.get("gcodes", "data/gcodes"))
        gcode_dir.mkdir(parents=True, exist_ok=True)
        # sanitizza il nome (spazi/speciali → underscore, evita %20)
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(job.model).stem)
        gcode_name = re.sub(r"_+", "_", stem) + ".gcode"
        out_path = gcode_dir / gcode_name

        override = self._override_ini(job)
        preset = PRINT_PROFILES.get(job.profile, {})
        cmd = [self.binary,
               "--load", str(self.profiles / "printer.ini")]

        if job.material in MATERIALS:
            cmd += ["--load", str(self.profiles / MATERIALS[job.material])]
        elif self._matman:
            ini_text = self._matman.to_ini(job.material)
            if not ini_text:
                raise ValueError(f"materiale {job.material} non generabile")
            mat_path = self.profiles / f"job_{job.id}_mat.ini"
            mat_path.write_text(ini_text)
            cmd += ["--load", str(mat_path)]
        else:
            raise ValueError(f"materiale non valido: {job.material}")

        cmd += ["--load", str(self.profiles / "print.ini")]
        preset_file = preset.get("file")
        if preset_file:
            preset_path = self.profiles / preset_file
            if not preset_path.is_file():
                raise FileNotFoundError(f"preset mancante: {preset_file}")
            cmd += ["--load", str(preset_path)]
        # orientamento del pezzo (gradi)
        if job.rotate_x:
            cmd += ["--rotate-x", str(job.rotate_x)]
        if job.rotate_y:
            cmd += ["--rotate-y", str(job.rotate_y)]
        if job.rotate_z:
            cmd += ["--rotate", str(job.rotate_z)]
        # centra sul piatto
        cmd += ["--center", "128,128",
                "--load", str(override),
                "--slice", str(model_path),
                "-o", str(out_path)]
        log.info("Slicing %s (profilo %s): %s", job.id, job.profile,
                 shlex.join(cmd)[:260])
        t0 = time.time()
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output = b""
        try:
            async for chunk in _reader(proc.stdout):
                output += chunk
                if len(output) > 60000:
                    output = output[-60000:]
                if time.time() - t0 > self.timeout:
                    proc.kill()
                    raise TimeoutError(f"slicing oltre {self.timeout}s (kill)")
            rc = await proc.wait()
        except asyncio.CancelledError:
            proc.kill()
            raise
        finally:
            override.unlink(missing_ok=True)
            (self.profiles / f"job_{job.id}_mat.ini").unlink(missing_ok=True)
        job.log_tail = output.decode(errors="replace")
        if rc != 0 or not out_path.is_file():
            raise RuntimeError(f"prusa-slicer rc={rc}: "
                               f"{job.log_tail[-300:]}")
        job.gcode = gcode_name
        log.info("Slicing %s completato in %.0fs → %s (%d KB)",
                 job.id, time.time() - t0, gcode_name, out_path.stat().st_size // 1024)
        if job.transfer and self._on_done is not None:
            try:
                await self._on_done(job, out_path)
                job.transfer = True
            except Exception as e:  # noqa: BLE001
                job.log_tail += f"\n[transfer] fallito: {e}"
                log.warning("Trasferimento %s fallito: %s", job.id, e)

    # ------------------------------------------------------------------ #
    def job(self, job_id: str) -> Optional[SliceJob]:
        return self.jobs.get(job_id)

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        jobs = sorted(self.jobs.values(), key=lambda j: -j.created)[:limit]
        return [j.public() for j in jobs]


async def _reader(stream):
    while True:
        chunk = await stream.readline()
        if not chunk:
            break
        yield chunk
