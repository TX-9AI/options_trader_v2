"""
data/market_data.py — Underlying price data (candles + live quote).

Candle history:    yfinance — free, reliable, no auth required
Live quote:        yfinance primary, TastyTrade SDK secondary

yfinance interval map:
  1m  -> "1m"   (last 7 days max)
  5m  -> "5m"   (last 60 days max)
  15m -> "15m"  (last 60 days max)
  1h  -> "1h"   (last 730 days max)
  1d  -> "1d"   (no limit)
"""

import logging
from typing import Optional, Dict
import pandas as pd

from data.tasty_client import get_session
from config import INSTRUMENT, TIMEFRAMES

logger = logging.getLogger(__name__)

YF_PERIOD_MAP = {
    "1m":  "1d",
    "5m":  "5d",
    "15m": "5d",
    "1h":  "30d",
    "1d":  "60d",
}


def fetch_candles(symbol: str, timeframe: str, count: int) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV candles via yfinance.

    Args:
        symbol:     e.g. "QQQ", "SPY", "SPX"
        timeframe:  "1m", "5m", "15m", "1h", "1d"
        count:      Number of most-recent candles to return

    Returns:
        DataFrame with columns [open, high, low, close, volume], ET datetime index
        Returns None on failure.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed — run: pip install yfinance")
        return None

    yf_symbol = "^SPX" if symbol == "SPX" else symbol
    period    = YF_PERIOD_MAP.get(timeframe, "5d")

    try:
        ticker = yf.Ticker(yf_symbol)
        df = ticker.history(period=period, interval=timeframe, auto_adjust=True)

        if df is None or df.empty:
            logger.warning(f"yfinance returned no data for {symbol} {timeframe}")
            return None

        df.columns = [c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]].copy()

        if df.index.tz is None:
            df.index = df.index.tz_localize("US/Eastern")
        else:
            df.index = df.index.tz_convert("US/Eastern")

        df = df.dropna()

        if len(df) > count:
            df = df.iloc[-count:]

        logger.debug(f"{symbol} {timeframe}: {len(df)} candles via yfinance")
        return df

    except Exception as e:
        logger.error(f"yfinance fetch failed for {symbol} {timeframe}: {e}")
        return None


def fetch_quote(symbol: str) -> Optional[float]:
    """
    Fetch current price.
    Primary:   yfinance (reliable, no auth issues)
    Secondary: TastyTrade SDK (real-time, requires market data permissions)

    Returns:
        Current price as float, or None on failure.
    """
    # Primary: yfinance
    try:
        import yfinance as yf
        yf_symbol = "^SPX" if symbol == "SPX" else symbol
        ticker = yf.Ticker(yf_symbol)

        # fast_info attributes vary by yfinance version — try multiple
        fi = ticker.fast_info
        for attr in ("last_price", "regular_market_price", "previousClose"):
            val = getattr(fi, attr, None)
            if val is not None and float(val) > 0:
                return float(val)

        # Last resort: 1m history last close
        hist = ticker.history(period="1d", interval="1m")
        if hist is not None and not hist.empty:
            return float(hist["Close"].iloc[-1])

    except Exception as e:
        logger.debug(f"yfinance quote failed for {symbol}: {e}")

    # Secondary: TastyTrade SDK
    try:
        from tastytrade.market_data import get_market_data
        from tastytrade.order import InstrumentType
        from data.tasty_client import run_async

        session   = get_session()
        inst_type = InstrumentType.INDEX if symbol == "SPX" else InstrumentType.EQUITY
        md        = run_async(get_market_data(session, symbol, inst_type))

        if md and md.mark is not None:
            return float(md.mark)
        if md and md.bid is not None and md.ask is not None:
            return float((md.bid + md.ask) / 2)
        if md and md.last is not None:
            return float(md.last)

    except Exception as e:
        logger.debug(f"TastyTrade quote unavailable for {symbol}: {e}")

    return None


def fetch_all_candles(symbol: str = INSTRUMENT) -> Dict[str, Optional[pd.DataFrame]]:
    """Fetch all configured timeframes for the underlying."""
    result = {}
    for tf, cfg in TIMEFRAMES.items():
        df = fetch_candles(symbol, tf, cfg["candles"])
        result[tf] = df
        if df is not None:
            logger.debug(f"{symbol} {tf}: {len(df)} candles")
    return result
