"""Outright VWAP engine for POWER trades.

Power analogue of the gas ``GasOutrightEngine``. It consumes the enriched trades
from a power tenor processor (``PowerROWTenorProcessor`` /
``PowerAMERTenorProcessor``) and computes Outright VWAPs on demand.

Power differences vs gas
------------------------
* There is **no GAS/LNG split**. Instead every VWAP carries a ``unit`` =
  ``CURRENCY/PRICE_UNIT`` (e.g. ``GBP/MWh``, ``USD/MWh``), because a power price
  is only comparable within one currency+unit. ``unit`` is a field in the **long**
  output.
* The **product NAME is built here** (not in the tenor stage) as
  ``COUNTRY_REGION_ZONE_CLASSIFICATION_UNIT`` (empty segments dropped), e.g.
  ``GB_Base load_GBP/MWh`` or ``US_PJM_West Hub_Off-peak_USD/MWh``. The unit is
  embedded so different currencies never share a product.
* In the **wide** output there is one column per product,
  ``f"{product}_vwap"`` - already unit-separated because the unit is in the name.

Two sub-calculations (same as gas): ``vwap()`` for linear instruments
(Future / Spot Fwd) and ``vwap_options()`` for option premiums.
"""

import pandas as pd
import numpy as np


class PowerOutrightEngine:
    """Compute Outright power VWAPs from enriched trades, on demand.

    Parameters
    ----------
    trades : pd.DataFrame
        Enriched per-trade frame from a power tenor processor (needs
        ``reference_date``, ``product``, ``periodicity_2``, ``tenor``, ``tenor2``,
        ``DEAL_PRICE``, ``QUANTITY`` and the price-unit / currency columns).
    vwap_filters : dict, optional
        Per-granularity computation window (hours / weekdays). Default: count all.
    *_col : str
        Configurable column names.
    """

    def __init__(self, trades: pd.DataFrame, *,
                 vwap_filters: dict | None = None,
                 instrument_col: str = "TRANSACTION_TYPE_ISDA",
                 strategy_col: str = "STRATEGY",
                 product_col: str = "product",
                 country_col: str = "COUNTRY",
                 region_col: str = "REGION",
                 zone_col: str = "ZONE",
                 classification_col: str = "CLASSIFICATION",
                 price_unit_col: str = "PRICE_UNIT",
                 currency_col: str = "CURRENCY",
                 datetime_col: str = "DEAL_EXECUTION_DATETIME",
                 option_type_col: str = "OPTION_TYPE",
                 strike_col: str = "STRIKE_PRICE_PER_UNIT",
                 option_expiry_col: str = "OPTION_EXPIRY",
                 option_style_col: str = "OPTION_STYLE"):
        self.trades = trades.copy()
        self.vwap_filters = vwap_filters or {}
        self.instrument_col = instrument_col
        self.strategy_col = strategy_col
        self.product_col = product_col
        self.country_col = country_col
        self.region_col = region_col
        self.zone_col = zone_col
        self.classification_col = classification_col
        self.price_unit_col = price_unit_col
        self.currency_col = currency_col
        self.datetime_col = datetime_col
        self.option_type_col = option_type_col
        self.strike_col = strike_col
        self.option_expiry_col = option_expiry_col
        self.option_style_col = option_style_col

        self._ensure_unit()
        self._build_product()

        self.df_vwap: pd.DataFrame = pd.DataFrame()
        self.df_vwap_tenor2: pd.DataFrame = pd.DataFrame()
        self.df_vwap_options: pd.DataFrame = pd.DataFrame()

    @classmethod
    def from_tenor(cls, tenor_processor, **kwargs) -> "PowerOutrightEngine":
        """Build the engine from a power tenor processor (runs it if needed)."""
        trades = getattr(tenor_processor, "df_trades", None)
        if trades is None or trades.empty:
            tenor_processor.run()
            trades = tenor_processor.df_trades
        return cls(trades, **kwargs)

    # --- unit = CURRENCY/PRICE_UNIT ------------------------------------------

    def _ensure_unit(self):
        """Add a ``unit`` column = ``CURRENCY/PRICE_UNIT`` (whatever is available)."""
        df = self.trades
        if "unit" in df.columns:
            return
        cur = (df[self.currency_col].astype(str).str.strip()
               if self.currency_col in df.columns else pd.Series("", index=df.index))
        pu = (df[self.price_unit_col].astype(str).str.strip()
              if self.price_unit_col in df.columns else pd.Series("", index=df.index))
        # "USD/MWh"; if only one side is present, drop the empty side / slash.
        unit = cur.where(cur != "", "") + "/" + pu.where(pu != "", "")
        unit = unit.str.strip("/").replace("", np.nan)
        df["unit"] = unit

    def _build_product(self):
        """Build the power product NAME = COUNTRY_REGION_ZONE_CLASSIFICATION_UNIT.

        Empty segments are dropped so a country with no region/zone collapses
        cleanly, e.g. ``GB_Base load_GBP/MWh`` or ``US_PJM_West Hub_Off-peak_USD/MWh``.
        The unit is embedded so different currencies/units never share a product.
        """
        df = self.trades

        def col(name):
            return (df[name].fillna("").astype(str).str.strip()
                    if name in df.columns else pd.Series("", index=df.index))

        unit = df["unit"].fillna("").astype(str).str.strip() if "unit" in df.columns \
            else pd.Series("", index=df.index)
        segments = pd.DataFrame({
            "country": col(self.country_col),
            "region": col(self.region_col),
            "zone": col(self.zone_col),
            "classification": col(self.classification_col),
            "unit": unit,
        })
        df[self.product_col] = segments.apply(
            lambda r: "_".join([p for p in r if p != ""]), axis=1)

    # --- Public API ----------------------------------------------------------

    def vwap(self, by: str = "tenor1", instruments=None) -> pd.DataFrame:
        """Outright VWAP grouped by the chosen tenor, product and unit.

        Parameters
        ----------
        by : {'tenor1', 'tenor2'}, default 'tenor1'
        instruments : iterable of str, optional
            TRANSACTION_TYPE_ISDA values to include (combinable). None = all.

        Returns
        -------
        pd.DataFrame
            reference_date, weekday, product, unit, periodicity_2, <by>, vwap,
            total_volume, trade_count.
        """
        col = {"tenor1": "tenor", "tenor2": "tenor2"}.get(by)
        if col is None:
            raise ValueError("by must be 'tenor1' or 'tenor2'")

        df = self._select(self.trades, instruments)
        df = self._apply_vwap_filters(df)
        result = self._aggregate(df, col, out_name=by)

        if by == "tenor1":
            self.df_vwap = result
        else:
            self.df_vwap_tenor2 = result
        return result

    def vwap_options(self, by: str = "tenor1", instruments=("Option",)) -> pd.DataFrame:
        """Premium VWAP for OUTRIGHT power options, grouped by the exact contract.

        On top of product / unit / tenor, the grouping adds option type, strike,
        expiry and style, since a premium is only comparable within one contract.
        """
        col = {"tenor1": "tenor", "tenor2": "tenor2"}.get(by)
        if col is None:
            raise ValueError("by must be 'tenor1' or 'tenor2'")

        df = self._select(self.trades, instruments)
        df = self._apply_vwap_filters(df)
        df = df.copy()
        df["_pv"] = df["DEAL_PRICE"] * df["QUANTITY"]

        keys = ["reference_date", self.product_col, "unit", "periodicity_2", col]
        opt_cols = [c for c in (self.option_type_col, self.strike_col,
                                self.option_expiry_col, self.option_style_col)
                    if c in df.columns]
        keys += opt_cols

        grouped = (
            df.groupby(keys, observed=True, dropna=False)
            .agg(pv_sum=("_pv", "sum"),
                 total_volume=("QUANTITY", "sum"),
                 trade_count=("DEAL_PRICE", "count"))
            .reset_index()
        )
        grouped["vwap"] = grouped["pv_sum"] / grouped["total_volume"]
        grouped["weekday"] = grouped["reference_date"].dt.day_name()

        rename = {self.product_col: "product", col: by,
                  self.option_type_col: "option_type", self.strike_col: "strike",
                  self.option_expiry_col: "option_expiry", self.option_style_col: "option_style"}
        grouped = grouped.rename(columns=rename)

        out = ["reference_date", "weekday", "product", "unit", "periodicity_2", by]
        out += [rename[c] for c in opt_cols]
        out += ["vwap", "total_volume", "trade_count"]
        self.df_vwap_options = grouped[out].sort_values(
            ["reference_date", "product", by]).reset_index(drop=True)
        return self.df_vwap_options

    def pivot_wide(self, by: str = "tenor1", **vwap_kwargs) -> pd.DataFrame:
        """Pivot the linear VWAP to wide format, one column per product.

        The product NAME already ends in the unit (``..._GBP/MWh``), so each wide
        column ``f"{product}_vwap"`` (e.g. ``GB_Base load_GBP/MWh_vwap``) is already
        separated by unit - different currencies never share a column.
        """
        long = self.vwap(by=by, **vwap_kwargs)
        index = ["reference_date", "weekday", by]
        wide = long.pivot_table(index=index, columns="product",
                                values="vwap", aggfunc="first")
        wide.columns = [f"{c}_vwap" for c in wide.columns]
        return wide.reset_index().sort_values(["reference_date", by]).reset_index(drop=True)

    # --- Selection / aggregation ---------------------------------------------

    def _select(self, df: pd.DataFrame, instruments) -> pd.DataFrame:
        """Keep Outright rows for the requested instruments."""
        df = df.copy()
        if self.strategy_col in df.columns:
            df = df[df[self.strategy_col].astype(str).str.strip().str.lower() == "outright"]
        if instruments is not None and self.instrument_col in df.columns:
            want = {str(i).strip().lower() for i in instruments}
            df = df[df[self.instrument_col].astype(str).str.strip().str.lower().isin(want)]
        return df

    def _aggregate(self, df: pd.DataFrame, tenor_col: str, out_name: str) -> pd.DataFrame:
        """Volume-weighted average price grouped by product / unit / tenor."""
        df = df.copy()
        df["_pv"] = df["DEAL_PRICE"] * df["QUANTITY"]

        keys = ["reference_date", self.product_col, "unit", "periodicity_2", tenor_col]
        grouped = (
            df.groupby(keys, observed=True, dropna=False)
            .agg(pv_sum=("_pv", "sum"),
                 total_volume=("QUANTITY", "sum"),
                 trade_count=("DEAL_PRICE", "count"))
            .reset_index()
        )
        grouped["vwap"] = grouped["pv_sum"] / grouped["total_volume"]
        grouped["weekday"] = grouped["reference_date"].dt.day_name()
        grouped = grouped.rename(columns={self.product_col: "product", tenor_col: out_name})

        cols = ["reference_date", "weekday", "product", "unit", "periodicity_2",
                out_name, "vwap", "total_volume", "trade_count"]
        return grouped[cols].sort_values(
            ["reference_date", "product", out_name]).reset_index(drop=True)

    # --- Per-granularity computation window (hours / weekdays) ----------------

    def _apply_vwap_filters(self, df: pd.DataFrame) -> pd.DataFrame:
        """Drop deals outside the configured hours/weekdays window before aggregating."""
        if not self.vwap_filters or df.empty or self.datetime_col not in df.columns:
            return df

        norm = {str(k).strip().lower(): v for k, v in self.vwap_filters.items()}
        ts = pd.to_datetime(df[self.datetime_col], errors="coerce")
        tod = ts.dt.hour * 60 + ts.dt.minute
        wd = ts.dt.dayofweek

        tenor_key = df["tenor"].astype(str).str.strip().str.lower()
        bucket_key = df["periodicity_2"].astype(str).str.strip().str.lower()
        applies = tenor_key.where(tenor_key.isin(norm), bucket_key)

        keep = pd.Series(True, index=df.index)
        for key, cond in norm.items():
            sel = applies == key
            if not sel.any():
                continue
            ok = pd.Series(True, index=df.index)
            if "hours" in cond:
                lo = self._to_minutes(cond["hours"][0])
                hi = self._to_minutes(cond["hours"][1])
                ok &= ((tod >= lo) & (tod <= hi)) if lo <= hi else ((tod >= lo) | (tod <= hi))
            if "weekdays" in cond:
                ok &= wd.isin(self._weekday_set(cond["weekdays"]))
            keep &= ~sel | ok
        return df[keep]

    @staticmethod
    def _to_minutes(t) -> int:
        if isinstance(t, str):
            parts = t.split(":")
            return int(parts[0]) * 60 + (int(parts[1]) if len(parts) > 1 else 0)
        return t.hour * 60 + t.minute

    @staticmethod
    def _weekday_set(weekdays) -> set:
        names = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        out = set()
        for w in weekdays:
            if isinstance(w, (int, np.integer)):
                out.add(int(w))
            else:
                out.add(names[str(w).strip().lower()[:3]])
        return out
