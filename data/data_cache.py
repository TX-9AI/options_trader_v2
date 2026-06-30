"""
data/data_cache.py — Caches underlying OHLCV candles to reduce API calls.
Respects staleness limits per timeframe. Provides get_all() for analysis modules.
"""

import logging
import time
from typing import Dict, Optional
import pandas as pd

from config import INSTRUMENT, TIMEFRAMES, CACHE_STALENESS_SECONDS
from data.market_data import fetch_candles, fetch_quote

logger = logging.getLogger(__name__)


class DataCache:
    """
    Caches candle data per timeframe with staleness tracking.
    Short timeframes (1m, 5m) refresh more frequently.
    """

    def __init__(self, symbol: str = INSTRUMENT):
        self.symbol  = symbol
        self._cache: Dict[str, pd.DataFrame] = {}
        self._fetched_at: Dict[str, float]   = {}
        self._last_price: Optional[float]    = None
        self._price_fetched_at: float        = 0

    def get(self, timeframe: str) -> Optional[pd.DataFrame]:
        """Return cached candles for timeframe, refreshing if stale."""
        staleness = CACHE_STALENESS_SECONDS.get(timeframe, 60)
        age = time.time() - self._fetched_at.get(timeframe, 0)
        if age > staleness or timeframe not in self._cache:
            self._refresh(timeframe)
        return self._cache.get(timeframe)

    def get_all(self) -> Dict[str, Optional[pd.DataFrame]]:
        """Return all timeframes, refreshing stale ones."""
        result = {}
        for tf in TIMEFRAMES.keys():
            result[tf] = self.get(tf)
        return result

    def get_price(self) -> Optional[float]:
        """Return current underlying price, refreshing if stale."""
        age = time.time() - self._price_fetched_at
        if age > 5 or self._last_price is None:
            price = fetch_quote(self.symbol)
            if price:
                self._last_price     = price
                self._price_fetched_at = time.time()
        return self._last_price

    def _refresh(self, timeframe: str):
        """Fetch fresh candles for a single timeframe."""
        count = TIMEFRAMES.get(timeframe, {}).get("candles", 50)
        df    = fetch_candles(self.symbol, timeframe, count)
        if df is not None and not df.empty:
            self._cache[timeframe]     = df
            self._fetched_at[timeframe] = time.time()
            logger.debug(f"Cache refresh {self.symbol} {timeframe}: {len(df)} candles")
        else:
            logger.warning(f"Cache refresh failed for {self.symbol} {timeframe}")

    def invalidate(self, timeframe: Optional[str] = None):
        """Force refresh on next access."""
        if timeframe:
            self._fetched_at.pop(timeframe, None)
        else:
            self._fetched_at.clear()


_cache: Optional[DataCache] = None


def get_cache(symbol: str = INSTRUMENT) -> DataCache:
    global _cache
    if _cache is None or _cache.symbol != symbol:
        _cache = DataCache(symbol)
    return _cache
