"""Gas EMEA VWAP Processor - calculates VWAP per reference date, product, and tenor.

Input:  Raw DataFrame of gas trades (from Snowflake or CSV) with columns:
            DEAL_EXECUTION_DATETIME, CLASSIFICATION_1, DEAL_PRICE, QUANTITY,
            CONTRACT_START_DATE, CONTRACT_END_DATE, PERIODICITY

Output: DataFrame with columns:
            reference_date, weekday, product, tenor, vwap, total_volume, trade_count
            (long format - one row per date x product x tenor)

        Optionally pivoted to wide format with products side by side.
"""

import pandas as pd
import numpy as np


class GasEMEAProcessor:
    """Processes raw gas EMEA trades into a VWAP table by reference date, product, and tenor.

    Attributes
    ----------
    df_raw : pd.DataFrame
        The original input DataFrame (untouched).
    df_trades : pd.DataFrame
        Cleaned trades with a 'tenor' column assigned to each trade.
    df_vwap : pd.DataFrame
        Aggregated VWAP result (reference_date, weekday, product, tenor, vwap, ...).
    """

    def __init__(self, df: pd.DataFrame, seasonal_type: str = "ROW"):
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
        """
        self.df_raw = df.copy()
        self.seasonal_type = seasonal_type
        self.df_trades: pd.DataFrame = pd.DataFrame()
        self.df_vwap: pd.DataFrame = pd.DataFrame()

    # --- Public API ----------------------------------------------------------

    def run(self) -> pd.DataFrame:
        """Execute the full pipeline: clean -> assign tenors -> compute VWAP.

        Returns
        -------
        DataFrame with columns:
            reference_date, weekday, product, tenor, vwap, total_volume, trade_count
        Also stored in self.df_vwap.
        The intermediate trades with tenor assigned are in self.df_trades.
        """
        self._clean()
        self._assign_tenors()
        self._compute_vwap()
        return self.df_vwap

    def pivot_wide(self) -> pd.DataFrame:
        """Pivot the VWAP table to wide format with products side by side.

        Returns
        -------
        DataFrame with columns:
            reference_date, weekday, tenor, NBP_vwap, TTF_vwap, PSV_vwap, ...
        """
        if self.df_vwap.empty:
            raise ValueError("Run .run() first before pivoting.")

        wide = self.df_vwap.pivot_table(
            index=["reference_date", "weekday", "tenor"],
            columns="product",
            values="vwap",
            aggfunc="first",
        )
        wide.columns = [f"{col}_vwap" for col in wide.columns]
        wide = wide.reset_index().sort_values(
            ["reference_date", "tenor"]
        ).reset_index(drop=True)
        return wide

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
        """Compute the tenor label for each trade and store in self.df_trades['tenor']."""
        self.df_trades["tenor"] = self.df_trades.apply(
            lambda r: self._compute_tenor(
                r["reference_date"],
                r["CONTRACT_START_DATE"],
                r["CONTRACT_END_DATE"],
                r.get("PERIODICITY", "other"),
            ),
            axis=1,
        )

    def _compute_vwap(self):
        """Aggregate trades into VWAP per (reference_date, product, tenor)."""
        df = self.df_trades.copy()
        df["_pv"] = df["DEAL_PRICE"] * df["QUANTITY"]

        grouped = (
            df.groupby(["reference_date", "CLASSIFICATION_1", "tenor"], observed=True)
            .agg(
                pv_sum=("_pv", "sum"),
                total_volume=("QUANTITY", "sum"),
                trade_count=("DEAL_PRICE", "count"),
            )
            .reset_index()
        )

        grouped["vwap"] = grouped["pv_sum"] / grouped["total_volume"]
        grouped["weekday"] = grouped["reference_date"].dt.day_name()

        result = grouped.rename(columns={"CLASSIFICATION_1": "product"})
        self.df_vwap = result[
            ["reference_date", "weekday", "product", "tenor", "vwap",
             "total_volume", "trade_count"]
        ].sort_values(["reference_date", "product", "tenor"]).reset_index(drop=True)

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
        # A single gas day is encoded as span 0 (inclusive end) OR span 1
        # (exclusive end). Both are Daily.
        if span <= 1:
            return "DAILY"
        # Fri/Sat start spanning 2-3 days = a weekend / long-weekend block.
        if 2 <= span <= 3 and S.weekday() in (4, 5):
            return "WEEKEND"
        if 6 <= span <= 8:
            return "WEEKLY"

        # --- Longer, duration-distinct buckets -------------------------------
        if 27 <= span <= 32:
            return "MONTHLY"
        if 88 <= span <= 93:
            return "QUARTERLY"
        # Season lengths differ by region (ROW ~181d; US winter ~150d, summer ~213d),
        # so use a wide pre-filter and let the calendar alignment decide.
        if 140 <= span <= 220:
            return "SEASONAL" if self._is_seasonal(S, E, self.seasonal_type) else (hint or "OTHER")
        if 360 <= span <= 368:
            return "ANNUAL"

        # --- Ambiguous span: defer to the column hint, else Other ------------
        return hint or "OTHER"

    def _compute_tenor(self, ref_date: pd.Timestamp, contract_start: pd.Timestamp,
                       contract_end: pd.Timestamp, periodicity=None) -> str:
        """Derive the market tenor label from reference date and contract dates.

        The period bucket is computed from the contract dates via
        ``_classify_period`` (PERIODICITY column is only a fallback hint).

        Convention:
            Daily      -> D+n  (days ahead from reference date)
            Weekly     -> W+n  (weeks ahead; 1-indexed via ceiling-Monday snap)
            Weekend    -> WE / WE+n (weekend periods ahead; same-week weekend = WE)
            Monthly    -> M+n  (months ahead, delivery month from contract_end)
            Quarterly  -> Q+n  (quarters ahead)
            Seasonal   -> Sum{yy} / Win{yy}
            Annual     -> Cal{yy}
        """
        ref_year = ref_date.year
        ref_month = ref_date.month

        bucket = self._classify_period(contract_start, contract_end, periodicity)

        # --- Daily -----------------------------------------------------------
        if bucket == "DAILY":
            delivery_day = contract_start
            days_diff = (delivery_day - ref_date).days
            return f"D+{days_diff}"

        # --- Weekly: snap contract_start to ceiling Monday ------------------
        elif bucket == "WEEKLY":
            cs_monday = self._snap_to_monday(contract_start)
            ref_monday = ref_date - pd.Timedelta(days=ref_date.weekday())
            weeks_diff = int((cs_monday - ref_monday).days / 7)
            return f"W+{weeks_diff}"

        # --- Weekend: 1-indexed (same-week weekend = WE) -------------------
        elif bucket == "WEEKEND":
            ref_monday = ref_date - pd.Timedelta(days=ref_date.weekday())
            cs_monday = contract_start - pd.Timedelta(days=contract_start.weekday())
            weeks_diff = int((cs_monday - ref_monday).days / 7)
            return "WE" if weeks_diff == 0 else f"WE+{weeks_diff}"

        # --- Quarterly -------------------------------------------------------
        elif bucket == "QUARTERLY":
            cs_year = contract_start.year
            cs_month = contract_start.month
            ref_quarter = (ref_month - 1) // 3
            cs_quarter = (cs_month - 1) // 3
            quarters_diff = (cs_year - ref_year) * 4 + (cs_quarter - ref_quarter)
            return f"Q+{quarters_diff}"

        # --- Seasonal --------------------------------------------------------
        elif bucket == "SEASONAL":
            cs_month = contract_start.month
            cs_year = contract_start.year
            if cs_month in (4, 5, 6, 7, 8, 9):
                return f"Sum{cs_year % 100:02d}"
            else:
                win_year = cs_year if cs_month >= 10 else cs_year - 1
                return f"Win{win_year % 100:02d}"

        # --- Annual ----------------------------------------------------------
        elif bucket == "ANNUAL":
            cs_year = contract_start.year
            return f"Cal{cs_year % 100:02d}"

        # --- Monthly -> M+n using delivery month from end date --------------
        elif bucket == "MONTHLY":
            del_year, del_month = self._delivery_month(contract_start, contract_end)
            months_diff = (del_year - ref_year) * 12 + (del_month - ref_month)
            return f"M+{months_diff}"

        # --- Non-standard contract -> its own label -------------------------
        # e.g. a 9-day strip starting mid-week, a balance-of-month, a broker
        # custom date range. We deliberately do NOT force it into M+n, so it
        # never gets pooled into (and contaminate) a standard-bucket VWAP.
        else:
            return "Custom"
