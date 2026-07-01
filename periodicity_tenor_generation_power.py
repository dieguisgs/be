"""Power Tenor Processor - product, periodicity & tenor logic (single class).

One class, ``PowerTenorProcessor(df, region_type="ROW"|"AMER")``, handles both
regions: the ROW vs AMER differences are entirely parameterised (start/end
anchoring, season windows, blocks, and the M+0/Q+0 front tenor), so there is no
region-specific logic written twice. Two thin convenience subclasses pin the
region if you prefer an explicit entry point:

  * ``PowerROWTenorProcessor``  = ``region_type="ROW"``
  * ``PowerAMERTenorProcessor`` = ``region_type="AMER"``

This is the power analogue of ``GasTenorProcessor``. It owns ONLY the
tenor-generation stage; the VWAP calculation lives in the separate engines
(``outright_vwap_engine_power.py`` / ``spread_vwap_engine_power.py``), which
consume the enriched trades produced here.

What it does (per trade)
------------------------
1. ``product``       : built here (unlike gas) as COUNTRY_[REGION_]CLASSIFICATION,
                       e.g. "GB_Base load", "JP_Tokyo_Base load", "GB_Other".
2. ``block``         : the block-of-hours code pulled from the NOTES column
                       (e.g. NOTES "UK_Block_1_2" -> block "1_2"). A block is a
                       load SHAPE (which hours of the day), orthogonal to the
                       calendar period.
3. ``periodicity_2`` : the corrected, date-derived periodicity bucket.
4. ``tenor``         : the market tenor. When a block is present it is a COMPOSITE
                       of the calendar tenor and the block, e.g. "D+3 Block_1_2",
                       "M+1 Block_1_2".
5. ``tenor2``        : same as ``tenor`` but a bare 'Custom' calendar part is
                       replaced by the raw TERM_DESCRIPTION.

ROW vs AMER (important)
-----------------------
Power tenors are assigned differently by region because of how the delivery
window is encoded (ROW is clear on hours; AMER runs 00:00 for 24h with the
lastTradingDate the day BEFORE the start):

  * ROW  -> the delivery period is **end-anchored**: for a Daily the valid day is
            the END date (e.g. 2024-12-31 -> 2025-01-01 is the 1-Jan gas/power
            day); for a Monthly the delivery month is read from the END.
  * AMER -> the delivery period is **start-anchored**: the Daily day is the START
            (2024-12-31), the Monthly month is read from the START.

``region_type='ROW'`` (default) or ``'AMER'`` selects this. It also selects the
season windows (ROW: Apr-Sep / Oct-Mar; AMER/US: Apr-Oct / Nov-Mar).

Periodicity comes from the DATES, not the column
------------------------------------------------
The upstream PERIODICITY is often 'Other' even for a real Base-load Monthly, so
it is NOT trusted. The bucket is derived from the contract dates. CLASSIFICATION
is a hint: a real load type (Base/Peak load) is a genuine periodic product, while
'Other' + a block note is a block/custom shape.
"""

import re
import pandas as pd
import numpy as np


class PowerTenorProcessor:
    """Enrich raw power trades with product, block, periodicity_2 and tenors.

    Parameters
    ----------
    df : pd.DataFrame
        Raw power trades. Expected columns include DEAL_EXECUTION_DATETIME (or
        EVENT_TIMESTAMP), COUNTRY, REGION, CLASSIFICATION, NOTES, DEAL_PRICE,
        QUANTITY, CONTRACT_START_DATE, CONTRACT_END_DATE, TERM_DESCRIPTION.
    region_type : {'ROW', 'AMER'}, default 'ROW'
        ROW = end-anchored delivery + Apr-Sep/Oct-Mar seasons + blocks.
        AMER = start-anchored delivery + Apr-Oct/Nov-Mar seasons, no blocks.

    Tolerances for tenor assignment (same idea as gas; each is applied at BOTH
    ends of the contract, and 0 means "exact boundary"):

    month_tolerance_days : int, default 2
        Slack (days) at each end to accept an aligned calendar month (absorbs the
        usual off-by-one encodings: end on the 1st of next month, start on the
        last day of the previous month, start 1-2 days in).
    week_tolerance_days : int, default 1
        Slack (days) at each end to accept an aligned Mon->Sun week; also the
        slack used to pin the end of a Balance-of-Week.
    quarter_tolerance_days : int, default 2
        Slack (days) at each end to accept an aligned calendar quarter (1-Jan /
        1-Apr / 1-Jul / 1-Oct boundaries).
    seasonal_tol_days : int, default 3
        Slack (days) at each end of a season window (summer / winter).
    bom_tolerance_days : int, default 3
        Max gap (days) between the reference date and a mid-month start for a
        partial current month to count as Balance-of-Month ('BOM').
    week_balance_tolerance_days : int, default 1
        Max gap (days) between the reference date and a mid-week start for a
        partial current week to count as Balance-of-Week ('BOW').
    allow_front_zero : bool or None, default None (-> True for ROW, False for AMER)
        If True the current month/quarter is a tradable front tenor ('M+0'/'Q+0')
        - this is the ROW convention (the ROW data shows M+0). If False (AMER) it
        behaves exactly like gas: no 'M+0'/'Q+0', so a full current month/quarter
        falls to 'Custom' (a partial current month is still 'BOM') and the periodic
        tenors are effectively >= 1.
    weekend_mode : {'tolerant', 'strict'}, default 'tolerant'
        strict = Sat->Sun (span 1) or Sat->Mon (span 2); tolerant = start in
        {Fri, Sat}, end in {Sun, Mon}, span in [1, 3].
    *_col : str
        Configurable source column names.
    """

    def __init__(self, df: pd.DataFrame, *, region_type: str = "ROW",
                 month_tolerance_days: int = 2, week_tolerance_days: int = 1,
                 quarter_tolerance_days: int = 2, seasonal_tol_days: int = 3,
                 bom_tolerance_days: int = 3, week_balance_tolerance_days: int = 1,
                 allow_front_zero: bool | None = None, weekend_mode: str = "tolerant",
                 country_col: str = "COUNTRY", region_col: str = "REGION",
                 classification_col: str = "CLASSIFICATION", notes_col: str = "NOTES",
                 datetime_col: str = "DEAL_EXECUTION_DATETIME"):
        self.df_raw = df.copy()
        self.region_type = str(region_type).strip().upper()
        # AMER / US / AMERICAS -> start-anchored + US seasons; everything else ROW.
        self.start_anchored = self.region_type in ("AMER", "US", "AMERICAS", "AMERS")
        self.seasonal_type = "US" if self.start_anchored else "ROW"
        # Blocks (NOTES hour-shapes) are a ROW feature only; AMER has no NOTES logic.
        self.blocks_enabled = not self.start_anchored

        self.month_tolerance_days = month_tolerance_days
        self.week_tolerance_days = week_tolerance_days
        self.quarter_tolerance_days = quarter_tolerance_days
        self.seasonal_tol_days = seasonal_tol_days
        self.bom_tolerance_days = bom_tolerance_days
        self.week_balance_tolerance_days = week_balance_tolerance_days
        # ROW keeps a front M+0 / Q+0 (the current period is tradable, per the ROW
        # data). AMER forward power is >= 1 (like gas). Default follows the region
        # (ROW -> True, AMER -> False) unless the caller passes it explicitly.
        self.allow_front_zero = (not self.start_anchored) if allow_front_zero is None else allow_front_zero
        self.weekend_mode = weekend_mode

        self.country_col = country_col.upper()
        self.region_col = region_col.upper()
        self.classification_col = classification_col.upper()
        self.notes_col = notes_col.upper()
        self.datetime_col = datetime_col.upper()

        self.df_trades: pd.DataFrame = pd.DataFrame()

    # --- Public API ----------------------------------------------------------

    def run(self) -> pd.DataFrame:
        """Enrich the trades: clean -> block -> periodicity_2 / tenor / tenor2.

        Note: the ``product`` NAME is built downstream in the VWAP engine
        (``COUNTRY_REGION_ZONE_CLASSIFICATION_UNIT``); the tenor stage only pulls
        the ROW ``block`` (which the composite tenor needs).
        """
        self._clean()
        self._extract_block()
        self._assign_tenors()
        return self.df_trades

    # --- Internal steps ------------------------------------------------------

    def _clean(self):
        """Parse dates, coerce numerics, drop invalid rows, set reference_date."""
        df = self.df_raw.copy()
        df.columns = [c.strip().upper() for c in df.columns]

        # Reference timestamp: prefer the configured column, fall back to EVENT_TIMESTAMP.
        dt_col = self.datetime_col if self.datetime_col in df.columns else "EVENT_TIMESTAMP"
        df[dt_col] = pd.to_datetime(df[dt_col], errors="coerce")
        df["CONTRACT_START_DATE"] = pd.to_datetime(df["CONTRACT_START_DATE"], errors="coerce")
        df["CONTRACT_END_DATE"] = pd.to_datetime(df["CONTRACT_END_DATE"], errors="coerce")

        df["DEAL_PRICE"] = pd.to_numeric(df["DEAL_PRICE"], errors="coerce")
        df["QUANTITY"] = pd.to_numeric(df["QUANTITY"], errors="coerce")

        df = df.dropna(subset=[dt_col, "CONTRACT_START_DATE", "DEAL_PRICE", "QUANTITY"])
        df = df[df["QUANTITY"] > 0]

        df["reference_date"] = df[dt_col].dt.normalize()
        self._dt_col = dt_col
        self.df_trades = df

    def _extract_block(self):
        """Pull the ROW ``block`` code from NOTES (``UK_Block_1_2`` -> ``1_2``).

        A block is a load SHAPE (which hours of the day), not a calendar period,
        so it is stored on its own column and composed into the tenor later. ROW
        only: AMER has no NOTES/block logic. (The ``product`` NAME is built in the
        VWAP engine, not here.)
        """
        df = self.df_trades
        if self.blocks_enabled and self.notes_col in df.columns:
            df["block"] = df[self.notes_col].apply(self._block_from_notes)
        else:
            df["block"] = None
        self.df_trades = df

    def _assign_tenors(self):
        """Assign, per trade, periodicity_2 and the two (block-composite) tenors."""
        def _row(r):
            s, e = r["CONTRACT_START_DATE"], r["CONTRACT_END_DATE"]
            bucket = self._classify_period(s, e)
            base = self._compute_tenor(r["reference_date"], s, e, bucket)

            # tenor2 base: a bare 'Custom' becomes the raw TERM_DESCRIPTION.
            base2 = base
            if base == "Custom":
                term = r.get("TERM_DESCRIPTION")
                if pd.notna(term) and str(term).strip():
                    base2 = str(term).strip()

            block = r.get("block")
            tenor = self._compose(base, block)
            tenor2 = self._compose(base2, block)
            # BOM / BOW are reference-date refinements: surface them in the
            # periodicity column too (like gas), else use the date bucket.
            label = base if base in ("BOM", "BOW") else bucket.capitalize()
            return pd.Series({"periodicity_2": label, "tenor": tenor, "tenor2": tenor2})

        self.df_trades[["periodicity_2", "tenor", "tenor2"]] = self.df_trades.apply(_row, axis=1)

    # --- Block & composition -------------------------------------------------

    @staticmethod
    def _block_from_notes(notes) -> str | None:
        """Return the block code from a NOTES value, e.g. 'UK_Block_1_2' -> '1_2'."""
        if notes is None or (isinstance(notes, float) and pd.isna(notes)):
            return None
        m = re.search(r"block[_\s]?([0-9]+(?:[_\-][0-9]+)*)", str(notes), flags=re.IGNORECASE)
        if not m:
            return None
        return m.group(1).replace("-", "_")

    @staticmethod
    def _compose(base_tenor: str, block) -> str:
        """Compose the calendar tenor with the block shape.

        No block            -> the calendar tenor unchanged ('D+3', 'M+1', ...).
        Block + calendar    -> composite 'D+3 Block_1_2'.
        Block but no calendar (base 'Custom') -> just 'Block_1_2'.
        """
        if block is None or (isinstance(block, float) and pd.isna(block)) or str(block).strip() == "":
            return base_tenor
        block = str(block).strip()
        if base_tenor == "Custom":
            return f"Block_{block}"
        return f"{base_tenor} Block_{block}"

    # --- Region-aware delivery anchoring -------------------------------------

    def _delivery_day(self, S: pd.Timestamp, E: pd.Timestamp) -> pd.Timestamp:
        """The single delivery day of a Daily contract, by region.

        ROW  -> the END date (end-anchored): 2024-12-31 -> 2025-01-01 is 1-Jan.
        AMER -> the START date (start-anchored): the day is 2024-12-31.
        """
        if self.start_anchored or pd.isna(E):
            return pd.Timestamp(S).normalize()
        return pd.Timestamp(E).normalize()

    def _delivery_month(self, S: pd.Timestamp, E: pd.Timestamp) -> tuple[int, int]:
        """The delivery (year, month) of a Monthly contract, by region.

        AMER -> read from the START. ROW -> read from the END, handling the
        off-by-one where an end on the 1st means the previous month.
        """
        if self.start_anchored or pd.isna(E):
            s = pd.Timestamp(S)
            return s.year, s.month
        E = pd.Timestamp(E)
        if E.day == 1:                      # end=2026-03-01 means February delivery
            prev = E - pd.Timedelta(days=1)
            return prev.year, prev.month
        return E.year, E.month

    def _is_bom(self, ref_date, S, E) -> bool:
        """True if the contract is the Balance Of (the current) Month.

        A partial current month: mid-month start, end on the month boundary, the
        delivery month equals the reference month, and the start is at/after the
        reference within ``bom_tolerance_days``.
        """
        if pd.isna(S) or pd.isna(E):
            return False
        S = pd.Timestamp(S).normalize()
        E = pd.Timestamp(E).normalize()
        R = pd.Timestamp(ref_date).normalize()

        if S.day == 1:                                  # a full-aligned month is not a BOM
            return False
        month_end = S + pd.offsets.MonthEnd(0)
        if not (E.day == 1 or E == month_end):          # must finish at the month boundary
            return False
        dy, dm = self._delivery_month(S, E)
        if (dy, dm) != (R.year, R.month):               # must be the current month
            return False
        gap = (S - R).days
        return 0 <= gap <= self.bom_tolerance_days

    def _is_bow(self, ref_date, S, E) -> bool:
        """True if the contract is the Balance Of (the current) Week.

        A partial current week: mid-week (non-Monday) start in the reference's
        week, at/after the reference within ``week_balance_tolerance_days``, ending
        near the week's end (Sunday or the following Monday within
        ``week_tolerance_days``).
        """
        if pd.isna(S) or pd.isna(E):
            return False
        S = pd.Timestamp(S).normalize()
        E = pd.Timestamp(E).normalize()
        R = pd.Timestamp(ref_date).normalize()

        if S.weekday() == 0:                            # Monday start = full week
            return False
        s_week = S - pd.Timedelta(days=S.weekday())     # Monday of the start's week
        r_week = R - pd.Timedelta(days=R.weekday())     # Monday of the reference week
        if s_week != r_week:                            # must be the current week
            return False
        gap = (S - R).days
        if not (0 <= gap <= self.week_balance_tolerance_days):
            return False
        sunday = s_week + pd.Timedelta(days=6)
        next_monday = s_week + pd.Timedelta(days=7)
        return (abs((E - sunday).days) <= self.week_tolerance_days
                or abs((E - next_monday).days) <= self.week_tolerance_days)

    # --- Tenor logic ---------------------------------------------------------

    def _compute_tenor(self, ref_date: pd.Timestamp, S: pd.Timestamp,
                       E: pd.Timestamp, bucket: str) -> str:
        """Derive the (calendar-only) tenor from the reference and contract dates.

        Unlike gas, power keeps a front M+0 / Q+0 (the current month / quarter is
        a valid tradable tenor for power), so those are NOT rewritten to a balance
        product.
        """
        ref_year, ref_month = ref_date.year, ref_date.month

        if bucket == "DAILY":
            day = self._delivery_day(S, E)
            return f"D+{(day - ref_date).days}"

        if bucket == "WEEKLY":
            wd = S.weekday()
            cs_monday = (S - pd.Timedelta(days=wd) if wd <= 3 else S + pd.Timedelta(days=7 - wd))
            ref_monday = ref_date - pd.Timedelta(days=ref_date.weekday())
            weeks_diff = int((cs_monday - ref_monday).days / 7)
            if self._is_bow(ref_date, S, E):          # balance of the current week
                return "BOW"
            return "W" if weeks_diff == 0 else f"W+{weeks_diff}"

        if bucket == "WEEKEND":
            ref_monday = ref_date - pd.Timedelta(days=ref_date.weekday())
            cs_monday = S - pd.Timedelta(days=S.weekday())
            weeks_diff = int((cs_monday - ref_monday).days / 7)
            return "WE" if weeks_diff == 0 else f"WE+{weeks_diff}"

        if bucket == "MONTHLY":
            dy, dm = self._delivery_month(S, E)
            months_diff = (dy - ref_year) * 12 + (dm - ref_month)
            if months_diff >= 1:
                return f"M+{months_diff}"
            # Current / past month. A partial current month is BOM in both regions.
            # A FULL current month is M+0 for ROW (tradable front month); AMER
            # behaves exactly like gas -> no M+0, so it falls to Custom.
            if self._is_bom(ref_date, S, E):
                return "BOM"
            if months_diff == 0 and self.allow_front_zero:
                return "M+0"
            return "Custom"

        if bucket == "QUARTERLY":
            cs_year, cs_q = self._delivery_quarter(S)
            ref_q = (ref_month - 1) // 3
            quarters_diff = (cs_year - ref_year) * 4 + (cs_q - ref_q)
            if quarters_diff >= 1:
                return f"Q+{quarters_diff}"
            if quarters_diff == 0:
                return "Q+0" if self.allow_front_zero else "Custom"
            return "Custom"

        if bucket == "SEASONAL":
            eff = self._nearest_month_start(S)
            if eff.month in (4, 5, 6, 7, 8, 9):
                return f"Sum{eff.year % 100:02d}"
            win_year = eff.year if eff.month >= 10 else eff.year - 1
            return f"Win{win_year % 100:02d}"

        if bucket == "ANNUAL":
            return f"Cal{self._nearest_month_start(S).year % 100:02d}"

        if bucket == "SEMI-ANNUAL":
            return "Custom"          # no clean +n label yet (pending desk preference)

        # Non-standard span: balance-of-week first (shorter, more specific), then
        # balance-of-month, else a broker custom range.
        if self._is_bow(ref_date, S, E):
            return "BOW"
        if self._is_bom(ref_date, S, E):
            return "BOM"
        return "Custom"

    def _classify_period(self, S: pd.Timestamp, E: pd.Timestamp) -> str:
        """Derive the period bucket from the contract DATES (not the column).

        Returns one of DAILY, WEEKEND, WEEKLY, MONTHLY, QUARTERLY, SEASONAL,
        SEMI-ANNUAL, ANNUAL, OTHER.
        """
        if pd.isna(S):
            return "OTHER"
        S = pd.Timestamp(S).normalize()
        if pd.isna(E):
            return "DAILY"
        E = pd.Timestamp(E).normalize()
        span = (E - S).days
        if span < 0:
            return "OTHER"

        if self._is_weekend(S, E, self.weekend_mode):
            return "WEEKEND"
        # A power day is span 0 (start==end) OR span 1 (start -> next day, the
        # ROW 'end is the valid day' encoding).
        if span <= 1:
            return "DAILY"
        if self._is_calendar_week(S, E, self.week_tolerance_days):
            return "WEEKLY"
        if self._is_calendar_month(S, E, self.month_tolerance_days):
            return "MONTHLY"
        if self._is_calendar_quarter(S, E, self.quarter_tolerance_days):
            return "QUARTERLY"
        if 140 <= span <= 220:
            if self._is_seasonal(S, E, self.seasonal_type, self.seasonal_tol_days):
                return "SEASONAL"
            if 180 <= span <= 185:
                return "SEMI-ANNUAL"
            return "OTHER"
        if 360 <= span <= 368:
            return "ANNUAL"
        if 180 <= span <= 185:
            return "SEMI-ANNUAL"
        return "OTHER"

    # --- Static date helpers (shared shape with the gas processor) ------------

    @staticmethod
    def _delivery_quarter(contract_start: pd.Timestamp) -> tuple[int, int]:
        d = pd.Timestamp(contract_start)
        q_month = ((d.month - 1) // 3) * 3 + 1
        this_q = pd.Timestamp(d.year, q_month, 1)
        next_q = this_q + pd.DateOffset(months=3)
        anchor = this_q if abs((d - this_q).days) <= abs((d - next_q).days) else next_q
        return anchor.year, (anchor.month - 1) // 3

    @staticmethod
    def _nearest_month_start(d: pd.Timestamp) -> pd.Timestamp:
        d = pd.Timestamp(d)
        this_first = d.replace(day=1)
        next_first = this_first + pd.offsets.MonthBegin(1)
        return (this_first if abs((d - this_first).days) <= abs((d - next_first).days)
                else next_first)

    @staticmethod
    def _is_weekend(S: pd.Timestamp, E: pd.Timestamp, mode: str = "tolerant") -> bool:
        """Weekend block detection (power)."""
        span = (E - S).days
        if mode == "strict":
            return ((S.weekday() == 5 and E.weekday() == 6 and span == 1) or
                    (S.weekday() == 5 and E.weekday() == 0 and span == 2))
        return S.weekday() in (4, 5) and E.weekday() in (6, 0) and 1 <= span <= 3

    @staticmethod
    def _is_calendar_month(S: pd.Timestamp, E: pd.Timestamp, tol_days: int = 2) -> bool:
        def month_anchor(d):
            this_first = d.replace(day=1)
            next_first = this_first + pd.offsets.MonthBegin(1)
            if abs((d - this_first).days) <= tol_days:
                return this_first
            if abs((d - next_first).days) <= tol_days:
                return next_first
            return None
        s_anchor = month_anchor(pd.Timestamp(S))
        e_anchor = month_anchor(pd.Timestamp(E))
        if s_anchor is None or e_anchor is None:
            return False
        return e_anchor == s_anchor + pd.offsets.MonthBegin(1)

    @staticmethod
    def _is_calendar_quarter(S: pd.Timestamp, E: pd.Timestamp, tol_days: int = 2) -> bool:
        def quarter_anchor(d):
            q_month = ((d.month - 1) // 3) * 3 + 1
            this_q = pd.Timestamp(d.year, q_month, 1)
            next_q = this_q + pd.DateOffset(months=3)
            for anchor in (this_q, next_q):
                if abs((d - anchor).days) <= tol_days:
                    return anchor
            return None
        s_anchor = quarter_anchor(pd.Timestamp(S))
        e_anchor = quarter_anchor(pd.Timestamp(E))
        if s_anchor is None or e_anchor is None:
            return False
        return e_anchor == s_anchor + pd.DateOffset(months=3)

    @staticmethod
    def _is_calendar_week(S: pd.Timestamp, E: pd.Timestamp, tol_days: int = 1) -> bool:
        wd = S.weekday()
        if min(wd, 7 - wd) > tol_days:
            return False
        s_monday = S - pd.Timedelta(days=wd) if wd <= 3 else S + pd.Timedelta(days=7 - wd)
        sunday = s_monday + pd.Timedelta(days=6)
        next_monday = s_monday + pd.Timedelta(days=7)
        return (abs((E - sunday).days) <= tol_days
                or abs((E - next_monday).days) <= tol_days)

    @staticmethod
    def _is_seasonal(S: pd.Timestamp, E: pd.Timestamp,
                     seasonal_type: str = "ROW", tol_days: int = 3) -> bool:
        y = S.year
        if seasonal_type == "US":
            windows = [
                (pd.Timestamp(y, 4, 1), pd.Timestamp(y, 10, 31)),
                (pd.Timestamp(y, 11, 1), pd.Timestamp(y + 1, 3, 31)),
            ]
        else:
            windows = [
                (pd.Timestamp(y, 4, 1), pd.Timestamp(y, 9, 30)),
                (pd.Timestamp(y, 10, 1), pd.Timestamp(y + 1, 3, 31)),
            ]
        for ws, we in windows:
            if abs((S - ws).days) <= tol_days and abs((E - we).days) <= tol_days:
                return True
        return False


class PowerROWTenorProcessor(PowerTenorProcessor):
    """ROW convention: end-anchored, ROW seasons, blocks, keeps M+0/Q+0."""

    def __init__(self, df, **kwargs):
        kwargs.pop("region_type", None)
        super().__init__(df, region_type="ROW", **kwargs)


class PowerAMERTenorProcessor(PowerTenorProcessor):
    """AMER convention: start-anchored, US seasons, no blocks, >=1 like gas."""

    def __init__(self, df, **kwargs):
        kwargs.pop("region_type", None)
        super().__init__(df, region_type="AMER", **kwargs)   # allow_front_zero -> False by region


# Backward-compatible aliases.
PowerTenorProcessorBase = PowerTenorProcessor
PowerEMEATenorProcessor = PowerTenorProcessor
