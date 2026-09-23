#!/usr/bin/env python3
"""Test di accettazione #4 — Detector ML (stack PrintGuard, GPL-2.0).

Verifica che l'encoder ONNX + prototipi:
  1. si carichino e inferiscano
  2. classifichino frame sani (piatto/pulito, stampa normale) come "success"
     con score basso (< 0.35)
  3. assegnano agli spaghetti sintetici uno score nettmente più alto
     (direzione corretta del rilevamento)
  4. restituiscano sempre score ∈ [0,1]

Salta con exit 0 se onnxruntime/modello non sono disponibili
(es. macchine senza il modello scaricato).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    results.append((name, bool(cond), extra))
    line = f"  [{'PASS' if cond else 'FAIL'}] {name}"
    if not cond and extra:
        line += f"  -> {extra}"
    print(line, flush=True)


def main() -> int:
    print("== Test ML detector (PrintGuard stack) ==", flush=True)
    try:
        from app.ai.ml_detector import MlDetector  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"  [SKIP] onnxruntime non disponibile: {e}")
        return 0
    model = ROOT / "models/encoder_float32.onnx"
    protos = ROOT / "models/prototypes.json"
    if not model.is_file() or not protos.is_file():
        print("  [SKIP] modelli non scaricati (models/)")
        return 0

    from tests.simulator import Simulator
    det = MlDetector(str(model), str(protos), crop=(0.03, 0.33, 0.94, 0.64))

    sim = Simulator()
    sim.print_status = 0
    empty = cv2.imdecode(np.frombuffer(sim.make_jpeg(), np.uint8), cv2.IMREAD_COLOR)
    sim.print_status = 1
    sim.current_ticks = 18000
    normal = cv2.imdecode(np.frombuffer(sim.make_jpeg(), np.uint8), cv2.IMREAD_COLOR)
    sim.anomaly = "spaghetti"
    spag = cv2.imdecode(np.frombuffer(sim.make_jpeg(), np.uint8), cv2.IMREAD_COLOR)

    r_empty = det.score_frame(empty)
    r_norm = det.score_frame(normal)
    r_spag = det.score_frame(spag)

    print(f"  vuoto: {r_empty['score']} ({r_empty['prediction']}) | "
          f"normale: {r_norm['score']} ({r_norm['prediction']}) | "
          f"spaghetti: {r_spag['score']} ({r_spag['prediction']})")

    check("Score sempre in [0,1]",
          all(0.0 <= r["score"] <= 1.0 for r in (r_empty, r_norm, r_spag)))
    check("Frame sano (piatto) → score basso (< 0.35)",
          r_empty["score"] < 0.35, f"{r_empty['score']}")
    check("Stampa normale → score basso (< 0.35)",
          r_norm["score"] < 0.35, f"{r_norm['score']}")
    check("Spaghetti → score MOLTO più alto del normale (+0.15)",
          r_spag["score"] > r_norm["score"] + 0.15,
          f"spag {r_spag['score']} vs norm {r_norm['score']}")

    passed = sum(1 for _, c, _ in results if c)
    total = len(results)
    print(f"\n== RISULTATO: {passed}/{total} PASS ==")
    if passed == total:
        print("TEST ML: SUCCESSO")
        return 0
    print("TEST ML: FALLITO")
    return 1


if __name__ == "__main__":
    sys.exit(main())
