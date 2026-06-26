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
``tenor_objetive_gas_proccessor.py`` so the tenor and the periodicity always agree.
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
                 seasonal_type: str = "ROW"):
        self.df_raw = df
        self.start_col = start_col
        self.end_col = end_col
        self.periodicity_col = periodicity_col
        self.out_col = out_col
        self.seasonal_type = seasonal_type
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
            self.classify(s, e, h, self.seasonal_type)
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
    def classify(start, end, periodicity=None, seasonal_type: str = "ROW") -> str:
        """Canonical periodicity label for a single contract (e.g. 'Daily')."""
        bucket = Periodicity2Processor._classify_bucket(start, end, periodicity, seasonal_type)
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
        """True if [S, E] aligns (within tol_days) with a gas season window."""
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
    def _classify_bucket(start, end, periodicity=None, seasonal_type: str = "ROW") -> str:
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
        # A single gas day is encoded as span 0 (inclusive end) OR span 1
        # (exclusive end). Both are Daily. THIS is the fix for the 'Daily-as-Other'
        # bug: tools_gas only accepted span == 0.
        if span <= 1:
            return "DAILY"
        if 2 <= span <= 3 and S.weekday() in (4, 5):   # Fri/Sat start -> weekend block
            return "WEEKEND"
        if 6 <= span <= 8:
            return "WEEKLY"

        # --- Longer, duration-distinct buckets -------------------------------
        if 27 <= span <= 32:
            return "MONTHLY"
        if 88 <= span <= 93:
            return "QUARTERLY"
        # Season lengths differ by region (ROW ~181d; US winter ~150d, summer ~213d),
        # so use a wide pre-filter and let the calendar alignment decide. A ~6-month
        # block that does NOT align to a season window is treated as ambiguous.
        if 140 <= span <= 220:
            return "SEASONAL" if Periodicity2Processor._is_seasonal(S, E, seasonal_type) else (hint or "OTHER")
        if 360 <= span <= 368:
            return "ANNUAL"

        # --- Ambiguous span: defer to the column hint, else Other ------------
        return hint or "OTHER"
