"""
logger_setup.py
One logging setup shared by the whole app:
  - writes everything to logs/app.log (rotating, so it never grows forever)
  - also prints to console when run locally
  - also keeps the last N lines in memory so the Streamlit UI can show a
    live "Debug Logs" panel without reading the file back.

Call init_logging() once, near the top of app.py, before anything else logs.
"""

import logging
from collections import deque
from logging.handlers import RotatingFileHandler

from backend.config import LOG_FILE

_MEMORY_LOG_MAXLEN = 500
_memory_log: "deque[str]" = deque(maxlen=_MEMORY_LOG_MAXLEN)


class MemoryHandler(logging.Handler):
    """Keeps the most recent log lines in a ring buffer for the UI."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _memory_log.append(self.format(record))
        except Exception:
            pass


def get_recent_logs(n: int = 200) -> list[str]:
    """Return the last n log lines, newest last."""
    return list(_memory_log)[-n:]


_initialized = False


def init_logging(level: int = logging.INFO) -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    fmt = "%(asctime)s  %(levelname)-8s  %(name)-22s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt=datefmt)

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    mem_handler = MemoryHandler()
    mem_handler.setFormatter(formatter)
    root.addHandler(mem_handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
