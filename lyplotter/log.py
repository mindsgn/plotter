"""Centralised logging for lyplotter.

The file handler writes to ``~/.lyplotter/lyplotter.log`` (rotated at 1 MB,
3 backups kept).  A stderr handler at WARNING shows crashes on the terminal.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_DIR = Path.home() / ".lyplotter"
_LOG_FILE = _LOG_DIR / "lyplotter.log"
_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def _setup() -> None:
    """Configure the root ``lyplotter`` logger once."""
    global _configured
    if _configured:
        return
    _configured = True

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("lyplotter")
    logger.setLevel(logging.DEBUG)

    fh = RotatingFileHandler(
        str(_LOG_FILE), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
    logger.addHandler(sh)


def get_logger(name: str = "lyplotter") -> logging.Logger:
    """Return a child logger under the ``lyplotter`` namespace.

    Args:
        name: Logger name, e.g. ``"lyplotter.grbl"``.

    Returns:
        Configured :class:`logging.Logger`.
    """
    _setup()
    return logging.getLogger(name)
