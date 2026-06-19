"""
Cached mapping of every Baltic route to its parent VesselClass.

On the live site each VesselClass (selected in Settings) reveals a
different set of TD/TC routes.  To scrape a *specific* route you must
first select its VesselClass.  This module stores that relationship so a
targeted run can jump straight to the right VesselClass without probing
all nine of them.

The map was produced by a full discovery run (``baltic-scraper --list``)
and can be regenerated at any time with
:func:`baltic_scraper.route_map.build_route_map`.
"""

from __future__ import annotations

# route code -> full route label (as shown in the radio button)
ROUTE_NAMES: dict[str, str] = {
    # VLCC (Dirty Tanker)
    "TD02": "TD02: Ras Tanura to Singapore",
    "TD03": "TD03: Ras Tanura to Ningbo",
    "TD15": "TD15: Serpentina to Ningbo",
    "TD22": "TD22: Galveston to Ningbo",
    # Suezmax (Dirty Tanker)
    "TD06": "TD06: CPC Marine Terminal to Augusta",
    "TD20": "TD20: Offshore Bonny to Rotterdam",
    "TD23": "TD23: Basrah to Lavera",
    "TD27": "TD27: Rotterdam to Guyana",
    # Aframax (Dirty Tanker)
    "TD07": "TD07: Hound Point to Wilhelmshaven",
    "TD08": "TD08: Mina Al Ahmadi to Singapore",
    "TD09": "TD09: Covenas to Corpus Christi",
    "TD14": "TD14: Seria to Brisbane",
    "TD19": "TD19: Ceyhan to Lavera",
    "TD25": "TD25: Houston to Rotterdam",
    "TD26": "TD26: Dos Bocas to Houston",
    # LR2 (Clean Tanker)
    "TC15": "TC15: Skikda to Chiba",
    "TC01": "TC01: Ras Tanura to Yokohama",
    "TC20": "TC20: Jubail to Rotterdam",
    # Panamax (Dirty Tanker)
    "TD12": "TD12: Antwerp to US Gulf",
    "TD21": "TD21: Mamonal to Houston",
    # LR1 (Clean Tanker)
    "TC05": "TC05: Ras Tanura to Yokohama",
    "TC08": "TC08: Jubail to Rotterdam",
    "TC16": "TC16: Amsterdam to Lome",
    # MR (Clean Tanker)
    "TC02": "TC02: Rotterdam to New York",
    "TC10": "TC10: Yosu to Los Angeles",
    "TC11": "TC11: Yosu to Singapore",
    "TC07": "TC07: Singapore to Sydney",
    "TC14": "TC14: Houston to Amsterdam",
    "TC12": "TC12: Jamangar to Chiba",
    "TC17": "TC17: Jubail to Dar-es-Salaam",
    "TC18": "TC18: Houston to Santos",
    "TC19": "TC19: Lagos to Amsterdam",
    "TC21": "TC21: US Gulf to Caribbean",
    "TC22": "TC22: Yeosu to Botany Bay",
    # Handysize (Dirty Tanker)
    "TD18": "TD18: Baltic to UK-Cont.",
    # Handysize (Clean Tanker)
    "TC06": "TC06: Skikda to Lavera",
    "TC23": "TC23: ARA to UK-Cont.",
}

# route code -> VesselClass label (the order also drives selection grouping)
ROUTE_TO_VESSEL_CLASS: dict[str, str] = {
    # VLCC (Dirty Tanker)
    "TD02": "VLCC (Dirty Tanker)",
    "TD03": "VLCC (Dirty Tanker)",
    "TD15": "VLCC (Dirty Tanker)",
    "TD22": "VLCC (Dirty Tanker)",
    # Suezmax (Dirty Tanker)
    "TD06": "Suezmax (Dirty Tanker)",
    "TD20": "Suezmax (Dirty Tanker)",
    "TD23": "Suezmax (Dirty Tanker)",
    "TD27": "Suezmax (Dirty Tanker)",
    # Aframax (Dirty Tanker)
    "TD07": "Aframax (Dirty Tanker)",
    "TD08": "Aframax (Dirty Tanker)",
    "TD09": "Aframax (Dirty Tanker)",
    "TD14": "Aframax (Dirty Tanker)",
    "TD19": "Aframax (Dirty Tanker)",
    "TD25": "Aframax (Dirty Tanker)",
    "TD26": "Aframax (Dirty Tanker)",
    # LR2 (Clean Tanker)
    "TC15": "LR2 (Clean Tanker)",
    "TC01": "LR2 (Clean Tanker)",
    "TC20": "LR2 (Clean Tanker)",
    # Panamax (Dirty Tanker)
    "TD12": "Panamax (Dirty Tanker)",
    "TD21": "Panamax (Dirty Tanker)",
    # LR1 (Clean Tanker)
    "TC05": "LR1 (Clean Tanker)",
    "TC08": "LR1 (Clean Tanker)",
    "TC16": "LR1 (Clean Tanker)",
    # MR (Clean Tanker)
    "TC02": "MR (Clean Tanker)",
    "TC10": "MR (Clean Tanker)",
    "TC11": "MR (Clean Tanker)",
    "TC07": "MR (Clean Tanker)",
    "TC14": "MR (Clean Tanker)",
    "TC12": "MR (Clean Tanker)",
    "TC17": "MR (Clean Tanker)",
    "TC18": "MR (Clean Tanker)",
    "TC19": "MR (Clean Tanker)",
    "TC21": "MR (Clean Tanker)",
    "TC22": "MR (Clean Tanker)",
    # Handysize (Dirty Tanker)
    "TD18": "Handysize (Dirty Tanker)",
    # Handysize (Clean Tanker)
    "TC06": "Handysize (Clean Tanker)",
    "TC23": "Handysize (Clean Tanker)",
}


def group_routes_by_vessel_class(
    route_codes: list[str],
) -> dict[str, list[str]]:
    """
    Group requested route codes by their parent VesselClass.

    This lets a targeted run select each VesselClass only once and scrape
    all of its requested routes before moving on.

    Parameters
    ----------
    route_codes : list of str
        Route codes such as ``["TD02", "TC05", "TD06"]`` (case-insensitive).

    Returns
    -------
    dict
        ``{vessel_class: [route_code, ...]}`` preserving the order in which
        vessel classes first appear.

    Raises
    ------
    KeyError
        If a route code is not present in :data:`ROUTE_TO_VESSEL_CLASS`.
    """
    grouped: dict[str, list[str]] = {}
    for raw in route_codes:
        code = raw.strip().upper()
        if code not in ROUTE_TO_VESSEL_CLASS:
            msg = (
                f"Unknown route code '{code}'. "
                f"Known codes: {', '.join(sorted(ROUTE_TO_VESSEL_CLASS))}"
            )
            raise KeyError(msg)
        vc = ROUTE_TO_VESSEL_CLASS[code]
        grouped.setdefault(vc, []).append(code)
    return grouped


def all_route_codes() -> list[str]:
    """
    Return every known route code, in canonical map order.

    Returns
    -------
    list of str
        All route codes.
    """
    return list(ROUTE_TO_VESSEL_CLASS.keys())
