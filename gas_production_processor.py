"""Gas Production Processor - rebuilds the production trade tables as a class.

WHAT THIS IS
------------
This is a class-based, fully documented replacement for the production notebook
(``NG_workbook_Production.ipynb``). It takes the raw SQL trade extract plus the
reference tables and produces the same three production tables the notebook does
(EMEA, APAC, AMERICAS) - but with the periodicity computed *correctly*.

WHY IT EXISTS (vs. the notebook + tools_gas)
--------------------------------------------
The notebook builds PERIODICITY in two messy steps:

  1. ``tools_gas.add_periodicity`` - a calendar-offset classifier that, among
     other issues, only flags Daily when CONTRACT_END == CONTRACT_START
     (``days_diff == 0``), missing single-day contracts encoded with an
     exclusive end (end = start + 1 day). It also assigns labels with a cascade
     of ``df.loc[mask] = label`` lines where *the last match wins*, so a row can
     end up with the wrong (less specific) label.

  2. Notebook cell 105 - a *text patch* that rescues some of those mistakes by
     reading keywords in LEG_DESCRIPTION0 ('day ahead', 'wd', 'Q1', 'summer'...),
     but only for rows already tagged 'Other'/'Semi-Annual', with a fragile
     substring regex, and ONLY on the RoW path (AMERICAS never gets patched).

This class collapses both into ONE method, :meth:`assign_periodicity`, that:
  * classifies from the contract DATES first (duration + season alignment),
  * accepts a single delivery day as span 0 OR span 1 (fixes the Daily bug),
  * gives each row exactly ONE label (no overwrite cascade - nothing "pisa" to
    anything else),
  * falls back to the description TEXT only when the dates are ambiguous, with a
    strict word-boundary regex, and
  * is applied identically to EMEA, APAC and AMERICAS (just a different
    ``seasonal_type``).

The bucketing thresholds here are intentionally identical to those in
``periodicity_2_processor.py`` and ``tenor_objetive_gas_proccessor.py`` so the
periodicity and the tenor always agree.

The stable, already-tested helpers (notes, date repair, strategy, row dropping)
are reused from ``tools_gas`` rather than re-implemented.

PIPELINE ORDER (see :meth:`run`)
--------------------------------
    load -> fix_dates -> add_notes -> fill_notional -> fix_options
         -> split_amers_row
         -> [RoW]   map_country_classification -> assign_venue
                    -> assign_periodicity(ROW) -> add_strategy -> add_blank_cols
                    -> split_emea_apac -> drop_bad_trades -> order_and_sort
         -> [AMERS] map_us_short_code -> map_us_leg_description
                    -> assign_periodicity(US) -> add_strategy -> add_blank_cols
                    -> order_and_sort
    -> {'EMEA': df, 'APAC': df, 'AMERICAS': df}
"""

from __future__ import annotations

import re
import numpy as np
import pandas as pd

import tools_gas


# --------------------------------------------------------------------------- #
#  Periodicity classification (the heart of this module)                      #
# --------------------------------------------------------------------------- #

# Internal bucket -> canonical label written to the PERIODICITY column.
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

# Text fallback rules (folds in notebook cell 105, but with STRICT word
# boundaries so 'da' no longer matches "Cana-da", "up-da-te", etc.).
# Order matters: first match wins. Applied ONLY when the dates are ambiguous.
_TEXT_RULES = [
    (re.compile(r"\bday[\s-]?ahead\b", re.I), "DAILY"),
    (re.compile(r"\bda\b", re.I), "DAILY"),
    (re.compile(r"\bwd\b", re.I), "DAILY"),
    (re.compile(r"\bq[1-4]\b", re.I), "QUARTERLY"),
    (re.compile(r"\b(?:sum(?:mer)?|win(?:ter)?)\b", re.I), "SEASONAL"),
]


class GasProductionProcessor:
    """Build the EMEA / APAC / AMERICAS production tables from raw inputs.

    Use :meth:`from_paths` to load everything from disk exactly like the
    notebook, or construct directly with already-loaded DataFrames (handy for
    testing). Then call :meth:`run`.
    """

    # AMERICAS trades are identified by the first token of LEG_DESCRIPTION1.
    AMERS_FIRST_TOKENS = ["Financially", "(HP)", "Natural", "Phys", "Fwd"]

    # ------------------------------------------------------------------ #
    #  Construction / loading                                            #
    # ------------------------------------------------------------------ #
    def __init__(
        self,
        trades: pd.DataFrame,
        ref_country: pd.DataFrame,
        ref_classification: pd.DataFrame,
        ref_australia: pd.DataFrame,
        ref_us: pd.DataFrame,
        ref_us_legdesc: pd.DataFrame,
        country_iso: pd.DataFrame,
    ):
        # Raw inputs (kept untouched; all work happens on copies).
        self.trades = trades
        self.ref_country = ref_country
        self.ref_classification = ref_classification
        self.ref_australia = ref_australia
        self.ref_us = ref_us
        self.ref_us_legdesc = ref_us_legdesc
        self.country_iso = country_iso

        # Lookups derived from the reference tables.
        self.iso_code = dict(zip(country_iso["Name"], country_iso["Code"]))
        self.iso_continent = dict(zip(country_iso["Name"], country_iso["Continent"]))
        self.classification_map = dict(
            zip(ref_classification["SHORT_CODE"], ref_classification["CLASSIFICATION_1"])
        )
        self.australia_venue = dict(
            zip(ref_australia["Australia hub"], ref_australia["Venue"])
        )

        # Filled in by run().
        self.outputs: dict[str, pd.DataFrame] = {}

    @classmethod
    def from_paths(
        cls,
        trades_csv: str,
        ref_row_xlsx: str = "REF_TABLES/GAS_TRADES_MAPPING_RoW.xlsx",
        ref_us_xlsx: str = "REF_TABLES/NG_US_MAPPING_TABLE.xlsx",
        country_iso_csv: str = "REF_TABLES/country_iso_codes_2.csv",
    ) -> "GasProductionProcessor":
        """Load the trades and all reference tables from disk (as the notebook does)."""
        return cls(
            trades=pd.read_csv(trades_csv),
            ref_country=pd.read_excel(ref_row_xlsx, sheet_name="Country mapping"),
            ref_classification=pd.read_excel(ref_row_xlsx, sheet_name="CLASSIFICATION_1 mapping"),
            ref_australia=pd.read_excel(ref_row_xlsx, sheet_name="Australia Venues mapping"),
            ref_us=pd.read_excel(ref_us_xlsx, sheet_name="Country_Mapping_Table"),
            ref_us_legdesc=pd.read_excel(ref_us_xlsx, sheet_name="Leg_description1_mapping"),
            country_iso=pd.read_csv(country_iso_csv),
        )

    # ------------------------------------------------------------------ #
    #  Orchestration                                                     #
    # ------------------------------------------------------------------ #
    def run(self) -> dict[str, pd.DataFrame]:
        """Run the full pipeline and return {'EMEA', 'APAC', 'AMERICAS'} DataFrames."""
        df = self.trades.copy()

        # --- Common enrichment (applies to every trade) ---
        df = self.fix_dates(df)
        df = self.add_notes(df)
        df = self.fill_notional(df)
        df = self.fix_options(df)

        # --- Split the two worlds: AMERICAS vs Rest-of-World ---
        df_amers, df_row = self.split_amers_row(df)

        # --- Rest-of-World branch (EMEA + APAC) ---
        df_row = self.map_country_classification(df_row)
        df_row = self.assign_venue(df_row)
        df_row = self.assign_periodicity(df_row, seasonal_type="ROW")  # ROW seasons
        df_row = tools_gas.add_strategy(df_row)
        df_row = self.add_blank_columns(df_row)
        df_row = self.finalise_row_country(df_row)
        df_emea, df_apac = self.split_emea_apac(df_row)
        df_emea, df_apac = self.drop_bad_trades(df_emea, df_apac)
        df_emea = self.order_and_sort(df_emea)
        df_apac = self.order_and_sort(df_apac)

        # --- AMERICAS branch ---
        df_amers = self.map_us_short_code(df_amers)
        df_amers = self.map_us_leg_description(df_amers)
        df_amers = df_amers.drop_duplicates(subset="DEAL_ID")
        df_amers = tools_gas.add_strategy(df_amers)
        df_amers = self.assign_venue_general(df_amers)
        df_amers = self.assign_periodicity(df_amers, seasonal_type="US")  # US seasons
        df_amers = self.add_blank_columns(df_amers, exchange_only=True)
        df_amers = self.fix_americas_strategy(df_amers)
        df_amers = self.order_and_sort(df_amers)

        self.outputs = {"EMEA": df_emea, "APAC": df_apac, "AMERICAS": df_amers}
        return self.outputs

    def export(self, out_dir: str = ".") -> None:
        """Write the three production CSVs (call after :meth:`run`)."""
        names = {
            "EMEA": "NG_trades_EMEA_SORT.csv",
            "APAC": "NG_trades_APAC_SORT.csv",
            "AMERICAS": "NG_trades_AMERICAS_SORT.csv",
        }
        for region, df in self.outputs.items():
            df.to_csv(f"{out_dir}/{names[region]}", index=False)

    # ------------------------------------------------------------------ #
    #  Common enrichment steps (reuse tools_gas where it already works)  #
    # ------------------------------------------------------------------ #
    def fix_dates(self, df: pd.DataFrame) -> pd.DataFrame:
        """Repair CONTRACT_END_DATE < CONTRACT_START_DATE from TERM_DESCRIPTION.

        This MUST run before periodicity, because periodicity is derived from
        (end - start). Also pushes the corrected date into OPTION_EXPIRY.
        """
        return tools_gas.fix_contract_end_dates(df, update_cols=["OPTION_EXPIRY"])

    def add_notes(self, df: pd.DataFrame) -> pd.DataFrame:
        """Tag the NOTES column from keywords in the two leg-description columns."""
        tags_descr1 = {"Heren Index": "heren", "TAPS": "taps", "TAS": "tas"}
        df = tools_gas.add_notes(df, text_col="LEG_DESCRIPTION1", out_col="NOTES", tags=tags_descr1)

        tags_descr0 = {
            "CSO": "cso", "BOM": "bom", "TAI: MIBGAS Index": "mibgas",
            "TAI: EGSI Index": "egsi", "GDAES": "gdaes", "GDAPT": "gdapt",
            "NDI": "ndi", "NGP": "ngp", "Day Ahead": ["da", "day ahead"],
            "API": "api", "LPI": "lpi",
        }
        df = tools_gas.add_notes(df, text_col="LEG_DESCRIPTION0", out_col="NOTES", tags=tags_descr0)
        return df

    def fill_notional(self, df: pd.DataFrame) -> pd.DataFrame:
        """Populate NOTIONAL_AMOUNT when it is missing but price/quantity units match."""
        mask = (df["PRICE_UNIT"] == df["QUANTITY_UNIT"]) & (df["NOTIONAL_AMOUNT"] == 0)
        df.loc[mask, "NOTIONAL_AMOUNT"] = (
            df.loc[mask, "DEAL_PRICE"] * df.loc[mask, "TOTAL_QUANTITY"]
        ).round(1)
        return df

    def fix_options(self, df: pd.DataFrame) -> pd.DataFrame:
        """Correct OPTION_STYLE / OPTION_TYPE from the description text."""
        def force(text_kw, col, target, conflicting):
            mask = (
                df["LEG_DESCRIPTION0"].str.contains(text_kw, case=False, regex=True)
                & ((df[col] == conflicting) | (df[col].isna()))
            )
            df.loc[mask, col] = target

        force("american", "OPTION_STYLE", "American", "European")
        force("european", "OPTION_STYLE", "European", "American")
        force("call", "OPTION_TYPE", "Call", "Put")
        force("put", "OPTION_TYPE", "Put", "Call")
        return df

    # ------------------------------------------------------------------ #
    #  AMERICAS vs Rest-of-World split                                   #
    # ------------------------------------------------------------------ #
    def split_amers_row(self, df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Split into (AMERICAS, RoW) using the first token of LEG_DESCRIPTION1."""
        df = df.copy()
        df["SHORT_CODE"] = df["LEG_DESCRIPTION1"].str.split().str[0]
        amers = df[df["SHORT_CODE"].isin(self.AMERS_FIRST_TOKENS)]
        row = df[~df.index.isin(amers.index)]
        return amers.copy(), row.copy()

    # ------------------------------------------------------------------ #
    #  Rest-of-World mappings                                            #
    # ------------------------------------------------------------------ #
    def map_country_classification(self, df: pd.DataFrame) -> pd.DataFrame:
        """Assign COUNTRY, COUNTRY_CODE, CONTINENT and CLASSIFICATION_1 (the VWAP product)."""
        df = df.merge(self.ref_country, on="SHORT_CODE", how="left")

        # Audit: which SHORT_CODEs failed to map to a country?
        self._unmatched_country = (
            df.loc[df["COUNTRY"].isna(), "LEG_DESCRIPTION1"].dropna().unique()
        )

        df["COUNTRY_CODE"] = df["COUNTRY"].map(self.iso_code)
        df["CONTINENT"] = df["COUNTRY"].map(self.iso_continent)

        def classify_row(row):
            if row["SHORT_CODE"] == "Australian":
                # Australian classification comes from the first word of LEG_DESCRIPTION0.
                return row["LEG_DESCRIPTION0"].split()[0] if pd.notnull(row["LEG_DESCRIPTION0"]) else None
            return self.classification_map.get(row["SHORT_CODE"])

        df["CLASSIFICATION_1"] = df.apply(classify_row, axis=1)
        return df

    def assign_venue(self, df: pd.DataFrame) -> pd.DataFrame:
        """Assign VENUE: New Zealand, Australia (by hub) and then the general rules."""
        mask_nz = df["VENUE"].isna() & (df["COUNTRY"] == "New Zealand")
        df.loc[mask_nz, "VENUE"] = "EMSTP"

        mask_aus = df["VENUE"].isna() & (df["COUNTRY"] == "Australia")
        df.loc[mask_aus, "VENUE"] = df.loc[mask_aus, "CLASSIFICATION_1"].map(self.australia_venue)

        return self.assign_venue_general(df)

    @staticmethod
    def assign_venue_general(df: pd.DataFrame) -> pd.DataFrame:
        """Spot-forward -> XOFF, BLT description -> BLT, specific NYMEX strings -> NGXC."""
        mask_xoff = df["VENUE"].isna() & (df["TRANSACTION_TYPE_ISDA"] == "Spot Fwd")
        df.loc[mask_xoff, "VENUE"] = "XOFF"

        mask_blt = df["VENUE"].isna() & df["LEG_DESCRIPTION1"].str.contains("BLT", na=False)
        df.loc[mask_blt, "VENUE"] = "BLT"

        leg_list = [
            "Financially settled NYMEX Option ICE Cleared: Fwd vs. NYMEX Penultimate Day Settle",
            "Financially settled NYMEX Option NYMEX Cleared: Fwd vs. NYMEX Penultimate Day Settle",
        ]
        mask_ngxc = df["VENUE"].isna() & df["LEG_DESCRIPTION1"].isin(leg_list)
        df.loc[mask_ngxc, "VENUE"] = "NGXC"
        return df

    def finalise_row_country(self, df: pd.DataFrame) -> pd.DataFrame:
        """Drop the long country name and promote the ISO code to COUNTRY; round premium."""
        df.loc[df["VENUE"] == "CMEX", "VENUE"] = "XCME"  # TIBCO typo
        df["PREMIUM_PER_UNIT"] = df["PREMIUM_PER_UNIT"].round(3)
        df = df.drop(columns="COUNTRY")
        df = df.rename(columns={"COUNTRY_CODE": "COUNTRY"})
        return df

    def split_emea_apac(self, df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Split RoW into EMEA and APAC & Oceania by CONTINENT."""
        emea = df.loc[df["CONTINENT"] == "EMEA", :].copy()
        apac = df.loc[df["CONTINENT"] == "APAC and Oceania", :].copy()
        return emea, apac

    def drop_bad_trades(self, emea: pd.DataFrame, apac: pd.DataFrame):
        """Remove unexplained negative-price trades (EMEA) and JKM noise (APAC)."""
        emea = tools_gas.drop_rows(emea, [
            ("NOTES", "not in", ["TAPS", "TAS", "Heren", "cso"]),
            ("STRATEGY", "not in", ["Spread"]),
            ("DEAL_PRICE", "<=", 0),
        ])
        apac = tools_gas.drop_rows(apac, [
            ("CLASSIFICATION_1", "not in", ["JKM"]),
            ("DEAL_PRICE", "<=", 1),
        ])
        # JKM with no second classification is an outright block.
        mask = (apac["CLASSIFICATION_1"] == "JKM") & apac["CLASSIFICATION_2"].isna()
        apac.loc[mask, "STRATEGY"] = "Outright"
        apac.loc[mask, "NOTES"] = "Block"
        return emea, apac

    # ------------------------------------------------------------------ #
    #  AMERICAS mappings                                                 #
    # ------------------------------------------------------------------ #
    def map_us_short_code(self, df: pd.DataFrame) -> pd.DataFrame:
        """Re-derive SHORT_CODE for AMERICAS from LEG_DESCRIPTION0 and merge US table."""
        df = df.drop(columns=["SHORT_CODE"])

        valid = {c.lower() for c in self.ref_us["SHORT_CODE"].dropna().unique()}
        delimiters = ["_Fin", "Basis", "_", " ", "_Phys"]

        def extract(description):
            if pd.isna(description):
                return None
            for delim in delimiters:
                candidate = description.split(delim)[0]
                if candidate.lower() in valid:
                    return candidate
            return None

        df["SHORT_CODE"] = df["LEG_DESCRIPTION0"].apply(extract)

        df = (
            df.assign(_merge_key=df["SHORT_CODE"].str.lower())
            .merge(
                self.ref_us.assign(_merge_key=self.ref_us["SHORT_CODE"].str.lower()),
                on="_merge_key", how="left",
            )
            .drop(columns=["_merge_key", "SHORT_CODE_y"])
            .rename(columns={"SHORT_CODE_x": "SHORT_CODE"})
        )

        self._unmatched_amers = (
            df.loc[df["SHORT_CODE"].isna(), "LEG_DESCRIPTION0"].drop_duplicates()
        )
        return df

    def map_us_leg_description(self, df: pd.DataFrame) -> pd.DataFrame:
        """Merge the US leg-description reference columns onto AMERICAS trades."""
        df = df.merge(
            self.ref_us_legdesc, on="LEG_DESCRIPTION1", how="left", indicator="merge_result"
        )
        self._unmatched_amers_leg = (
            df.loc[df["merge_result"] == "left_only", "LEG_DESCRIPTION1"].drop_duplicates()
        )
        return df

    def fix_americas_strategy(self, df: pd.DataFrame) -> pd.DataFrame:
        """A CSO-noted outright is actually a spread."""
        mask = (df["STRATEGY"] == "Outright") & (df["NOTES"] == "CSO")
        df.loc[mask, "STRATEGY"] = "Spread"
        return df

    # ------------------------------------------------------------------ #
    #  Shared finishing steps                                            #
    # ------------------------------------------------------------------ #
    @staticmethod
    def add_blank_columns(df: pd.DataFrame, exchange_only: bool = False) -> pd.DataFrame:
        """Create the placeholder columns the downstream schema expects."""
        df["EXCHANGE_CODE"] = ""
        df["UNDERLYING_EXCHANGE_CODE"] = ""
        if exchange_only:
            df.loc[df["VENUE"] == "CMEX", "VENUE"] = "XCME"
            return df
        for col in ("ZONE", "REGION", "CONTRACT_PAYMENT_SPECIFICATIONS",
                    "CONTRACT_PAYMENT_LEG1", "CONTRACT_PAYMENT_LEG2"):
            df[col] = ""
        return df

    @staticmethod
    def order_and_sort(df: pd.DataFrame) -> pd.DataFrame:
        """Select/reorder the production columns and sort chronologically."""
        cols = [
            "ASSET_CLASS_ISDA", "BASE_PRODUCT_ISDA", "SUB_PRODUCT_ISDA", "TRANSACTION_TYPE_ISDA",
            "EVENT_TIMESTAMP", "DEAL_EXECUTION_DATETIME", "DEAL_ID", "EXECUTION_CHAIN_ID",
            "DEAL_STATUS", "VENUE", "DEAL_TYPE", "STRATEGY", "COUNTRY", "REGION", "ZONE",
            "CLASSIFICATION_1", "CLASSIFICATION_2", "CONTRACT_START_DATE", "CONTRACT_END_DATE",
            "TERM_DESCRIPTION", "PERIODICITY", "OPTION_EXPIRY", "STRIKE_PRICE_PER_UNIT",
            "OPTION_TYPE", "OPTION_STYLE", "PREMIUM_PER_UNIT", "SETTLEMENT_METHOD", "DEAL_PRICE",
            "PRICE_UNIT", "QUANTITY_UNIT", "CURRENCY", "NOTIONAL_AMOUNT", "QUANTITY",
            "TOTAL_QUANTITY", "QUANTITY_FREQ", "EXCHANGE_CODE", "UNDERLYING_EXCHANGE_CODE",
            "CONTRACT_PAYMENT_SPECIFICATIONS", "CONTRACT_PAYMENT_LEG1", "CONTRACT_PAYMENT_LEG2",
            "NOTES",
        ]
        df = df[[c for c in cols if c in df.columns]]
        return df.sort_values(
            by=["DEAL_EXECUTION_DATETIME", "EXECUTION_CHAIN_ID"], ascending=[True, True]
        )

    # ================================================================== #
    #  PERIODICITY - the corrected, single-pass classifier               #
    # ================================================================== #
    def assign_periodicity(
        self,
        df: pd.DataFrame,
        seasonal_type: str = "ROW",
        text_col: str = "LEG_DESCRIPTION0",
    ) -> pd.DataFrame:
        """Write a correct PERIODICITY column in ONE pass (replaces add_periodicity + cell 105).

        For each row, exactly one label is decided by :meth:`classify_periodicity`:
        dates first, description text only as a tie-breaker when the dates are
        ambiguous. No overwrite cascade, so nothing can be silently re-labelled.

        Parameters
        ----------
        df : DataFrame with CONTRACT_START_DATE / CONTRACT_END_DATE.
        seasonal_type : 'ROW' (EMEA/APAC) or 'US' (AMERICAS) - controls the
            summer/winter window used for the Seasonal bucket.
        text_col : description column used for the text fallback.
        """
        df = df.copy()
        start = pd.to_datetime(df["CONTRACT_START_DATE"], errors="coerce")
        end = pd.to_datetime(df["CONTRACT_END_DATE"], errors="coerce")
        text = df[text_col] if text_col in df.columns else pd.Series([""] * len(df), index=df.index)

        df["PERIODICITY"] = [
            self.classify_periodicity(s, e, t, seasonal_type)
            for s, e, t in zip(start, end, text)
        ]
        return df

    @classmethod
    def classify_periodicity(cls, start, end, text="", seasonal_type="ROW") -> str:
        """Return ONE canonical periodicity label (e.g. 'Daily') for a single contract.

        Step 1 - dates: classify from the contract span and calendar alignment.
        Step 2 - text : only if the dates were ambiguous ('Other'), look for an
                        explicit keyword (Q1, Summer, WD, Day Ahead) in the
                        description and use that instead.
        """
        bucket = cls._bucket_from_dates(start, end, seasonal_type)
        if bucket == "OTHER":
            text_bucket = cls._bucket_from_text(text)
            if text_bucket is not None:
                bucket = text_bucket
        return _LABELS.get(bucket, "Other")

    # ---- Step 1: from the dates --------------------------------------- #
    @staticmethod
    def _bucket_from_dates(start, end, seasonal_type="ROW") -> str:
        """Duration + calendar-alignment classifier. Returns an UPPERCASE bucket.

        Key fix vs. tools_gas: a single delivery day is accepted whether it is
        encoded as span 0 (inclusive end) OR span 1 (exclusive end).
        """
        if pd.isna(start):
            return "OTHER"
        S = pd.Timestamp(start).normalize()
        if pd.isna(end):
            return "DAILY"          # a start with no end is most likely one day
        E = pd.Timestamp(end).normalize()

        span = (E - S).days
        if span < 0:
            return "OTHER"

        # --- short end ---
        if span <= 1:                                  # 0 or 1 day = one gas day
            return "DAILY"
        if 2 <= span <= 3 and S.weekday() in (4, 5):   # Fri/Sat start = weekend block
            return "WEEKEND"
        if 6 <= span <= 8:
            return "WEEKLY"

        # --- longer, duration-distinct buckets ---
        if 27 <= span <= 32:
            return "MONTHLY"
        if 88 <= span <= 93:
            return "QUARTERLY"
        # Season lengths differ by region (ROW ~181d; US winter ~150d, summer ~213d),
        # so use a wide pre-filter and let the calendar alignment decide.
        if 140 <= span <= 220:
            return "SEASONAL" if GasProductionProcessor._is_seasonal(S, E, seasonal_type) else "OTHER"
        if 360 <= span <= 368:
            return "ANNUAL"

        # anything else is genuinely non-standard
        return "OTHER"

    @staticmethod
    def _is_seasonal(S: pd.Timestamp, E: pd.Timestamp, seasonal_type="ROW", tol_days=3) -> bool:
        """True if [S, E] aligns (within tol_days) with a gas season window."""
        y = S.year
        if seasonal_type == "US":
            windows = [
                (pd.Timestamp(y, 4, 1), pd.Timestamp(y, 10, 31)),      # Summer Apr-Oct
                (pd.Timestamp(y, 11, 1), pd.Timestamp(y + 1, 3, 31)),  # Winter Nov-Mar
            ]
        else:  # ROW
            windows = [
                (pd.Timestamp(y, 4, 1), pd.Timestamp(y, 9, 30)),       # Summer Apr-Sep
                (pd.Timestamp(y, 10, 1), pd.Timestamp(y + 1, 3, 31)),  # Winter Oct-Mar
            ]
        return any(
            abs((S - ws).days) <= tol_days and abs((E - we).days) <= tol_days
            for ws, we in windows
        )

    # ---- Step 2: from the description text (fallback only) ------------ #
    @staticmethod
    def _bucket_from_text(text) -> str | None:
        """Map an explicit keyword in the description to a bucket, else None.

        Strict word boundaries, so 'da' no longer matches inside other words -
        the bug in notebook cell 105.
        """
        if text is None or (isinstance(text, float) and pd.isna(text)):
            return None
        s = str(text)
        for pattern, bucket in _TEXT_RULES:
            if pattern.search(s):
                return bucket
        return None
