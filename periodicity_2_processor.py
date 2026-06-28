"""Periodicity_2 Processor - recomputes a corrected periodicity from contract dates.

Why this exists
---------------
The production PERIODICITY column (written by ``tools_gas.add_periodicity`` inside
the notebook) is sometimes wrong. The clearest case: it only flags ``Daily`` when
CONTRACT_END == CONTRACT_START (``days_diff == 0``), so a single delivery day
encoded with an *exclusive* end (end = start + 1 day) is missed and falls through
to ``Other``.

This processor produces a *second opinion*, ``PERIODICITY_2``, derived directly
from the contract DATES (duration + calendar/season alignment), and only consults
the original ``PERIODICITY`` as a fallback when the dates are missing or genuinely
ambiguous. It does NOT modify the production pipeline - run it on the output and
compare the two columns (see ``audit``) to find the rows that were mislabelled.

The bucketing logic here is intentionally identical to ``_classify_period`` in
``periodicity_tenor_generation.py`` so the tenor and the periodicity always agree.
"""

import pandas as pd
import numpy as np


# Internal bucket -> canonical label (matches the notebook's PERIODICITY casing)
_LABELS = {
    "DAILY": "Daily",
    "WEEKEND": "Weekend",
    "WEEKLY": "Weekly",
    "MONTHLY": "Monthly",
    "QUARTERLY": "Quarterly",
    "SEASONAL": "Seasonal",
    "ANNUAL": "Annual",
    "OTHER": "Other",
}


class Periodicity2Processor:
    """Adds a ``PERIODICITY_2`` column computed from the contract dates.

    Parameters
    ----------
    df : pd.DataFrame
        Trades with contract start/end dates (and optionally a PERIODICITY column).
    start_col, end_col : str
        Names of the contract start / end datetime columns.
    periodicity_col : str
        Existing periodicity column, used ONLY as a fallback hint.
    out_col : str
        Name of the column to write the corrected periodicity into.
    seasonal_type : 'ROW' (Apr-Sep / Oct-Mar) or 'US' (Apr-Oct / Nov-Mar).
    """

    def __init__(self, df: pd.DataFrame, *,
                 start_col: str = "CONTRACT_START_DATE",
                 end_col: str = "CONTRACT_END_DATE",
                 periodicity_col: str = "PERIODICITY",
                 out_col: str = "PERIODICITY_2",
                 seasonal_type: str = "ROW",
                 month_tolerance_days: int = 2,
                 week_tolerance_days: int = 1,
                 quarter_tolerance_days: int = 2):
        self.df_raw = df
        self.start_col = start_col
        self.end_col = end_col
        self.periodicity_col = periodicity_col
        self.out_col = out_col
        self.seasonal_type = seasonal_type
        self.month_tolerance_days = month_tolerance_days
        self.week_tolerance_days = week_tolerance_days
        self.quarter_tolerance_days = quarter_tolerance_days
        self.df: pd.DataFrame | None = None

    # --- Public API ----------------------------------------------------------

    def run(self) -> pd.DataFrame:
        """Return a copy of the input DataFrame with ``out_col`` added."""
        df = self.df_raw.copy()

        start = pd.to_datetime(df[self.start_col], errors="coerce")
        end = pd.to_datetime(df[self.end_col], errors="coerce")
        if self.periodicity_col in df.columns:
            hint = df[self.periodicity_col]
        else:
            hint = pd.Series([None] * len(df), index=df.index)

        df[self.out_col] = [
            self.classify(s, e, h, self.seasonal_type,
                          self.month_tolerance_days, self.week_tolerance_days,
                          self.quarter_tolerance_days)
            for s, e, h in zip(start, end, hint)
        ]

        self.df = df
        return df

    def audit(self) -> pd.DataFrame:
        """Return only the rows where ``PERIODICITY_2`` disagrees with the original.

        Handy for finding exactly which production rows were mislabelled.
        """
        if self.df is None:
            self.run()
        df = self.df
        if self.periodicity_col not in df.columns:
            return df.iloc[0:0]
        old = df[self.periodicity_col].astype(str).str.strip().str.lower()
        new = df[self.out_col].astype(str).str.strip().str.lower()
        return df[old != new]

    # --- Classification (dates primary, column fallback) ---------------------

    @staticmethod
    def classify(start, end, periodicity=None, seasonal_type: str = "ROW",
                 month_tolerance_days: int = 2, week_tolerance_days: int = 1,
                 quarter_tolerance_days: int = 2) -> str:
        """Canonical periodicity label for a single contract (e.g. 'Daily')."""
        bucket = Periodicity2Processor._classify_bucket(
            start, end, periodicity, seasonal_type, month_tolerance_days,
            week_tolerance_days, quarter_tolerance_days)
        return _LABELS.get(bucket, "Other")

    @staticmethod
    def _normalize_periodicity(periodicity) -> str | None:
        """Normalise the hint column, or None when it carries no usable signal."""
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

        Start and end may each be off a month boundary by up to ``tol_days`` in
        either direction (e.g. 30-Nov -> 31-Dec is a December month; 01-Dec ->
        01-Jan too).

        Note: this module has no reference date, so a partial month is left as
        'Other' here; whether it is a Balance-of-Month is decided by the tenor
        processor (which knows the reference date).

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
        """Test whether ``[S, E]`` is an aligned calendar quarter.

        The ideal quarter starts on a quarter boundary (1-Jan / 1-Apr / 1-Jul /
        1-Oct) and ends on the following boundary; start and end may each be off
        that boundary by up to ``tol_days`` in either direction (e.g. 30-Sep ->
        31-Dec is Q4; 01-Oct -> 01-Jan too). A mid-quarter strip (e.g. start on
        the 15th) is NOT quarterly.

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
            q_month = ((d.month - 1) // 3) * 3 + 1   # quarter start month: 1,4,7,10
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
    def _is_weekend(S: pd.Timestamp, E: pd.Timestamp) -> bool:
        """Test whether ``[S, E]`` is a Saturday->Monday weekend (Sat+Sun).

        e.g. Sat 18th -> Mon 20th (span 2). A Sat -> Sun (span 1) is a Saturday
        daily, not a weekend.

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

        Start within ``tol_days`` of a Monday; end within ``tol_days`` of that
        week's Sunday or the following Monday. A misaligned strip (Fri -> Sat) is
        NOT weekly.

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
        wd = S.weekday()
        if min(wd, 7 - wd) > tol_days:
            return False
        s_monday = S - pd.Timedelta(days=wd) if wd <= 3 else S + pd.Timedelta(days=7 - wd)
        sunday = s_monday + pd.Timedelta(days=6)
        next_monday = s_monday + pd.Timedelta(days=7)
        return (abs((E - sunday).days) <= tol_days
                or abs((E - next_monday).days) <= tol_days)

    @staticmethod
    def _classify_bucket(start, end, periodicity=None, seasonal_type: str = "ROW",
                         month_tolerance_days: int = 2, week_tolerance_days: int = 1,
                         quarter_tolerance_days: int = 2) -> str:
        """Derive the period bucket from the DATES, with the column as fallback.

        Returns one of:
            DAILY, WEEKEND, WEEKLY, MONTHLY, QUARTERLY, SEASONAL, ANNUAL, OTHER
        """
        hint = Periodicity2Processor._normalize_periodicity(periodicity)

        S, E = start, end
        if pd.isna(S):                      # no start date -> trust the column
            return hint or "OTHER"
        S = pd.Timestamp(S).normalize()

        if pd.isna(E):                      # no end date -> most likely a single day
            return hint or "DAILY"
        E = pd.Timestamp(E).normalize()

        span = (E - S).days
        if span < 0:
            return hint or "OTHER"

        # --- Short end -------------------------------------------------------
        # Weekend = Saturday -> Monday (span 2): Sat+Sun delivered as one block.
        if Periodicity2Processor._is_weekend(S, E):
            return "WEEKEND"
        # A single gas day is encoded as span 0 (inclusive end) OR span 1
        # (exclusive end). Both are Daily (incl. a lone Saturday, Sat -> Sun).
        # THIS fixed the 'Daily-as-Other' bug: tools_gas only accepted span == 0.
        if span <= 1:
            return "DAILY"
        # Weekly = an aligned Mon->Sun(/next Mon) week (start Monday +/- margin).
        if Periodicity2Processor._is_calendar_week(S, E, week_tolerance_days):
            return "WEEKLY"

        # --- Longer, duration-distinct buckets -------------------------------
        if Periodicity2Processor._is_calendar_month(S, E, month_tolerance_days):
            return "MONTHLY"
        # Quarterly = an aligned calendar quarter (start on a quarter boundary
        # +/- margin, end on the following boundary +/- margin).
        if Periodicity2Processor._is_calendar_quarter(S, E, quarter_tolerance_days):
            return "QUARTERLY"
        # Season lengths differ by region (ROW ~181d; US winter ~150d, summer ~213d),
        # so use a wide pre-filter and let the calendar alignment decide.
        if 140 <= span <= 220:
            return "SEASONAL" if Periodicity2Processor._is_seasonal(S, E, seasonal_type) else "OTHER"
        if 360 <= span <= 368:
            return "ANNUAL"

        # --- Span fits NO standard bucket -> non-standard contract. ----------
        # We do NOT fall back to the column here: a span matching no bucket
        # cannot be Daily/Weekly/Monthly/etc., so trusting a (possibly wrong)
        # PERIODICITY would re-introduce the bug (e.g. a 21-day strip tagged
        # 'Daily'). The column is only used when the DATES are unusable.
        return "OTHER"
