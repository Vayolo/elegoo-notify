#!/usr/bin/env python3
"""Scarica il detector ML (stack PrintGuard, GPL-2.0) in models/.

Il repo NON distribuisce i binari del modello: questo script li preleva
dalla repo originale di PrintGuard (https://github.com/oliverbravery/PrintGuard).
Senza i modelli il servizio gira comunque con lo stack CV di fallback;
con i modelli attiva anche il rilevamento ML (ShuffleNetV2 + prototipi).

Uso:  python3 scripts/download_models.py
"""
import sys
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/oliverbravery/PrintGuard/main/models"
FILES = ["encoder_float32.onnx", "prototypes.json", "metadata.json"]

def main() -> int:
    out_dir = Path(__file__).resolve().parents[1] / "models"
    out_dir.mkdir(exist_ok=True)
    for name in FILES:
        dest = out_dir / name
        if dest.is_file() and dest.stat().st_size > 0:
            print(f"  ✓ {name} già presente")
            continue
        print(f"  ↓ {name} …")
        urllib.request.urlretrieve(f"{BASE}/{name}", dest)
        print(f"  ✓ {dest} ({dest.stat().st_size // 1024} KB)")
    print("Modelli pronti: il detector ML si attiva al prossimo avvio.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
