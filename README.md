# Baltic Exchange TCE Scraper

A **Playwright**-based scraper for
**https://emissions.research.balticexchange.com/earnings/tce**.

For every **VesselClass** and every **TD/TC route**, it extracts all data
sections on the page (Income, Canals, Ports, Navigation Consumption, …) with
their **Name | Your Outcome | Baltic Outcome | Difference** columns, and exports
them to **JSON** (nested dictionary) and/or **Excel** (one sheet per route).

---

## Context: what is this?

The [Baltic Exchange](https://www.balticexchange.com/) is the world's reference
body for maritime freight pricing. Its **TCE Earnings** (Time Charter
Equivalent) tool compares the economic performance of *your vessel* against the
Baltic's *benchmark vessel* on a given route.

- **TCE (Time Charter Equivalent):** the daily-equivalent income ($/day) of a
  voyage — the standard metric for comparing spot voyages.
- **VesselClass:** vessel type (VLCC, Suezmax, Aframax, …). Each class exposes a
  different set of available routes.
- **TD / TC routes:** Baltic route codes. `TD*` = *Dirty* (crude / fuel oil),
  `TC*` = *Clean* (refined products).
- **Your Outcome vs Baltic Outcome vs Difference:** your result, the benchmark's
  result, and the difference, for every line item (income, canals, ports,
  navigation consumption, emissions, …).

The site is built with [Anvil](https://anvil.works/) (a Python framework that
renders a SPA over WebSockets). **There is no public REST API** and the content
is generated dynamically in the browser, so a static HTTP scraper does not work:
**a headless browser (Playwright) is mandatory**.

---

## Data freshness & reliability

The Baltic Exchange publishes its route assessments **once per UK business day**
(Monday–Friday, excluding London public holidays). Each morning a panel of
shipbrokers submits assessments, and the Baltic publishes the consolidated
figures later the same day (typically in the **afternoon, London/UK time**).

- **Update cadence:** **daily on business days** — not intraday, not monthly.
  Numbers do **not** change over the weekend or on UK holidays.
- **This tool:** the TCE calculator recomputes its output from the **latest
  daily Baltic assessment**, so re-running the scraper on the same day generally
  yields the same numbers; new figures appear the next business day.
- **No on-page timestamp:** the page does **not** display an explicit
  "last updated" date, so the scraper does not capture one. Treat the data as
  *"the most recent business-day assessment at the time of scraping"*. For an
  auditable history, run the scraper on a daily schedule (the output filename is
  timestamped, e.g. `baltic_tce_YYYYMMDD_HHMMSS.json`).
- **Reliability:** values come straight from the Baltic's official tool — they
  are as authoritative as the Baltic's published assessments. The scraper reads
  the rendered figures verbatim (as strings, e.g. `"$46,997.73"`), so no
  rounding or transformation is introduced.

> **Recommended schedule:** run once per business day, after the daily
> publication (late afternoon UK time) to capture that day's assessment.

---

## Data dictionary — what each field means

Every route returns the same **12 sections**. Each section is a table whose rows
are line items, and whose three value columns are:

| Column | Meaning |
|---|---|
| **Your Outcome** | Result computed for *your* configured vessel (Settings). |
| **Baltic Outcome** | Result for the Baltic's standard **benchmark** vessel. |
| **Difference** | `Your Outcome − Baltic Outcome` (per line item). |

### Sections

| Section | What it covers |
|---|---|
| **Time Charter Equivalent (TCE) Outcome** | Headline result: the voyage's TCE in `$/day`. |
| **Income** | Revenue side: TCE `$/day`, total voyage days, freight/earnings components. |
| **Expenses** | Total and additional voyage expenses (`$`). |
| **Emissions** | Regulatory carbon costs: **FuelEU** (FuelEU Maritime penalty) and **ETS** (EU Emissions Trading System cost). |
| **Canals** | Canal dues — **Suez Canal Dues** and **Non-Suez Canal Dues** (`$`). |
| **Ports** | **Load Port Charges** and **Discharge Port Charges** (`$`). |
| **Navigation Consumption** | Fuel cost while sailing, split **ECA** vs **Non-ECA** (Emission Control Areas), plus per-tonne fuel prices (`$/tn`). |
| **Navigation Days** | Days at sea, split ballast/laden and ECA/Non-ECA. |
| **Idle Consumption** | Fuel use & rates while idle/waiting. |
| **Loading Consumption** | Fuel use & rates during cargo loading. |
| **Discharging Consumption** | Fuel use & rates during cargo discharge. |
| **Heating Consumption** | Fuel use & rates for cargo heating. |

### Common terms in row names

| Term | Meaning |
|---|---|
| **Ballast** | Sailing empty (no cargo) toward the load port. |
| **Laden** | Sailing loaded with cargo. |
| **ECA / Non-ECA** | Emission Control Area (stricter, low-sulphur fuel) vs outside it. |
| **VLSFOeq** | Very Low Sulphur Fuel Oil equivalent — fuel normalised to a common basis. |
| **$/day** | Time-charter-equivalent daily rate. |
| **$/tn** | Price per tonne of fuel. |
| **GRT** | Gross Registered Tonnage (vessel size measure). |

---

## The route map (37 routes, 9 vessel classes)

Each route only appears under its VesselClass. This mapping is cached in
[`src/baltic_scraper/route_map.py`](src/baltic_scraper/route_map.py) so the
scraper can jump straight to the right class when specific routes are requested:

| VesselClass | Type | Routes |
|---|---|---|
| VLCC | Dirty | TD02, TD03, TD15, TD22 |
| Suezmax | Dirty | TD06, TD20, TD23, TD27 |
| Aframax | Dirty | TD07, TD08, TD09, TD14, TD19, TD25, TD26 |
| LR2 | Clean | TC15, TC01, TC20 |
| Panamax | Dirty | TD12, TD21 |
| LR1 | Clean | TC05, TC08, TC16 |
| MR | Clean | TC02, TC07, TC10, TC11, TC12, TC14, TC17, TC18, TC19, TC21, TC22 |
| Handysize | Dirty | TD18 |
| Handysize | Clean | TC06, TC23 |

> To regenerate the map if the site changes: `baltic-scraper --list`.

---

## Installation

**Requirements:** Python ≥ 3.11. [uv](https://docs.astral.sh/uv/) recommended.

Installation has **two parts** — this trips people up: the Python library alone
is not enough, Playwright also needs an actual browser to drive.

By default the scraper drives **Microsoft Edge** (`--channel msedge`), which is
pre-installed on every Windows machine, so there is **nothing extra to
download** — you only install the Python library:

```bash
# Just the Python library + dependencies (Edge is used by default)
uv pip install --system -e ".[dev]"     # or: pip install -e ".[dev]"
```

Prefer Playwright's bundled Chromium instead? Download it once and pass
`--channel chromium`:

```bash
playwright install chromium              # one-time, ~150 MB
baltic-scraper --channel chromium --format both
```

### Do I need to "install Playwright" on the machine?

- **The Python library** (`playwright`) is a normal pip/uv dependency — it
  installs into your Python environment, like any other package. It is **not** a
  system-wide program. This is the only thing you must install.
- **The browser**: by **default the scraper uses your system Microsoft Edge**
  (`msedge`), so no browser download is needed. Alternatively you can:
  - download Playwright's self-contained Chromium with `playwright install
    chromium` (lands in a cache folder, e.g. `%LOCALAPPDATA%\ms-playwright` on
    Windows — **not** a system install) and run with `--channel chromium`, or
  - use system Chrome with `--channel chrome`.

So on a typical Windows machine you can run **right after installing the Python
library**, with no extra download:

```bash
baltic-scraper --format both        # uses Edge by default
```

---

## Usage

There are three equivalent ways to run it — they all accept the **same
arguments**:

```bash
# 1) As an installed command
baltic-scraper --format both

# 2) As a Python module (no console-script needed)
python -m baltic_scraper --format both

# 3) As a plain Python app, straight from a checkout (no install required)
python run.py --format both
```

`run.py` adds `src/` to the path itself, so it works even before
`pip install`. Examples below use `baltic-scraper`, but any of the three forms
works identically.

```bash
# Everything: all 37 routes across all 9 vessel classes (JSON + Excel)
baltic-scraper --format both

# Specific routes only (jumps straight to each route's vessel class)
baltic-scraper --routes "TD02,TD06,TC05"

# Via a config file
baltic-scraper --config config.toml

# Substring filters (discovery mode)
baltic-scraper --vessel-class VLCC          # VLCC class only
baltic-scraper --route TD02                 # routes containing "TD02"

# List available classes and routes (no data scraping)
baltic-scraper --list

# Watch the browser + log to a file
baltic-scraper --routes TD02 --headed --log-file logs/run.log

# Debug selectors: save HTML/PNG snapshots and network requests
baltic-scraper --debug --capture-network --headed -vc VLCC -r TD02
```

### Options

| Flag | Description |
|------|-------------|
| `--config FILE`, `-c` | TOML config file (see `config.toml`) |
| `--routes CODES` | Comma-separated route codes (`TD02,TC05`). Uses the route map to jump straight to each vessel class |
| `--vessel-class FILTER`, `-vc` | Substring filter for vessel class |
| `--route FILTER`, `-r` | Substring filter for route name |
| `--format {json,excel,both}` | Output format (default `json`) |
| `--output FILE`, `-o` | JSON path (default: auto-timestamped) |
| `--output-excel FILE`, `-oe` | Excel path (default: auto-timestamped) |
| `--log-file FILE` | Write the execution log to a file |
| `--list` | List classes and routes only; do not scrape |
| `--headed` | Show the browser window |
| `--channel {chromium,chrome,msedge}` | Which browser to drive. **Default `msedge`** (system Edge, no download). Use `chromium` for Playwright's bundled browser, or `chrome` for system Chrome |
| `--timeout MS` | Element-wait timeout (default 20000) |
| `--debug` | Save HTML/PNG snapshots to `output/` |
| `--capture-network` | Log XHR/fetch/WebSocket to `output/api_calls.json` |
| `--quiet`, `-q` | Silence the console (file logging still happens) |

### Configuration file (`config.toml`)

```toml
[scrape]
routes = "all"            # "all" or ["TD02", "TC05", ...]
format = "both"           # "json" | "excel" | "both"

[output]
json_path = ""            # empty = auto-timestamped name
excel_path = ""

[browser]
headed = false
timeout = 20000
channel = "msedge"        # default: system Edge. "chromium" = bundled; "chrome" = system Chrome

[logging]
file = "logs/baltic_scraper.log"   # empty = console only
```

CLI flags take priority over the config file.

---

## Performance — how long does it take?

Measured on a full run of all 37 routes:

| Metric | Time |
|---|---|
| **Full run (37 routes)** | **~3.8 min (~229 s)** |
| **Per route** (select + extract) | **~4 s** (median; 3–13 s) |
| **Per vessel-class switch** | **~9 s** (Settings → select → OK → refresh) |

Rough formula: `total ≈ 9 × (class switch) + N × (4 s per route)`.
A single targeted route (`--routes TD02`) takes ~20 s (initial load ~10 s +
class switch ~9 s + route ~4 s).

The time is dominated by **fixed waits** that let Anvil's async grids render, not
by the browser itself.

---

## Architecture & logic

`src/baltic_scraper/` package (src-layout, installable):

```
src/baltic_scraper/
├── cli.py            # CLI + orchestration (discovery mode / targeted mode)
├── scraper.py        # Playwright interaction layer + extraction (injected JS)
├── route_map.py      # Cached route -> vessel class map (+ grouping)
├── config.py         # TOML loader -> ScrapeConfig (BOM-tolerant)
├── output.py         # JSON and Excel serializers
└── logging_setup.py  # Console and/or file logger
```

### Two scraping modes

1. **Targeted mode** (`--routes` / config with a list) — the efficient path.
   `route_map.group_routes_by_vessel_class()` groups the requested codes by
   their VesselClass, so each class is selected **once** and only its requested
   routes are scraped. Requesting 8 routes never walks all 9 classes.

2. **Discovery mode** (`--list`, `--vessel-class`, or no filters) — opens
   Settings, reads all VesselClasses, and discovers each one's routes live.
   Useful for regenerating the map or sweeping everything.

### How the site is driven (the non-obvious bits)

The site is Anvil (Material-3). Learned by DOM inspection:

- The class selector lives in an `#alert-modal` modal (the **Settings** button).
- The dropdown is `.anvil-m3-dropdownMenu-container` (text "Vessel Class"), not a
  `<select>`. Its options are `.anvil-m3-menuItem-labelText`.
- **Selecting an option requires a coordinate click** (`mouse.click` on the
  centre of the bounding box); a normal `locator.click` does not trigger Anvil's
  handler.
- You must click **OK** to apply and return to the main screen; the route radios
  then refresh **asynchronously**.
- Routes are `input[type=radio]`, filtered with the pattern `T[DC]\d+:` to
  discard other radios ("$/tn", "TCE", "Lump Sum", …).
- Each section is an `.anvil-data-grid`. Extraction (injected JS, `_EXTRACT_JS`)
  walks the grids, reads the `.anvil-data-row-panel`/`.anvil-data-row-col` rows,
  and associates each grid with the section title (`span.anvil-label-text`
  outside any grid) that precedes it in document order.

### Resilience ("get the data no matter what")

- **Retries in `select_route`:** after a class switch the routes take time to
  refresh; it polls up to 6 times before giving up.
- **Resilient extraction:** `extract_sections_resilient` retries when it gets 0
  sections (grids render late).
- **Error isolation:** each vessel class and each route runs in its own
  `try/except`; a failure is logged and skipped, never aborting the run.
- **Guaranteed save:** a `try/finally` persists whatever was collected **even if
  the run fails midway**.
- **UTF-8 / BOM:** console output forced to UTF-8 (Windows) and config loading is
  BOM-tolerant.

---

## Output format

### JSON (nested dictionary)

```json
{
  "VLCC (Dirty Tanker)": {
    "TD02: Ras Tanura to Singapore": {
      "Time Charter Equivalent (TCE) Outcome": {
        "Time Charter Equivalent ($/day)": {
          "your_outcome": "$46,997.73",
          "baltic_outcome": "$46,997.73",
          "difference": "$0.00"
        }
      },
      "Income": { "Total Voyage Days": { "your_outcome": "34.706", "...": "..." } },
      "Canals": { "...": {} },
      "Ports": { "...": {} },
      "Navigation Consumption": { "...": {} }
    }
  }
}
```

### Excel

**One sheet per route** (`TD02`, `TD06`, …). Inside: a header with the route
name and the vessel class, then per section a styled
**Name | Your Outcome | Baltic Outcome | Difference** table.

---

## Development, lint & tests

```bash
ruff check .                 # linter (config in pyproject.toml)
ruff check --fix .           # autofix

pytest                       # tests (with coverage, fail_under = 80%)
pytest --cov-report=html     # HTML report in htmlcov/
```

- **Coverage:** > 85% (minimum 80%, configured in `pyproject.toml`).
- Tests use async Playwright *fakes* (`tests/conftest.py`), no real browser, so
  they run in ~2 s.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `No vessel classes detected` | The Anvil app failed to load. Run with `--headed` to watch the browser. |
| A route returns 0 sections | Grids did not render in time. Raise `--timeout 40000`; automatic retries already help. |
| `Route 'X' not found` | The code does not exist or the site changed its mapping. Check with `--list`. |
| `TOMLDecodeError` | The config has invalid TOML (BOM is already tolerated). Check the syntax. |
| `playwright ... executable doesn't exist` | Browser binary missing: `playwright install chromium`. |
| Broken selectors after a site redesign | Run `--debug --headed` (saves HTML/PNG to `output/`) and inspect; the extraction JS is in `_EXTRACT_JS` (`scraper.py`). |

---

## Note on "less intrusive" scraping

The site uses Anvil's proprietary WebSockets; **no public REST API** returning
this data was found. A real browser is therefore the only reliable route. To
minimise footprint:

- It runs **headless** by default (no window).
- **Targeted mode** (`--routes`) visits only what is needed.
- `--capture-network` lets you inspect whether a usable HTTP endpoint ever
  appears in the future.
