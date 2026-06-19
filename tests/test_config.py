"""Tests for TOML config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from baltic_scraper.config import ScrapeConfig, load_config


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(content, encoding="utf-8")
    return p


def test_default_all(tmp_path: Path) -> None:
    """routes='all' yields an is_all() config."""
    cfg = load_config(_write(tmp_path, '[scrape]\nroutes = "all"\nformat = "both"\n'))
    assert cfg.is_all()
    assert cfg.fmt == "both"


def test_route_list(tmp_path: Path) -> None:
    """A list of route codes is preserved and is_all() is False."""
    cfg = load_config(
        _write(tmp_path, '[scrape]\nroutes = ["TD02", "TC05"]\nformat = "json"\n')
    )
    assert cfg.routes == ["TD02", "TC05"]
    assert not cfg.is_all()
    assert cfg.fmt == "json"


def test_single_bare_route_becomes_list(tmp_path: Path) -> None:
    """A single non-'all' string route is normalised to a list."""
    cfg = load_config(_write(tmp_path, '[scrape]\nroutes = "TD02"\n'))
    assert cfg.routes == ["TD02"]


def test_output_paths(tmp_path: Path) -> None:
    """Non-empty output paths are parsed to Path objects."""
    cfg = load_config(
        _write(
            tmp_path,
            '[scrape]\nroutes = "all"\n[output]\n'
            'json_path = "a.json"\nexcel_path = "b.xlsx"\n',
        )
    )
    assert cfg.json_path == Path("a.json")
    assert cfg.excel_path == Path("b.xlsx")


def test_empty_output_paths_are_none(tmp_path: Path) -> None:
    """Empty path strings resolve to None (auto-naming)."""
    cfg = load_config(
        _write(tmp_path, '[scrape]\nroutes = "all"\n[output]\njson_path = ""\n')
    )
    assert cfg.json_path is None


def test_browser_section(tmp_path: Path) -> None:
    """headed/timeout are read from [browser]."""
    cfg = load_config(
        _write(
            tmp_path,
            '[scrape]\nroutes = "all"\n[browser]\nheaded = true\ntimeout = 5000\n',
        )
    )
    assert cfg.headed is True
    assert cfg.timeout == 5000


def test_invalid_format_raises(tmp_path: Path) -> None:
    """An unknown format value raises ValueError."""
    with pytest.raises(ValueError, match="Invalid format"):
        load_config(_write(tmp_path, '[scrape]\nformat = "xml"\n'))


def test_missing_file_raises(tmp_path: Path) -> None:
    """A nonexistent config path raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


def test_scrapeconfig_defaults() -> None:
    """The dataclass defaults to scrape-all / both."""
    cfg = ScrapeConfig()
    assert cfg.is_all()
    assert cfg.fmt == "both"
