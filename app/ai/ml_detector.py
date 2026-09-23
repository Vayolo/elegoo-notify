"""Detector ML — encoder ShuffleNetV2-x1.0 + prototipi (stack PrintGuard).

Derivato da PrintGuard di Oliver Bravery
(https://github.com/oliverbravery/PrintGuard), licenza GPL-2.0:
preprocessing, classificazione nearest-prototype e defect score sono
replicati fedelmente da `printguard/engine/vision.py` e `models/metadata.json`
(ShuffleNetV2 encoder → embedding 1024-d → distanze euclidee dai prototipi
success/failure → score = 0.5·(1+tanh((d²success − d²failure)/2))).

Leggerezza: ~5 MB di modello, CPU-only (onnxruntime), 1 inferenza ogni
`ai.interval_seconds` (4 s): carico trascurabile anche su Celeron.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np

log = logging.getLogger("elegoo.ai.ml")

INPUT_SIZE = 224
RESIZE_SHORTEST = 256
GREYSCALE_WEIGHTS = np.asarray([0.2989, 0.5870, 0.1140], dtype=np.float32)


def _resize(arr: np.ndarray, nw: int, nh: int) -> np.ndarray:
    """Nearest-neighbour resize (identico a PrintGuard/vision.py)."""
    h, w = arr.shape[:2]
    y_idx = np.linspace(0, h - 1, nh).astype(np.int64)
    x_idx = np.linspace(0, w - 1, nw).astype(np.int64)
    return arr[y_idx[:, None], x_idx[None, :]]


class MlDetector:
    def __init__(self, model_path: str, prototypes_path: str,
                 crop: Optional[tuple[float, float, float, float]] = None):
        import onnxruntime as ort  # import pigro: solo se il ML è attivo
        self.session = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        out_shape = self.session.get_outputs()[0].shape
        log.info("Modello ML caricato: %s (output %s)", Path(model_path).name, out_shape)

        meta_path = Path(model_path).parent / "metadata.json"
        pre: dict = {}
        if meta_path.is_file():
            pre = json.loads(meta_path.read_text()).get("preprocessing", {})
        self.mean = tuple(float(x) for x in pre.get("normalise_mean", (0.485, 0.456, 0.406)))
        self.std = tuple(float(x) for x in pre.get("normalise_std", (0.229, 0.224, 0.225)))

        raw = json.loads(Path(prototypes_path).read_text())
        protos = raw.get("prototypes", raw)
        self.prototypes: dict[str, np.ndarray] = {
            k: np.asarray(v, dtype=np.float32) for k, v in protos.items()}
        self.crop = tuple(crop) if crop else None
        self.last_result: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    def _preprocess(self, rgb: np.ndarray) -> np.ndarray:
        """RGB → tensore NCHW (1,3,224,224) come PrintGuard:
        resize shortest edge 256 → center crop 224 → luminanza replicata
        su 3 canali con normalizzazione ImageNet."""
        arr = rgb.astype(np.float32) / 255.0
        h, w = arr.shape[:2]
        scale = RESIZE_SHORTEST / min(w, h)
        arr = _resize(arr, max(INPUT_SIZE, round(w * scale)),
                      max(INPUT_SIZE, round(h * scale)))
        h, w = arr.shape[:2]
        top, left = (h - INPUT_SIZE) // 2, (w - INPUT_SIZE) // 2
        grey = arr[top:top + INPUT_SIZE, left:left + INPUT_SIZE] @ GREYSCALE_WEIGHTS
        chans = np.stack([(grey - m) / s for m, s in zip(self.mean, self.std)], axis=0)
        return chans[np.newaxis, ...].astype(np.float32)

    # ------------------------------------------------------------------ #
    def score_frame(self, frame_bgr: np.ndarray) -> dict[str, Any]:
        """Inferenza su un frame BGR. Ritorna score ∈ [0,1] = probabilità
        che il frame mostri una stampa in difetto (0.5 = boundary)."""
        frame = frame_bgr
        if self.crop:
            h, w = frame.shape[:2]
            x, y, cw, ch = self.crop
            frame = frame[max(0, int(y * h)):int((y + ch) * h),
                          max(0, int(x * w)):int((x + cw) * w)]
            if frame.size == 0:
                frame = frame_bgr
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        tensor = self._preprocess(rgb)
        emb = self.session.run(None, {self.input_name: tensor})[0].flatten()

        if not np.isfinite(emb).all():
            self.last_result = {"score": 0.5, "prediction": "unknown",
                                "distances": {}, "margin": 0.0}
            return self.last_result
        distances = {cls: float(np.linalg.norm(emb - proto))
                     for cls, proto in self.prototypes.items()}
        if any(math.isnan(d) or math.isinf(d) for d in distances.values()):
            self.last_result = {"score": 0.5, "prediction": "unknown",
                                "distances": {}, "margin": 0.0}
            return self.last_result
        ordered = sorted(distances.items(), key=lambda kv: kv[1])
        margin = ordered[1][1] - ordered[0][1] if len(ordered) > 1 else 0.0
        score = 0.5
        if "success" in distances and "failure" in distances:
            score = 0.5 * (1.0 + math.tanh(
                (distances["success"] ** 2 - distances["failure"] ** 2) / 2))
        self.last_result = {"score": round(float(score), 4),
                            "prediction": ordered[0][0],
                            "distances": {k: round(v, 3) for k, v in distances.items()},
                            "margin": round(float(margin), 3)}
        return self.last_result
