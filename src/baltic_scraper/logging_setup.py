"""
Logging configuration for the scraper.

Every execution emits a timestamped log to the console and, optionally,
to a file whose path is configurable (CLI ``--log-file`` or the
``[logging] file`` key in ``config.toml``).
"""

from __future__ import annotations

import logging
from pathlib import Path

LOGGER_NAME = "baltic_scraper"
_DEFAULT_FMT = "%(asctime)s [%(levelname)s] %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    log_file: Path | None = None,
    *,
    verbose: bool = True,
    level: int = logging.INFO,
) -> logging.Logger:
    """
    Configure and return the package logger.

    Parameters
    ----------
    log_file : Path, optional
        If given, execution logs are also written to this file (parent
        directories are created).  Each run *appends* a session banner.
    verbose : bool
        When ``False``, the console handler is suppressed (file logging,
        if configured, still happens).
    level : int
        Logging level for both handlers (default :data:`logging.INFO`).

    Returns
    -------
    logging.Logger
        The configured ``baltic_scraper`` logger.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.handlers.clear()  # avoid duplicate handlers across calls
    logger.propagate = False

    formatter = logging.Formatter(_DEFAULT_FMT, datefmt=_DATE_FMT)

    if verbose:
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(formatter)
        logger.addHandler(console)

    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.info("=== New scraping session ===")
        logger.info("Logging to %s", log_file)

    return logger


def get_logger() -> logging.Logger:
    """
    Return the package logger (configure with :func:`setup_logging` first).

    Returns
    -------
    logging.Logger
        The ``baltic_scraper`` logger.
    """
    return logging.getLogger(LOGGER_NAME)
