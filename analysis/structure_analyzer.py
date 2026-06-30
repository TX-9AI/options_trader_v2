"""
analysis/structure_analyzer.py — Market structure analysis.
Swing highs/lows, S/R levels, Fair Value Gaps, order blocks,
and HH/HL vs LH/LL sequence detection.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import pandas as pd
import numpy as np

from config import (
    SWING_LOOKBACK, SR_TOUCH_MIN, SR_ZONE_PCT,
    FVG_MIN_SIZE_PCT, ORDER_BLOCK_LOOKBACK
)
from utils.math_utils import find_swing_highs, find_swing_lows, within_pct

logger = logging.getLogger(__name__)


@dataclass
class SwingPoint:
    """A single swing high or low."""
    price:      float
    index:      int
    kind:       str    # "high" or "low"
    timeframe:  str    = ""
    strength:   float  = 1.0   # Normalized 0–1 based on prominence


@dataclass
class SRLevel:
    """A support/resistance level with zone boundaries."""
    price:      float
    zone_top:   float
    zone_bot:   float
    touches:    int    = 0
    kind:       str    = "both"   # "support" / "resistance" / "both"
    timeframe:  str    = ""
    strength:   float  = 1.0


@dataclass
class FairValueGap:
    """A 3-candle imbalance / Fair Value Gap."""
    top:        float
    bottom:     float
    size_pct:   float
    direction:  str    = "bullish"   # "bullish" (gap up) or "bearish" (gap down)
    index:      int    = 0
    filled:     bool   = False


@dataclass
class OrderBlock:
    """Last opposing candle before a significant impulse move."""
    top:        float
    bottom:     float
    direction:  str    = "bullish"  # direction of the impulse that followed
    index:      int    = 0
    timeframe:  str    = ""


@dataclass
class StructureMap:
    """Complete market structure picture from all timeframes."""

    # Swing structure
    swing_highs:        List[SwingPoint]  = field(default_factory=list)
    swing_lows:         List[SwingPoint]  = field(default_factory=list)
    structure_sequence: str               = "NEUTRAL"  # HH_HL / LH_LL / MIXED

    # Key levels
    sr_levels:          List[SRLevel]     = field(default_factory=list)
    nearest_resistance: Optional[float]   = None
    nearest_support:    Optional[float]   = None

    # Imbalances
    fvgs:               List[FairValueGap] = field(default_factory=list)
    order_blocks:       List[OrderBlock]   = field(default_factory=list)

    # Session levels
    prev_session_high:  Optional[float]   = None
    prev_session_low:   Optional[float]   = None
    session_high:       Optional[float]   = None
    session_low:        Optional[float]   = None

    # Convenience
    in_sr_zone:         bool              = False
    nearest_sr_distance_pct: float        = 0.0


class StructureAnalyzer:
    """
    Identifies and maps key market structure elements.
    Operates primarily on 15m and 1H for swing structure,
    5m for entry-level detail.
    """

    def analyze(self, df_5m: pd.DataFrame, df_15m: pd.DataFrame,
                df_1h: pd.DataFrame, current_price: float) -> StructureMap:
        """
        Full structure analysis across timeframes.
        Returns a StructureMap with all identified elements.
        """
        smap = StructureMap()

        if df_15m is not None and not df_15m.empty:
            self._find_swings(smap, df_15m, "15m")
            self._find_sr_levels(smap, df_15m, "15m")
            self._find_fvgs(smap, df_15m, "15m")
            self._find_order_blocks(smap, df_15m, "15m")

        if df_1h is not None and not df_1h.empty:
            self._find_swings(smap, df_1h, "1h", weight=2.0)
            self._find_sr_levels(smap, df_1h, "1h")

        if df_5m is not None and not df_5m.empty:
            self._find_fvgs(smap, df_5m, "5m")
            self._session_levels(smap, df_5m)

        # Classify structural sequence from 15m swings
        self._classify_sequence(smap)

        # Nearest S/R to current price
        self._nearest_levels(smap, current_price)

        res_str = f"{smap.nearest_resistance:.0f}" if smap.nearest_resistance is not None else "N/A"
        sup_str = f"{smap.nearest_support:.0f}" if smap.nearest_support is not None else "N/A"
        logger.debug(
            f"Structure: {smap.structure_sequence} "
            f"SRlevels={len(smap.sr_levels)} "
            f"FVGs={len(smap.fvgs)} "
            f"OBs={len(smap.order_blocks)} "
            f"res={res_str} "
            f"sup={sup_str}"
        )
        return smap

    def _find_swings(self, smap: StructureMap, df: pd.DataFrame,
                     tf: str, weight: float = 1.0):
        """Detect and add swing highs and lows."""
        highs  = df["high"].tolist()
        lows   = df["low"].tolist()
        closes = df["close"].tolist()

        lb = min(SWING_LOOKBACK, len(highs) // 4)

        for idx, price in find_swing_highs(highs, lb):
            smap.swing_highs.append(SwingPoint(
                price=price, index=idx, kind="high",
                timeframe=tf, strength=weight
            ))

        for idx, price in find_swing_lows(lows, lb):
            smap.swing_lows.append(SwingPoint(
                price=price, index=idx, kind="low",
                timeframe=tf, strength=weight
            ))

    def _find_sr_levels(self, smap: StructureMap, df: pd.DataFrame, tf: str):
        """
        Identify S/R levels by clustering swing points that are within
        SR_ZONE_PCT of each other and have been touched SR_TOUCH_MIN times.
        """
        all_levels = (
            [(s.price, "high") for s in smap.swing_highs if s.timeframe == tf] +
            [(s.price, "low")  for s in smap.swing_lows  if s.timeframe == tf]
        )

        if not all_levels:
            return

        # Cluster nearby levels
        used = [False] * len(all_levels)
        for i, (price_i, kind_i) in enumerate(all_levels):
            if used[i]:
                continue
            cluster = [price_i]
            for j, (price_j, kind_j) in enumerate(all_levels[i+1:], i+1):
                if not used[j] and within_pct(price_i, price_j, SR_ZONE_PCT):
                    cluster.append(price_j)
                    used[j] = True
            used[i] = True

            if len(cluster) >= SR_TOUCH_MIN:
                avg_price = sum(cluster) / len(cluster)
                zone_top  = avg_price * (1 + SR_ZONE_PCT / 2)
                zone_bot  = avg_price * (1 - SR_ZONE_PCT / 2)

                # Check if already in smap
                exists = any(within_pct(l.price, avg_price, SR_ZONE_PCT)
                             for l in smap.sr_levels)
                if not exists:
                    smap.sr_levels.append(SRLevel(
                        price=avg_price,
                        zone_top=zone_top,
                        zone_bot=zone_bot,
                        touches=len(cluster),
                        timeframe=tf,
                        strength=min(len(cluster) / 5, 1.0)
                    ))

        # Also add key OHLCV levels from recent candles
        if len(df) >= 20:
            for col in ["high", "low"]:
                recent_extreme = float(df[col].iloc[-20:].max() if col == "high"
                                       else df[col].iloc[-20:].min())
                if not any(within_pct(l.price, recent_extreme, SR_ZONE_PCT)
                           for l in smap.sr_levels):
                    smap.sr_levels.append(SRLevel(
                        price=recent_extreme,
                        zone_top=recent_extreme * (1 + SR_ZONE_PCT / 2),
                        zone_bot=recent_extreme * (1 - SR_ZONE_PCT / 2),
                        touches=1,
                        timeframe=tf,
                        strength=0.5
                    ))

    def _find_fvgs(self, smap: StructureMap, df: pd.DataFrame, tf: str):
        """
        Detect Fair Value Gaps (3-candle imbalances).
        Bullish FVG: candle[i].low > candle[i-2].high
        Bearish FVG: candle[i].high < candle[i-2].low
        """
        if len(df) < 3:
            return

        for i in range(2, len(df)):
            # Bullish FVG
            gap_bot = float(df["high"].iloc[i - 2])
            gap_top = float(df["low"].iloc[i])
            if gap_top > gap_bot:
                size_pct = (gap_top - gap_bot) / gap_bot
                if size_pct >= FVG_MIN_SIZE_PCT:
                    smap.fvgs.append(FairValueGap(
                        top=gap_top, bottom=gap_bot,
                        size_pct=size_pct, direction="bullish", index=i
                    ))

            # Bearish FVG
            gap_top2 = float(df["low"].iloc[i - 2])
            gap_bot2 = float(df["high"].iloc[i])
            if gap_bot2 < gap_top2:
                size_pct = (gap_top2 - gap_bot2) / gap_top2
                if size_pct >= FVG_MIN_SIZE_PCT:
                    smap.fvgs.append(FairValueGap(
                        top=gap_top2, bottom=gap_bot2,
                        size_pct=size_pct, direction="bearish", index=i
                    ))

        # Keep only the 10 most recent FVGs
        smap.fvgs = sorted(smap.fvgs, key=lambda f: f.index, reverse=True)[:10]

    def _find_order_blocks(self, smap: StructureMap, df: pd.DataFrame, tf: str):
        """
        Detect order blocks: last bearish candle before bullish impulse
        or last bullish candle before bearish impulse.
        Look back ORDER_BLOCK_LOOKBACK candles.
        """
        if len(df) < 5:
            return

        lookback = min(ORDER_BLOCK_LOOKBACK, len(df) - 2)
        for i in range(lookback, len(df) - 1):
            this_candle = df.iloc[i]
            next_candle = df.iloc[i + 1]

            this_body = abs(float(this_candle["close"]) - float(this_candle["open"]))
            next_body = abs(float(next_candle["close"]) - float(next_candle["open"]))

            # Bullish OB: bearish candle followed by strong bullish
            if (float(this_candle["close"]) < float(this_candle["open"]) and
                    float(next_candle["close"]) > float(next_candle["open"]) and
                    next_body > this_body * 1.5):
                smap.order_blocks.append(OrderBlock(
                    top=float(this_candle["open"]),
                    bottom=float(this_candle["close"]),
                    direction="bullish", index=i, timeframe=tf
                ))

            # Bearish OB: bullish candle followed by strong bearish
            elif (float(this_candle["close"]) > float(this_candle["open"]) and
                  float(next_candle["close"]) < float(next_candle["open"]) and
                  next_body > this_body * 1.5):
                smap.order_blocks.append(OrderBlock(
                    top=float(this_candle["close"]),
                    bottom=float(this_candle["open"]),
                    direction="bearish", index=i, timeframe=tf
                ))

        # Keep most recent 5
        smap.order_blocks = sorted(smap.order_blocks,
                                   key=lambda ob: ob.index, reverse=True)[:5]

    def _classify_sequence(self, smap: StructureMap):
        """
        Classify structure as HH_HL (uptrend), LH_LL (downtrend), or MIXED.
        Uses the 3 most recent swing highs and lows on 15m.
        """
        highs_15 = sorted(
            [s for s in smap.swing_highs if s.timeframe == "15m"],
            key=lambda s: s.index
        )
        lows_15 = sorted(
            [s for s in smap.swing_lows if s.timeframe == "15m"],
            key=lambda s: s.index
        )

        if len(highs_15) >= 2 and len(lows_15) >= 2:
            hh = highs_15[-1].price > highs_15[-2].price
            hl = lows_15[-1].price > lows_15[-2].price
            lh = highs_15[-1].price < highs_15[-2].price
            ll = lows_15[-1].price < lows_15[-2].price

            if hh and hl:
                smap.structure_sequence = "HH_HL"   # Uptrend
            elif lh and ll:
                smap.structure_sequence = "LH_LL"   # Downtrend
            else:
                smap.structure_sequence = "MIXED"
        else:
            smap.structure_sequence = "NEUTRAL"

    def _session_levels(self, smap: StructureMap, df_5m: pd.DataFrame):
        """Extract current session high/low from 5m data."""
        if df_5m is None or df_5m.empty:
            return
        # Current session: last 78 5m candles ≈ 6.5 hours
        session = df_5m.iloc[-78:] if len(df_5m) >= 78 else df_5m
        smap.session_high = float(session["high"].max())
        smap.session_low  = float(session["low"].min())

        # Previous session approximation
        if len(df_5m) >= 156:
            prev = df_5m.iloc[-156:-78]
            smap.prev_session_high = float(prev["high"].max())
            smap.prev_session_low  = float(prev["low"].min())

    def _nearest_levels(self, smap: StructureMap, price: float):
        """Find nearest S/R above and below current price."""
        if not smap.sr_levels or price == 0:
            return

        above = [l.price for l in smap.sr_levels if l.price > price]
        below = [l.price for l in smap.sr_levels if l.price < price]

        smap.nearest_resistance = min(above) if above else None
        smap.nearest_support    = max(below) if below else None

        # Is price currently inside an S/R zone?
        smap.in_sr_zone = any(
            l.zone_bot <= price <= l.zone_top for l in smap.sr_levels
        )

        # Distance to nearest level as %
        nearest = None
        if smap.nearest_resistance and smap.nearest_support:
            dist_r = abs(smap.nearest_resistance - price) / price
            dist_s = abs(price - smap.nearest_support) / price
            nearest = min(dist_r, dist_s)
        elif smap.nearest_resistance:
            nearest = abs(smap.nearest_resistance - price) / price
        elif smap.nearest_support:
            nearest = abs(price - smap.nearest_support) / price

        smap.nearest_sr_distance_pct = nearest or 0.0

    def sr_between(self, smap: StructureMap,
                   price_a: float, price_b: float) -> List[SRLevel]:
        """Return S/R levels between two prices (for partial exit logic)."""
        lo = min(price_a, price_b)
        hi = max(price_a, price_b)
        return [l for l in smap.sr_levels if lo < l.price < hi]


# Module-level singleton
_structure_analyzer: Optional[StructureAnalyzer] = None


def get_structure_analyzer() -> StructureAnalyzer:
    global _structure_analyzer
    if _structure_analyzer is None:
        _structure_analyzer = StructureAnalyzer()
    return _structure_analyzer
