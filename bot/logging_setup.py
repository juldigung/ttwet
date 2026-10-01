"""Logging: verständliche deutsche Meldungen mit Zeitstempel in Datei und Konsole."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

LEVEL_NAMES = {"DEBUG": "DETAIL", "INFO": "INFO", "WARNING": "WARNUNG", "ERROR": "FEHLER", "CRITICAL": "KRITISCH"}


class GermanFormatter(logging.Formatter):
    def __init__(self, tz_name: str):
        super().__init__()
        self.tz = ZoneInfo(tz_name)

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).astimezone(self.tz)
        level = LEVEL_NAMES.get(record.levelname, record.levelname)
        text = f"{ts.strftime('%d.%m.%Y %H:%M:%S %Z')} | {level:<8} | {record.getMessage()}"
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


def setup_logging(log_path: Path, tz_name: str = "Europe/Berlin", console: bool = True) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = GermanFormatter(tz_name)
    fh = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        try:
            sys.stdout.reconfigure(errors="replace")  # Windows-Konsole: nie an Sonderzeichen abstürzen
        except (AttributeError, ValueError):
            pass
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        root.addHandler(ch)
    # Bibliotheken nicht zu gesprächig
    for noisy in ("urllib3", "ccxt", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
