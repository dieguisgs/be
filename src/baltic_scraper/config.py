"""
Configuration loading for the Baltic Exchange scraper.

A TOML config file selects which routes to scrape and how to output them.
By default every route is scraped (``routes = "all"``); a list of route
codes restricts the run to those routes only, using the cached
:mod:`baltic_scraper.route_map` to jump straight to each route's
VesselClass.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ScrapeConfig:
    """
    Resolved scraper configuration.

    Attributes
    ----------
    routes : "all" or list of str
        ``"all"`` scrapes every known route; a list restricts to those
        route codes (e.g. ``["TD02", "TC05"]``).
    fmt : str
        Output format: ``"json"``, ``"excel"`` or ``"both"``.
    json_path : Path or None
        Explicit JSON output path; ``None`` uses an auto-timestamped name.
    excel_path : Path or None
        Explicit Excel output path; ``None`` uses an auto-timestamped name.
    headed : bool
        Run Chromium in visible mode.
    timeout : int
        Playwright element-wait timeout in milliseconds.
    log_file : Path or None
        Where to write the execution log; ``None`` disables file logging.
    channel : str or None
        Browser channel: ``None`` uses bundled Chromium; ``"chrome"`` /
        ``"msedge"`` drive the system-installed browser.
    """

    routes: str | list[str] = "all"
    fmt: str = "both"
    json_path: Path | None = None
    excel_path: Path | None = None
    headed: bool = False
    timeout: int = 20_000
    log_file: Path | None = None
    channel: str | None = None

    def is_all(self) -> bool:
        """
        Return whether every route should be scraped.

        Returns
        -------
        bool
            ``True`` if *routes* is the string ``"all"``.
        """
        return isinstance(self.routes, str) and self.routes.strip().lower() == "all"


def load_config(path: Path) -> ScrapeConfig:
    """
    Load and validate a TOML configuration file.

    Expected schema::

        [scrape]
        routes = "all"            # or ["TD02", "TC05", ...]
        format = "both"           # "json" | "excel" | "both"

        [output]
        json_path = ""            # empty -> auto-timestamped
        excel_path = ""

        [browser]
        headed = false
        timeout = 20000

    Parameters
    ----------
    path : Path
        Path to the TOML config file.

    Returns
    -------
    ScrapeConfig
        Parsed configuration.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    ValueError
        If a field has an invalid value (e.g. unknown format).
    """
    if not path.exists():
        msg = f"Config file not found: {path}"
        raise FileNotFoundError(msg)

    # Read tolerantly: strip a UTF-8 BOM that Windows editors often add,
    # which tomllib.load() would otherwise reject.
    text = path.read_text(encoding="utf-8-sig")
    data = tomllib.loads(text)

    scrape = data.get("scrape", {})
    output = data.get("output", {})
    browser = data.get("browser", {})
    logging_cfg = data.get("logging", {})

    routes = scrape.get("routes", "all")
    if isinstance(routes, str) and routes.strip().lower() != "all":
        # A single bare route code is allowed; normalise to a list
        routes = [routes]

    fmt = scrape.get("format", "both")
    if fmt not in ("json", "excel", "both"):
        msg = f"Invalid format '{fmt}'; expected 'json', 'excel' or 'both'."
        raise ValueError(msg)

    def _opt_path(value: str) -> Path | None:
        value = (value or "").strip()
        return Path(value) if value else None

    return ScrapeConfig(
        routes=routes,
        fmt=fmt,
        json_path=_opt_path(output.get("json_path", "")),
        excel_path=_opt_path(output.get("excel_path", "")),
        headed=bool(browser.get("headed", False)),
        timeout=int(browser.get("timeout", 20_000)),
        log_file=_opt_path(logging_cfg.get("file", "")),
        channel=(browser.get("channel", "") or "").strip() or None,
    )
