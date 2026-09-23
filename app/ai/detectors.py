"""Rilevatori CV (stack v2 — metodologia 3DPrintSaviour + PrintSight).

Struttura del rilevamento (tutto OpenCV, Celeron-friendly, ZERO ML):

FrameAnalyzer (frame-a-frame, ROI senza gantry, warm-up):
  * spaghetti/stringing — detector STATICO (PrintSight): morfologia
    `thresh - erode(thresh)` per isolare strutture SOTTELI (thinness
    P²/4πA > 2) + Canny/Hough linee sottili NON-orizzontali, valutati
    SOLO nel vicinato dell'oggetto (lo skirt fuori maschera non conta).
    Nessun frame differencing → la testa in movimento non produce falsi.
  * layer_shift — segmenti diagonali anomali rispetto all'orientamento
    DOMINANTE della scena (non 0°/90° assoluti: la webcam è in prospettiva)
  * smoke — cadita nitidezza (var. Laplaciana) + variazione luminosità

LayerWatch (ai/layer_watch.py — un frame per LAYER):
  * score/deviance NRMSE → detach, breakage, runout (filamento finito)
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

SENSITIVITY_PRESETS: dict[str, dict[str, float]] = {
    "low": {
        "spaghetti_severity": 0.012,     # frazione area ROI occupata da stringhe
        "spaghetti_thinness": 2.2,
        "spaghetti_neighborhood": 0.18,  # raggio vicinato oggetto (fraz. bbox)
        "shift_angle_dev": 14.0,
        "shift_min_length_ratio": 0.28,
        "smoke_blur_drop": 0.55,
        "smoke_bright_change": 0.25,
    },
    "medium": {
        "spaghetti_severity": 0.008,
        "spaghetti_thinness": 2.0,
        "spaghetti_neighborhood": 0.15,
        "shift_angle_dev": 10.0,
        "shift_min_length_ratio": 0.22,
        "smoke_blur_drop": 0.45,
        "smoke_bright_change": 0.20,
    },
    "high": {
        "spaghetti_severity": 0.004,
        "spaghetti_thinness": 1.8,
        "spaghetti_neighborhood": 0.12,
        "shift_angle_dev": 7.0,
        "shift_min_length_ratio": 0.15,
        "smoke_blur_drop": 0.35,
        "smoke_bright_change": 0.12,
    },
}

# ROI di default: esclude la fascia ALTA del frame dove vivono gantry e
# testa (misurato empiricamente sulla webcam reale della Centauri Carbon).
DEFAULT_ROI = (0.03, 0.33, 0.94, 0.64)


@dataclass
class Detection:
    type: str                 # spaghetti | layer_shift | smoke | detach | breakage | runout
    score: float              # 0..1 (grado di anomalia)
    description: str


def _roi(frame: np.ndarray, roi: tuple[float, float, float, float]) -> np.ndarray:
    h, w = frame.shape[:2]
    x = int(w * roi[0])
    y = int(h * roi[1])
    rw = int(w * roi[2])
    rh = int(h * roi[3])
    return frame[y:y + rh, x:x + rw]


class FrameAnalyzer:
    """Analizzatore frame-a-frame (spaghetti statico, layer_shift, smoke)."""

    def __init__(self, sensitivity: str = "medium", history: int = 30,
                 roi: tuple[float, float, float, float] | None = None,
                 warmup_samples: int = 8, spag_baseline_factor: float = 3.0):
        self.p = dict(SENSITIVITY_PRESETS.get(sensitivity, SENSITIVITY_PRESETS["medium"]))
        self.roi = tuple(roi) if roi else DEFAULT_ROI
        self.warmup_samples = int(warmup_samples)
        self._samples = 0
        # smoke: baseline adattive
        self.sharpness_baseline: Optional[float] = None
        self.brightness_baseline: Optional[float] = None
        self.angle_baseline: Optional[float] = None   # orientamento dominante
        self._spag_baseline: Optional[float] = None  # severità "normale" del pezzo
        self.spag_baseline_factor: float = float(spag_baseline_factor)
        self.history = history
        self.last_metrics: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    def analyze(self, frame_bgr: np.ndarray) -> list[Detection]:
        out: list[Detection] = []
        self._samples += 1
        if self._samples == 1:      # primo frame: solo init baselines
            return out
        warmup = self._samples <= self.warmup_samples

        frame = cv2.resize(frame_bgr, (480, 270)) if frame_bgr.shape[1] > 480 else frame_bgr
        roi = _roi(frame, self.roi)
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        edges = cv2.Canny(gray, 60, 150)

        # ---- Spaghetti/stringing: detector STATICO (PrintSight) ---- #
        spag = self._detect_stringing(gray, edges)
        if spag is not None and not warmup:
            out.append(spag)

        # ---- Layer shift: diagonali NUOVE vs orientamento dominante ---- #
        shift = self._detect_layer_shift(edges)
        if shift is not None and not warmup:
            out.append(shift)

        # ---- Smoke: sfocatura + luminosità (parte alta ROI) ---- #
        smoke = self._detect_smoke(gray)
        if smoke is not None and not warmup:
            out.append(smoke)

        self.last_metrics = {
            "samples": self._samples,
            "warmup": warmup,
            **({"spaghetti_severity": round(self._spag_last, 5),
                "spaghetti_count": self._spag_count,
                "spaghetti_baseline": round(self._spag_baseline, 5) if self._spag_baseline is not None else None
                } if hasattr(self, "_spag_last") else {}),
            **({"angle_baseline": round(self.angle_baseline, 2),
                "diagonals": self._diag_last} if self.angle_baseline is not None else {}),
            **({"sharpness": round(self._sharp_last, 2),
                "sharpness_baseline": round(self.sharpness_baseline, 2),
                "brightness": round(self._bright_last, 2),
                "brightness_baseline": round(self.brightness_baseline, 2)} if self.sharpness_baseline else {}),
            "spaghetti_threshold": self.p["spaghetti_severity"],
        }
        return out

    # ------------------------------------------------------------------ #
    def _object_bbox(self, gray: np.ndarray) -> Optional[tuple[int, int, int, int]]:
        """BBox del blob principale (oggetto stampato) nella ROI."""
        mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        c = max(contours, key=cv2.contourArea)
        if cv2.contourArea(c) < 400:
            return None
        return cv2.boundingRect(c)

    def _detect_stringing(self, gray: np.ndarray, edges: np.ndarray) -> Optional[Detection]:
        """Stringing/spaghetti STATICI: strutture sottili intorno all'oggetto."""
        # 1) feature sottili via morfologia (PrintSight): thresh - erode(thresh)
        thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY_INV, 31, 2)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)
        eroded = cv2.erode(thresh, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
        thin = cv2.subtract(thresh, eroded)
        thin = cv2.dilate(thin, kernel, iterations=1)

        # 2) maschera vicinato oggetto: le stringhe VIVE stanno addosso
        #    all'oggetto; skirt/purge (che restano per tutta la stampa)
        #    stanno fuori e non devono contare
        bbox = self._object_bbox(gray)
        h, w = gray.shape
        if bbox is None:
            # nessun oggetto rilevabile: niente maschera, ma soglia più alta
            valid = np.ones_like(gray, bool)
        else:
            x, y, bw, bh = bbox
            m = int(max(bw, bh) * self.p["spaghetti_neighborhood"])
            x0, y0 = max(0, x - m), max(0, y - m)
            x1, y1 = min(w, x + bw + m), min(h, y + bh + m)
            valid = np.zeros_like(gray, bool)
            valid[y0:y1, x0:x1] = True
            # escludi SOLO l'anello di bordo del bbox (il perimetro dell'oggetto
            # risulterebbe una "struttura sottile" fittizia). L'INTERNO resta
            # valido: gli spaghetti veri atterrano sull'oggetto, e i dettagli
            # interni del pezzo finiscono nella baseline adattiva, non negli
            # alert.
            ex = 3
            outer = np.zeros_like(gray, bool)
            outer[max(0, y - ex):min(h, y + bh + ex), max(0, x - ex):min(w, x + bw + ex)] = True
            inner = np.zeros_like(gray, bool)
            inner[max(0, y + ex):min(h, y + bh - ex), max(0, x + ex):min(w, x + bw - ex)] = True
            valid &= ~(outer & ~inner)

        thin_in = thin * valid
        # 3) contour: struttura "sottile" = thinness P²/4πA alta
        contours, _ = cv2.findContours(thin_in, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        total_len = 0.0
        count = 0
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < 8:
                continue
            per = cv2.arcLength(cnt, closed=True)
            thinness = per * per / (4 * np.pi * area) if area > 0 else 0
            if thinness > self.p["spaghetti_thinness"] and per > 20:
                total_len += per
                count += 1

        # 4) Canny+Hough: linee sottili non-orizzontali nel vicinato
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=25,
                                minLineLength=25, maxLineGap=8)
        hough_len = 0.0
        if lines is not None:
            for seg in np.asarray(lines).reshape(-1, 4):
                x1, y1, x2, y2 = (int(v) for v in seg)
                length = float(np.hypot(x2 - x1, y2 - y1))
                ang = abs(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
                if ang < 15 or ang > 165:      # orizzontali escluse
                    continue
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                if not (0 <= cy < h and 0 <= cx < w and valid[cy, cx]):
                    continue
                hough_len += length
                count += 1

        severity = (total_len + hough_len) / float(h * w)
        self._spag_last = severity
        self._spag_count = count
        # Soglia ADATTIVA: ogni pezzo ha la sua "normalità" di dettagli sottili
        # (pareti fini, alette, texture). La baseline (EMA lenta) apprende la
        # normalità e si allerta solo sopra max(soglia_assoluta, baseline*N).
        if self._spag_baseline is None:
            self._spag_baseline = severity
            return None
        threshold = max(self.p["spaghetti_severity"],
                        self._spag_baseline * self.spag_baseline_factor)
        if severity > threshold:
            return Detection(
                "spaghetti", min(severity / (threshold * 4), 1.0),
                f"{count} strutture sottili, severità {severity:.4f} "
                f"(baseline {self._spag_baseline:.4f}, soglia {threshold:.4f})")
        # nessuna anomalia: aggiorna lentamente la baseline
        self._spag_baseline = 0.93 * self._spag_baseline + 0.07 * severity
        return None

    # ------------------------------------------------------------------ #
    def _detect_layer_shift(self, edges: np.ndarray) -> Optional[Detection]:
        """Segmenti diagonali anomali rispetto all'orientamento dominante."""
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=40,
                                minLineLength=int(edges.shape[1] * self.p["shift_min_length_ratio"]),
                                maxLineGap=10)
        if lines is None or not len(lines):
            return None
        segs = np.asarray(lines).reshape(-1, 4)
        angs = []
        for x1, y1, x2, y2 in segs:
            a = abs(np.degrees(np.arctan2(int(y2) - int(y1), int(x2) - int(x1)))) % 180.0
            angs.append(min(a, 180.0 - a))
        angs = np.array(angs)
        self._diag_last = 0
        if self.angle_baseline is None:
            if len(angs) >= 3:
                self.angle_baseline = float(np.median(angs))
            return None
        dev = np.abs(angs - self.angle_baseline)
        dev = np.minimum(dev, 180.0 - dev)
        diagonals = int(np.sum(dev > self.p["shift_angle_dev"]))
        self._diag_last = diagonals
        if diagonals >= 4:   # severo: 4+ segmenti anomali simultanei
            return Detection("layer_shift", min(diagonals / 10.0, 1.0),
                             f"{diagonals} segmenti anomali inclinati")
        return None

    # ------------------------------------------------------------------ #
    def _detect_smoke(self, gray: np.ndarray) -> Optional[Detection]:
        top = gray[: gray.shape[0] // 2, :]
        sharpness = float(cv2.Laplacian(top, cv2.CV_64F).var())
        brightness = float(top.mean())
        self._sharp_last = sharpness
        self._bright_last = brightness
        if self.sharpness_baseline is None:
            self.sharpness_baseline = sharpness
            self.brightness_baseline = brightness
            return None
        blur_drop = 1.0 - (sharpness / self.sharpness_baseline) if self.sharpness_baseline > 1 else 0.0
        bright_change = abs(brightness - self.brightness_baseline) / max(self.brightness_baseline, 1.0)
        det = None
        if blur_drop > self.p["smoke_blur_drop"] and bright_change > self.p["smoke_bright_change"]:
            det = Detection("smoke", min(blur_drop + bright_change, 1.0),
                            f"sfocatura +{blur_drop * 100:.0f}%, "
                            f"luminosità {bright_change * 100:+.0f}%")
        # baseline adattiva lenta (cambi di luce normali)
        self.sharpness_baseline = 0.95 * self.sharpness_baseline + 0.05 * sharpness
        self.brightness_baseline = 0.95 * self.brightness_baseline + 0.05 * brightness
        return det
