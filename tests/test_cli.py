"""Tests for the CLI: argument parsing, path resolution, targeted scraping."""

from __future__ import annotations

from pathlib import Path

import pytest

from baltic_scraper import cli
from baltic_scraper.cli import (
    _resolve_paths,
    _safe_filename,
    _scrape_targeted,
    build_parser,
    main,
)
from tests.conftest import FakePage

# ── helpers ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "expected"),
    [("TD02", "TD02"), ("VLCC (Dirty)", "VLCC__Dirty_"), ("a/b\\c", "a_b_c")],
)
def test_safe_filename(raw: str, expected: str) -> None:
    assert _safe_filename(raw) == expected


# ── argument parsing ─────────────────────────────────────────────────────────────

def test_parser_defaults() -> None:
    args = build_parser().parse_args([])
    assert args.fmt == "json"
    assert args.routes is None
    assert args.list is False


def test_parser_routes_and_format() -> None:
    args = build_parser().parse_args(["--routes", "TD02,TD06", "--format", "both"])
    assert args.routes == "TD02,TD06"
    assert args.fmt == "both"


# ── path resolution ──────────────────────────────────────────────────────────────

def test_resolve_paths_list_mode() -> None:
    args = build_parser().parse_args(["--list"])
    assert _resolve_paths(args) == (None, None)


def test_resolve_paths_json_only() -> None:
    args = build_parser().parse_args(["--format", "json"])
    json_path, excel_path = _resolve_paths(args)
    assert json_path is not None and json_path.suffix == ".json"
    assert excel_path is None


def test_resolve_paths_both_custom() -> None:
    args = build_parser().parse_args(
        ["--format", "both", "--output", "x.json", "--output-excel", "y.xlsx"]
    )
    json_path, excel_path = _resolve_paths(args)
    assert json_path == Path("x.json")
    assert excel_path == Path("y.xlsx")


# ── targeted scraping ────────────────────────────────────────────────────────────

async def test_scrape_targeted_builds_nested_result() -> None:
    payload = {"Income": {"Total Voyage Days": {"your_outcome": "1"}}}
    page = FakePage(
        radios=["TD02: Ras Tanura to Singapore"],
        menu_items=[
            {"text": "VLCC (Dirty Tanker)", "visible": True,
             "box": {"x": 0, "y": 0, "width": 10, "height": 10}},
        ],
        evaluate_result=payload,
    )
    result: dict = {}
    await _scrape_targeted(
        page=page,
        route_plan={"VLCC (Dirty Tanker)": ["TD02"]},
        timeout=1000,
        debug=False,
        verbose=False,
        result=result,
    )
    assert result == {
        "VLCC (Dirty Tanker)": {"TD02: Ras Tanura to Singapore": payload}
    }


async def test_scrape_targeted_isolates_route_errors() -> None:
    """A failing route is skipped, not fatal."""
    page = FakePage(
        radios=["TD02: x"],  # TD03 not present -> select_route raises
        menu_items=[
            {"text": "VLCC (Dirty Tanker)", "visible": True,
             "box": {"x": 0, "y": 0, "width": 10, "height": 10}},
        ],
        evaluate_result={},
    )
    result: dict = {}
    await _scrape_targeted(
        page=page,
        route_plan={"VLCC (Dirty Tanker)": ["TD03"]},
        timeout=1000,
        debug=False,
        verbose=False,
        result=result,
    )
    # vessel class key exists but no routes scraped
    assert result == {"VLCC (Dirty Tanker)": {}}


# ── main() with config + route plan (run mocked) ────────────────────────────────

def test_main_unknown_route_returns_2(capsys: pytest.CaptureFixture) -> None:
    rc = main(["--routes", "ZZ99"])
    assert rc == 2
    assert "Unknown route code" in capsys.readouterr().err


def test_main_routes_builds_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() groups --routes into a plan and passes it to run()."""
    captured: dict = {}

    async def fake_run(**kwargs):  # noqa: ANN002, ANN003
        captured.update(kwargs)
        return {"VLCC (Dirty Tanker)": {"TD02: x": {"Income": {}}}}

    monkeypatch.setattr(cli, "run", fake_run)
    rc = main(["--routes", "TD02,TD06", "--format", "json", "--quiet"])
    assert rc == 0
    assert captured["route_plan"] == {
        "VLCC (Dirty Tanker)": ["TD02"],
        "Suezmax (Dirty Tanker)": ["TD06"],
    }


def test_main_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A config file with specific routes drives the plan."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[scrape]\nroutes = ["TD02"]\nformat = "json"\n', encoding="utf-8"
    )
    captured: dict = {}

    async def fake_run(**kwargs):  # noqa: ANN002, ANN003
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(cli, "run", fake_run)
    rc = main(["--config", str(cfg), "--quiet"])
    assert rc == 0
    assert captured["route_plan"] == {"VLCC (Dirty Tanker)": ["TD02"]}


def test_main_list_mode(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """--list prints vessel classes and routes."""
    async def fake_run(**_):  # noqa: ANN002, ANN003
        return {"VLCC (Dirty Tanker)": ["TD02: x", "TD03: y"]}

    monkeypatch.setattr(cli, "run", fake_run)
    rc = main(["--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "VLCC (Dirty Tanker)" in out
    assert "TD02: x" in out


def test_main_summary(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """A normal run prints the summary counts."""
    async def fake_run(**_):  # noqa: ANN002, ANN003
        return {"VLCC (Dirty Tanker)": {"TD02: x": {"Income": {"a": {}}}}}

    monkeypatch.setattr(cli, "run", fake_run)
    rc = main(["--routes", "TD02", "--format", "json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 vessel class(es)" in out
    assert "1 route(s)" in out


# ── discovery-mode _scrape ───────────────────────────────────────────────────────

async def test_scrape_discovery_mode() -> None:
    """Discovery mode iterates discovered vessel classes and routes."""
    payload = {"Income": {"Total Voyage Days": {"your_outcome": "1"}}}
    page = FakePage(
        radios=["TD02: Ras Tanura to Singapore"],
        menu_items=[
            {"text": "VLCC (Dirty Tanker)", "visible": True,
             "box": {"x": 0, "y": 0, "width": 10, "height": 10}},
        ],
        evaluate_result=payload,
    )
    result: dict = {}
    await cli._scrape(
        page=page,
        vessel_class_filter=None,
        route_filter=None,
        timeout=1000,
        list_only=False,
        debug=False,
        capture_network=False,
        verbose=False,
        result=result,
        route_plan=None,
    )
    assert result == {
        "VLCC (Dirty Tanker)": {"TD02: Ras Tanura to Singapore": payload}
    }


async def test_scrape_list_only_mode() -> None:
    """list_only collects route names without scraping data."""
    page = FakePage(
        radios=["TD02: Ras Tanura to Singapore", "TD03: Ras Tanura to Ningbo"],
        menu_items=[
            {"text": "VLCC (Dirty Tanker)", "visible": True,
             "box": {"x": 0, "y": 0, "width": 10, "height": 10}},
        ],
    )
    result: dict = {}
    await cli._scrape(
        page=page,
        vessel_class_filter="VLCC",
        route_filter=None,
        timeout=1000,
        list_only=True,
        debug=False,
        capture_network=False,
        verbose=False,
        result=result,
        route_plan=None,
    )
    assert result == {
        "VLCC (Dirty Tanker)": [
            "TD02: Ras Tanura to Singapore",
            "TD03: Ras Tanura to Ningbo",
        ]
    }


# ── run() end-to-end with a fake browser ─────────────────────────────────────────

async def test_run_saves_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run() drives the fake browser and writes JSON + Excel."""
    payload = {"Income": {"Total Voyage Days": {"your_outcome": "1",
               "baltic_outcome": "1", "difference": "0"}}}
    page = FakePage(
        radios=["TD02: Ras Tanura to Singapore"],
        menu_items=[
            {"text": "VLCC (Dirty Tanker)", "visible": True,
             "box": {"x": 0, "y": 0, "width": 10, "height": 10}},
        ],
        evaluate_result=payload,
    )

    class FakeBrowser:
        async def new_page(self):  # noqa: ANN202
            return page

        async def close(self) -> None:
            return None

    class FakeChromium:
        async def launch(self, **_):  # noqa: ANN202
            return FakeBrowser()

    class FakePW:
        chromium = FakeChromium()

    class FakePWContext:
        async def __aenter__(self):  # noqa: ANN202
            return FakePW()

        async def __aexit__(self, *_):  # noqa: ANN002
            return False

    monkeypatch.setattr(cli, "async_playwright", lambda: FakePWContext())
    monkeypatch.setattr(cli, "OUTPUT_DIR", tmp_path)

    json_path = tmp_path / "out.json"
    excel_path = tmp_path / "out.xlsx"
    result = await cli.run(
        vessel_class_filter=None,
        route_filter=None,
        headed=False,
        timeout=1000,
        json_path=json_path,
        excel_path=excel_path,
        list_only=False,
        debug=False,
        capture_network=False,
        verbose=False,
        route_plan={"VLCC (Dirty Tanker)": ["TD02"]},
    )
    assert json_path.exists()
    assert excel_path.exists()
    assert "VLCC (Dirty Tanker)" in result
