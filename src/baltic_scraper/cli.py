"""
Command-line interface for the Baltic Exchange TCE scraper.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from baltic_scraper.config import load_config
from baltic_scraper.logging_setup import get_logger, setup_logging
from baltic_scraper.output import write_excel, write_json
from baltic_scraper.route_map import ROUTE_NAMES, group_routes_by_vessel_class
from baltic_scraper.scraper import (
    URL,
    capture_api_calls,
    extract_sections_resilient,
    get_routes,
    get_vessel_classes,
    save_dom,
    select_route,
    select_vessel_class,
    take_screenshot,
    wait_for_app_ready,
)

DEFAULT_TIMEOUT: int = 20_000  # ms
OUTPUT_DIR: Path = Path("output")


# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg: str, *, verbose: bool = True) -> None:  # noqa: ARG001
    """
    Emit a progress message through the package logger.

    Output destinations (console and/or file) are decided once by
    :func:`baltic_scraper.logging_setup.setup_logging`: the console handler
    is only attached when not ``--quiet``, and the file handler only when a
    log file is configured.  ``verbose`` is accepted for call-site
    compatibility but no longer gates output here.

    Parameters
    ----------
    msg : str
        Message to log; surrounding separator/newline noise is trimmed.
    verbose : bool
        Unused; retained so existing ``log(..., verbose=...)`` calls work.
    """
    clean = msg.strip("\n").strip()
    if clean:
        get_logger().info(clean)


def _safe_filename(text: str) -> str:
    """
    Replace characters that are unsafe in filenames with underscores.

    Parameters
    ----------
    text : str
        Raw string (vessel class name, route name, …).

    Returns
    -------
    str
        Sanitised string suitable for use in a file name.
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "_", text)


# ── Orchestration ──────────────────────────────────────────────────────────────

async def run(  # noqa: PLR0913
    vessel_class_filter: str | None,
    route_filter: str | None,
    headed: bool,
    timeout: int,
    json_path: Path | None,
    excel_path: Path | None,
    list_only: bool,
    debug: bool,
    capture_network: bool,
    verbose: bool,
    route_plan: dict[str, list[str]] | None = None,
    channel: str | None = None,
) -> dict:
    """
    Main orchestration coroutine; drives Playwright and returns collected data.

    Parameters
    ----------
    vessel_class_filter : str, optional
        Case-insensitive substring used to limit which vessel classes are
        scraped.  ``None`` means all classes.  Ignored when *route_plan* is set.
    route_filter : str, optional
        Case-insensitive substring used to limit which routes are scraped.
        ``None`` means all routes.  Ignored when *route_plan* is set.
    headed : bool
        When ``True``, Chromium runs in visible (non-headless) mode.
    timeout : int
        Playwright element-wait timeout in milliseconds.
    json_path : Path, optional
        Destination for the JSON output file.  Skipped when ``None`` or
        *list_only* is ``True``.
    excel_path : Path, optional
        Destination for the Excel output file.  Skipped when ``None`` or
        *list_only* is ``True``.
    list_only : bool
        When ``True``, only discover vessel classes and routes (no data).
    debug : bool
        Save HTML snapshots and screenshots at each major step.
    capture_network : bool
        Log all XHR/fetch/WebSocket URLs to ``output/api_calls.json``.
    verbose : bool
        Print progress messages to stdout.
    route_plan : dict, optional
        Targeted plan ``{vessel_class: [route_code, ...]}``.  When provided,
        discovery is skipped and only these vessel classes / routes are
        scraped (the efficient path for "just these N routes").
    channel : str, optional
        Browser channel for the Chromium engine: ``None`` (default) uses
        Playwright's bundled Chromium; ``"chrome"`` / ``"msedge"`` use the
        system-installed Google Chrome / Microsoft Edge instead (no Chromium
        download needed).

    Returns
    -------
    dict
        ``{vessel_class: {route: {section: {name: row_dict}}}}`` or, when
        *list_only* is ``True``, ``{vessel_class: [route, ...]}``.
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    result: dict = {}

    launch_kwargs: dict = {"headless": not headed}
    if channel:
        launch_kwargs["channel"] = channel

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        page = await browser.new_page()

        try:
            await _scrape(
                page=page,
                vessel_class_filter=vessel_class_filter,
                route_filter=route_filter,
                timeout=timeout,
                list_only=list_only,
                debug=debug,
                capture_network=capture_network,
                verbose=verbose,
                result=result,
                route_plan=route_plan,
            )
        finally:
            # Always persist whatever was collected, even on a fatal error
            if not list_only:
                if json_path:
                    write_json(result, json_path)
                    log(f"\nJSON saved -> {json_path}", verbose=verbose)
                if excel_path:
                    write_excel(result, excel_path)
                    log(f"Excel saved -> {excel_path}", verbose=verbose)
            await browser.close()

    return result


async def _scrape(  # noqa: PLR0912, PLR0913, PLR0915
    page,
    vessel_class_filter: str | None,
    route_filter: str | None,
    timeout: int,
    list_only: bool,
    debug: bool,
    capture_network: bool,
    verbose: bool,
    result: dict,
    route_plan: dict[str, list[str]] | None = None,
) -> None:
    """
    Drive the browser through the requested vessel-class / route combinations.

    Mutates *result* in place so partial data survives a mid-run failure.

    Two modes:

    * **Discovery mode** (*route_plan* is ``None``) — discover all vessel
      classes, apply optional substring filters, scrape each.
    * **Targeted mode** (*route_plan* provided) — skip discovery and scrape
      exactly the ``{vessel_class: [route_code, ...]}`` plan.

    Parameters
    ----------
    page : Page
        Active Playwright page (already created, not yet navigated).
    vessel_class_filter, route_filter : str or None
        Substring filters for discovery mode; ``None`` means no filtering.
    timeout : int
        Playwright element-wait timeout in milliseconds.
    list_only : bool
        Discover vessel classes and routes only.
    debug : bool
        Save HTML/PNG snapshots at each step.
    capture_network : bool
        Persist captured XHR/fetch/WebSocket URLs at the end.
    verbose : bool
        Emit progress messages.
    result : dict
        Accumulator mutated in place.
    route_plan : dict, optional
        Targeted plan ``{vessel_class: [route_code, ...]}``.
    """
    if capture_network:
        await capture_api_calls(page, OUTPUT_DIR / "api_calls.json")

    log(f"Navigating to {URL}", verbose=verbose)
    await page.goto(URL, wait_until="domcontentloaded")

    log("Waiting for Anvil app...", verbose=verbose)
    await wait_for_app_ready(page, timeout)
    log("App ready.", verbose=verbose)

    # ── Targeted mode: scrape exactly the requested route plan ──────────────
    if route_plan is not None:
        await _scrape_targeted(
            page=page,
            route_plan=route_plan,
            timeout=timeout,
            debug=debug,
            verbose=verbose,
            result=result,
        )
        return

    if debug:
        await save_dom(page, OUTPUT_DIR / "debug_00_initial.html")
        await take_screenshot(page, OUTPUT_DIR / "debug_00_initial.png")

    # ── Vessel class discovery ─────────────────────────────────────────────
    log("Discovering vessel classes...", verbose=verbose)
    vessel_classes = await get_vessel_classes(page, timeout)

    if not vessel_classes:
        log("WARNING: No vessel classes detected; using page default.", verbose=verbose)

    log(f"Found {len(vessel_classes)} vessel class(es): {vessel_classes}", verbose=verbose)

    if vessel_class_filter:
        vessel_classes = [
            vc for vc in vessel_classes
            if vessel_class_filter.lower() in vc.lower()
        ]
        log(f"Filtered to: {vessel_classes}", verbose=verbose)

    # If nothing matched (or none found), do a single pass with current state
    iterations: list[str | None] = vessel_classes if vessel_classes else [None]

    for vc in iterations:
        if vc:
            log(f"\n{'-' * 60}", verbose=verbose)
            log(f"Selecting vessel class: {vc}", verbose=verbose)
            try:
                await select_vessel_class(page, vc, timeout)
            except Exception as exc:  # noqa: BLE001
                log(f"  ERROR selecting '{vc}': {exc}. Skipping.", verbose=verbose)
                continue

        vc_key = vc or "Default"

        if debug:
            safe_vc = _safe_filename(vc_key)
            await save_dom(page, OUTPUT_DIR / f"debug_vc_{safe_vc}.html")
            await take_screenshot(page, OUTPUT_DIR / f"debug_vc_{safe_vc}.png")

        # ── Route discovery ────────────────────────────────────────────────
        routes = await get_routes(page)
        log(f"Routes ({len(routes)}): {routes}", verbose=verbose)

        if list_only:
            result[vc_key] = routes
            continue

        if route_filter:
            routes = [r for r in routes if route_filter.upper() in r.upper()]
            log(f"Filtered routes: {routes}", verbose=verbose)

        result[vc_key] = {}

        for route in routes:
            log(f"  -> {route}", verbose=verbose)
            try:
                await select_route(page, route)
                if debug:
                    safe_r = _safe_filename(route)
                    safe_vc2 = _safe_filename(vc_key)
                    await save_dom(
                        page, OUTPUT_DIR / f"debug_{safe_vc2}__{safe_r}.html"
                    )
                sections = await extract_sections_resilient(page)
            except Exception as exc:  # noqa: BLE001
                log(f"    ERROR scraping '{route}': {exc}. Skipping.", verbose=verbose)
                continue

            log(f"    Sections: {list(sections.keys())}", verbose=verbose)
            result[vc_key][route] = sections

    # Flush API call log if capturing
    if capture_network and hasattr(page, "_api_flush"):
        await page._api_flush()  # type: ignore[attr-defined]


async def _scrape_targeted(
    page,
    route_plan: dict[str, list[str]],
    timeout: int,
    debug: bool,
    verbose: bool,
    result: dict,
) -> None:
    """
    Scrape exactly the routes in *route_plan*, grouped by vessel class.

    Each vessel class is selected once; then every requested route within
    it is scraped.  Per-vessel and per-route failures are isolated so one
    error never aborts the whole plan.

    Parameters
    ----------
    page : Page
        Active Playwright page, already navigated and ready.
    route_plan : dict
        ``{vessel_class: [route_code, ...]}``.
    timeout : int
        Playwright element-wait timeout in milliseconds.
    debug : bool
        Save HTML snapshots per route.
    verbose : bool
        Emit progress messages.
    result : dict
        Accumulator mutated in place.
    """
    for vessel_class, codes in route_plan.items():
        log(f"\n{'-' * 60}", verbose=verbose)
        log(f"Selecting vessel class: {vessel_class}", verbose=verbose)
        try:
            await select_vessel_class(page, vessel_class, timeout)
        except Exception as exc:  # noqa: BLE001
            log(f"  ERROR selecting '{vessel_class}': {exc}. Skipping.", verbose=verbose)
            continue

        result[vessel_class] = {}

        for code in codes:
            full_name = ROUTE_NAMES.get(code, code)
            log(f"  -> {full_name}", verbose=verbose)
            try:
                # select_route matches by substring, so the code (e.g. "TD02")
                # is enough to find the right radio button.
                await select_route(page, code)
                if debug:
                    safe_r = _safe_filename(code)
                    safe_vc = _safe_filename(vessel_class)
                    await save_dom(
                        page, OUTPUT_DIR / f"debug_{safe_vc}__{safe_r}.html"
                    )
                sections = await extract_sections_resilient(page)
            except Exception as exc:  # noqa: BLE001
                log(f"    ERROR scraping '{code}': {exc}. Skipping.", verbose=verbose)
                continue

            log(f"    Sections: {list(sections.keys())}", verbose=verbose)
            result[vessel_class][full_name] = sections


# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    """
    Build and return the argument parser for the CLI.

    Returns
    -------
    argparse.ArgumentParser
        Fully configured parser.
    """
    p = argparse.ArgumentParser(
        prog="baltic-scraper",
        description=(
            "Scrape Baltic Exchange TCE earnings for all "
            "VesselClass x Route combinations."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  # All vessel classes, all routes (JSON + Excel)
  baltic-scraper --format both

  # Only VLCC routes, JSON only
  baltic-scraper --vessel-class VLCC

  # Only TD02 across every vessel class, Excel only
  baltic-scraper --route TD02 --format excel

  # List available vessel classes and routes (no scraping)
  baltic-scraper --list

  # Show the browser window
  baltic-scraper --headed

  # Debug: save HTML/PNG at each step + capture API calls
  baltic-scraper --debug --capture-network --headed -vc VLCC -r TD02

  # Custom output paths
  baltic-scraper --output my_data.json --output-excel my_data.xlsx
""",
    )
    p.add_argument(
        "--config", "-c", metavar="FILE",
        help="TOML config file selecting routes and output (see config.toml)",
    )
    p.add_argument(
        "--routes", metavar="CODES",
        help="Comma-separated route codes to scrape, e.g. 'TD02,TD06,TC05'. "
             "Uses the cached route map to jump straight to each route's "
             "vessel class. Overrides --vessel-class/--route.",
    )
    p.add_argument(
        "--vessel-class", "-vc", metavar="FILTER",
        help="Case-insensitive substring filter for vessel class "
             "(e.g. 'VLCC', 'Suezmax', 'Clean')",
    )
    p.add_argument(
        "--route", "-r", metavar="FILTER",
        help="Case-insensitive substring filter for route name "
             "(e.g. 'TD02', 'TC05', 'Singapore')",
    )
    p.add_argument(
        "--format", choices=["json", "excel", "both"], default="json",
        dest="fmt",
        help="Output format: json (default), excel, or both",
    )
    p.add_argument(
        "--output", "-o", metavar="FILE",
        help="JSON output path (default: output/baltic_tce_YYYYMMDD_HHMMSS.json)",
    )
    p.add_argument(
        "--output-excel", "-oe", metavar="FILE",
        help="Excel output path (default: output/baltic_tce_YYYYMMDD_HHMMSS.xlsx)",
    )
    p.add_argument(
        "--list", action="store_true",
        help="List vessel classes and routes only; do not scrape data",
    )
    p.add_argument(
        "--headed", action="store_true",
        help="Run the browser in visible (headed) mode",
    )
    p.add_argument(
        "--channel", choices=["chromium", "chrome", "msedge"], default=None,
        help="Browser to drive (default 'msedge' = system Microsoft Edge, no "
             "Chromium download needed). Use 'chromium' for Playwright's "
             "bundled browser, or 'chrome' for system Google Chrome.",
    )
    p.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT, metavar="MS",
        help=f"Playwright element-wait timeout in ms (default: {DEFAULT_TIMEOUT})",
    )
    p.add_argument(
        "--debug", action="store_true",
        help="Save HTML snapshots and screenshots at each major step to output/",
    )
    p.add_argument(
        "--capture-network", action="store_true",
        help="Log all XHR/fetch/WebSocket requests to output/api_calls.json",
    )
    p.add_argument(
        "--log-file", metavar="FILE",
        help="Write the execution log to this file (also set via [logging] "
             "file in config). Each run appends a session banner.",
    )
    p.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress console progress messages (file logging still happens)",
    )
    return p


def _resolve_paths(
    args: argparse.Namespace,
) -> tuple[Path | None, Path | None]:
    """
    Derive JSON and Excel output paths from parsed CLI arguments.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed argument namespace.

    Returns
    -------
    tuple of (Path or None, Path or None)
        ``(json_path, excel_path)``.  Either may be ``None`` if the
        corresponding format was not requested.
    """
    if args.list:
        return None, None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    json_path: Path | None = None
    excel_path: Path | None = None

    if args.fmt in ("json", "both"):
        json_path = Path(args.output) if args.output else OUTPUT_DIR / f"baltic_tce_{ts}.json"

    if args.fmt in ("excel", "both"):
        excel_path = (
            Path(args.output_excel)
            if args.output_excel
            else OUTPUT_DIR / f"baltic_tce_{ts}.xlsx"
        )

    return json_path, excel_path


def main(argv: list[str] | None = None) -> int:
    """
    Entry point for the ``baltic-scraper`` command.

    Parameters
    ----------
    argv : list of str, optional
        Argument list (defaults to ``sys.argv[1:]``).

    Returns
    -------
    int
        Exit code: ``0`` on success.
    """
    # Ensure UTF-8 output on Windows consoles (cp1252 default chokes on
    # box-drawing characters and any non-Latin route names).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):  # pragma: no cover
                reconfigure(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)

    # A config file fills in defaults; explicit CLI flags still take priority.
    log_file: Path | None = Path(args.log_file) if args.log_file else None
    if args.config:
        cfg = load_config(Path(args.config))
        if args.routes is None and not cfg.is_all():
            args.routes = ",".join(cfg.routes)
        # Only override format/headed/timeout if left at their defaults
        if args.fmt == "json":
            args.fmt = cfg.fmt
        if not args.headed:
            args.headed = cfg.headed
        if args.timeout == DEFAULT_TIMEOUT:
            args.timeout = cfg.timeout
        if args.output is None and cfg.json_path:
            args.output = str(cfg.json_path)
        if args.output_excel is None and cfg.excel_path:
            args.output_excel = str(cfg.excel_path)
        if log_file is None and cfg.log_file:
            log_file = cfg.log_file
        if args.channel is None and cfg.channel:
            args.channel = cfg.channel

    # Resolve the effective browser: CLI flag > config > default ("msedge").
    # "chromium" means the bundled engine -> pass None (no channel).
    effective_channel = args.channel or "msedge"
    channel = None if effective_channel == "chromium" else effective_channel

    # Configure logging (console unless --quiet; file when a path is set).
    setup_logging(log_file=log_file, verbose=not args.quiet)

    # Build a targeted route plan when specific route codes are requested.
    route_plan: dict[str, list[str]] | None = None
    if args.routes:
        codes = [c.strip() for c in args.routes.split(",") if c.strip()]
        try:
            route_plan = group_routes_by_vessel_class(codes)
        except KeyError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    json_path, excel_path = _resolve_paths(args)

    result = asyncio.run(
        run(
            vessel_class_filter=args.vessel_class,
            route_filter=args.route,
            headed=args.headed,
            timeout=args.timeout,
            json_path=json_path,
            excel_path=excel_path,
            list_only=args.list,
            debug=args.debug,
            capture_network=args.capture_network,
            verbose=not args.quiet,
            route_plan=route_plan,
            channel=channel,
        )
    )

    if args.list:
        print("\n=== Available Vessel Classes and Routes ===\n")
        for vc, routes in result.items():
            print(f"  {vc}")
            if isinstance(routes, list):
                for r in routes:
                    print(f"    - {r}")
        return 0

    total_routes = sum(len(v) for v in result.values() if isinstance(v, dict))
    total_sections = sum(
        len(s)
        for v in result.values()
        if isinstance(v, dict)
        for s in v.values()
        if isinstance(s, dict)
    )
    print(
        f"\nDone. {len(result)} vessel class(es), "
        f"{total_routes} route(s), "
        f"{total_sections} section(s) scraped."
    )
    if json_path:
        print(f"JSON:  {json_path}")
    if excel_path:
        print(f"Excel: {excel_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
