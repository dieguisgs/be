# Gas Tenor Processor (`GasTenorProcessor`)

> Companion to the engines README ([`README.md`](./README.md)). This document
> covers **Stage 1 only**: how raw gas trades are turned into tenor labels. The
> VWAP engines that consume the output are documented in `README.md`.

`periodicity_tenor_generation.py` — `class GasTenorProcessor`
(alias `GasEMEAProcessor` kept for backward compatibility).

---

## 1. What it does

It owns **only** the tenor-generation stage. From the raw trades it derives, per
trade, the corrected periodicity bucket and the market tenor(s). It does **not**
compute any VWAP — that is the engines' job (`GasOutrightEngine`,
`GasSpreadEngine`), which consume the enriched frame produced here.

**Flow**

```
raw trades
  → clean / parse dates, set reference_date (= DEAL_EXECUTION_DATETIME)
  → classify periodicity_2 from CONTRACT_START_DATE / CONTRACT_END_DATE (+ tolerances)
  → assign tenor (tenor1) and tenor2
  → run() returns the enriched df_trades
```

`run()` adds **three columns** to every trade:

- **`periodicity_2`** — the date-derived bucket (Daily, Weekend, Weekly, Monthly,
  Quarterly, Seasonal, Annual, or the balance refinements BOM / BOW).
- **`tenor`** (= `tenor1`) — the canonical market tenor.
- **`tenor2`** — same as `tenor1`, but a `Custom` is replaced by its raw
  `TERM_DESCRIPTION`.

---

## 2. Why two tenors

| Column | `Custom` handling | Use it when… |
|---|---|---|
| `tenor` (`tenor1`) | every non-standard period collapses to the single label `Custom` | you want a clean, standardised curve and don't care about the odd shapes. |
| `tenor2` | a `Custom` is replaced by its raw `TERM_DESCRIPTION` | you want to keep the non-standard periods distinguishable (each odd term stays separate). |

Both are computed once and stored; the engines then let you VWAP **by `tenor1` or
by `tenor2`** on demand (the `by=` argument).

---

## 3. The tenor labels

| Bucket (`periodicity_2`) | `tenor1` label | Meaning |
|---|---|---|
| Weekend | `WE`, `WE+1`, … | a Sat–Mon weekend (n weekends ahead). |
| Daily | `D+n` | a single gas day, n days ahead. |
| Weekly | `W`, `W+1`, … | a full Mon–Sun week, n weeks ahead. |
| **BOW** | `BOW` | **Balance of Week** — mid-week start to the end of the *current* week. |
| Monthly | `M+1`, `M+2`, … | a full calendar month, n months ahead (n ≥ 1). |
| **BOM** | `BOM` | **Balance of Month** — mid-month start to month-end, *current* month. |
| Quarterly | `Q+1`, `Q+2`, … | a calendar quarter, n quarters ahead (n ≥ 1). |
| Seasonal | `Sum{yy}` / `Win{yy}` | a summer / winter season. |
| Annual | `Cal{yy}` | a calendar year. |
| Other | `Custom` | matches none of the above (→ `tenor2` = `TERM_DESCRIPTION`). |

`M+0` and `Q+0` do **not** exist: a current/past month or quarter resolves to
`BOM` / `BOW` / `Custom`, never `M+0`.

`BOM` / `BOW` need the **`reference_date`** (the trade date) to know what the
"current" period is, so they can only be computed here — never upstream in
pre-production where the reference date is not available.

---

## 4. Master mapping matrix (dates → bucket → tenor)

The dates `CONTRACT_START_DATE` / `CONTRACT_END_DATE` are matched **top-down,
first match wins**:

| Order | Period (`periodicity_2`) | Detection from dates | Tolerance arg(s) | `tenor1` | `tenor2` |
|---|---|---|---|---|---|
| 0 | *(fallback)* | dates missing / `end < start` | — | column hint, else `Custom` | = `tenor1` / term |
| 1 | **Weekend** | `start=Sat` & `end=Mon` (span 2) | — | `WE` / `WE+n` | = `tenor1` |
| 2 | **Daily** | span `0` or `1` | — | `D+n` | = `tenor1` |
| 3 | **Weekly** | `start≈Mon` & `end≈Sun`/next-Mon | `week_tolerance_days` | `W` / `W+n` | = `tenor1` |
| 3b | **BOW** *(balance of week)* | mid-week start, current week, end≈week-end | `week_balance_tolerance_days` | `BOW` | = `tenor1` |
| 4 | **Monthly** | `start`&`end` ≈ month boundary | `month_tolerance_days` | `M+n` (n≥1) | = `tenor1` |
| 4b | **BOM** *(balance of month)* | mid-month start, end=month boundary, current month | `bom_tolerance_days` | `BOM` | = `tenor1` |
| 5 | **Quarterly** | `start`&`end` ≈ quarter boundary | `quarter_tolerance_days` | `Q+n` (n≥1) | = `tenor1` |
| 6 | **Seasonal** | span 140–220 & season-aligned | `seasonal_type` (+3d) | `Sum{yy}` / `Win{yy}` | = `tenor1` |
| 7 | **Annual** | span 360–368 | — | `Cal{yy}` | = `tenor1` |
| 8 | **Other** | matches none of the above | — | `Custom` | **`TERM_DESCRIPTION`** (else `Custom`) |

---

## 5. Configurable tolerances (the only calculation options)

All options are set **once in the constructor** — they shape the tenor labels, not
a VWAP, so there is no per-call option at this stage. Each margin is applied on
**both** ends of the contract, and **`0` means "exact boundary"**.

| Parameter | Default | Meaning |
|---|---|---|
| `seasonal_type` | `"ROW"` | Season window: `ROW` (EMEA/APAC: Apr–Sep summer / Oct–Mar winter) or `US` (AMERICAS: Apr–Oct summer / Nov–Mar winter). |
| `month_tolerance_days` | `2` | Slack to accept an aligned calendar **month**. Absorbs the usual encodings (end on the 1st of next month, start on the last day of the previous month, start 1–2 days in). |
| `week_tolerance_days` | `1` | Slack to accept an aligned **week** (Mon start, Sun or next-Mon end). |
| `quarter_tolerance_days` | `2` | Slack to accept an aligned **quarter** (boundaries 1-Jan / 1-Apr / 1-Jul / 1-Oct; e.g. 30-Sep→31-Dec, 01-Oct→01-Jan). |
| `bom_tolerance_days` | `3` | Max `start − reference` gap for **Balance-of-Month** (covers next gas day + a weekend). |
| `week_balance_tolerance_days` | `1` | Max `start − reference` gap for **Balance-of-Week**. |

### Off-by-one alignment

Seasonal and Annual labels are anchored with `_nearest_month_start`, so an
end-of-month start such as `30-Sep` is treated as `01-Oct` for the
year/season label (e.g. `30-Sep-25 → 31-Mar-26` is `Win25`, not `Sum25`; and
`31-Dec-25 → 31-Dec-26` is `Cal26`, not `Cal25`).

---

## 6. Input columns used

| Column | Used for |
|---|---|
| `DEAL_EXECUTION_DATETIME` | the **reference date** (defines "current" month/week for BOM/BOW). |
| `CONTRACT_START_DATE` / `CONTRACT_END_DATE` | the delivery period → bucket + tenor. |
| `PERIODICITY` | fallback hint only (the bucket is derived from the dates, not this). |
| `TERM_DESCRIPTION` | the value `tenor2` takes when `tenor1 == Custom`. |

(The columns the *engines* need — price, quantity, instrument, strategy, deal
type, etc. — are listed in `README.md` → *Input data*.)

---

## 7. Usage

```python
import pandas as pd
from periodicity_tenor_generation import GasTenorProcessor

tp = GasTenorProcessor(
    pd.read_csv("NG_trades_EMEA_SORT.csv"),
    seasonal_type="ROW",        # ROW for EMEA/APAC, US for AMERICAS
    quarter_tolerance_days=2,   # any tolerance can be overridden
)
tp.run()
tp.df_trades.head()             # + periodicity_2 / tenor / tenor2
```

The enriched `tp` (or `tp.df_trades`) is then fed straight into the VWAP engines —
see `README.md` for `GasOutrightEngine.from_tenor(tp)` and
`GasSpreadEngine.from_tenor(tp)`.
