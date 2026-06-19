"""Tests for the Playwright interaction layer (using fakes, no browser)."""

from __future__ import annotations

import pytest

from baltic_scraper import scraper
from baltic_scraper.scraper import (
    ROUTE_RE,
    extract_sections,
    extract_sections_resilient,
    get_routes,
    get_vessel_classes,
    select_route,
    select_vessel_class,
    wait_for_app_ready,
)
from tests.conftest import FakePage

# ── ROUTE_RE ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "label",
    ["TD02: Ras Tanura to Singapore", "TC05: Ras Tanura to Yokohama", "td18: x"],
)
def test_route_re_matches_routes(label: str) -> None:
    assert ROUTE_RE.match(label)


@pytest.mark.parametrize("label", ["$/tn", "TCE", "Lump Sum", "Freight Rate", ""])
def test_route_re_rejects_non_routes(label: str) -> None:
    assert not ROUTE_RE.match(label)


# ── get_routes ──────────────────────────────────────────────────────────────────

async def test_get_routes_filters_and_dedupes() -> None:
    page = FakePage(
        radios=[
            "TD02: Ras Tanura to Singapore",
            "TD03: Ras Tanura to Ningbo",
            "TD02: Ras Tanura to Singapore",  # duplicate
            "$/tn",  # not a route
            "TCE",  # not a route
        ]
    )
    routes = await get_routes(page)
    assert routes == [
        "TD02: Ras Tanura to Singapore",
        "TD03: Ras Tanura to Ningbo",
    ]


# ── get_vessel_classes ──────────────────────────────────────────────────────────

async def test_get_vessel_classes_dedupes_and_filters_tanker() -> None:
    page = FakePage(
        menu_items=[
            {"text": "VLCC (Dirty Tanker)"},
            {"text": "VLCC (Dirty Tanker)"},  # duplicate
            {"text": "Suezmax (Dirty Tanker)"},
            {"text": "Some Other Option"},  # no "Tanker" -> excluded
        ]
    )
    classes = await get_vessel_classes(page, timeout=1000)
    assert classes == ["VLCC (Dirty Tanker)", "Suezmax (Dirty Tanker)"]


# ── extract_sections ────────────────────────────────────────────────────────────

async def test_extract_sections_returns_evaluate_result() -> None:
    payload = {"Income": {"Total Voyage Days": {"your_outcome": "1"}}}
    page = FakePage(evaluate_result=payload)
    assert await extract_sections(page) == payload


async def test_extract_sections_resilient_returns_data() -> None:
    payload = {"Income": {"Total Voyage Days": {"your_outcome": "1"}}}
    page = FakePage(evaluate_result=payload)
    assert await extract_sections_resilient(page) == payload


async def test_extract_sections_resilient_gives_up_empty() -> None:
    """When data never appears, it returns empty after exhausting retries."""
    page = FakePage(evaluate_result={})
    assert await extract_sections_resilient(page, retries=2) == {}


# ── select_route ────────────────────────────────────────────────────────────────

async def test_select_route_clicks_matching_radio() -> None:
    page = FakePage(radios=["TD02: Ras Tanura to Singapore", "TD03: x"])
    # Should not raise
    await select_route(page, "TD03")


async def test_select_route_unknown_raises() -> None:
    page = FakePage(radios=["TD02: x"])
    with pytest.raises(ValueError, match="not found"):
        await select_route(page, "ZZ99")


# ── select_vessel_class ─────────────────────────────────────────────────────────

async def test_select_vessel_class_clicks_via_bounding_box() -> None:
    page = FakePage(
        menu_items=[
            {"text": "VLCC (Dirty Tanker)", "visible": True,
             "box": {"x": 10, "y": 20, "width": 100, "height": 20}},
            {"text": "Suezmax (Dirty Tanker)", "visible": True,
             "box": {"x": 10, "y": 50, "width": 100, "height": 20}},
        ]
    )
    await select_vessel_class(page, "Suezmax (Dirty Tanker)", timeout=1000)
    # The mouse should have clicked the centre of the Suezmax box
    assert page.mouse.clicks == [(60.0, 60.0)]


async def test_select_vessel_class_not_selectable_raises() -> None:
    page = FakePage(menu_items=[{"text": "VLCC (Dirty Tanker)", "visible": True}])
    with pytest.raises(ValueError, match="not selectable"):
        await select_vessel_class(page, "Nonexistent (Clean Tanker)", timeout=1000)


# ── wait_for_app_ready ──────────────────────────────────────────────────────────

async def test_wait_for_app_ready_ok() -> None:
    page = FakePage()
    # FakeLocator.wait_for never raises -> should complete
    await wait_for_app_ready(page, timeout=1000)


async def test_wait_for_app_ready_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the Settings button times out, it falls back to any button."""
    page = FakePage()
    calls = {"n": 0}

    real_get_by_role = page.get_by_role

    def flaky_get_by_role(role: str, *, name: str = ""):  # noqa: ANN202
        calls["n"] += 1
        loc = real_get_by_role(role, name=name)

        async def boom(**_: object) -> None:
            raise scraper.PlaywrightTimeout("timeout")

        loc.wait_for = boom  # type: ignore[assignment]
        return loc

    monkeypatch.setattr(page, "get_by_role", flaky_get_by_role)
    # Should fall back to page.locator("button").first.wait_for (no raise)
    await wait_for_app_ready(page, timeout=10)
