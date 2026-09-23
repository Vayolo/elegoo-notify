"""LayerWatch: rilevamento anomalie ancorato ai LAYER (metodologia 3DPrintSaviour).

Invece di confrontare frame arbitrari nel tempo (dove la testa che si muove
produce tutto il rumore), cattura UN frame per layer e confronta:

  score    = NRMSE(layer N, layer N-1)     → quanto è cambiato l'ultimo layer
  deviance = NRMSE(layer N, layer N-5)     → quanto la stampa si è allontanata
                                             da 5 layer fa

Scelte chiave (per una webcam GRANDANGOLO sul piatto, non per foto ravvicinate):
  * guadagno FISSO: la normalizzazione (p1..p99 → 0..255) è calcolata sul
    primo frame e riusata per tutti → le immagini restano confrontabili
  * NRMSE calcolato sulla REGIONE DELL'OGGETTO (union bbox dei due frame,
    inflatata 10%): il metrico "vede" l'oggetto come se riempisse l'inquadratura
    (come le timelapse di Octolapse usate da 3DPrintSaviour)
  * regola decisiva: se l'oggetto SPARISCE (silhouette presente prima,
    assente ora, o viceversa) → score = 2.0 diretto ("modello mancante")

Soglie empiriche (configurabili):
  detach   : score > 1.0 AND deviance > 1.0
  breakage : |Δscore| > 0.2 AND |Δdeviance| > 0.2
  runout   : score < 0.2 AND deviance < 0.2 su ≥3 layer consecutivi
             (niente cresce → filamento esaurito / ugello intasato)

Nessun verdetto nei primi `min_layers` layer (default 7, come 3DPrintSaviour).
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger("elegoo.ai.layers")


@dataclass
class LayerDetections:
    detach: bool
    breakage: bool
    runout: bool
    score: float
    deviance: float


class LayerWatch:
    def __init__(self,
                 min_layers: int = 7,
                 deviance_lag: int = 5,
                 nozzle_mask: bool = True,
                 thresholds: Optional[dict] = None):
        self.min_layers = int(min_layers)
        self.deviance_lag = int(deviance_lag)
        self.nozzle_mask = bool(nozzle_mask)
        t = {"detach": 1.0, "breakage_diff": 0.2, "runout": 0.02,
             "runout_consecutive": 6}
        t.update(thresholds or {})
        self.th = t
        self.layers: dict[int, np.ndarray] = {}
        self.order: deque[int] = deque(maxlen=12)
        self.scores: list[float] = []
        self.deviations: list[float] = []
        self.last_layer: Optional[int] = None
        # guadagno fisso per la normalizzazione (dal primo frame)
        self._lo: Optional[float] = None
        self._hi: Optional[float] = None

    # ------------------------------------------------------------------ #
    def reset(self) -> None:
        self.layers.clear()
        self.order.clear()
        self.scores.clear()
        self.deviations.clear()
        self.last_layer = None
        self._lo = None
        self._hi = None

    # ------------------------------------------------------------------ #
    def _normalize(self, gray: np.ndarray) -> np.ndarray:
        g = cv2.GaussianBlur(gray, (7, 7), 0)
        if self._lo is None:
            self._lo = float(np.percentile(g, 1))
            self._hi = max(float(np.percentile(g, 99)), self._lo + 1.0)
        return np.clip((g.astype(np.float32) - self._lo) / (self._hi - self._lo) * 255.0,
                       0, 255).astype(np.uint8)

    def _object_bbox(self, img: np.ndarray) -> Optional[tuple[int, int, int, int]]:
        """Bbox della silhouette dell'OGGETTO. Filtri anti-blob-falso:
        * solidità: contourArea ≥ 10% dell'area del bbox (esclude bordi sottili
          come skirt e riflessi: sono "outline", non oggetti)
        * non tocca i bordi dell'immagine (piatto/sfondo sì, l'oggetto no)"""
        mask = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        if float(mask.mean()) > 127.0:
            mask = cv2.bitwise_not(mask)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        h, w = img.shape
        best, best_area = None, 0.0
        for c in contours:
            area = float(cv2.contourArea(c))
            if area < 300:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            if bw * bh > 0 and area < 0.20 * bw * bh:
                continue   # struttura "vuota" (skirt, bordi sottili, riflessi)
            if x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1:
                continue                      # tocca il bordo: piatto/sfondo
            if area > best_area:
                best, best_area = c, area
        if best is None:
            return None
        return cv2.boundingRect(best)

    def _region(self, cur: np.ndarray, ref: np.ndarray) -> Optional[tuple[slice, slice, bool]]:
        """Union bbox delle silhouette dei due frame. La soglia di segmentazione
        è calibrata con Otsu sul frame di RIFERIMENTO (dove l'oggetto esiste)
        e riusata FISSA sul frame corrente: se l'oggetto sparisce, il frame
        corrente non presenta blob solidi → verdetto 'missing' decisivo.
        Ritorna (slice_y, slice_x, object_missing) o None (usa tutto)."""
        h, w = cur.shape
        t = float(cv2.threshold(ref, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[0])
        if t < 8.0:                      # scena quasi uniforme: niente di affidabile
            return None

        def blobs(img: np.ndarray) -> list[tuple[int, int, int, int]]:
            mask = cv2.threshold(img, t, 255, cv2.THRESH_BINARY)[1]
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_SIMPLE)
            out = []
            for c in contours:
                area = float(cv2.contourArea(c))
                if area < 300:
                    continue
                x, y, bw, bh = cv2.boundingRect(c)
                if bw * bh > 0 and area < 0.20 * bw * bh:
                    continue        # strutture "vuote": skirt, riflessi lineari
                if x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1:
                    continue        # tocca il bordo: piatto/sfondo
                out.append((x, y, bw, bh))
            return out

        boxes = blobs(cur) + blobs(ref)
        missing = (len(blobs(ref)) > 0 and len(blobs(cur)) == 0)
        if missing:
            # Guardia anti-transizione-luce: se la scena è cambiata di
            # luminosità (luce interna on/off), la soglia fissa del frame di
            # riferimento rifiuta TUTTO. Ricalibra con l'Otsu del frame
            # corrente: se l'oggetto torna visibile NON è 'missing'.
            t_cur = float(cv2.threshold(cur, 0, 255,
                                         cv2.THRESH_BINARY + cv2.THRESH_OTSU)[0])
            if abs(t_cur - t) / max(t, 1.0) > 0.15:
                mask2 = cv2.threshold(cur, t_cur, 255, cv2.THRESH_BINARY)[1]
                cs, _ = cv2.findContours(mask2, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                reblobs = []
                for c in cs:
                    area = float(cv2.contourArea(c))
                    if area < 300:
                        continue
                    x, y, bw, bh = cv2.boundingRect(c)
                    if bw * bh > 0 and area < 0.20 * bw * bh:
                        continue
                    if x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1:
                        continue
                    reblobs.append((x, y, bw, bh))
                # il blob ricalibrato è un "oggetto" solo se è paragonabile al
                # riferimento (dimensione ≤ 3× e centroid vicin*): altrimenti è
                # il piatto/sfondo diventato visibile con la nuova luce
                ref_boxes = blobs(ref)
                ref_area = max((bw * bh) for (_, _, bw, bh) in ref_boxes) if ref_boxes else 0
                ok_reblobs = []
                for (x, y, bw, bh) in reblobs:
                    if ref_area and bw * bh > 3 * ref_area:
                        continue
                    ok_reblobs.append((x, y, bw, bh))
                if ok_reblobs:
                    log.info("Transizione luminosità (Otsu %.0f→%.0f): oggetto "
                             "di nuovo visibile, niente 'missing'", t, t_cur)
                    boxes = ok_reblobs
                    missing = False
        if not boxes:
            return None
        x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
        x1 = max(b[0] + b[2] for b in boxes); y1 = max(b[1] + b[3] for b in boxes)
        m = int(0.10 * max(x1 - x0, y1 - y0)) + 4
        sy = slice(max(0, y0 - m), min(h, y1 + m))
        sx = slice(max(0, x0 - m), min(w, x1 + m))
        return sy, sx, missing

    def _nrmse(self, cur: np.ndarray, ref: np.ndarray) -> float:
        """NRMSE concentrato sulla regione dell'oggetto (3DPS-style),
        con verdetto decisivo se la silhouette sparisce/compare."""
        region = self._region(cur, ref)
        if region is None:
            a, b = cur.flatten(), ref.flatten()
        else:
            sy, sx, missing = region
            if missing:
                # l'oggetto (solido, centrale nel frame di riferimento) non c'è
                # più: deviazione strutturale massima
                return 1.5
            a, b = cur[sy, sx].flatten(), ref[sy, sx].flatten()
        if a.size < 300:
            return 0.0
        rng = float(b.max() - b.min())
        if rng < 1e-6:
            return 0.0
        return float(np.sqrt(np.mean((a.astype(np.float32) - b.astype(np.float32)) ** 2)) / rng)

    # ------------------------------------------------------------------ #
    def observe(self, layer: int, frame_bgr: np.ndarray) -> Optional[LayerDetections]:
        """Registra il frame del layer e calcola score/deviance."""
        if layer <= 0:
            return None
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        if gray.shape[1] > 480:
            k = 480 / gray.shape[1]
            gray = cv2.resize(gray, (480, int(gray.shape[0] * k)))
        img = self._normalize(gray)

        if layer in self.layers:      # layer già catturato
            return None
        self.layers[layer] = img
        self.order.append(layer)
        self.last_layer = layer

        score = 0.0
        if len(self.order) >= 2:
            score = self._nrmse(img, self.layers[self.order[-2]])
        deviance = 0.0
        if len(self.order) >= self.deviance_lag + 1:
            deviance = self._nrmse(img, self.layers[self.order[-1 - self.deviance_lag]])
        self.scores.append(score)
        self.deviations.append(deviance)

        det = LayerDetections(detach=False, breakage=False, runout=False,
                              score=score, deviance=deviance)
        if len(self.scores) < self.min_layers:
            return det  # warm-up: nessun verdetto (come 3DPrintSaviour)

        prev_score = self.scores[-2] if len(self.scores) >= 2 else 0.0
        prev_dev = self.deviations[-2] if len(self.deviations) >= 2 else 0.0

        if score > self.th["detach"] and deviance > self.th["detach"]:
            det.detach = True
        if (abs(score - prev_score) > self.th["breakage_diff"]
                and abs(deviance - prev_dev) > self.th["breakage_diff"]):
            det.breakage = True
        n = int(self.th.get("runout_consecutive", 6))
        if len(self.scores) >= n:
            det.runout = (all(s < self.th["runout"] for s in self.scores[-n:])
                          and all(d < self.th["runout"] for d in self.deviations[-n:]))
        log.info("Layer %d: score=%.3f deviance=%.3f", layer, score, deviance)
        return det

    # ------------------------------------------------------------------ #
    def history(self, n: int = 20) -> list[dict]:
        out = []
        for i, layer in enumerate(list(self.order)[-n:]):
            out.append({
                "layer": layer,
                "score": round(self.scores[i], 3) if i < len(self.scores) else None,
                "deviance": round(self.deviations[i], 3) if i < len(self.deviations) else None,
            })
        return out

    def status(self) -> dict:
        return {
            "last_layer": self.last_layer,
            "samples": len(self.scores),
            "min_layers_warmup": self.min_layers,
            "deviance_lag": self.deviance_lag,
            "thresholds": self.th,
            "nozzle_mask": self.nozzle_mask,
            "history": self.history(),
        }
