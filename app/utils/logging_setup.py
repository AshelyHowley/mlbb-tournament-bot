from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Iterable


class RedactingFilter(logging.Filter):
    """Masks secrets in every log record, including formatted exception text."""

    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 4]

    def redact(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        red = self.redact(msg)
        if red != msg:
            record.msg, record.args = red, None
        if record.exc_info:
            import traceback

            tb = self.redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_text = tb
            record.exc_info = None
        return True


def setup_logging(secrets: Iterable[str], log_dir: Path, level: int = logging.INFO) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    redact = RedactingFilter(secrets)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(log_dir / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
    ]
    root = logging.getLogger()
    root.setLevel(level)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(redact)
        root.addHandler(h)
