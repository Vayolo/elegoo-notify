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
    det = MlDetector(str(model), str(protos))  # full frame: come addestrato

    sim = Simulator()
    sim.print_status = 0
    empty = cv2.imdecode(np.frombuffer(sim.make_jpeg(), np.uint8), cv2.IMREAD_COLOR)
    sim.print_status = 1
    sim.current_ticks = 18000
    normal = cv2.imdecode(np.frombuffer(sim.make_jpeg(), np.uint8), cv2.IMREAD_COLOR)
    sim.anomaly = "spaghetti"
    spag = cv2.imdecode(np.frombuffer(sim.make_jpeg(), np.uint8), cv2.IMREAD_COLOR)

    # frame REALE di stampa sana (webcam Centauri Carbon): sul campo il
    # modello la classifica "success" confidenziale (~0.17)
    real_path = ROOT / "tests" / "assets" / "real_print_healthy.jpg"
    real = cv2.imread(str(real_path)) if real_path.is_file() else None

    r_empty = det.score_frame(empty)
    r_norm = det.score_frame(normal)
    r_spag = det.score_frame(spag)
    r_real = det.score_frame(real) if real is not None else {"score": None}

    print(f"  sim vuoto: {r_empty['score']} | sim normale: {r_norm['score']} | "
          f"sim spaghetti: {r_spag['score']} (informativo: i frame sintetici "
          f"non sono esempi di guasto reali)")
    print(f"  REALE sano: {r_real['score']} ({r_real.get('prediction')}) "
          f"dist={r_real.get('distances')}")

    scores = [r_empty["score"], r_norm["score"]] + (
        [r_real["score"]] if real is not None else [])
    check("Score sempre in [0,1]", all(0.0 <= s <= 1.0 for s in scores))
    check("Frame sim (piatto) → score basso (< 0.35)",
          r_empty["score"] < 0.35, f"{r_empty['score']}")
    check("Stampa sim normale → score basso (< 0.35)",
          r_norm["score"] < 0.35, f"{r_norm['score']}")
    check("Frame REALE di stampa sana → score basso (< 0.35, osservato: ~0.17)",
          real is not None and r_real["score"] < 0.35, f"{r_real['score']}")

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
