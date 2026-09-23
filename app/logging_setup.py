"""Logging con rotazione su file + mirror su stdout."""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path


def setup_logging(level: str = "INFO", log_dir: str = "data/logs",
                  max_bytes: int = 10 * 1024 * 1024, backups: int = 5) -> logging.Logger:
    """Configura il logger radice 'elegoo' con RotatingFileHandler."""
    root = logging.getLogger()
    # Evita handler duplicati su chiamate ripetute (es. nei test)
    for h in list(root.handlers):
        root.removeHandler(h)

    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)-18s %(message)s", "%Y-%m-%d %H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(getattr(logging, level.upper(), logging.INFO))
    sh.setFormatter(fmt)
    root.addHandler(sh)

    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            Path(log_dir) / "elegoo-notify.log",
            maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:  # cartella non scrivibile: continua solo su stdout
        root.warning("Impossibile creare %s: log solo su stdout", log_dir)

    logging.getLogger("websockets").setLevel(logging.INFO)
    logging.getLogger("aiomqtt").setLevel(logging.INFO)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    return logging.getLogger("elegoo")
