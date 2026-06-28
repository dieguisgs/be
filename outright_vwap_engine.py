"""Outright VWAP engine for gas trades.

Separation of concerns
----------------------
Tenor generation (``periodicity_2`` + ``tenor1`` + ``tenor2``) lives in
``GasTenorProcessor`` (``periodicity_tenor_generation.py``). THIS module only
computes VWAPs from the already-enriched trades it produces - one class produces
the result the other consumes. Spreads are a different beast and live in their
own independent class (``spread_vwap_engine.py``); this engine is **Outright only**.

Region is IMPLICIT
------------------
EMEA, APAC and AMERICAS share the same gas column structure, so this engine is
region-agnostic: run it once per regional dataset (the product, ``CLASSIFICATION_1``,
is already built per region upstream by ``gas_production_processor.py``). There is
no ``region`` argument - the difference between regions lives in pre-production
(country/classification mapping, the Australia special-case, the US tables) and in
the ``seasonal_type`` you pass to the tenor processor (ROW for EMEA/APAC, US for AMER).

Two outright sub-calculations
-----------------------------
  * ``vwap()``         - **linear** instruments (Future / Spot Fwd). Combinable:
                         pass both to pool them, one to isolate it.
  * ``vwap_options()`` - **options**, priced on the PREMIUM and grouped by the
                         exact contract (strike / call-put / expiry / style), since
                         a premium is only comparable within one such contract.

Common selections: ``by`` ('tenor1' merges Custom / 'tenor2' splits it by
TERM_DESCRIPTION), ``instruments`` (``TRANSACTION_TYPE_ISDA``, combinable) and
``deal_types`` (GAS vs LNG via ``DEAL_TYPE``, always kept in separate rows). Only
**Outright** trades are aggregated; Spreads go to ``GasSpreadEngine``.
"""

import pandas as pd
import numpy as np


class GasOutrightEngine:
    """Compute Outright VWAPs from enriched gas trades, on demand.

    Parameters
    ----------
    trades : pd.DataFrame
        Enriched per-trade frame from ``GasTenorProcessor`` (needs
        ``reference_date``, ``periodicity_2``, ``tenor``, ``tenor2``,
        ``DEAL_PRICE``, ``QUANTITY``, ``DEAL_EXECUTION_DATETIME`` and the
        product / instrument / strategy / deal-type columns).
    vwap_filters : dict, optional
        Per-granularity computation window (hours / weekdays). Same shape as in
        ``GasTenorProcessor``; see ``_apply_vwap_filters``. Default: count all.
    instrument_col, strategy_col, deal_type_col, product_col : str
        Column names for the instrument (Future/Spot Fwd/Option), the
        Outright/Spread strategy, the GAS/LNG split and the product. Configurable
        so the engine does not hard-code the exact strings of a given extract.
    option_type_col, strike_col, option_expiry_col, option_style_col : str
        Option-specific columns used by ``vwap_options`` (Call/Put, strike,
        expiry, exercise style).
    """

    def __init__(self, trades: pd.DataFrame, *,
                 vwap_filters: dict | None = None,
                 instrument_col: str = "TRANSACTION_TYPE_ISDA",
                 strategy_col: str = "STRATEGY",
                 deal_type_col: str = "DEAL_TYPE",
                 product_col: str = "CLASSIFICATION_1",
                 option_type_col: str = "OPTION_TYPE",
                 strike_col: str = "STRIKE_PRICE_PER_UNIT",
                 option_expiry_col: str = "OPTION_EXPIRY",
                 option_style_col: str = "OPTION_STYLE"):
        self.trades = trades.copy()
        self.vwap_filters = vwap_filters or {}
        self.instrument_col = instrument_col
        self.strategy_col = strategy_col
        self.deal_type_col = deal_type_col
        self.product_col = product_col
        self.option_type_col = option_type_col
        self.strike_col = strike_col
        self.option_expiry_col = option_expiry_col
        self.option_style_col = option_style_col
        self.df_vwap: pd.DataFrame = pd.DataFrame()         # last linear result by tenor1
        self.df_vwap_tenor2: pd.DataFrame = pd.DataFrame()  # last linear result by tenor2
        self.df_vwap_options: pd.DataFrame = pd.DataFrame() # last option-premium result

    @classmethod
    def from_tenor(cls, tenor_processor, **kwargs) -> "GasOutrightEngine":
        """Build the engine from a ``GasTenorProcessor`` (runs it if needed)."""
        trades = getattr(tenor_processor, "df_trades", None)
        if trades is None or trades.empty:
            tenor_processor.run()
            trades = tenor_processor.df_trades
        return cls(trades, **kwargs)

    # --- Public API ----------------------------------------------------------

    def vwap(self, by: str = "tenor1", instruments=None, deal_types=None,
             combine_deal_types: bool = False) -> pd.DataFrame:
        """Outright VWAP grouped by the chosen tenor, for the chosen instruments.

        Parameters
        ----------
        by : {'tenor1', 'tenor2'}, default 'tenor1'
            Which tenor drives the grouping.
        instruments : iterable of str, optional
            TRANSACTION_TYPE_ISDA values to include (combinable). None = all.
        deal_types : iterable of str, optional
            DEAL_TYPE values to include (e.g. ['GAS']). None = all.
        combine_deal_types : bool, default False
            If ``False`` (default) GAS and LNG are kept in **separate rows**
            (``DEAL_TYPE`` stays a grouping key). If ``True`` they are **pooled**
            into a single VWAP per (product, tenor) and the ``deal_type`` column is
            dropped from the output.

            .. warning::
               GAS and LNG are different commodities priced on different curves;
               pooling them mixes two price series. Use ``combine_deal_types=True``
               only when that is genuinely what you want.

        Returns
        -------
        pd.DataFrame
            reference_date, weekday, [deal_type], product, periodicity_2, <by>,
            vwap, total_volume, trade_count. (``deal_type`` is omitted when
            ``combine_deal_types=True``.)
        """
        col = {"tenor1": "tenor", "tenor2": "tenor2"}.get(by)
        if col is None:
            raise ValueError("by must be 'tenor1' or 'tenor2'")

        df = self._select(self.trades, instruments, deal_types)
        df = self._apply_vwap_filters(df)
        result = self._aggregate(df, col, out_name=by, combine_deal_types=combine_deal_types)

        if by == "tenor1":
            self.df_vwap = result
        else:
            self.df_vwap_tenor2 = result
        return result

    def vwap_options(self, by: str = "tenor1", deal_types=None,
                     instruments=("Option",), combine_deal_types: bool = False) -> pd.DataFrame:
        """Premium VWAP for OUTRIGHT options, grouped by the exact contract.

        An option premium is only comparable within a single contract, so on top
        of the usual keys the grouping adds option type (Call/Put), strike, expiry
        and exercise style. ``DEAL_PRICE`` is the premium, volume-weighted by
        ``QUANTITY``. This is kept SEPARATE from the linear ``vwap()`` so premiums
        are never pooled with forward prices.

        Parameters
        ----------
        by : {'tenor1', 'tenor2'}, default 'tenor1'
        deal_types : iterable of str, optional
            DEAL_TYPE (GAS/LNG) restriction; GAS and LNG stay in separate rows.
        instruments : iterable of str, default ('Option',)
            TRANSACTION_TYPE_ISDA value(s) that identify options.
        combine_deal_types : bool, default False
            If True, pool GAS and LNG into one row (drop the ``deal_type``
            column) instead of keeping them separate. See ``vwap`` for the caveat.

        Returns
        -------
        pd.DataFrame
            reference_date, weekday, [deal_type], product, periodicity_2, <by>,
            option_type, strike, option_expiry, option_style, vwap (= mean
            premium), total_volume, trade_count.
        """
        col = {"tenor1": "tenor", "tenor2": "tenor2"}.get(by)
        if col is None:
            raise ValueError("by must be 'tenor1' or 'tenor2'")

        df = self._select(self.trades, instruments, deal_types)   # Outright + Option
        df = self._apply_vwap_filters(df)
        df = df.copy()
        df["_pv"] = df["DEAL_PRICE"] * df["QUANTITY"]

        keys = ["reference_date"]
        has_dt = self.deal_type_col in df.columns and not combine_deal_types
        if has_dt:
            keys.append(self.deal_type_col)
        keys += [self.product_col, "periodicity_2", col]
        # the option contract identity
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
        if has_dt:
            rename[self.deal_type_col] = "deal_type"
        grouped = grouped.rename(columns=rename)

        out = ["reference_date", "weekday"]
        if has_dt:
            out.append("deal_type")
        out += ["product", "periodicity_2", by]
        out += [rename[c] for c in opt_cols]
        out += ["vwap", "total_volume", "trade_count"]
        self.df_vwap_options = grouped[out].sort_values(["reference_date", "product", by]).reset_index(drop=True)
        return self.df_vwap_options

    def pivot_wide(self, by: str = "tenor1", **vwap_kwargs) -> pd.DataFrame:
        """Pivot the linear (Outright) VWAP to wide format, products side by side."""
        long = self.vwap(by=by, **vwap_kwargs)
        index = ["reference_date", "weekday"]
        if "deal_type" in long.columns:
            index.append("deal_type")
        index.append(by)
        wide = long.pivot_table(index=index, columns="product", values="vwap", aggfunc="first")
        wide.columns = [f"{c}_vwap" for c in wide.columns]
        return wide.reset_index().sort_values(["reference_date", by]).reset_index(drop=True)

    # --- Selection (instrument / strategy / deal type) -----------------------

    def _select(self, df: pd.DataFrame, instruments, deal_types) -> pd.DataFrame:
        """Keep Outright rows for the requested instruments and deal types."""
        df = df.copy()

        # Outright only - Spreads are handled by the separate spread engine.
        if self.strategy_col in df.columns:
            df = df[df[self.strategy_col].astype(str).str.strip().str.lower() == "outright"]

        # Instrument selection (combinable). None -> keep all.
        if instruments is not None and self.instrument_col in df.columns:
            want = {str(i).strip().lower() for i in instruments}
            df = df[df[self.instrument_col].astype(str).str.strip().str.lower().isin(want)]

        # Optional GAS/LNG restriction (they are still split as a grouping key).
        if deal_types is not None and self.deal_type_col in df.columns:
            want = {str(d).strip().lower() for d in deal_types}
            df = df[df[self.deal_type_col].astype(str).str.strip().str.lower().isin(want)]

        return df

    def _aggregate(self, df: pd.DataFrame, tenor_col: str, out_name: str,
                   combine_deal_types: bool = False) -> pd.DataFrame:
        """Volume-weighted average price grouped by deal_type / product / tenor.

        When ``combine_deal_types`` is True the GAS/LNG split is dropped so both
        pool into a single VWAP per (product, tenor).
        """
        df = df.copy()
        df["_pv"] = df["DEAL_PRICE"] * df["QUANTITY"]

        keys = ["reference_date"]
        has_dt = self.deal_type_col in df.columns and not combine_deal_types
        if has_dt:
            keys.append(self.deal_type_col)
        keys += [self.product_col, "periodicity_2", tenor_col]

        grouped = (
            df.groupby(keys, observed=True)
            .agg(pv_sum=("_pv", "sum"),
                 total_volume=("QUANTITY", "sum"),
                 trade_count=("DEAL_PRICE", "count"))
            .reset_index()
        )
        grouped["vwap"] = grouped["pv_sum"] / grouped["total_volume"]
        grouped["weekday"] = grouped["reference_date"].dt.day_name()

        rename = {self.product_col: "product", tenor_col: out_name}
        if has_dt:
            rename[self.deal_type_col] = "deal_type"
        grouped = grouped.rename(columns=rename)

        cols = ["reference_date", "weekday"]
        if has_dt:
            cols.append("deal_type")
        cols += ["product", "periodicity_2", out_name, "vwap", "total_volume", "trade_count"]
        return grouped[cols].sort_values(["reference_date", "product", out_name]).reset_index(drop=True)

    # --- Per-granularity computation window (hours / weekdays) ----------------

    def _apply_vwap_filters(self, df: pd.DataFrame) -> pd.DataFrame:
        """Drop deals outside the configured computation window before aggregating.

        For each row the applicable filter is chosen by exact ``tenor`` (tenor1)
        first, then by ``periodicity_2`` bucket; rows whose bucket/tenor is not in
        ``vwap_filters`` are always kept.
        """
        if not self.vwap_filters or df.empty:
            return df

        norm = {str(k).strip().lower(): v for k, v in self.vwap_filters.items()}
        ts = df["DEAL_EXECUTION_DATETIME"]
        tod = ts.dt.hour * 60 + ts.dt.minute          # minutes since midnight
        wd = ts.dt.dayofweek                           # 0=Mon ... 6=Sun

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
        """Convert ``"HH:MM"`` (or a ``datetime.time``) to minutes since midnight."""
        if isinstance(t, str):
            parts = t.split(":")
            return int(parts[0]) * 60 + (int(parts[1]) if len(parts) > 1 else 0)
        return t.hour * 60 + t.minute

    @staticmethod
    def _weekday_set(weekdays) -> set[int]:
        """Normalise an iterable of weekday names/ints to a set of 0=Mon..6=Sun."""
        names = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        out: set[int] = set()
        for w in weekdays:
            if isinstance(w, (int, np.integer)):
                out.add(int(w))
            else:
                out.add(names[str(w).strip().lower()[:3]])
        return out


# Backward-compatible alias (the class used to be called GasVWAPEngine).
GasVWAPEngine = GasOutrightEngine
