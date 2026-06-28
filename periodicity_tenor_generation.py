"""Gas Tenor Processor - enriches gas trades with periodicity and tenor labels.

This class owns ONLY the tenor-generation stage: from the raw trades it derives,
per trade, the corrected periodicity bucket and the market tenors. The VWAP
calculation is a separate concern handled by ``GasOutrightEngine`` (Outright,
``outright_vwap_engine.py``) and ``GasSpreadEngine`` (Spread,
``spread_vwap_engine.py``), each of which consumes the enriched trades produced
here.

Input:  Raw DataFrame of gas trades (from Snowflake or CSV) with columns:
            DEAL_EXECUTION_DATETIME, CLASSIFICATION_1, DEAL_PRICE, QUANTITY,
            CONTRACT_START_DATE, CONTRACT_END_DATE, PERIODICITY, TERM_DESCRIPTION

Output (``run()`` / ``df_trades``): the input rows plus three columns:
            periodicity_2 : the date-derived periodicity bucket (corrected).
            tenor         : the canonical market tenor (D+n, W, M+1, Q+2, BOM,
                            BOW, ..., or 'Custom'). Exposed downstream as 'tenor1'.
            tenor2        : same as 'tenor' but 'Custom' is replaced by the raw
                            TERM_DESCRIPTION.
"""

import pandas as pd
import numpy as np


class GasTenorProcessor:
    """Enrich raw gas trades with the corrected periodicity and the two tenors.

    Attributes
    ----------
    df_raw : pd.DataFrame
        The original input DataFrame (untouched).
    df_trades : pd.DataFrame
        Cleaned trades with 'periodicity_2', 'tenor' and 'tenor2' assigned. This
        is the result consumed by the VWAP / spread engines.
    """

    def __init__(self, df: pd.DataFrame, seasonal_type: str = "ROW",
                 bom_tolerance_days: int = 3, month_tolerance_days: int = 2,
                 week_tolerance_days: int = 1, quarter_tolerance_days: int = 2,
                 week_balance_tolerance_days: int = 1):
        """Initialise with a raw gas trades DataFrame.

        Parameters
        ----------
        df : Raw trades DataFrame. Expected columns:
            - DEAL_EXECUTION_DATETIME : trade timestamp (-> reference date)
            - CLASSIFICATION_1        : gas product (NBP, TTF, PSV, THE, ...)
            - DEAL_PRICE              : trade price
            - QUANTITY                : trade volume (used as VWAP weight)
            - CONTRACT_START_DATE     : delivery period start
            - CONTRACT_END_DATE       : delivery period end
            - PERIODICITY             : contract periodicity (Monthly, Quarterly, ...)
                                        Used ONLY as a fallback hint; the tenor bucket
                                        is derived from the contract dates (see
                                        ``_classify_period``).
        seasonal_type : 'ROW' (Apr-Sep summer / Oct-Mar winter) or 'US'
            (Apr-Oct summer / Nov-Mar winter). Controls season-window alignment.
        bom_tolerance_days : int, default 3
            Max gap (in days) allowed between the reference date and the contract
            start for a partial current-month contract to count as a Balance-of-
            Month ('BOM'). 3 covers the next gas day plus a weekend; set 0 for a
            strict rule (start must equal the reference date).
        month_tolerance_days : int, default 2
            Slack (in days) allowed at BOTH the start and the end of a contract
            for it to still count as an aligned calendar month. Handles the usual
            encodings: end on the 1st of next month, start on the last day of the
            previous month, or start 1-2 days into the month.
        week_tolerance_days : int, default 1
            Slack (in days) at the start AND the end of a weekly. For gas a week
            starts Monday and ends on the Sunday or (often) the following Monday;
            +/- this margin absorbs the usual off-by-one encodings.
        quarter_tolerance_days : int, default 2
            Slack (in days) at the start AND the end of a quarter. The ideal
            quarter starts on a boundary (1-Jan / 1-Apr / 1-Jul / 1-Oct) and ends
            on the following boundary; +/- this margin absorbs the off-by-one
            encodings (e.g. 30-Sep -> 31-Dec, 01-Oct -> 01-Jan). With 0 the start
            must land exactly on the boundary.
        week_balance_tolerance_days : int, default 1
            Max gap (in days) between the reference date and the contract start
            for a mid-week contract to count as a Balance-of-Week ('BOW') - the
            weekly analogue of ``bom_tolerance_days``. A WEEKLY contract whose
            start is NOT a Monday but falls in the CURRENT week (same week as the
            reference) and within this gap is labelled 'BOW' instead of 'W'. The
            end is already pinned near the week's end by ``week_tolerance_days``,
            so this knob only governs the reference->start relationship. With 0
            the start must equal the reference date.
        """
        self.df_raw = df.copy()
        self.seasonal_type = seasonal_type
        self.bom_tolerance_days = bom_tolerance_days
        self.month_tolerance_days = month_tolerance_days
        self.week_tolerance_days = week_tolerance_days
        self.quarter_tolerance_days = quarter_tolerance_days
        self.week_balance_tolerance_days = week_balance_tolerance_days
        self.df_trades: pd.DataFrame = pd.DataFrame()

    # --- Public API ----------------------------------------------------------

    def run(self) -> pd.DataFrame:
        """Enrich the trades: clean -> assign periodicity_2 / tenor / tenor2.

        Returns
        -------
        pd.DataFrame
            The cleaned trades (``df_trades``) with three columns added:
            ``periodicity_2``, ``tenor`` (canonical, exposed as 'tenor1') and
            ``tenor2`` ('Custom' -> TERM_DESCRIPTION). Feed this DataFrame to a
            ``GasVWAPEngine`` (Outright) or ``GasSpreadEngine`` (Spread) to
            compute VWAPs.
        """
        self._clean()
        self._assign_tenors()
        return self.df_trades

    # --- Internal steps ------------------------------------------------------

    def _clean(self):
        """Parse dates, coerce numerics, drop invalid rows."""
        df = self.df_raw.copy()

        # Normalise column names to upper case
        df.columns = [c.strip().upper() for c in df.columns]

        # Parse dates
        df["DEAL_EXECUTION_DATETIME"] = pd.to_datetime(
            df["DEAL_EXECUTION_DATETIME"], errors="coerce"
        )
        df["CONTRACT_START_DATE"] = pd.to_datetime(
            df["CONTRACT_START_DATE"], errors="coerce"
        )
        df["CONTRACT_END_DATE"] = pd.to_datetime(
            df["CONTRACT_END_DATE"], errors="coerce"
        )

        # Coerce numerics
        df["DEAL_PRICE"] = pd.to_numeric(df["DEAL_PRICE"], errors="coerce")
        df["QUANTITY"] = pd.to_numeric(df["QUANTITY"], errors="coerce")

        # Drop rows missing essential fields
        df = df.dropna(subset=[
            "DEAL_EXECUTION_DATETIME", "CONTRACT_START_DATE",
            "DEAL_PRICE", "QUANTITY", "CLASSIFICATION_1",
        ])
        df = df[df["QUANTITY"] > 0]

        # Reference date = date part of execution datetime
        df["reference_date"] = df["DEAL_EXECUTION_DATETIME"].dt.normalize()

        self.df_trades = df

    def _assign_tenors(self):
        """Assign, per trade, the recomputed periodicity bucket and BOTH tenors.

        Writes three columns to self.df_trades:
          - 'periodicity_2' : the date-derived bucket (Daily, Monthly, ...), i.e.
                              the corrected periodicity the tenor is built on.
          - 'tenor'         : the market tenor label (D+n, M+n, Q+n, Custom, ...).
          - 'tenor2'        : same as 'tenor', except a 'Custom' (non-standard)
                              contract is replaced by its raw TERM_DESCRIPTION, so
                              the opaque 'Custom' shows the actual contract term.
        """
        def _row(r):
            s, e = r["CONTRACT_START_DATE"], r["CONTRACT_END_DATE"]
            per = r.get("PERIODICITY", "other")
            bucket = self._classify_period(s, e, per)          # computed once...
            tenor = self._compute_tenor(r["reference_date"], s, e, bucket=bucket)  # ...and reused
            # BOM / BOW are tenor-level refinements that need the reference date
            # (balance of the current month / week), so surface them in the
            # periodicity column too instead of the raw bucket.
            label = tenor if tenor in ("BOM", "BOW") else bucket.capitalize()
            # Second tenor: a 'Custom' becomes its TERM_DESCRIPTION when available,
            # otherwise it stays 'Custom'. Every other tenor is carried unchanged.
            tenor2 = tenor
            if tenor == "Custom":
                term = r.get("TERM_DESCRIPTION")
                if pd.notna(term) and str(term).strip():
                    tenor2 = str(term).strip()
            return pd.Series({"periodicity_2": label, "tenor": tenor, "tenor2": tenor2})

        self.df_trades[["periodicity_2", "tenor", "tenor2"]] = self.df_trades.apply(_row, axis=1)

    # --- Tenor logic ---------------------------------------------------------

    @staticmethod
    def _delivery_month(contract_start: pd.Timestamp,
                        contract_end: pd.Timestamp) -> tuple[int, int]:
        """Determine the effective delivery month from contract dates.

        Handles off-by-one start dates:
        - contract 2025-11-30 -> 2025-12-31 is really December delivery
        - contract 2025-02-28 -> 2025-03-31 is really March delivery

        Logic: use contract_end to identify the delivery month.
        - If contract_end is the 1st of a month -> delivery = previous month
        - Otherwise -> delivery = contract_end's month
        """
        if pd.isna(contract_end):
            return contract_start.year, contract_start.month

        if contract_end.day == 1:
            # e.g. end=2026-03-01 means delivery is February
            prev = contract_end - pd.Timedelta(days=1)
            return prev.year, prev.month
        else:
            return contract_end.year, contract_end.month

    @staticmethod
    def _delivery_quarter(contract_start: pd.Timestamp) -> tuple[int, int]:
        """Determine the effective delivery quarter from an aligned quarter start.

        Snaps the start to its nearest quarter boundary (1-Jan / 1-Apr / 1-Jul /
        1-Oct), so an off-by-one start such as 30-Sep (-> 31-Dec) is read as the
        October quarter (Q4), not Q3.

        Parameters
        ----------
        contract_start : pd.Timestamp
            Normalised contract start date (already confirmed to be an aligned
            quarter by ``_is_calendar_quarter``).

        Returns
        -------
        tuple[int, int]
            ``(year, quarter_index)`` where quarter_index is 0..3 (Q1..Q4).
        """
        d = pd.Timestamp(contract_start)
        q_month = ((d.month - 1) // 3) * 3 + 1
        this_q = pd.Timestamp(d.year, q_month, 1)
        next_q = this_q + pd.DateOffset(months=3)
        anchor = this_q if abs((d - this_q).days) <= abs((d - next_q).days) else next_q
        return anchor.year, (anchor.month - 1) // 3

    @staticmethod
    def _nearest_month_start(d: pd.Timestamp) -> pd.Timestamp:
        """Snap a date to the nearest 1st-of-month.

        Used to read off-by-one starts consistently when labelling Seasonal and
        Annual tenors (e.g. a winter encoded as 30-Sep belongs to October; an
        annual encoded as 31-Dec belongs to the following January/year).

        Parameters
        ----------
        d : pd.Timestamp
            A contract start date.

        Returns
        -------
        pd.Timestamp
            The 1st-of-month closest to ``d`` (ties resolve to this month's 1st).
        """
        d = pd.Timestamp(d)
        this_first = d.replace(day=1)
        next_first = this_first + pd.offsets.MonthBegin(1)
        return (this_first if abs((d - this_first).days) <= abs((d - next_first).days)
                else next_first)

    @staticmethod
    def _snap_to_monday(dt: pd.Timestamp) -> pd.Timestamp:
        """Snap a date to the next Monday (ceiling). If already Monday, keep it.

        Handles contract_start landing on Sunday (belongs to next week's delivery).
        """
        weekday = dt.weekday()  # 0=Mon, 6=Sun
        if weekday == 0:
            return dt
        # Days until next Monday
        days_ahead = 7 - weekday
        return dt + pd.Timedelta(days=days_ahead)

    @staticmethod
    def _normalize_periodicity(periodicity) -> str | None:
        """Normalise the upstream PERIODICITY column to a canonical bucket, or None.

        Returns None for values that carry no usable signal ('Other', 'Invalid',
        blanks, NaN) and for 'Semi-Annual' (there is no semi-annual tenor) so the
        caller falls through to date-based logic.
        """
        if periodicity is None:
            return None
        try:
            if pd.isna(periodicity):
                return None
        except (TypeError, ValueError):
            pass
        p = str(periodicity).strip().upper()
        if p in ("", "OTHER", "NAN", "NONE", "INVALID", "SEMI-ANNUAL", "SEMI ANNUAL"):
            return None
        return p

    @staticmethod
    def _is_seasonal(S: pd.Timestamp, E: pd.Timestamp,
                     seasonal_type: str = "ROW", tol_days: int = 3) -> bool:
        """Test whether ``[S, E]`` aligns with a gas season (summer/winter) window.

        Parameters
        ----------
        S, E : pd.Timestamp
            Normalised contract start and end dates.
        seasonal_type : {'ROW', 'US'}, default 'ROW'
            Season convention. ``ROW`` uses Apr-Sep / Oct-Mar; ``US`` uses
            Apr-Oct / Nov-Mar.
        tol_days : int, default 3
            Maximum slack, in days, allowed at each end of the window.

        Returns
        -------
        bool
            ``True`` when both ends fall within ``tol_days`` of a season window.
        """
        y = S.year
        if seasonal_type == "US":
            windows = [
                (pd.Timestamp(y, 4, 1), pd.Timestamp(y, 10, 31)),       # Summer Apr-Oct
                (pd.Timestamp(y, 11, 1), pd.Timestamp(y + 1, 3, 31)),   # Winter Nov-Mar
            ]
        else:  # ROW
            windows = [
                (pd.Timestamp(y, 4, 1), pd.Timestamp(y, 9, 30)),        # Summer Apr-Sep
                (pd.Timestamp(y, 10, 1), pd.Timestamp(y + 1, 3, 31)),   # Winter Oct-Mar
            ]
        for ws, we in windows:
            if abs((S - ws).days) <= tol_days and abs((E - we).days) <= tol_days:
                return True
        return False

    @staticmethod
    def _is_calendar_month(S: pd.Timestamp, E: pd.Timestamp, tol_days: int = 2) -> bool:
        """Test whether ``[S, E]`` is an aligned calendar month.

        Start and end may each be off a month boundary by up to ``tol_days``, in
        either direction. This accepts the common off-by-one encodings
        (e.g. 30-Nov -> 31-Dec is really a December month) as well as a clean
        01-Dec -> 01-Jan, while still rejecting a clearly mid-month strip such as
        04-Dec -> 01-Jan (that becomes BOM/Custom).

        Parameters
        ----------
        S, E : pd.Timestamp
            Normalised contract start and end dates.
        tol_days : int, default 2
            Maximum slack, in days, allowed at each end. ``0`` forces both ends
            onto an exact month boundary.

        Returns
        -------
        bool
            ``True`` when both ends anchor to month boundaries exactly one month
            apart, otherwise ``False``.
        """
        def month_anchor(d):
            # the 1st-of-month within tol_days of d (this month's 1st or next's), or None
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
        # the two anchors must be exactly one month apart
        return e_anchor == s_anchor + pd.offsets.MonthBegin(1)

    @staticmethod
    def _is_calendar_quarter(S: pd.Timestamp, E: pd.Timestamp, tol_days: int = 2) -> bool:
        """Test whether ``[S, E]`` is an aligned calendar quarter.

        The ideal quarter starts on a quarter boundary (1-Jan / 1-Apr / 1-Jul /
        1-Oct) and ends on the following boundary. Start and end may each be off
        that boundary by up to ``tol_days``, in either direction, so the common
        off-by-one encodings are accepted (e.g. 30-Sep -> 31-Dec is a Q4; a clean
        01-Oct -> 01-Jan too) while a clearly mid-quarter strip (start on the
        15th) is rejected and falls through to Custom.

        Parameters
        ----------
        S, E : pd.Timestamp
            Normalised contract start and end dates.
        tol_days : int, default 2
            Maximum slack, in days, allowed at each end. ``0`` forces both ends
            onto an exact quarter boundary.

        Returns
        -------
        bool
            ``True`` when both ends anchor to quarter boundaries exactly one
            quarter apart, otherwise ``False``.
        """
        def quarter_anchor(d):
            # the quarter-start (1,4,7,10 / day 1) within tol_days of d, or None
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
        # the two anchors must be exactly one quarter apart
        return e_anchor == s_anchor + pd.DateOffset(months=3)

    @staticmethod
    def _is_weekend(S: pd.Timestamp, E: pd.Timestamp) -> bool:
        """Test whether ``[S, E]`` is a Saturday->Monday weekend (Sat+Sun delivered).

        Gas convention: start = Saturday, end = the following Monday (span 2,
        exclusive end), e.g. Sat 18th -> Mon 20th. A Sat -> Sun (span 1) is a
        single Saturday daily, NOT a weekend; a Sat -> Tue (span 3) is not one
        either.

        Parameters
        ----------
        S, E : pd.Timestamp
            Normalised contract start and end dates.

        Returns
        -------
        bool
            ``True`` only when start is a Saturday, end the following Monday.
        """
        return S.weekday() == 5 and E.weekday() == 0 and (E - S).days == 2

    @staticmethod
    def _is_calendar_week(S: pd.Timestamp, E: pd.Timestamp, tol_days: int = 1) -> bool:
        """Test whether ``[S, E]`` is an aligned Monday->Sunday week.

        For gas the start is Monday and the end is the Sunday or (often) the next
        Monday. Start must be within ``tol_days`` of a Monday, and end within
        ``tol_days`` of that week's Sunday or following Monday. A misaligned strip
        (e.g. Fri -> Sat) is therefore NOT weekly.

        Parameters
        ----------
        S, E : pd.Timestamp
            Normalised contract start and end dates.
        tol_days : int, default 1
            Maximum slack, in days, allowed at each end. ``0`` forces start on a
            Monday and end on the Sunday/next Monday exactly.

        Returns
        -------
        bool
            ``True`` when both ends align to a week within tolerance.
        """
        wd = S.weekday()  # 0=Mon ... 6=Sun
        if min(wd, 7 - wd) > tol_days:           # start not within tol of a Monday
            return False
        s_monday = S - pd.Timedelta(days=wd) if wd <= 3 else S + pd.Timedelta(days=7 - wd)
        sunday = s_monday + pd.Timedelta(days=6)
        next_monday = s_monday + pd.Timedelta(days=7)
        return (abs((E - sunday).days) <= tol_days
                or abs((E - next_monday).days) <= tol_days)

    def _classify_period(self, contract_start: pd.Timestamp,
                         contract_end: pd.Timestamp, periodicity=None) -> str:
        """Derive the period bucket from the contract DATES (primary signal).

        The upstream PERIODICITY column is consulted ONLY as a fallback, when the
        dates are missing or do not match any window cleanly. This decouples the
        tenor from the (sometimes wrong) PERIODICITY column.

        Returns one of:
            DAILY, WEEKEND, WEEKLY, MONTHLY, QUARTERLY, SEASONAL, ANNUAL, OTHER
        """
        hint = self._normalize_periodicity(periodicity)

        S = contract_start
        E = contract_end

        # No start date -> we cannot judge from data, trust the column.
        if pd.isna(S):
            return hint or "OTHER"
        S = pd.Timestamp(S).normalize()

        # No end date -> a single delivery day is the most likely intent.
        if pd.isna(E):
            return hint or "DAILY"
        E = pd.Timestamp(E).normalize()

        span = (E - S).days
        if span < 0:
            return hint or "OTHER"

        # --- Short end -------------------------------------------------------
        # Weekend = Saturday -> Monday (span 2): Sat+Sun delivered as one block.
        if self._is_weekend(S, E):
            return "WEEKEND"
        # A single gas day is encoded as span 0 (inclusive end) OR span 1
        # (exclusive end). Both are Daily (incl. a lone Saturday, Sat -> Sun).
        if span <= 1:
            return "DAILY"
        # Weekly = an aligned Mon->Sun(/next Mon) week (start Monday +/- margin).
        if self._is_calendar_week(S, E, self.week_tolerance_days):
            return "WEEKLY"

        # --- Longer, duration-distinct buckets -------------------------------
        if self._is_calendar_month(S, E, self.month_tolerance_days):
            return "MONTHLY"
        # Quarterly = an aligned calendar quarter (start on a quarter boundary
        # +/- margin, end on the following boundary +/- margin).
        if self._is_calendar_quarter(S, E, self.quarter_tolerance_days):
            return "QUARTERLY"
        # Season lengths differ by region (ROW ~181d; US winter ~150d, summer ~213d),
        # so use a wide pre-filter and let the calendar alignment decide.
        if 140 <= span <= 220:
            return "SEASONAL" if self._is_seasonal(S, E, self.seasonal_type) else "OTHER"
        if 360 <= span <= 368:
            return "ANNUAL"

        # --- Span fits NO standard bucket -> the contract is non-standard. ----
        # Crucially we do NOT fall back to the column here: a span that matches
        # no bucket cannot be Daily/Weekly/Monthly/etc., so trusting a (possibly
        # wrong) PERIODICITY would re-introduce the very bug we are fixing
        # (e.g. a 21-day strip tagged 'Daily'). The column is only a fallback
        # when the DATES are unusable (handled above for NaT / negative span).
        return "OTHER"

    def _is_bom(self, ref_date, contract_start, contract_end) -> bool:
        """True if the contract is the Balance Of (the current) Month.

        BOM = the remaining part of the current month, i.e.:
          * the end lands on the month boundary (1st of next month, or last day),
          * the start is mid-month (not the 1st)  -> it is a PARTIAL month,
          * the delivery month equals the reference month,
          * the start is at/after the reference date, within ``bom_tolerance_days``.
        """
        if pd.isna(contract_start) or pd.isna(contract_end):
            return False
        S = pd.Timestamp(contract_start).normalize()
        E = pd.Timestamp(contract_end).normalize()
        R = pd.Timestamp(ref_date).normalize()

        if S.day == 1:                                  # a full-aligned month is not a BOM
            return False
        month_end = S + pd.offsets.MonthEnd(0)
        if not (E.day == 1 or E == month_end):          # must finish at the month boundary
            return False

        del_year, del_month = self._delivery_month(S, E)
        if (del_year, del_month) != (R.year, R.month):  # must be the current month
            return False

        gap = (S - R).days                              # days between trade and start
        return 0 <= gap <= self.bom_tolerance_days

    def _is_bow(self, ref_date, contract_start, contract_end) -> bool:
        """True if the contract is the Balance Of (the current) Week.

        BOW = the remaining part of the current week (the weekly analogue of BOM):
          * the start is mid-week (NOT a Monday)            -> a PARTIAL week,
          * the start's week equals the reference's week    -> the current week,
          * the start is at/after the reference date, within
            ``week_balance_tolerance_days``,
          * the end lands near the week's end (the Sunday or the following Monday,
            within ``week_tolerance_days``).

        This catches a "buy the rest of this week" product regardless of the
        weekday it starts on (Tue, Wed, ...), which the calendar-week alignment
        alone would otherwise mislabel as 'W' (Tue start) or drop to 'Other'
        (Wed+ start).
        """
        if pd.isna(contract_start) or pd.isna(contract_end):
            return False
        S = pd.Timestamp(contract_start).normalize()
        E = pd.Timestamp(contract_end).normalize()
        R = pd.Timestamp(ref_date).normalize()

        if S.weekday() == 0:                            # a Monday start = full week
            return False
        s_week = S - pd.Timedelta(days=S.weekday())     # Monday of the start's week
        r_week = R - pd.Timedelta(days=R.weekday())     # Monday of the reference week
        if s_week != r_week:                            # must be the current week
            return False

        gap = (S - R).days                              # days between trade and start
        if not (0 <= gap <= self.week_balance_tolerance_days):
            return False

        sunday = s_week + pd.Timedelta(days=6)          # week end (inclusive)
        next_monday = s_week + pd.Timedelta(days=7)     # week end (exclusive encoding)
        return (abs((E - sunday).days) <= self.week_tolerance_days
                or abs((E - next_monday).days) <= self.week_tolerance_days)

    def _current_month_tenor(self, ref_date, contract_start, contract_end) -> str:
        """Label a current-month / non-standard contract: 'BOM' or 'Custom'."""
        if self._is_bom(ref_date, contract_start, contract_end):
            return "BOM"
        return "Custom"

    def _compute_tenor(self, ref_date: pd.Timestamp, contract_start: pd.Timestamp,
                       contract_end: pd.Timestamp, periodicity=None, bucket=None) -> str:
        """Derive the market tenor label from reference date and contract dates.

        The period bucket is computed from the contract dates via
        ``_classify_period`` (PERIODICITY column is only a fallback hint). A
        precomputed ``bucket`` may be passed in to avoid classifying twice.

        Convention:
            Daily      -> D+n  (days ahead from reference date)
            Weekly     -> W / W+n (same-week = W, by the week of contract_start)
            Weekend    -> WE / WE+n (weekend periods ahead; same-week weekend = WE)
            Monthly    -> M+n  (months ahead, delivery month from contract_end)
            Quarterly  -> Q+n  (quarters ahead)
            Seasonal   -> Sum{yy} / Win{yy}
            Annual     -> Cal{yy}
        """
        ref_year = ref_date.year
        ref_month = ref_date.month

        if bucket is None:
            bucket = self._classify_period(contract_start, contract_end, periodicity)

        # --- Daily -----------------------------------------------------------
        if bucket == "DAILY":
            delivery_day = contract_start
            days_diff = (delivery_day - ref_date).days
            return f"D+{days_diff}"

        # --- Weekly: same-week = W, next week = W+1 ------------------------
        elif bucket == "WEEKLY":
            wd = contract_start.weekday()
            # Monday of the contract's week (nearest, so a Sun day-before snaps up)
            cs_monday = (contract_start - pd.Timedelta(days=wd) if wd <= 3
                         else contract_start + pd.Timedelta(days=7 - wd))
            ref_monday = ref_date - pd.Timedelta(days=ref_date.weekday())
            weeks_diff = int((cs_monday - ref_monday).days / 7)

            # Balance of Week: a mid-week start inside the current week (analogue
            # of BOM). Otherwise a clean 'W' / 'W+n'.
            if self._is_bow(ref_date, contract_start, contract_end):
                return "BOW"
            return "W" if weeks_diff == 0 else f"W+{weeks_diff}"

        # --- Weekend: 1-indexed (same-week weekend = WE) -------------------
        elif bucket == "WEEKEND":
            ref_monday = ref_date - pd.Timedelta(days=ref_date.weekday())
            cs_monday = contract_start - pd.Timedelta(days=contract_start.weekday())
            weeks_diff = int((cs_monday - ref_monday).days / 7)
            return "WE" if weeks_diff == 0 else f"WE+{weeks_diff}"

        # --- Quarterly -> Q+n (front quarter or beyond; no forward Q+0) ----
        elif bucket == "QUARTERLY":
            # Use the ALIGNED quarter (anchors an off-by-one start, e.g. 30-Sep
            # -> Q4), not the raw start month, so the +n count is correct.
            cs_year, cs_quarter = self._delivery_quarter(contract_start)
            ref_quarter = (ref_month - 1) // 3
            quarters_diff = (cs_year - ref_year) * 4 + (cs_quarter - ref_quarter)
            if quarters_diff >= 1:
                return f"Q+{quarters_diff}"
            # quarters_diff <= 0 means the delivery quarter is the current (or a
            # past) quarter: there is no forward 'Q+0', so it is Custom.
            return "Custom"

        # --- Seasonal --------------------------------------------------------
        elif bucket == "SEASONAL":
            # Anchor an off-by-one start to its month boundary (30-Sep -> Oct =
            # winter), so summer/winter and the year are read correctly.
            eff = self._nearest_month_start(contract_start)
            cs_month = eff.month
            cs_year = eff.year
            if cs_month in (4, 5, 6, 7, 8, 9):
                return f"Sum{cs_year % 100:02d}"
            else:
                win_year = cs_year if cs_month >= 10 else cs_year - 1
                return f"Win{win_year % 100:02d}"

        # --- Annual ----------------------------------------------------------
        elif bucket == "ANNUAL":
            # Anchor an off-by-one start (31-Dec -> next Jan) before reading year.
            cs_year = self._nearest_month_start(contract_start).year
            return f"Cal{cs_year % 100:02d}"

        # --- Monthly -> M+n (front month or beyond; no forward M+0) --------
        elif bucket == "MONTHLY":
            del_year, del_month = self._delivery_month(contract_start, contract_end)
            months_diff = (del_year - ref_year) * 12 + (del_month - ref_month)
            if months_diff >= 1:
                return f"M+{months_diff}"
            # months_diff <= 0 means delivery is the CURRENT (or a past) month, so
            # there is no forward 'M+0' - resolve it as balance-of-month / custom.
            return self._current_month_tenor(ref_date, contract_start, contract_end)

        # --- Non-standard contract -> balance of week / month, else Custom ----
        # e.g. a 9-day mid-week strip, a partial week (Wed->Sun), a partial month,
        # or a broker custom range. Checked: balance-of-week first (it is the
        # shorter, more specific period), then balance-of-month, else Custom.
        # Never forced into a standard +n, so it cannot contaminate a VWAP.
        else:
            if self._is_bow(ref_date, contract_start, contract_end):
                return "BOW"
            return self._current_month_tenor(ref_date, contract_start, contract_end)


# Backward-compatible alias: the class used to be the all-in-one EMEA processor.
# It is now the tenor-generation stage; the VWAP lives in GasVWAPEngine.
GasEMEAProcessor = GasTenorProcessor
