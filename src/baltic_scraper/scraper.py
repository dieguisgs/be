"""
Core scraping logic for Baltic Exchange TCE earnings page.

The site is an Anvil (Python SPA over WebSockets) application, so all
interaction is driven through Playwright Chromium.
"""

from __future__ import annotations

import re
from pathlib import Path

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

URL: str = "https://emissions.research.balticexchange.com/earnings/tce"

# A route radio label looks like "TD02: Ras Tanura to Singapore" or
# "TC05: ...".  This pattern isolates real routes from other radio groups
# on the page (e.g. "$/tn", "TCE", "Lump Sum", "Freight Rate").
ROUTE_RE = re.compile(r"^T[DC]\d+\s*:", re.IGNORECASE)

# Anvil Material-3 component selectors (discovered via DOM inspection).
_DROPDOWN_CONTAINER = ".anvil-m3-dropdownMenu-container"
_MENU_ITEM_LABEL = ".anvil-m3-menuItem-labelText"

# JavaScript injected into the page to extract all section tables.
#
# The earnings page renders each section as an Anvil "data grid":
#
#   .anvil-data-grid
#     .anvil-data-grid-child-panel
#       .anvil-data-row-panel.anvil-auto-grid-header   <- header row
#         .anvil-data-row-col  (Name | Your Outcome | Baltic Outcome | Difference)
#       .anvil-data-row-panel                          <- data rows
#         .anvil-data-row-col  (value cells)
#
# Section titles (dark-blue headers) are `span.anvil-label-text` elements
# that live OUTSIDE any data grid.  Each grid is associated with the
# nearest section title preceding it in document order.
_EXTRACT_JS = r"""() => {
    const result = {};

    // Section-title spans = label spans not inside any data grid, in doc order
    const titleSpans = [...document.querySelectorAll('span.anvil-label-text')]
        .filter(s => !s.closest('.anvil-data-grid'));

    const sectionFor = (grid) => {
        let best = 'Unknown';
        for (const s of titleSpans) {
            // grid follows s  =>  s precedes grid
            if (s.compareDocumentPosition(grid) & Node.DOCUMENT_POSITION_FOLLOWING) {
                const t = s.textContent.trim();
                if (t) best = t;
            } else {
                break;  // titleSpans is ordered; past the grid -> stop
            }
        }
        return best;
    };

    const grids = [...document.querySelectorAll('.anvil-data-grid')];

    for (const grid of grids) {
        // Skip hidden grids (Anvil pre-renders some off-screen)
        if (grid.offsetParent === null) continue;

        const panels = [...grid.querySelectorAll('.anvil-data-row-panel')];
        const rows = {};

        for (const panel of panels) {
            if (panel.classList.contains('anvil-auto-grid-header')) continue;
            const cells = [...panel.querySelectorAll(':scope > .anvil-data-row-col')];
            if (cells.length < 4) continue;
            const name = cells[0].textContent.trim();
            if (!name || name === 'Name') continue;
            rows[name] = {
                your_outcome:   cells[1].textContent.trim(),
                baltic_outcome: cells[2].textContent.trim(),
                difference:     cells[3].textContent.trim()
            };
        }
        if (Object.keys(rows).length === 0) continue;

        let key = sectionFor(grid);
        if (key in result) {
            let idx = 2;
            while (`${key} (${idx})` in result) idx++;
            key = `${key} (${idx})`;
        }
        result[key] = rows;
    }

    return result;
}"""


# ── Wait helpers ───────────────────────────────────────────────────────────────

async def wait_for_app_ready(page: Page, timeout: int) -> None:
    """
    Wait for the Anvil app to complete its initial render.

    Parameters
    ----------
    page : Page
        Active Playwright page pointed at the Baltic Exchange URL.
    timeout : int
        Maximum wait in milliseconds before raising ``PlaywrightTimeout``.

    Raises
    ------
    PlaywrightTimeout
        If neither the Settings button nor any button becomes visible
        within *timeout* milliseconds.
    """
    await page.wait_for_load_state("domcontentloaded")
    try:
        await page.get_by_role("button", name="Settings").wait_for(
            state="visible", timeout=timeout
        )
    except PlaywrightTimeout:
        await page.locator("button").first.wait_for(state="visible", timeout=timeout)


# ── Settings modal ─────────────────────────────────────────────────────────────

async def open_settings(page: Page, timeout: int) -> None:
    """
    Click the Settings button and wait for the modal dropdowns to render.

    The Settings panel is an Anvil ``#alert-modal`` containing several
    Material-3 dropdowns (Vessel Class, Benchmark Speeds, ...).

    Parameters
    ----------
    page : Page
        Active Playwright page.
    timeout : int
        Milliseconds to wait for the Vessel Class dropdown to appear.
    """
    await page.get_by_role("button", name="Settings").click()
    await page.locator(
        _DROPDOWN_CONTAINER, has_text="Vessel Class"
    ).first.wait_for(state="visible", timeout=timeout)
    await page.wait_for_timeout(800)


async def close_settings(page: Page) -> None:
    """
    Close the Settings modal by clicking its ``OK`` button.

    Clicking ``OK`` both applies any selection and returns to the main
    earnings screen.  Escape is used as a fallback.

    Parameters
    ----------
    page : Page
        Active Playwright page with the Settings modal open.
    """
    await page.wait_for_timeout(300)

    ok = page.locator("#alert-modal button", has_text="OK")
    if await ok.count() > 0:
        try:
            await ok.first.click(force=True)
            await page.wait_for_timeout(1500)
            return
        except Exception:  # noqa: BLE001
            pass

    await page.keyboard.press("Escape")
    await page.wait_for_timeout(1000)


# ── VesselClass discovery & selection ─────────────────────────────────────────

async def get_vessel_classes(page: Page, timeout: int) -> list[str]:
    """
    Return all VesselClass labels available in the Settings dropdown.

    Opens the Settings modal, reads the ``<select>`` options (or ARIA
    listbox items), closes the modal.

    Parameters
    ----------
    page : Page
        Active Playwright page.
    timeout : int
        Milliseconds to wait for the Settings modal to open.

    Returns
    -------
    list of str
        Vessel class labels, e.g.
        ``["VLCC (Dirty Tanker)", "Suezmax (Dirty Tanker)", ...]``.
    """
    await open_settings(page, timeout)

    # Vessel class menu items are pre-rendered with this class and contain
    # the word "Tanker".  De-duplicate while preserving order.
    texts = await page.locator(_MENU_ITEM_LABEL).all_text_contents()
    vessel_classes: list[str] = []
    for t in texts:
        t = t.strip()
        if "Tanker" in t and t not in vessel_classes:
            vessel_classes.append(t)

    await close_settings(page)
    return vessel_classes


async def select_vessel_class(page: Page, vessel_class: str, timeout: int) -> None:
    """
    Select a VesselClass in Settings and close the modal.

    The selection sequence (discovered via DOM inspection):

    1. Open Settings.
    2. Click the Vessel Class dropdown container to open its menu.
    3. Click the matching menu item via **mouse coordinates** -- a normal
       ``locator.click`` does not trigger Anvil's selection handler.
    4. Click ``OK`` to apply and return to the main screen.

    Waits 3 s after closing for the route list to refresh.

    Parameters
    ----------
    page : Page
        Active Playwright page.
    vessel_class : str
        Exact label to select, e.g. ``"VLCC (Dirty Tanker)"``.
    timeout : int
        Milliseconds to wait for the Settings modal to open.

    Raises
    ------
    ValueError
        If no visible menu item matches *vessel_class*.
    """
    await open_settings(page, timeout)

    # Open the Vessel Class dropdown menu
    await page.locator(
        _DROPDOWN_CONTAINER, has_text="Vessel Class"
    ).first.click()
    await page.wait_for_timeout(1000)

    # Click the matching, *visible* menu item via its bounding box
    items = page.locator(_MENU_ITEM_LABEL)
    n = await items.count()
    clicked = False
    for i in range(n):
        item = items.nth(i)
        text = (await item.text_content() or "").strip()
        if text != vessel_class:
            continue
        if not await item.is_visible():
            continue
        box = await item.bounding_box()
        if not box:
            continue
        await page.mouse.click(
            box["x"] + box["width"] / 2,
            box["y"] + box["height"] / 2,
        )
        clicked = True
        break

    if not clicked:
        await close_settings(page)
        msg = f"Vessel class '{vessel_class}' not selectable in dropdown."
        raise ValueError(msg)

    await page.wait_for_timeout(1200)
    await close_settings(page)
    await page.wait_for_timeout(3000)


# ── Route discovery & selection ────────────────────────────────────────────────

async def get_routes(page: Page) -> list[str]:
    """
    Return all route names currently visible as radio-button labels.

    Parameters
    ----------
    page : Page
        Active Playwright page with a vessel class already selected.

    Only radio labels matching :data:`ROUTE_RE` (``TD##:`` / ``TC##:``)
    are returned, filtering out unrelated radio groups such as ``$/tn``,
    ``TCE`` or ``Lump Sum``.

    Returns
    -------
    list of str
        Route labels, e.g. ``["TD02: Ras Tanura to Singapore", ...]``.
    """
    radios = page.locator("input[type='radio']")
    count = await radios.count()
    routes: list[str] = []
    for i in range(count):
        label = await radios.nth(i).evaluate(
            """el => {
                if (el.id) {
                    const lbl = document.querySelector(`label[for="${el.id}"]`);
                    if (lbl) return lbl.textContent.trim();
                }
                const ancestor = el.closest('label') || el.parentElement;
                return ancestor ? ancestor.textContent.replace(/\\s+/g, ' ').trim() : '';
            }"""
        )
        label = label.strip()
        if ROUTE_RE.match(label) and label not in routes:
            routes.append(label)
    return routes


async def select_route(page: Page, route_name: str, retries: int = 6) -> None:
    """
    Click the radio button whose label contains *route_name*.

    After a VesselClass change the route radios refresh asynchronously, so
    the target route may not be present immediately.  This polls up to
    *retries* times (1 s apart) before giving up.  Waits 3 s after a
    successful click for the section data to reload.

    Parameters
    ----------
    page : Page
        Active Playwright page.
    route_name : str
        Full or partial route label, e.g. ``"TD02"`` or
        ``"TD02: Ras Tanura to Singapore"``.
    retries : int, optional
        Number of poll attempts while waiting for the route to appear.

    Raises
    ------
    ValueError
        If no radio button label contains *route_name* after all retries.
    """
    label_js = """el => {
        if (el.id) {
            const lbl = document.querySelector(`label[for="${el.id}"]`);
            if (lbl) return lbl.textContent.trim();
        }
        const ancestor = el.closest('label') || el.parentElement;
        return ancestor ? ancestor.textContent.replace(/\\s+/g, ' ').trim() : '';
    }"""

    for attempt in range(retries):
        radios = page.locator("input[type='radio']")
        count = await radios.count()
        for i in range(count):
            radio = radios.nth(i)
            label = await radio.evaluate(label_js)
            if route_name in label.strip():
                await radio.click()
                await page.wait_for_timeout(3000)
                return
        # Route not present yet — the list may still be refreshing
        if attempt < retries - 1:
            await page.wait_for_timeout(1000)

    msg = f"Route '{route_name}' not found. Use --list to see available routes."
    raise ValueError(msg)


# ── Data extraction ────────────────────────────────────────────────────────────

async def extract_sections(page: Page) -> dict[str, dict[str, dict[str, str]]]:
    """
    Extract all section tables for the currently selected route.

    Injects ``_EXTRACT_JS`` into the page and returns the structured
    result.

    Parameters
    ----------
    page : Page
        Active Playwright page with a route selected and data visible.

    Returns
    -------
    dict
        Nested mapping::

            {
                "<Section Name>": {
                    "<Row Name>": {
                        "your_outcome":   "<value>",
                        "baltic_outcome": "<value>",
                        "difference":     "<value>",
                    }
                }
            }

        Example section names: ``"Income"``, ``"Canals"``, ``"Ports"``,
        ``"Navigation Consumption"``.
    """
    return await page.evaluate(_EXTRACT_JS)  # type: ignore[return-value]


async def extract_sections_resilient(
    page: Page, retries: int = 4
) -> dict[str, dict[str, dict[str, str]]]:
    """
    Extract sections, retrying while the result is empty.

    Data grids render asynchronously after a route is selected, so an
    immediate extraction can return nothing.  This polls up to *retries*
    times (1.5 s apart) until at least one section is found.

    Parameters
    ----------
    page : Page
        Active Playwright page with a route selected.
    retries : int, optional
        Maximum extraction attempts.

    Returns
    -------
    dict
        The same structure as :func:`extract_sections`; empty only if every
        attempt yielded no data.
    """
    sections: dict = {}
    for attempt in range(retries):
        sections = await extract_sections(page)
        if sections:
            return sections
        if attempt < retries - 1:
            await page.wait_for_timeout(1500)
    return sections


# ── Debug helpers ──────────────────────────────────────────────────────────────

async def save_dom(page: Page, path: Path) -> None:
    """
    Write the current page HTML to *path*.

    Parameters
    ----------
    page : Page
        Active Playwright page.
    path : Path
        Destination file; written as UTF-8.
    """
    html = await page.content()
    path.write_text(html, encoding="utf-8")
    print(f"  [debug] DOM saved -> {path}")


async def take_screenshot(page: Page, path: Path) -> None:
    """
    Save a full-page PNG screenshot to *path*.

    Parameters
    ----------
    page : Page
        Active Playwright page.
    path : Path
        Destination file path (should end in ``.png``).
    """
    await page.screenshot(path=str(path), full_page=True)
    print(f"  [debug] Screenshot -> {path}")


async def capture_api_calls(page: Page, output_path: Path) -> None:
    """
    Attach a request listener that records XHR, fetch and WebSocket URLs.

    Must be called *before* ``page.goto()``.  After scraping, call
    ``page._api_flush()`` to write the log to *output_path*.

    Parameters
    ----------
    page : Page
        Playwright page (before navigation).
    output_path : Path
        Destination JSON file for request records.
    """
    records: list[dict[str, str]] = []

    def _on_request(req) -> None:  # type: ignore[type-arg]
        if req.resource_type in ("xhr", "fetch", "websocket"):
            records.append({"type": req.resource_type, "url": req.url, "method": req.method})

    page.on("request", _on_request)

    async def flush() -> None:
        output_path.write_text(
            __import__("json").dumps(records, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  [debug] API calls -> {output_path} ({len(records)} requests)")

    page._api_flush = flush  # type: ignore[attr-defined]
