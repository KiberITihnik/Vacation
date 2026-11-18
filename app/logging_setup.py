from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: str, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        file = RotatingFileHandler(
            Path(log_dir) / "job-alerts.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file.setFormatter(fmt)
        root.addHandler(file)
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
