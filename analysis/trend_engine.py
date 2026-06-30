"""
analysis/trend_engine.py — Trend detection via EMA stacks, ADX, momentum.
Operates on 5m, 15m, and 1H timeframes for multi-TF trend alignment.
v1.0 — original release
v1.1 — 2026-06-30 — primary_adx now sourced from the 5m timeframe instead
        of 1H. This is a 0DTE intraday bot trading off ORB/butterfly setups
        on 5-min structure — 1H ADX lags the actual session move and was
        causing trend days to misclassify as RANGING (ADX stuck near 0
        until the 1H candle had enough history to show directional
        persistence, which can be hours after the actual breakout fired).
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional
import pandas as pd

from config import EMA_FAST, EMA_MID, EMA_SLOW, EMA_ANCHOR, ADX_TREND_THRESHOLD, ADX_RANGE_THRESHOLD
from utils.math_utils import ema_series, adx_series, current_adx

logger = logging.getLogger(__name__)


@dataclass
class TrendVote:
    """Trend assessment for a single timeframe."""
    timeframe:   str   = ""
    direction:   str   = "NEUTRAL"   # BULLISH / BEARISH / NEUTRAL
    ema_aligned: bool  = False        # All EMAs in stack order
    adx:         float = 0.0
    momentum:    str   = "FLAT"      # ACCELERATING / DECELERATING / FLAT

    ema_fast:    float = 0.0
    ema_mid:     float = 0.0
    ema_slow:    float = 0.0
    ema_anchor:  float = 0.0

    price_vs_ema_fast: str = "ABOVE"  # ABOVE / BELOW
    conviction: float  = 0.0          # 0.0 – 1.0


@dataclass
class TrendState:
    """Aggregated trend picture across all analyzed timeframes."""
    overall_direction:  str               = "NEUTRAL"
    overall_conviction: float             = 0.0
    votes:              Dict[str, TrendVote] = field(default_factory=dict)
    aligned_timeframes: int               = 0   # TFs agreeing with overall direction
    total_timeframes:   int               = 0

    # Convenience flags
    is_trending:       bool  = False
    is_bullish:        bool  = False
    is_bearish:        bool  = False
    primary_adx:       float = 0.0    # ADX from the 5m TF — matches the bot's trading timeframe


class TrendEngine:
    """
    Multi-timeframe trend analysis.
    Analyzes 1m, 5m, 15m, 1H and votes on direction.
    """

    def _analyze_single(self, df: pd.DataFrame, timeframe: str) -> TrendVote:
        """Run full trend analysis on a single timeframe DataFrame."""
        vote = TrendVote(timeframe=timeframe)

        if df is None or len(df) < EMA_SLOW + 5:
            vote.direction = "NEUTRAL"
            return vote

        closes = df["close"]
        price  = float(closes.iloc[-1])

        # EMA stack
        vote.ema_fast   = float(ema_series(closes, EMA_FAST).iloc[-1])
        vote.ema_mid    = float(ema_series(closes, EMA_MID).iloc[-1])
        vote.ema_slow   = float(ema_series(closes, EMA_SLOW).iloc[-1])
        vote.ema_anchor = float(ema_series(closes, EMA_ANCHOR).iloc[-1])

        vote.price_vs_ema_fast = "ABOVE" if price >= vote.ema_fast else "BELOW"

        # Full bullish stack: price > fast > mid > slow > anchor
        bullish_stack = (
            price > vote.ema_fast > vote.ema_mid >
            vote.ema_slow > vote.ema_anchor
        )
        # Full bearish stack: price < fast < mid < slow < anchor
        bearish_stack = (
            price < vote.ema_fast < vote.ema_mid <
            vote.ema_slow < vote.ema_anchor
        )

        vote.ema_aligned = bullish_stack or bearish_stack

        # ADX
        if len(df) >= ADX_TREND_THRESHOLD:
            try:
                vote.adx = current_adx(df)
            except Exception:
                vote.adx = 0.0

        # Determine direction
        if bullish_stack:
            vote.direction = "BULLISH"
        elif bearish_stack:
            vote.direction = "BEARISH"
        elif price > vote.ema_slow:
            vote.direction = "BULLISH"  # Weak bullish
        elif price < vote.ema_slow:
            vote.direction = "BEARISH"  # Weak bearish
        else:
            vote.direction = "NEUTRAL"

        # Momentum: compare last 3 EMA fast values
        fast_series = ema_series(closes, EMA_FAST)
        if len(fast_series) >= 5:
            recent_slope = fast_series.iloc[-1] - fast_series.iloc[-4]
            if vote.direction == "BULLISH":
                vote.momentum = "ACCELERATING" if recent_slope > 0 else "DECELERATING"
            elif vote.direction == "BEARISH":
                vote.momentum = "ACCELERATING" if recent_slope < 0 else "DECELERATING"
            else:
                vote.momentum = "FLAT"

        # Conviction: ADX + EMA alignment
        adx_score = min(vote.adx / 50, 1.0) if vote.adx else 0.0
        align_score = 1.0 if vote.ema_aligned else 0.5
        vote.conviction = adx_score * 0.6 + align_score * 0.4

        return vote

    def analyze(self, data: Dict[str, Optional[pd.DataFrame]]) -> TrendState:
        """
        Analyze trend across all available timeframes.

        Args:
            data: Dict of timeframe → DataFrame from DataCache

        Returns:
            TrendState with directional votes and overall conviction
        """
        state = TrendState()

        # Timeframes to analyze for trend (ordered by weight)
        tf_weights = {"1d": 0.3, "4h": 0.25, "1h": 0.25, "15m": 0.15, "5m": 0.05}

        weighted_bull  = 0.0
        weighted_bear  = 0.0
        total_weight   = 0.0
        primary_adx    = 0.0

        for tf, weight in tf_weights.items():
            df = data.get(tf)
            if df is None or df.empty:
                continue

            vote = self._analyze_single(df, tf)
            state.votes[tf] = vote
            state.total_timeframes += 1

            # primary_adx drives the regime classifier's trending/ranging
            # decision. For an intraday 0DTE bot this must reflect the
            # timeframe the bot actually trades on (5m ORB/butterfly
            # structure), not 1H which lags the live session by hours.
            if tf == "5m":
                primary_adx = vote.adx

            if vote.direction == "BULLISH":
                weighted_bull += weight * vote.conviction
                total_weight  += weight
            elif vote.direction == "BEARISH":
                weighted_bear += weight * vote.conviction
                total_weight  += weight
            else:
                total_weight += weight * 0.5  # Neutral still counts as weight

        state.primary_adx = primary_adx

        # Overall direction from weighted votes
        if total_weight > 0:
            bull_score = weighted_bull / total_weight
            bear_score = weighted_bear / total_weight

            if bull_score > bear_score and bull_score > 0.3:
                state.overall_direction  = "BULLISH"
                state.overall_conviction = bull_score
            elif bear_score > bull_score and bear_score > 0.3:
                state.overall_direction  = "BEARISH"
                state.overall_conviction = bear_score
            else:
                state.overall_direction  = "NEUTRAL"
                state.overall_conviction = 0.0

        # Count aligned timeframes
        state.aligned_timeframes = sum(
            1 for v in state.votes.values()
            if v.direction == state.overall_direction
        )

        state.is_trending = primary_adx >= ADX_TREND_THRESHOLD
        state.is_bullish  = state.overall_direction == "BULLISH"
        state.is_bearish  = state.overall_direction == "BEARISH"

        logger.debug(
            f"Trend: {state.overall_direction} "
            f"conviction={state.overall_conviction:.2f} "
            f"ADX(5m)={primary_adx:.1f} "
            f"aligned={state.aligned_timeframes}/{state.total_timeframes}"
        )
        return state


# Module-level singleton
_trend_engine: Optional[TrendEngine] = None


def get_trend_engine() -> TrendEngine:
    global _trend_engine
    if _trend_engine is None:
        _trend_engine = TrendEngine()
    return _trend_engine
