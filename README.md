# Gas Tenor & VWAP Engine

> ✅ **OFFICIAL / canonical location of the engine code.** Work here.
> (A copy of these `.py` files also exists in the parent `GAS/` folder as the
> "integrated-with-pre-production" snapshot; it is left in place but is **not**
> the source of truth.)


Post-production toolkit that turns a flat book of gas trades into **tenor-tagged
VWAP curves**. It is split into small, single-responsibility classes — one
produces the result the next consumes — so tenor logic, Outright pricing and
Spread pricing never get tangled.

> This folder is a **self-contained copy** of the post-production engines, kept
> apart from the pre-production pipeline (`gas_production_processor.py`,
> `tools_gas.py`, the production notebook) on purpose. For the full
> Data-Engineering PRD (classification rules, acceptance criteria, decisions),
> see the top-level `README.md`.

---

## 1. Architecture

```
raw trades
  │
  │  ┌── GasTenorProcessor  (tenor generation) ─────────────────────────────┐
  ├─►│ clean → classify periodicity → compute tenor1 / tenor2               │
  │  │ run() → enriched df_trades (+ periodicity_2, tenor, tenor2)          │
  │  └─────────────────────────────────────────────────────────────────────┘
  │            │
  │            ├──►  GasOutrightEngine    (Outright VWAP, on demand)
  │            └──►  GasSpreadEngine  (Spread VWAP, independent)
```

| File | Class | Responsibility |
|---|---|---|
| `periodicity_tenor_generation.py` | **`GasTenorProcessor`** | Enrich trades with the corrected periodicity (`periodicity_2`) and the two tenors (`tenor`=tenor1, `tenor2`). `run()` returns the enriched trades. Detailed docs: **[`README_TENOR.md`](./README_TENOR.md)**. *(`GasEMEAProcessor` kept as an alias.)* |
| `outright_vwap_engine.py` | **`GasOutrightEngine`** | **Outright** VWAP. Consumes the enriched trades; groups by `tenor1`/`tenor2`, by selected **instrument** (Future/Spot Fwd/Option, combinable), GAS/LNG split by `DEAL_TYPE`. |
| `spread_vwap_engine.py` | **`GasSpreadEngine`** | **Spread** VWAP (independent). Resolves the differential and VWAPs it. |
| `periodicity_2_processor.py` | `Periodicity2Processor` | Audit tool: recomputes `PERIODICITY_2` from dates and lists rows where it disagrees with the production `PERIODICITY`. |

The three engines only need `pandas` / `numpy` and do **not** import each other —
they communicate through the enriched DataFrame.

**Region is implicit.** EMEA, APAC and AMERICAS share the same gas column structure,
so the engines are region-agnostic — run them once per regional dataset. The product
name (`CLASSIFICATION_1`) and the region split are built **upstream** in pre-production;
here you only pass the matching `seasonal_type` (ROW for EMEA/APAC, US for AMERICAS).

---

## 2. Input data — gas trade structure

Every engine consumes a **flat, per-trade table** (one row per trade leg; rows can
be **duplicated per `DEAL_ID`**). EMEA, APAC and AMERICAS share this same column
layout — that is why the engines are region-agnostic. The columns, grouped by role:

**Identity & timing**

| Column | Meaning |
|---|---|
| `DEAL_ID` | Unique deal id. Rows are duplicated per `DEAL_ID` → **dedupe before summing**. |
| `DEAL_EXECUTION_DATETIME` | Trade timestamp → the **reference date** (and the hours window). |
| `IS_DELETED` | Soft-delete flag; `TRUE` rows are dropped. |

**Price & volume (the VWAP inputs)**

| Column | Meaning |
|---|---|
| `DEAL_PRICE` | Trade price. For options = the **premium**; for a booked spread (form A) = the **differential**. |
| `QUANTITY` | Trade volume — the **VWAP weight**. |
| `QUANTITY_UNIT` / `PRICE_UNIT` / `QUANTITY_FREQ` | Units; must match across the legs of a spread chain before they can be combined. |

**What is traded (classification)**

| Column | Meaning |
|---|---|
| `CLASSIFICATION_1` | The gas **product** (NBP, TTF, PSV, THE, …) → the product label. |
| `CLASSIFICATION_2` | Second product; when present it marks a **booked spread** (form A). |
| `DEAL_TYPE` | **GAS vs LNG** — the only thing that splits the two; kept in separate rows by default. |
| `TRANSACTION_TYPE_ISDA` | **Instrument**: `Future`, `Spot Fwd` or `Option` (selectable & combinable). |
| `STRATEGY` | `Outright` vs `Spread` → routes the trade to the Outright or Spread engine. |
| `CONTRACT_START_DATE` / `CONTRACT_END_DATE` | Delivery period → the periodicity & tenor (Stage 1). |
| `PERIODICITY` / `TERM_DESCRIPTION` | Periodicity hint; raw term used for `tenor2`. |

**Options**

| Column | Meaning |
|---|---|
| `OPTION_TYPE`, `STRIKE_PRICE_PER_UNIT`, `OPTION_EXPIRY`, `OPTION_STYLE` | The exact option contract; a premium is only comparable within one such contract. |

**Spreads (form B — two legs)**

| Column | Meaning |
|---|---|
| `EXECUTION_CHAIN_ID` | Links the two legs of an unbooked spread. |
| `BUYER_GCD_ID` / `SELLER_GCD_ID` | Counterparty codes; the firm is buyer on one leg, seller on the other (swapped) → used to **pair** the legs. |

---

## 3. Stage 1 — tenor generation (`GasTenorProcessor`)

The first stage enriches every trade with `periodicity_2`, `tenor` (= `tenor1`)
and `tenor2`, derived from the contract dates. Its full logic — the mapping
matrix, the BOM/BOW balance buckets and the configurable tolerances — has its own
document: **[`README_TENOR.md`](./README_TENOR.md)**.

In short, `run()` adds three columns the engines then group by:

- **`periodicity_2`** — date-derived bucket (Daily, Weekend, Weekly, Monthly,
  Quarterly, Seasonal, Annual, or the balance refinements BOM / BOW).
- **`tenor`** (= `tenor1`) — the canonical market tenor (`Custom` merges the odd
  periods).
- **`tenor2`** — same, but `Custom` → raw `TERM_DESCRIPTION` (odd periods stay
  distinguishable).

The engines then let you VWAP **by `tenor1` or `tenor2`** on demand.

---

## 4. Stage 2a — Outright VWAP (`GasOutrightEngine`)

Consumes the enriched trades and computes VWAPs **on demand**. Only **Outright**
trades are aggregated (Spreads go to §5); the product label is always
`CLASSIFICATION_1`.

**Flow**

```
enriched trades
  → _select  : keep Outright, apply instrument + deal_type filters
  → _apply_vwap_filters : drop deals outside the hours/weekdays window (if any)
  → group by [reference_date, (deal_type), product, periodicity_2, tenor]
  → VWAP = Σ(price·qty) / Σ(qty)
```

**Methods (what you can compute)**

| Method | Computes | Price | Extra grouping |
|---|---|---|---|
| `vwap()` | **linear** Outright (Future / Spot Fwd) | `DEAL_PRICE` | — |
| `vwap_options()` | Outright **options** | premium (`DEAL_PRICE`) | option_type, strike, expiry, style |
| `pivot_wide()` | `vwap()` pivoted, products side by side | — | — |

`vwap_options()` is separate so option **premiums are never pooled with forward
prices**; a premium only makes sense within one exact contract, hence the extra
keys.

**Calculation options** (per call unless noted)

| Option | Where | Default | Effect |
|---|---|---|---|
| `by` | `vwap` / `vwap_options` | `"tenor1"` | **tenor1** (every `Custom` merged) vs **tenor2** (`Custom` split by `TERM_DESCRIPTION`). |
| `instruments` | `vwap` (+ `vwap_options`) | `None` = all | Which `TRANSACTION_TYPE_ISDA`. **Combinable**: `["Future","Spot Fwd"]` pools both, one isolates it. ⚠️ Don't pool an **Option** with linear instruments (premium vs forward price). |
| `deal_types` | `vwap` / `vwap_options` | `None` = all | Restrict to a subset of `DEAL_TYPE` (e.g. `["GAS"]`). |
| `combine_deal_types` | `vwap` / `vwap_options` | `False` | `False` → **GAS and LNG in separate rows**. `True` → **pool both** into one VWAP (drops the `deal_type` column). ⚠️ Different commodities / curves — mixing two price series. The product **name** is `CLASSIFICATION_1` either way; GAS/LNG lives only in the `deal_type` column, never in the name. |
| `vwap_filters` | **constructor** | `{}` = all deals | Per-granularity **computation window**: a dict keyed by tenor/bucket → `{"hours": ("08:00","17:00"), "weekdays": ["Mon",…]}`. Restricts *which deals are counted* for that granularity (e.g. "only count Daily deals from 08:00 to 17:00"); buckets not listed keep all deals. |

**Future and Spot Fwd: separate or together.** Because `instruments` is
combinable you choose explicitly:

```python
eng.vwap("tenor1", instruments=["Future"])                 # Future only
eng.vwap("tenor1", instruments=["Spot Fwd"])               # Spot Fwd only
eng.vwap("tenor1", instruments=["Future", "Spot Fwd"])     # both pooled in one VWAP
eng.vwap("tenor1")                                          # None = every instrument
```

The same applies to GAS/LNG (`combine_deal_types`) and to the tenor (`by`) — each
selection is independent, so any combination is valid.

Output: `reference_date, weekday, [deal_type], product, periodicity_2,
<tenor1|tenor2>, vwap, total_volume, trade_count` (the `deal_type` column is
omitted when `combine_deal_types=True`; option output adds the four option keys).

---

## 5. Stage 2b — Spread VWAP (`GasSpreadEngine`, independent)

A spread price is a **differential** (can be negative), so it must never be pooled
into an Outright VWAP.

**Flow**

```
enriched trades
  → _select  : drop IS_DELETED, keep Spread, apply instrument + deal_type filters
  → _dedupe  : one row per DEAL_ID (rows are duplicated)
  → build_spreads : resolve each spread to one priced row (form A + form B)
  → group by [reference_date, deal_type, product, periodicity_2, tenor_near, tenor_far]
  → VWAP = Σ(spread_price·volume) / Σ(volume)
```

A spread is keyed by **both leg tenors** (`tenor_near`, `tenor_far`) — see §6.1.
For a time spread they differ (e.g. `M+1` / `M+2`); for a location spread they are
equal (one delivery period). This is what stops `M+1/M+2` and `Q4/Q1` from
collapsing onto the same row.

**Methods (what you can compute)**

| Method | Computes |
|---|---|
| `build_spreads()` | Resolve every spread to one priced row (no VWAP yet); unusable chains go to `self._unresolved`. |
| `vwap()` | VWAP of **linear** spreads (Future / Spot Fwd). |
| `vwap_options()` | VWAP of **option** spreads (premium differential; multi-leg cases left unresolved for now). |

**Calculation options**

| Option | Where | Default | Effect |
|---|---|---|---|
| `by` | `vwap` / `vwap_options` | `"tenor1"` | tenor1 vs tenor2 (same meaning as Outright). |
| `instruments` | `vwap` (`("Future","Spot Fwd")`) / `vwap_options` (`("Option",)`) / `build_spreads` (`None`) | per method | Which `TRANSACTION_TYPE_ISDA`; combinable, same as Outright. |
| `deal_types` | all three | `None` = all | Restrict `DEAL_TYPE` (GAS/LNG). |
| `own_gcd` | **constructor** | `None` | The firm's GCD code(s). With the fixed `near − far` convention the sign no longer needs it, so it is now only an **optional pairing sanity-check** (firm is buyer on one leg, seller on the other). |
| `contract_start_col` | **constructor** | `"CONTRACT_START_DATE"` | Delivery-start column used to order the near/far legs of a time spread. |

> GAS and LNG are **always** kept in separate rows here (no `combine_deal_types`
> yet — say the word and I mirror the Outright option).

**Two forms of a spread:**

- **Form A** — `CLASSIFICATION_2` has a value → `DEAL_PRICE` is already the
  differential. Product = `"Spread C1_C2"`.
- **Form B** — `CLASSIFICATION_2` is null → two legs share an
  `EXECUTION_CHAIN_ID` (both `STRATEGY=Spread`, one buy + one sell). The
  differential uses a **fixed convention**, not the firm's buy/sell side — see
  §6.2: `price(near) − price(far)` for a time spread, fixed product order for a
  location spread. Both leg prices are in the data, so the value is computed
  directly; `BUYER_GCD_ID` / `SELLER_GCD_ID` only confirm the leg pairing.
  Product = `"Spread C1near_C1far"` (location) or `"Spread C1"` + a `(near, far)`
  tenor pair (time, see §6.1).

Mandatory data hygiene: **(1)** dedupe by `DEAL_ID` (rows are duplicated);
**(2)** skip chains whose legs differ in `QUANTITY_UNIT`/`PRICE_UNIT`/`QUANTITY_FREQ`
(recorded in `_unresolved`); **(3)** GAS vs LNG only by `DEAL_TYPE`.

Output: `reference_date, weekday, deal_type, product, periodicity_2, tenor_near,
tenor_far, vwap, total_volume, trade_count` (`build_spreads()` also exposes
`spread_kind` = `time`/`location` and the `tenor2_*` variants).

**Open items (need real data to finalise):** the volume convention for
unequal-size legs; multi-leg option spreads (>2 legs); the third spread type.

---

## 6. Spreads, tenors & the forward curve (concepts)

This section captures *why* a spread does not fit the outright `(product, tenor)`
model, and how the two relate when building a forward curve.

### 6.1 An outright is a point; a time spread is a slope

- An **outright** is a single point of the forward curve:
  `(product, tenor) → price` — e.g. `(TTF, M+1) = 31.00`.
- A **time / calendar spread** is **not** a single point. It is the **difference
  between two delivery periods of the same product**, so it carries **two
  tenors** — a near (front) leg and a far (back) leg. A single tenor cannot
  describe it; that is the conceptual mismatch.

| | Outright | Time spread | Location spread |
|---|---|---|---|
| Meaning | absolute level at one period | slope between two periods (same product) | basis between two products (same period) |
| Tenor(s) | **one** (`M+1`) | **two** (`near=M+1`, `far=M+2`) | one period, **two products** |
| Natural key | `(product, tenor)` | `(product, tenor_near, tenor_far)` | `(product_near, product_far, tenor)` |
| Price | forward price | differential (can be negative) | differential (can be negative) |

So a time spread should be keyed by **`(product, tenor_near, tenor_far)`**, not by
a single tenor — otherwise `Dec25/Jan26` and `Q4/Q1` would collapse onto the same
`(TTF, …)` row and the VWAP would mix unrelated spreads.

### 6.2 Sign convention — `near − far`, *not* `buy − sell`

For a spread VWAP to be meaningful the sign must follow a **fixed convention**:

- **Time spread:** `price(near) − price(far)` (front − back).
- **Location spread:** a fixed product order (e.g. `TTF − NBP`).

It must **not** use the firm's buy/sell perspective. The same market spread
executed in opposite directions by two firms would otherwise carry opposite signs
and **cancel out in the VWAP**:

```
Firm A buys M+1 / sells M+2  → executes  M+1 − M+2 = −0.46
Firm B buys M+2 / sells M+1  → executes  M+2 − M+1 = +0.46   (same market spread!)
buy−sell VWAP → ≈ 0   (wrong)
near−far VWAP → −0.46 (correct)
```

Because **both leg prices are present in the data**, the differential is computed
directly as `price(near) − price(far)`; `BUYER_GCD_ID` / `SELLER_GCD_ID` are then
only needed to **confirm leg pairing within the chain**, not to set the sign.

### 6.3 Building a forward curve (bootstrapping)

Spreads alone give only the **shape** (slope) of the curve, never the absolute
level. A forward curve is reconstructed by combining **anchor outrights** (front
month, Cal, season) with **spreads**, propagating from a known point:

```
price(far) = price(near) − spread(near, far)

Anchor (GasOutrightEngine):  TTF M+1 = 31.00
Spreads (GasSpreadEngine):   M+1/M+2 = −0.46 ,  M+2/M+3 = −0.30
Bootstrap:
  M+2 = 31.00 − (−0.46) = 31.46
  M+3 = 31.46 − (−0.30) = 31.76
→ Forward curve: M+1 = 31.00, M+2 = 31.46, M+3 = 31.76
```

This is why the two engines are complementary: **`GasOutrightEngine` provides the
anchors/levels, `GasSpreadEngine` provides the increments**, and a curve builder
joins them on the tenor axis. (A dedicated bootstrapper is a possible future
component; today the two VWAP outputs are produced and can be combined downstream.)

---

## 7. Usage

```python
import pandas as pd
from periodicity_tenor_generation import GasTenorProcessor
from outright_vwap_engine import GasOutrightEngine
from spread_vwap_engine import GasSpreadEngine

# 1) Tenor generation (per region with its seasonal_type)
tp = GasTenorProcessor(pd.read_csv("NG_trades_EMEA_SORT.csv"), seasonal_type="ROW")
tp.run()                                        # df_trades + periodicity_2 / tenor / tenor2

# 2) Outright VWAP — on demand
#    (optional per-granularity window: only count Daily deals between 08:00-17:00)
eng = GasOutrightEngine.from_tenor(tp, vwap_filters={"Daily": {"hours": ("08:00", "17:00")}})
v1       = eng.vwap("tenor1")                                     # Custom merged
v2       = eng.vwap("tenor2")                                     # Custom -> TERM_DESCRIPTION
linear   = eng.vwap("tenor1", instruments=["Future", "Spot Fwd"], deal_types=["GAS"])
combined = eng.vwap("tenor1", combine_deal_types=True)           # GAS + LNG pooled in one row
opts     = eng.vwap_options("tenor1")                            # option premiums, on their own

# 3) Spread VWAP — independent
sp = GasSpreadEngine.from_tenor(tp, own_gcd=MY_GCD_ID)
spread_vwap = sp.vwap("tenor1")                                  # linear spreads (Future/Spot Fwd)
opt_spreads = sp.vwap_options("tenor1")                          # option spreads
```

---

## 8. Files

| File | Purpose |
|---|---|
| `periodicity_tenor_generation.py` | `GasTenorProcessor` — tenor & periodicity generation. |
| `outright_vwap_engine.py` | `GasOutrightEngine` — Outright VWAP. |
| `spread_vwap_engine.py` | `GasSpreadEngine` — Spread VWAP. |
| `periodicity_2_processor.py` | `Periodicity2Processor` — periodicity audit. |
| `README.md` | **This file** — engines (Outright + Spread) & data structure. |
| `README_TENOR.md` | Stage 1 tenor generation (`GasTenorProcessor`) in detail. |
