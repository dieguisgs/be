"""Tests for logging setup and config log_file wiring."""

from __future__ import annotations

import logging
from pathlib import Path

from baltic_scraper.config import load_config
from baltic_scraper.logging_setup import get_logger, setup_logging


def test_setup_logging_console_only() -> None:
    """Without a log file, only a console handler is attached."""
    logger = setup_logging(log_file=None, verbose=True)
    assert any(isinstance(h, logging.StreamHandler) for h in logger.handlers)
    assert not any(isinstance(h, logging.FileHandler) for h in logger.handlers)


def test_setup_logging_writes_file(tmp_path: Path) -> None:
    """A configured log file receives the session banner and messages."""
    log_path = tmp_path / "nested" / "run.log"
    logger = setup_logging(log_file=log_path, verbose=False)
    logger.info("hello world")
    for h in logger.handlers:
        h.flush()
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "New scraping session" in content
    assert "hello world" in content


def test_setup_logging_quiet_no_console(tmp_path: Path) -> None:
    """--quiet (verbose=False) drops the console handler."""
    logger = setup_logging(log_file=tmp_path / "x.log", verbose=False)
    assert not any(
        type(h) is logging.StreamHandler for h in logger.handlers
    )
    assert any(isinstance(h, logging.FileHandler) for h in logger.handlers)


def test_setup_logging_clears_old_handlers(tmp_path: Path) -> None:
    """Re-running setup does not accumulate duplicate handlers."""
    setup_logging(log_file=tmp_path / "a.log", verbose=True)
    logger = setup_logging(log_file=tmp_path / "b.log", verbose=True)
    file_handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1


def test_get_logger_returns_named_logger() -> None:
    assert get_logger().name == "baltic_scraper"


def test_config_log_file(tmp_path: Path) -> None:
    """[logging] file is parsed into ScrapeConfig.log_file."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[scrape]\nroutes = "all"\n[logging]\nfile = "logs/run.log"\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.log_file == Path("logs/run.log")


def test_config_no_log_file_is_none(tmp_path: Path) -> None:
    """Missing [logging] section yields log_file=None."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[scrape]\nroutes = "all"\n', encoding="utf-8")
    assert load_config(cfg_path).log_file is None
