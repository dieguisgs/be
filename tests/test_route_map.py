"""Tests for the cached route -> vessel-class map."""

from __future__ import annotations

import pytest

from baltic_scraper.route_map import (
    ROUTE_NAMES,
    ROUTE_TO_VESSEL_CLASS,
    all_route_codes,
    group_routes_by_vessel_class,
)


def test_every_code_has_a_name() -> None:
    """Each mapped route code must also have a human-readable name."""
    assert set(ROUTE_TO_VESSEL_CLASS) == set(ROUTE_NAMES)


def test_known_route_counts() -> None:
    """Spot-check the discovered totals (37 routes, 9 vessel classes)."""
    assert len(ROUTE_TO_VESSEL_CLASS) == 37
    assert len(set(ROUTE_TO_VESSEL_CLASS.values())) == 9


def test_group_single_vessel_class() -> None:
    """Routes from the same class group together."""
    grouped = group_routes_by_vessel_class(["TD02", "TD03"])
    assert grouped == {"VLCC (Dirty Tanker)": ["TD02", "TD03"]}


def test_group_multiple_vessel_classes_preserves_order() -> None:
    """Different classes appear in first-seen order."""
    grouped = group_routes_by_vessel_class(["TD06", "TD02"])
    assert list(grouped.keys()) == ["Suezmax (Dirty Tanker)", "VLCC (Dirty Tanker)"]
    assert grouped["Suezmax (Dirty Tanker)"] == ["TD06"]


def test_group_is_case_insensitive_and_strips() -> None:
    """Lower-case and padded codes are normalised."""
    grouped = group_routes_by_vessel_class([" td02 ", "tc05"])
    assert grouped == {
        "VLCC (Dirty Tanker)": ["TD02"],
        "LR1 (Clean Tanker)": ["TC05"],
    }


def test_group_unknown_code_raises() -> None:
    """An unknown code raises KeyError with a helpful message."""
    with pytest.raises(KeyError, match="Unknown route code 'ZZ99'"):
        group_routes_by_vessel_class(["ZZ99"])


def test_all_route_codes_matches_map() -> None:
    """all_route_codes returns every key, in order."""
    assert all_route_codes() == list(ROUTE_TO_VESSEL_CLASS.keys())
