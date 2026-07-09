"""
analysis/liquidity_mapper.py — Institutional liquidity mapping.
Tracks equal highs/lows, stop clusters, liquidity sweeps, and
imbalance fills. Core input for the sweep reversal strategy.

v1.3 — 2026-07-08 — SWEEP DEFINITION CORRECTION (rejection, not just penetration).
        The old rejection_pct measured distance from the wick to the LAST close in
        the window — it never checked whether price came back INSIDE the swept
        level. A breakout candle that poked a pool and CLOSED THROUGH it (accepted)
        scored a high rejection_pct and was stamped a confirmed sweep. That is the
        defect that let breakouts (AVGO 380+ open-air ladder) be classified as
        sweeps. A sweep BY DEFINITION requires penetration AND rejection — price
        thrown back and holding inside the level. Now each LiquiditySweep also
        records `reclaimed` (did closes return inside the pool and hold) and
        `closes_beyond` (how many closes ACCEPTED through it). rejection_pct is now
        measured off a close that is actually back inside the level. Acceptance
        (closes_beyond >= ACCEPT_CLOSES) is no longer a sweep — it is a breakout.
        Calibration constants (ACCEPT_CLOSES, hold bars) are placeholders to be
        tightened as candle-logger sessions accumulate; the reclaim REQUIREMENT
        itself is definitional, not a tunable.
v1.2 — 2026-07-06 — detection fixes: recent_sweep is now selected by ACTUAL
        TIME (own-timeframe bars_ago × tf minutes), not raw cross-timeframe bar
        index; sweep_age_bars reported in consistent 5m-equivalent bars (fixes
        the nonsense/negative age that let junk sweeps flip the regime and made
        fresh sweeps look stale); duplicate same-level sweeps are collapsed.

v1.1 additions:
- Previous Day High/Low (PDH/PDL) as named high-value liquidity pools
- Previous Session High/Low (Asia, London, NY) as named pools
- Named pools carry higher confluence weight in sweep reversal strategy
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from datetime import datetime, timezone, timedelta
import pandas as pd

from config import (
    EQUAL_HIGH_LOW_LOOKBACK, EQUAL_LEVEL_PCT,
    SWEEP_REJECTION_CANDLES, IMBALANCE_MIN_SIZE_PCT
)
from utils.math_utils import within_pct

logger = logging.getLogger(__name__)


@dataclass
class LiquidityPool:
    """A cluster of equal highs or lows (stop resting zone)."""
    price:          float
    kind:           str    = "high"     # "high" or "low"
    touch_count:    int    = 0
    timeframe:      str    = ""
    swept:          bool   = False
    swept_index:    int    = -1
    rejection_confirmed: bool = False
    # Named pools carry extra confluence weight
    name:           str    = ""         # e.g. "PDH", "PDL", "Asia High", "London Low"
    is_named:       bool   = False      # True for PDH/PDL/session levels


@dataclass
class LiquiditySweep:
    """A confirmed liquidity sweep event."""
    pool_price:     float
    sweep_price:    float
    kind:           str     # "high_sweep" or "low_sweep"
    rejection_candles: int  = 0
    rejection_pct:  float   = 0.0
    confirmed:      bool    = False
    bar_index:      int     = 0
    bars_ago:       int     = 0      # bars since the sweep, in its OWN timeframe
    timeframe:      str     = ""
    # Was this sweep of a named level? (PDH/PDL/session)
    swept_named_level: str  = ""        # Name of the level swept, if any
    # v1.3 — rejection vs acceptance (the truth that makes it a sweep, not a breakout)
    reclaimed:      bool    = False     # price closed back INSIDE the level and held
    closes_beyond:  int     = 0         # # of closes that ACCEPTED through the level


@dataclass
class LiquidityMap:
    """Complete liquidity landscape."""
    pools:          List[LiquidityPool]  = field(default_factory=list)
    sweeps:         List[LiquiditySweep] = field(default_factory=list)
    recent_sweep:   Optional[LiquiditySweep] = None
    sweep_age_bars: int                  = 999

    # Named key levels
    prev_day_high:      Optional[float] = None
    prev_day_low:       Optional[float] = None
    asia_session_high:  Optional[float] = None
    asia_session_low:   Optional[float] = None
    london_session_high: Optional[float] = None
    london_session_low:  Optional[float] = None
    ny_session_high:    Optional[float] = None
    ny_session_low:     Optional[float] = None

    # Stop cluster levels
    stop_clusters_above: List[float]    = field(default_factory=list)
    stop_clusters_below: List[float]    = field(default_factory=list)

    near_pool_above:     Optional[float] = None
    near_pool_below:     Optional[float] = None
    near_pool_pct:       float           = 0.05


class LiquidityMapper:
    """
    Maps institutional liquidity levels and detects sweep events.
    Includes Previous Day High/Low and session highs/lows as named pools.
    Named pools provide extra confluence when swept.
    """

    # Session hours in UTC
    ASIA_START    = 0    # 00:00 UTC
    ASIA_END      = 8    # 08:00 UTC
    LONDON_START  = 7    # 07:00 UTC (overlap with Asia close)
    LONDON_END    = 16   # 16:00 UTC
    NY_START      = 13   # 13:00 UTC
    NY_END        = 22   # 22:00 UTC

    def analyze(self, df_5m: pd.DataFrame, df_15m: pd.DataFrame,
                current_price: float) -> LiquidityMap:
        lmap = LiquidityMap()

        primary = df_15m if (df_15m is not None and not df_15m.empty) else df_5m
        if primary is None or primary.empty:
            return lmap

        # Standard equal high/low pools
        self._find_pools(lmap, primary, "15m")
        if df_5m is not None and not df_5m.empty:
            self._find_pools(lmap, df_5m, "5m")

        # Named key levels (PDH/PDL, session H/L)
        if df_5m is not None and not df_5m.empty:
            self._find_named_levels(lmap, df_5m)
        elif primary is not None:
            self._find_named_levels(lmap, primary)

        # Sweep detection
        self._detect_sweeps(lmap, primary, "15m")
        if df_5m is not None and not df_5m.empty:
            self._detect_sweeps(lmap, df_5m, "5m")

        # Stop clusters
        self._identify_stop_clusters(lmap, primary, current_price)

        # Nearby pools
        self._flag_nearby_pools(lmap, current_price)

        # Most recent sweep — selected by ACTUAL TIME, not raw bar index.
        # bar_index is per-timeframe, so comparing a 15m index to a 5m index (as
        # the old max-by-bar_index did) was meaningless and produced a nonsense
        # (often negative) age. We convert each sweep's own-timeframe bars_ago
        # into minutes, dedupe same-level sweeps found on multiple scans, pick
        # the most recent by minutes, and report age in 5m-equivalent bars so
        # the downstream <=8 thresholds stay consistent across timeframes.
        confirmed = [s for s in lmap.sweeps if s.confirmed]
        if confirmed:
            deduped = self._dedupe_sweeps(confirmed)
            recent = min(deduped, key=lambda s: s.bars_ago * self._tf_minutes(s.timeframe))
            minutes_ago = recent.bars_ago * self._tf_minutes(recent.timeframe)
            lmap.recent_sweep   = recent
            lmap.sweep_age_bars = max(0, round(minutes_ago / 5.0))   # 5m-equivalent bars

        named_levels = [p.name for p in lmap.pools if p.is_named]
        logger.debug(
            f"Liquidity: pools={len(lmap.pools)} sweeps={len(lmap.sweeps)} "
            f"named={named_levels} "
            f"recent_sweep={'YES' if lmap.recent_sweep else 'NO'} "
            f"age={lmap.sweep_age_bars}bars"
        )
        return lmap

    @staticmethod
    def _tf_minutes(tf: str) -> int:
        """Minutes per bar for a timeframe label."""
        return {"1m": 1, "5m": 5, "15m": 15, "1h": 60}.get(tf, 5)

    @staticmethod
    def _dedupe_sweeps(sweeps):
        """Collapse sweeps of the same side + price level (found on multiple
        timeframe scans) to a single entry, keeping the most recent by
        minutes-ago. Prevents duplicate/phantom sweeps from skewing selection."""
        best = {}
        for s in sweeps:
            key = (s.kind, round(s.pool_price, 2))
            mins = s.bars_ago * LiquidityMapper._tf_minutes(s.timeframe)
            cur = best.get(key)
            if cur is None or mins < (cur.bars_ago * LiquidityMapper._tf_minutes(cur.timeframe)):
                best[key] = s
        return list(best.values())

    def _find_named_levels(self, lmap: LiquidityMap, df: pd.DataFrame):
        """
        Extract Previous Day High/Low and session highs/lows from candle data.
        These are the most important liquidity levels — institutions specifically
        target these for stop hunts before reversing.
        """
        if df is None or len(df) < 50:
            return

        now_utc = datetime.now(timezone.utc)

        # Build candle timestamp index if available
        has_timestamps = hasattr(df.index, 'hour') or (
            hasattr(df.index, 'dtype') and 'datetime' in str(df.index.dtype)
        )

        if has_timestamps:
            self._find_named_levels_from_timestamps(lmap, df, now_utc)
        else:
            # Fallback: estimate from candle count (5m candles)
            self._find_named_levels_from_candle_count(lmap, df)

    def _find_named_levels_from_timestamps(self, lmap: LiquidityMap,
                                            df: pd.DataFrame, now_utc: datetime):
        """Extract named levels using actual timestamps."""
        try:
            idx = pd.DatetimeIndex(df.index)
            if idx.tz is None:
                idx = idx.tz_localize('UTC')
            else:
                idx = idx.tz_convert('UTC')

            today = now_utc.date()
            yesterday = today - timedelta(days=1)

            # Previous day
            prev_day_mask = idx.date == yesterday
            if prev_day_mask.any():
                prev_day_data = df[prev_day_mask]
                pdh = float(prev_day_data["high"].max())
                pdl = float(prev_day_data["low"].min())
                lmap.prev_day_high = pdh
                lmap.prev_day_low  = pdl
                self._add_named_pool(lmap, pdh, "high", "PDH")
                self._add_named_pool(lmap, pdl, "low", "PDL")

            # Today's sessions
            today_mask = idx.date == today

            # Asia session (00:00-08:00 UTC)
            asia_mask = today_mask & (idx.hour >= self.ASIA_START) & (idx.hour < self.ASIA_END)
            if asia_mask.any():
                asia_data = df[asia_mask]
                ash = float(asia_data["high"].max())
                asl = float(asia_data["low"].min())
                lmap.asia_session_high = ash
                lmap.asia_session_low  = asl
                self._add_named_pool(lmap, ash, "high", "Asia High")
                self._add_named_pool(lmap, asl, "low",  "Asia Low")

            # London session (07:00-16:00 UTC)
            london_mask = today_mask & (idx.hour >= self.LONDON_START) & (idx.hour < self.LONDON_END)
            if london_mask.any():
                london_data = df[london_mask]
                lsh = float(london_data["high"].max())
                lsl = float(london_data["low"].min())
                lmap.london_session_high = lsh
                lmap.london_session_low  = lsl
                self._add_named_pool(lmap, lsh, "high", "London High")
                self._add_named_pool(lmap, lsl, "low",  "London Low")

            # NY session (13:00-22:00 UTC)
            ny_mask = today_mask & (idx.hour >= self.NY_START) & (idx.hour < self.NY_END)
            if ny_mask.any():
                ny_data = df[ny_mask]
                nyh = float(ny_data["high"].max())
                nyl = float(ny_data["low"].min())
                lmap.ny_session_high = nyh
                lmap.ny_session_low  = nyl
                self._add_named_pool(lmap, nyh, "high", "NY High")
                self._add_named_pool(lmap, nyl, "low",  "NY Low")

        except Exception as e:
            logger.debug(f"Named level extraction failed: {e}")
            self._find_named_levels_from_candle_count(lmap, df)

    def _find_named_levels_from_candle_count(self, lmap: LiquidityMap, df: pd.DataFrame):
        """
        Fallback: estimate session levels from candle count.
        5m candles: 288/day, 96/session (8hrs), 48/4hrs
        """
        n = len(df)
        if n < 50:
            return

        # Previous day = candles 288-576 ago (rough)
        prev_day_start = min(n, 576)
        prev_day_end   = min(n, 288)
        if prev_day_start > prev_day_end:
            prev_day = df.iloc[n - prev_day_start : n - prev_day_end]
            if len(prev_day) > 0:
                pdh = float(prev_day["high"].max())
                pdl = float(prev_day["low"].min())
                lmap.prev_day_high = pdh
                lmap.prev_day_low  = pdl
                self._add_named_pool(lmap, pdh, "high", "PDH")
                self._add_named_pool(lmap, pdl, "low",  "PDL")

        # Asia session estimate = 96 candles ago (8hrs of 5m)
        asia_candles = min(96, n // 3)
        asia_data = df.iloc[n - asia_candles * 2 : n - asia_candles]
        if len(asia_data) > 0:
            ash = float(asia_data["high"].max())
            asl = float(asia_data["low"].min())
            lmap.asia_session_high = ash
            lmap.asia_session_low  = asl
            self._add_named_pool(lmap, ash, "high", "Asia High")
            self._add_named_pool(lmap, asl, "low",  "Asia Low")

    def _add_named_pool(self, lmap: LiquidityMap, price: float,
                         kind: str, name: str):
        """Add a named liquidity pool, avoiding duplicates."""
        # Don't add if too close to existing named pool
        for pool in lmap.pools:
            if pool.is_named and within_pct(pool.price, price, 0.002):
                return

        lmap.pools.append(LiquidityPool(
            price=price,
            kind=kind,
            touch_count=1,
            timeframe="daily" if "PD" in name else "session",
            name=name,
            is_named=True
        ))

    def _find_pools(self, lmap: LiquidityMap, df: pd.DataFrame, tf: str):
        """Find equal highs and equal lows."""
        highs = df["high"].tolist()
        lows  = df["low"].tolist()
        n     = min(len(highs), EQUAL_HIGH_LOW_LOOKBACK)

        used_h = [False] * n
        for i in range(n - 1, 0, -1):
            if used_h[i]:
                continue
            cluster = [highs[-(n) + i]]
            for j in range(i - 1, max(i - 20, -1), -1):
                if not used_h[j] and within_pct(highs[-(n)+i], highs[-(n)+j], EQUAL_LEVEL_PCT):
                    cluster.append(highs[-(n)+j])
                    used_h[j] = True
            used_h[i] = True
            if len(cluster) >= 2:
                avg = sum(cluster) / len(cluster)
                if not any(within_pct(p.price, avg, EQUAL_LEVEL_PCT)
                           for p in lmap.pools if p.kind == "high"):
                    lmap.pools.append(LiquidityPool(
                        price=avg, kind="high",
                        touch_count=len(cluster), timeframe=tf
                    ))

        used_l = [False] * n
        for i in range(n - 1, 0, -1):
            if used_l[i]:
                continue
            cluster = [lows[-(n) + i]]
            for j in range(i - 1, max(i - 20, -1), -1):
                if not used_l[j] and within_pct(lows[-(n)+i], lows[-(n)+j], EQUAL_LEVEL_PCT):
                    cluster.append(lows[-(n)+j])
                    used_l[j] = True
            used_l[i] = True
            if len(cluster) >= 2:
                avg = sum(cluster) / len(cluster)
                if not any(within_pct(p.price, avg, EQUAL_LEVEL_PCT)
                           for p in lmap.pools if p.kind == "low"):
                    lmap.pools.append(LiquidityPool(
                        price=avg, kind="low",
                        touch_count=len(cluster), timeframe=tf
                    ))

    def _detect_sweeps(self, lmap: LiquidityMap, df: pd.DataFrame, tf: str):
        """Detect sweep events with named level tagging."""
        if not lmap.pools:
            return

        highs  = df["high"].tolist()
        lows   = df["low"].tolist()
        closes = df["close"].tolist()
        n      = len(highs)

        for pool in lmap.pools:
            for i in range(1, n):
                if pool.kind == "high" and highs[i] > pool.price and not pool.swept:
                    # A sweep of a HIGH requires REJECTION: after poking above the
                    # pool, price must CLOSE BACK BELOW it (inside) and hold. Closes
                    # that stay ABOVE the pool are ACCEPTANCE — a breakout, not a sweep.
                    window = range(i, min(i + SWEEP_REJECTION_CANDLES + 1, n))
                    closes_beyond = sum(1 for k in window if closes[k] > pool.price)
                    reclaimed = closes[i] <= pool.price or any(
                        closes[k] <= pool.price for k in window)
                    # rejection measured off a close that is actually back INSIDE
                    reject_close = min((closes[k] for k in window
                                        if closes[k] <= pool.price), default=closes[i])
                    rejection_pct = (highs[i] - reject_close) / highs[i]
                    if reclaimed and rejection_pct >= 0.002:
                        sweep = LiquiditySweep(
                            pool_price=pool.price,
                            sweep_price=highs[i],
                            kind="high_sweep",
                            rejection_candles=SWEEP_REJECTION_CANDLES,
                            rejection_pct=rejection_pct,
                            confirmed=True,
                            bar_index=i,
                            bars_ago=(n - 1 - i),
                            timeframe=tf,
                            swept_named_level=pool.name if pool.is_named else "",
                            reclaimed=reclaimed,
                            closes_beyond=closes_beyond
                        )
                        lmap.sweeps.append(sweep)
                        pool.swept = True
                        pool.swept_index = i
                        pool.rejection_confirmed = True

                elif pool.kind == "low" and lows[i] < pool.price and not pool.swept:
                    # A sweep of a LOW requires REJECTION: after poking below the
                    # pool, price must CLOSE BACK ABOVE it (inside) and hold. Closes
                    # that stay BELOW the pool are ACCEPTANCE — a breakdown, not a sweep.
                    window = range(i, min(i + SWEEP_REJECTION_CANDLES + 1, n))
                    closes_beyond = sum(1 for k in window if closes[k] < pool.price)
                    reclaimed = closes[i] >= pool.price or any(
                        closes[k] >= pool.price for k in window)
                    reject_close = max((closes[k] for k in window
                                        if closes[k] >= pool.price), default=closes[i])
                    rejection_pct = (reject_close - lows[i]) / lows[i]
                    if reclaimed and rejection_pct >= 0.002:
                        sweep = LiquiditySweep(
                            pool_price=pool.price,
                            sweep_price=lows[i],
                            kind="low_sweep",
                            rejection_candles=SWEEP_REJECTION_CANDLES,
                            rejection_pct=rejection_pct,
                            confirmed=True,
                            bar_index=i,
                            bars_ago=(n - 1 - i),
                            timeframe=tf,
                            swept_named_level=pool.name if pool.is_named else "",
                            reclaimed=reclaimed,
                            closes_beyond=closes_beyond
                        )
                        lmap.sweeps.append(sweep)
                        pool.swept = True
                        pool.swept_index = i
                        pool.rejection_confirmed = True

    def _identify_stop_clusters(self, lmap: LiquidityMap,
                                 df: pd.DataFrame, current_price: float):
        if df is None or len(df) < 10:
            return
        recent = df.iloc[-30:] if len(df) >= 30 else df
        highs_above = [float(h) * 1.001 for h in recent["high"].tolist()
                       if float(h) > current_price]
        lmap.stop_clusters_above = sorted(set([round(h, 0) for h in highs_above]))[:5]
        lows_below = [float(l) * 0.999 for l in recent["low"].tolist()
                      if float(l) < current_price]
        lmap.stop_clusters_below = sorted(set([round(l, 0) for l in lows_below]),
                                          reverse=True)[:5]

    def _flag_nearby_pools(self, lmap: LiquidityMap, price: float):
        buffer = price * lmap.near_pool_pct / 100
        for pool in lmap.pools:
            if pool.swept:
                continue
            if pool.kind == "high" and pool.price > price:
                if pool.price - price < buffer:
                    lmap.near_pool_above = pool.price
            elif pool.kind == "low" and pool.price < price:
                if price - pool.price < buffer:
                    lmap.near_pool_below = pool.price

    def is_near_pool(self, lmap: LiquidityMap, price: float,
                     direction: str, buffer_pct: float = 0.003) -> bool:
        for pool in lmap.pools:
            if pool.swept:
                continue
            dist_pct = abs(pool.price - price) / price
            if dist_pct <= buffer_pct:
                if direction == "long" and pool.kind == "high" and pool.price > price:
                    return True
                if direction == "short" and pool.kind == "low" and pool.price < price:
                    return True
        return False

    def recent_sweep_exists(self, lmap: LiquidityMap, max_bars: int = 10) -> bool:
        return lmap.recent_sweep is not None and lmap.sweep_age_bars <= max_bars


_liquidity_mapper: Optional[LiquidityMapper] = None


def get_liquidity_mapper() -> LiquidityMapper:
    global _liquidity_mapper
    if _liquidity_mapper is None:
        _liquidity_mapper = LiquidityMapper()
    return _liquidity_mapper
