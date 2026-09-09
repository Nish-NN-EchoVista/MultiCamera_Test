"""Application logging.

Rotating file log at `logs/emcc.log`, 5 MB x 5 backups (~25 MB retained).

The guiding rule is that the log stays *useful*: semantic events only. The raw
RX/TX stream is never logged, and neither is every temperature sample -- at
2 Hz across 25 devices that would be 4 million lines a day and would bury the
one line that mattered. Temperature is logged only when it *crosses* the
warning threshold, in either direction.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "emcc.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
_CONSOLE_FORMAT = "%(levelname)-7s %(message)s"

_configured = False


def setup_logging(
    level: int = logging.INFO,
    console: bool = True,
    log_dir: Path | None = None,
) -> Path | None:
    """Configure root logging once. Returns the log file path, or None.

    A failure to open the log file is not fatal -- the app continues with
    console logging only, because losing logs is much better than refusing to
    start a control application.
    """
    global _configured
    if _configured:
        return LOG_FILE

    directory = log_dir if log_dir is not None else LOG_DIR
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    path: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / LOG_FILE.name
        file_handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        file_handler.setLevel(level)
        root.addHandler(file_handler)
    except OSError as exc:
        path = None
        print(f"warning: file logging unavailable ({exc})", file=sys.stderr)

    if console:
        # pythonw has no console; sys.stderr can be None there.
        if sys.stderr is not None:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
            stream.setLevel(max(level, logging.INFO))
            root.addHandler(stream)

    # Tk's own noise is not interesting at INFO.
    logging.getLogger("PIL").setLevel(logging.WARNING)

    _configured = True
    return path


def shutdown_logging() -> None:
    """Flush and close handlers. Called last on shutdown."""
    logging.shutdown()


def device_logger(name: str) -> logging.LoggerAdapter:
    """A logger that prefixes every record with the device it concerns.

    Produces the shape the spec asks for::

        INFO  emcc.device  Camera 2 [192.168.2.251] connected
    """
    return logging.LoggerAdapter(logging.getLogger("emcc.device"), {"device": name})
