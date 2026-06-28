"""Independent VWAP engine for SPREAD gas trades.

Why a separate class
--------------------
A spread price is a *differential* (can be negative) between two legs, so it must
NEVER be mixed into an Outright VWAP. It is also genuinely more complex than an
outright, so per the desk it lives in its OWN class, consuming the enriched trades
produced by ``GasTenorProcessor`` (the same input the Outright ``GasVWAPEngine``
uses).

The two forms of a spread
-------------------------
A) ``CLASSIFICATION_2`` has a value -> the differential is ALREADY booked on the
   row: ``DEAL_PRICE`` is the spread price. Product label = ``"Spread C1_C2"``.

B) ``CLASSIFICATION_2`` is null -> the spread is **two legs** sharing an
   ``EXECUTION_CHAIN_ID`` (both ``STRATEGY == 'Spread'``). The differential uses a
   FIXED convention (NOT the firm's buy/sell side, which would flip between two
   firms doing the same spread and cancel in the VWAP). Both leg prices are on the
   rows, so it is computed directly. Two sub-cases:
     * legs with DIFFERENT ``CLASSIFICATION_1`` -> a location spread; product =
       ``"Spread C1a_C1b"`` with the products in a fixed (alphabetical) order;
       price = ``price(a) - price(b)``.
     * legs with the SAME ``CLASSIFICATION_1`` but different period -> a time
       spread; product = ``"Spread C1"`` plus a (near, far) tenor pair; price =
       ``price(near) - price(far)`` (near = earlier delivery, far = later). The two
       tenors are kept so e.g. M+1/M+2 and Q4/Q1 never collapse together.
   ``BUYER_GCD_ID`` / ``SELLER_GCD_ID`` (``own_gcd``) are now only an optional
   pairing sanity check, not the sign.

Critical data hygiene (mandatory, per desk guidance)
----------------------------------------------------
1. Rows are DUPLICATED per ``DEAL_ID`` -> dedupe by ``DEAL_ID`` before any sum.
2. Do NOT mix legs of a chain that differ in ``QUANTITY_UNIT`` / ``PRICE_UNIT`` /
   ``QUANTITY_FREQ`` without normalising first (such chains are flagged and
   skipped here rather than silently averaged).
3. GAS vs LNG is split ONLY by ``DEAL_TYPE``.

Confirmed from the data: ``BUYER_GCD_ID`` / ``SELLER_GCD_ID`` exist (numeric
codes); within a chain the firm is the BUYER on one leg and the SELLER on the
other (swapped), so ``own_gcd`` (the firm's code, or codes) signs the spread.
``IS_DELETED = TRUE`` rows are dropped.

OPEN ITEMS (still need confirmation) - see ``_unresolved``:
  * the volume convention for a 2-leg spread whose legs differ in size.
  * a time spread is labelled ``"Spread C1"`` only - it does not yet encode WHICH
    periods (e.g. Dec/Jan vs Q4/Q1); the two tenors disambiguate it for now.
  * multi-leg option spreads (chains with >2 legs) are left unresolved.
  * the THIRD spread type (besides location & time) is not specialised yet.
"""

import pandas as pd
import numpy as np


class GasSpreadEngine:
    """Compute Spread VWAPs from enriched gas trades, on demand.

    Parameters
    ----------
    trades : pd.DataFrame
        Enriched per-trade frame from ``GasTenorProcessor``.
    own_gcd : optional
        The reporting entity's GCD id, used to identify the buy leg of a
        chain-based spread (form B): the leg whose ``BUYER_GCD_ID == own_gcd`` is
        the bought leg. If ``None``, form-B spreads are left unresolved.
    *_col : str
        Configurable column names (so the exact extract strings are not
        hard-coded).
    """

    def __init__(self, trades: pd.DataFrame, *, own_gcd=None,
                 strategy_col: str = "STRATEGY",
                 instrument_col: str = "TRANSACTION_TYPE_ISDA",
                 deal_type_col: str = "DEAL_TYPE",
                 product_col: str = "CLASSIFICATION_1",
                 product2_col: str = "CLASSIFICATION_2",
                 chain_col: str = "EXECUTION_CHAIN_ID",
                 deal_id_col: str = "DEAL_ID",
                 buyer_col: str = "BUYER_GCD_ID",
                 seller_col: str = "SELLER_GCD_ID",
                 is_deleted_col: str = "IS_DELETED",
                 contract_start_col: str = "CONTRACT_START_DATE",
                 qty_unit_col: str = "QUANTITY_UNIT",
                 price_unit_col: str = "PRICE_UNIT",
                 qty_freq_col: str = "QUANTITY_FREQ"):
        self.trades = trades.copy()
        self.own_gcd = own_gcd
        self.strategy_col = strategy_col
        self.instrument_col = instrument_col
        self.deal_type_col = deal_type_col
        self.product_col = product_col
        self.product2_col = product2_col
        self.chain_col = chain_col
        self.deal_id_col = deal_id_col
        self.buyer_col = buyer_col
        self.seller_col = seller_col
        self.is_deleted_col = is_deleted_col
        self.contract_start_col = contract_start_col
        # own_gcd may be a single firm code or several (the firm's own entities)
        if own_gcd is None:
            self.own_gcds = set()
        elif isinstance(own_gcd, (list, tuple, set)):
            self.own_gcds = {self._norm_gcd(g) for g in own_gcd}
        else:
            self.own_gcds = {self._norm_gcd(own_gcd)}
        self.qty_unit_col = qty_unit_col
        self.price_unit_col = price_unit_col
        self.qty_freq_col = qty_freq_col

        self.df_vwap: pd.DataFrame = pd.DataFrame()
        self.legs: pd.DataFrame = pd.DataFrame()        # per-spread (one row per spread)
        self._unresolved: list[dict] = []               # chains we could not sign / normalise

    @classmethod
    def from_tenor(cls, tenor_processor, **kwargs) -> "GasSpreadEngine":
        """Build the engine from a ``GasTenorProcessor`` (runs it if needed)."""
        trades = getattr(tenor_processor, "df_trades", None)
        if trades is None or trades.empty:
            tenor_processor.run()
            trades = tenor_processor.df_trades
        return cls(trades, **kwargs)

    # --- Public API ----------------------------------------------------------

    def build_spreads(self, instruments=None, deal_types=None) -> pd.DataFrame:
        """Resolve every spread into a single priced row (form A + form B).

        Parameters
        ----------
        instruments : iterable of str, optional
            TRANSACTION_TYPE_ISDA to include, e.g. ['Future', 'Spot Fwd'] for the
            linear spreads or ['Option'] for the option spreads. None = all.
        deal_types : iterable of str, optional
            DEAL_TYPE (GAS/LNG) restriction.

        Returns
        -------
        pd.DataFrame
            One row per spread with: reference_date, deal_type, product (the
            'Spread ...' label), spread_kind ('time'/'location'), periodicity_2,
            tenor_near, tenor_far, tenor2_near, tenor2_far, spread_price, volume.
            A time spread carries TWO tenors (the near/far legs); a location
            spread has near == far. Chains that cannot be ordered or normalised are
            recorded in ``self._unresolved`` and excluded.
        """
        self._unresolved = []
        df = self._select(self.trades, instruments, deal_types)
        df = self._dedupe(df)

        has_c2 = self.product2_col in df.columns and df[self.product2_col].notna()
        form_a = df[has_c2] if self.product2_col in df.columns else df.iloc[0:0]
        form_b = df[~has_c2] if self.product2_col in df.columns else df

        rows = []
        rows.extend(self._resolve_form_a(form_a))
        rows.extend(self._resolve_form_b(form_b))

        self.legs = pd.DataFrame(rows)
        return self.legs

    def vwap(self, by: str = "tenor1", instruments=("Future", "Spot Fwd"),
             deal_types=None) -> pd.DataFrame:
        """VWAP of the LINEAR spread differential (Future / Spot Fwd).

        Mirrors ``GasOutrightEngine.vwap``: ``instruments`` is combinable
        (Future + Spot Fwd pooled, or one isolated). For option spreads use
        ``vwap_options``.
        """
        near_col, far_col = {"tenor1": ("tenor_near", "tenor_far"),
                             "tenor2": ("tenor2_near", "tenor2_far")}.get(by, (None, None))
        if near_col is None:
            raise ValueError("by must be 'tenor1' or 'tenor2'")
        self.build_spreads(instruments=instruments, deal_types=deal_types)
        if self.legs.empty:
            self.df_vwap = self.legs
            return self.legs

        df = self.legs.copy()
        df["_pv"] = df["spread_price"] * df["volume"]
        # A spread is keyed by BOTH leg tenors so e.g. M+1/M+2 and Q4/Q1 never mix.
        keys = ["reference_date", "deal_type", "product", "periodicity_2", near_col, far_col]
        grouped = (
            df.groupby(keys, observed=True)
            .agg(pv_sum=("_pv", "sum"),
                 total_volume=("volume", "sum"),
                 trade_count=("spread_price", "count"))
            .reset_index()
        )
        grouped["vwap"] = grouped["pv_sum"] / grouped["total_volume"]
        grouped["weekday"] = grouped["reference_date"].dt.day_name()
        grouped = grouped.rename(columns={near_col: "tenor_near", far_col: "tenor_far"})
        self.df_vwap = grouped[
            ["reference_date", "weekday", "deal_type", "product", "periodicity_2",
             "tenor_near", "tenor_far", "vwap", "total_volume", "trade_count"]
        ].sort_values(["reference_date", "product", "tenor_near", "tenor_far"]).reset_index(drop=True)
        return self.df_vwap

    def vwap_options(self, by: str = "tenor1", deal_types=None,
                     instruments=("Option",)) -> pd.DataFrame:
        """VWAP of OPTION spreads (premium differential).

        Same resolution as the linear ``vwap`` but restricted to option
        instruments. NOTE: option spreads are often multi-leg structures (e.g.
        call/put spreads booked as 4-8 legs in one chain) and a per-strike
        treatment is NOT specialised yet - this resolves the 2-leg case and
        records anything else in ``self._unresolved`` ("haz lo que puedas").
        """
        return self.vwap(by=by, instruments=instruments, deal_types=deal_types)

    # --- Selection / hygiene -------------------------------------------------

    def _select(self, df: pd.DataFrame, instruments, deal_types) -> pd.DataFrame:
        """Keep Spread rows for the requested instruments and deal types (GAS/LNG)."""
        df = df.copy()
        # drop soft-deleted rows (IS_DELETED = TRUE)
        if self.is_deleted_col in df.columns:
            deleted = df[self.is_deleted_col].astype(str).str.strip().str.lower().isin(("true", "1", "yes"))
            df = df[~deleted]
        if self.strategy_col in df.columns:
            df = df[df[self.strategy_col].astype(str).str.strip().str.lower() == "spread"]
        if instruments is not None and self.instrument_col in df.columns:
            want = {str(i).strip().lower() for i in instruments}
            df = df[df[self.instrument_col].astype(str).str.strip().str.lower().isin(want)]
        if deal_types is not None and self.deal_type_col in df.columns:
            want = {str(d).strip().lower() for d in deal_types}
            df = df[df[self.deal_type_col].astype(str).str.strip().str.lower().isin(want)]
        return df

    def _dedupe(self, df: pd.DataFrame) -> pd.DataFrame:
        """Rule 1: each DEAL_ID is duplicated -> keep one row per DEAL_ID."""
        if self.deal_id_col in df.columns:
            return df.drop_duplicates(subset=self.deal_id_col)
        return df

    def _deal_type(self, row) -> str:
        return row[self.deal_type_col] if self.deal_type_col in row.index else "GAS"

    # --- Form A: the differential is already on the row ----------------------

    def _resolve_form_a(self, df: pd.DataFrame) -> list[dict]:
        """One booked row = one spread; DEAL_PRICE is the differential.

        A booked differential carries a single delivery period, so the near and
        far tenors are the same (it is a location-style spread between
        ``CLASSIFICATION_1`` and ``CLASSIFICATION_2`` for one period).
        """
        rows = []
        for _, r in df.iterrows():
            c1 = r.get(self.product_col)
            c2 = r.get(self.product2_col)
            product = f"Spread {c1}_{c2 if pd.notna(c2) else ''}"
            t1, t2 = r.get("tenor"), r.get("tenor2")
            rows.append({
                "reference_date": r["reference_date"],
                "deal_type": self._deal_type(r),
                "product": product,
                "spread_kind": "location",
                "periodicity_2": r.get("periodicity_2"),
                "tenor_near": t1, "tenor_far": t1,
                "tenor2_near": t2, "tenor2_far": t2,
                "spread_price": r["DEAL_PRICE"],
                "volume": r["QUANTITY"],
            })
        return rows

    # --- Form B: two legs related by EXECUTION_CHAIN_ID ----------------------

    def _resolve_form_b(self, df: pd.DataFrame) -> list[dict]:
        """Pair the two legs of each chain and price them with a FIXED convention.

        The sign does NOT use the firm's buy/sell side (that would flip between two
        firms doing the same market spread and cancel in the VWAP). Instead a fixed
        convention is applied (see ``_order_legs``):

          * time spread (same product, different period): ``price(near) - price(far)``
            where *near* is the earlier delivery and *far* the later one;
          * location spread (different product, same period):
            ``price(leg_a) - price(leg_b)`` with the products in a fixed
            (alphabetical) order.

        Because both leg prices are on the rows, the differential is computed
        directly; ``own_gcd`` / buyer-seller are only an optional pairing check.
        A chain is usable only when (rule 2) its legs share QUANTITY_UNIT,
        PRICE_UNIT and QUANTITY_FREQ, and the two legs can be ordered. Unusable
        chains go to ``self._unresolved``.
        """
        rows = []
        if self.chain_col not in df.columns:
            return rows

        for chain_id, grp in df.groupby(self.chain_col):
            if len(grp) != 2:
                self._unresolved.append({"chain": chain_id, "reason": f"{len(grp)} legs (expected 2)"})
                continue
            if not self._units_consistent(grp):                     # rule 2
                self._unresolved.append({"chain": chain_id, "reason": "leg units differ (normalise first)"})
                continue

            leg_a, leg_b, kind = self._order_legs(grp)
            if kind is None:
                self._unresolved.append({"chain": chain_id, "reason": "cannot order legs (same product & period, or missing dates)"})
                continue

            c1_a, c1_b = leg_a.get(self.product_col), leg_b.get(self.product_col)
            if kind == "time":
                product = f"Spread {c1_a}"                          # one product is enough
            else:
                product = f"Spread {c1_a}_{c1_b}"                   # location: both products

            rows.append({
                "reference_date": leg_a["reference_date"],
                "deal_type": self._deal_type(leg_a),
                "product": product,
                "spread_kind": kind,
                "periodicity_2": leg_a.get("periodicity_2"),
                # near = leg_a (earlier period for time / first product for location)
                "tenor_near": leg_a.get("tenor"), "tenor_far": leg_b.get("tenor"),
                "tenor2_near": leg_a.get("tenor2"), "tenor2_far": leg_b.get("tenor2"),
                "spread_price": leg_a["DEAL_PRICE"] - leg_b["DEAL_PRICE"],
                # Volume convention is unconfirmed; use the near/first leg for now.
                "volume": leg_a["QUANTITY"],
            })
        return rows

    def _order_legs(self, grp: pd.DataFrame):
        """Order the two legs by a fixed convention.

        Returns ``(leg_a, leg_b, kind)`` where the spread is ``price(leg_a) -
        price(leg_b)`` and ``kind`` is ``'time'`` or ``'location'``:

          * different products  -> location spread, legs sorted by product name
            (alphabetical) so the sign is deterministic;
          * same product, different delivery start -> time spread, leg_a = the
            earlier (near) delivery, leg_b = the later (far) one.

        Returns ``(None, None, None)`` if the legs cannot be ordered (same product
        and same/blank period).
        """
        a, b = grp.iloc[0], grp.iloc[1]
        pa, pb = a.get(self.product_col), b.get(self.product_col)
        if pa != pb:                                                # location spread
            if str(pb) < str(pa):
                a, b = b, a
            return a, b, "location"
        # same product -> time spread, order by delivery start (near before far)
        if self.contract_start_col not in grp.columns:
            return None, None, None
        sa, sb = a.get(self.contract_start_col), b.get(self.contract_start_col)
        if pd.isna(sa) or pd.isna(sb) or sa == sb:
            return None, None, None
        if sb < sa:
            a, b = b, a
        return a, b, "time"

    def _units_consistent(self, grp: pd.DataFrame) -> bool:
        for col in (self.qty_unit_col, self.price_unit_col, self.qty_freq_col):
            if col in grp.columns and grp[col].nunique(dropna=False) > 1:
                return False
        return True

    @staticmethod
    def _norm_gcd(g) -> str:
        """Normalise a GCD code for comparison (str, trimmed; 142992.0 -> 142992)."""
        if isinstance(g, float) and g.is_integer():
            g = int(g)
        return str(g).strip()

    def _buy_sell_legs(self, grp: pd.DataFrame):
        """Return (buy_leg, sell_leg) from the firm's perspective.

        Within a spread chain the firm is the BUYER on one leg and the SELLER on
        the other (buyer/seller are swapped between the two legs). So the leg where
        one of ``own_gcds`` is the buyer is the bought leg, and the leg where the
        same firm is the seller is the sold leg. Returns (None, None) if the firm
        is not set or cannot be matched on exactly one buy and one sell leg.
        """
        if not self.own_gcds or self.buyer_col not in grp.columns or self.seller_col not in grp.columns:
            return None, None
        buyer = grp[self.buyer_col].map(self._norm_gcd)
        seller = grp[self.seller_col].map(self._norm_gcd)
        buy = grp[buyer.isin(self.own_gcds)]
        sell = grp[seller.isin(self.own_gcds)]
        if len(buy) == 1 and len(sell) == 1 and buy.index[0] != sell.index[0]:
            return buy.iloc[0], sell.iloc[0]
        return None, None
