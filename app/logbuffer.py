from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from logging.handlers import RotatingFileHandler

from app.config import AppConfig


class LogBuffer(logging.Handler):
    """Keeps the last N formatted log lines and forwards each new line to a listener."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)
        self.listener: Callable[[str], None] | None = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001
            self.handleError(record)
            return
        self.lines.append(line)
        if self.listener is not None:
            self.listener(line)


def setup_logging(config: AppConfig, buffer: LogBuffer) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_ytjs", False):
            root.removeHandler(handler)
            handler.close()
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(config.logs_dir / "app.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    for handler in (file_handler, logging.StreamHandler(), buffer):
        handler.setFormatter(formatter)
        handler._ytjs = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
