"""
analysis/volatility_engine.py — Volatility regime detection.
ATR expansion/contraction, Bollinger Band compression,
and VIX-based macro volatility context.
"""

import logging
from dataclasses import dataclass
from typing import Optional, List
import pandas as pd

from config import (
    ATR_PERIOD, BB_PERIOD, BB_STD,
    ATR_EXPANSION_MULTIPLIER, BB_WIDTH_COMPRESSION_PCT,
    VIX_LOW_THRESHOLD, VIX_ELEVATED_THRESHOLD, VIX_CRISIS_THRESHOLD
)
from utils.math_utils import (
    atr_series, bollinger_bands, bb_width,
    bb_width_percentile, adx_series
)

logger = logging.getLogger(__name__)


@dataclass
class VolatilityState:
    """Complete volatility picture across timeframes."""

    # ATR metrics
    atr_current:        float = 0.0    # Current ATR in USD
    atr_normalized:     float = 0.0    # ATR as % of price
    atr_avg_20:         float = 0.0    # 20-period ATR average
    atr_state:          str   = "STABLE"  # EXPANDING / CONTRACTING / STABLE

    # Bollinger Band metrics
    bb_upper:           float = 0.0
    bb_middle:          float = 0.0
    bb_lower:           float = 0.0
    bb_width_current:   float = 0.0
    bb_width_pct:       float = 0.5    # Percentile in recent history
    bb_state:           str   = "NORMAL"  # SQUEEZE / EXPANDING / NORMAL

    # Price position
    price_vs_bb:        str   = "INSIDE"  # ABOVE_UPPER / BELOW_LOWER / INSIDE
    vwap:               float = 0.0
    price_vs_vwap:      str   = "ABOVE"   # ABOVE / BELOW

    # Derived
    is_compressing:     bool  = False
    is_expanding:       bool  = False
    stop_atr_distance:  float = 0.0    # Recommended stop distance in USD


class VolatilityEngine:
    """
    Analyzes volatility across timeframes.
    Primary input: 5m and 1H DataFrames.
    """

    def __init__(self):
        self._last_state: Optional[VolatilityState] = None

    def analyze(self, df_5m: pd.DataFrame,
                df_1h: pd.DataFrame,
                current_price: float,
                atr_stop_multiplier: float = 1.5) -> VolatilityState:
        """
        Compute full volatility state from 5m (primary) and 1H (context).

        Args:
            df_5m:              5-minute OHLCV DataFrame
            df_1h:              1-hour OHLCV DataFrame
            current_price:      Latest price
            atr_stop_multiplier: Multiplier for recommended stop distance

        Returns:
            VolatilityState dataclass
        """
        state = VolatilityState()

        if df_5m is None or df_5m.empty:
            logger.warning("VolatilityEngine: no 5m data available")
            return state

        # ── ATR Analysis ──────────────────────────────────────────────────────
        atr_s = atr_series(df_5m, ATR_PERIOD)
        if atr_s.empty or atr_s.iloc[-1] != atr_s.iloc[-1]:  # NaN check
            logger.warning("ATR calculation returned NaN")
            return state

        state.atr_current    = float(atr_s.iloc[-1])
        state.atr_avg_20     = float(atr_s.iloc[-20:].mean()) if len(atr_s) >= 20 else state.atr_current
        state.atr_normalized = state.atr_current / current_price if current_price else 0

        # ATR trend: compare last 5 bars
        if len(atr_s) >= 10:
            recent_5  = float(atr_s.iloc[-5:].mean())
            prior_5   = float(atr_s.iloc[-10:-5].mean())
            if prior_5 > 0:
                atr_ratio = recent_5 / prior_5
                if atr_ratio > 1.2:
                    state.atr_state = "EXPANDING"
                elif atr_ratio < 0.85:
                    state.atr_state = "CONTRACTING"
                else:
                    state.atr_state = "STABLE"

        state.is_expanding   = (state.atr_current > state.atr_avg_20 * ATR_EXPANSION_MULTIPLIER)

        # ── Bollinger Band Analysis ────────────────────────────────────────────
        closes = df_5m["close"]
        if len(closes) >= BB_PERIOD:
            bb_upper_s, bb_mid_s, bb_lower_s = bollinger_bands(closes, BB_PERIOD, BB_STD)
            state.bb_upper   = float(bb_upper_s.iloc[-1])
            state.bb_middle  = float(bb_mid_s.iloc[-1])
            state.bb_lower   = float(bb_lower_s.iloc[-1])

            state.bb_width_current = bb_width(state.bb_upper, state.bb_lower, state.bb_middle)

            # Historical widths for percentile
            hist_widths = [
                bb_width(float(bb_upper_s.iloc[i]), float(bb_lower_s.iloc[i]),
                         float(bb_mid_s.iloc[i]))
                for i in range(len(bb_upper_s))
                if not (bb_upper_s.iloc[i] != bb_upper_s.iloc[i])  # not NaN
            ]
            state.bb_width_pct = bb_width_percentile(hist_widths, state.bb_width_current, 50)

            # BB state
            if state.bb_width_pct <= BB_WIDTH_COMPRESSION_PCT:
                state.bb_state      = "SQUEEZE"
                state.is_compressing = True
            elif state.atr_state == "EXPANDING":
                state.bb_state = "EXPANDING"
            else:
                state.bb_state = "NORMAL"

            # Price vs Bands
            if current_price > state.bb_upper:
                state.price_vs_bb = "ABOVE_UPPER"
            elif current_price < state.bb_lower:
                state.price_vs_bb = "BELOW_LOWER"
            else:
                state.price_vs_bb = "INSIDE"

        # ── VWAP Position ─────────────────────────────────────────────────────
        try:
            typical  = (df_5m["high"] + df_5m["low"] + df_5m["close"]) / 3
            vwap_val = float((typical * df_5m["volume"]).cumsum().iloc[-1] /
                              df_5m["volume"].cumsum().iloc[-1])
            state.vwap         = vwap_val
            state.price_vs_vwap = "ABOVE" if current_price >= vwap_val else "BELOW"
        except Exception as e:
            logger.debug(f"VWAP calculation error: {e}")

        # ── Recommended Stop Distance ─────────────────────────────────────────
        state.stop_atr_distance = state.atr_current * atr_stop_multiplier

        self._last_state = state

        logger.debug(
            f"Volatility: ATR={state.atr_current:.2f} ({state.atr_state}) "
            f"BB={state.bb_state} squeeze={state.is_compressing} "
            f"expanding={state.is_expanding} VWAP={state.price_vs_vwap}"
        )
        return state

    def stop_distance(self, multiplier: float = 1.5) -> float:
        """Return ATR-based stop distance from last analysis."""
        if self._last_state:
            return self._last_state.atr_current * multiplier
        return 0.0


# Module-level singleton
_vol_engine: Optional[VolatilityEngine] = None


def get_volatility_engine() -> VolatilityEngine:
    global _vol_engine
    if _vol_engine is None:
        _vol_engine = VolatilityEngine()
    return _vol_engine
