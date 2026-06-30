"""
utils/math_utils.py — Math helpers for options trading.
Includes ORB calculations, contract sizing, strike rounding, P&L.
"""

import math
from typing import Optional
import numpy as np
import pandas as pd


# ─── STRIKE UTILITIES ─────────────────────────────────────────────────────────

def round_to_strike(price: float, increment: int) -> int:
    """Round price to nearest valid strike increment."""
    return int(round(price / increment) * increment)


def floor_to_strike(price: float, increment: int) -> int:
    """Round DOWN to nearest valid strike (for OTM put selection)."""
    return int(math.floor(price / increment) * increment)


def ceil_to_strike(price: float, increment: int) -> int:
    """Round UP to nearest valid strike (for OTM call selection)."""
    return int(math.ceil(price / increment) * increment)


# ─── ORB MATH ─────────────────────────────────────────────────────────────────

def orb_width(orb_high: float, orb_low: float) -> float:
    """Width of the opening range."""
    return orb_high - orb_low


def orb_breakout_target(orb_high: float, orb_low: float, direction: str) -> float:
    """
    Project the 100% TP from an ORB breakout.
    direction: 'long' (price broke above) or 'short' (price broke below)
    """
    width = orb_width(orb_high, orb_low)
    if direction == "long":
        return orb_high + width
    else:
        return orb_low - width


def orb_strike_selection(orb_high: float, orb_low: float,
                          direction: str, strike_increment: int) -> int:
    """
    Select the option strike based on ORB width projection.

    For a downside break: target = orb_low - orb_width
    → buy put at that strike (rounded to nearest increment)

    For an upside break: target = orb_high + orb_width
    → buy call at that strike (rounded to nearest increment)

    Example: QQQ ORB 599–608 (width=9), break below 599
    → target = 599 - 9 = 590 → buy 590 puts
    """
    target = orb_breakout_target(orb_high, orb_low, direction)
    return round_to_strike(target, strike_increment)


# ─── CONTRACT SIZING ──────────────────────────────────────────────────────────

def contracts_from_risk(risk_usd: float, option_cost_per_contract: float,
                         grade_multiplier: float = 1.0) -> int:
    """
    Calculate number of whole contracts to buy.

    contracts = floor(risk_usd × grade_multiplier / cost_per_contract)

    Args:
        risk_usd:               Fixed dollar risk from config
        option_cost_per_contract: Net debit × 100 (contract multiplier)
        grade_multiplier:       From setup grade (A=1.5, B=1.0, C=0.5)

    Returns:
        Whole number of contracts (minimum 1 if affordable)
    """
    if option_cost_per_contract <= 0:
        return 0
    adjusted_risk = risk_usd * grade_multiplier
    count = int(math.floor(adjusted_risk / option_cost_per_contract))
    return max(count, 0)


def option_cost_per_contract(premium: float, multiplier: int = 100) -> float:
    """Cost to buy one options contract."""
    return premium * multiplier


# ─── P&L CALCULATIONS ─────────────────────────────────────────────────────────

def option_pnl_pct(entry_premium: float, current_premium: float) -> float:
    """Current P&L as percentage of entry premium."""
    if entry_premium <= 0:
        return 0.0
    return (current_premium - entry_premium) / entry_premium


def option_pnl_usd(entry_premium: float, current_premium: float,
                    contracts: int, multiplier: int = 100) -> float:
    """Unrealized P&L in USD."""
    return (current_premium - entry_premium) * contracts * multiplier


def butterfly_max_profit(long_wing_premium: float, short_body_premium: float,
                          multiplier: int = 100) -> float:
    """
    Max profit for a debit butterfly:
    = (short body premium × 2 - long wing premium × 2) × multiplier
    Simplified: wing_width - net_debit
    """
    net_debit = long_wing_premium - (2 * short_body_premium)
    # Max profit = wing_width (in strike $) - net_debit, expressed as $ per point
    # We compute this from the actual prices in butterfly_strategy.py
    return net_debit * multiplier   # placeholder — override in strategy


# ─── TECHNICAL INDICATORS ─────────────────────────────────────────────────────

def ema_series(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def bb_bands(series: pd.Series, period: int = 20, std: float = 2.0):
    """Returns (upper, middle, lower)."""
    mid   = series.rolling(period).mean()
    sigma = series.rolling(period).std()
    return (mid + std * sigma), mid, (mid - std * sigma)


def bollinger_bands(series: pd.Series, period: int = 20, std: float = 2.0):
    """Alias for bb_bands — returns (upper, middle, lower)."""
    return bb_bands(series, period, std)


def bb_width(upper: float, lower: float, middle: float) -> float:
    """Bollinger Band width as fraction of middle band."""
    if middle == 0:
        return 0.0
    return (upper - lower) / middle


def bb_width_percentile(widths: list, current_width: float,
                         lookback: int = 50) -> float:
    """
    Return the percentile (0.0–1.0) of current_width within recent history.
    Used to detect squeeze (low percentile) vs expansion (high percentile).
    """
    if not widths or len(widths) < 2:
        return 0.5
    recent = widths[-lookback:] if len(widths) >= lookback else widths
    below  = sum(1 for w in recent if w <= current_width)
    return below / len(recent)


def within_pct(a: float, b: float, pct: float) -> bool:
    """True if a and b are within pct of each other."""
    if a == 0:
        return False
    return abs(a - b) / a <= pct


def adx_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute ADX series."""
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    plus_dm  = high.diff()
    minus_dm = -low.diff()
    plus_dm  = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(span=period, adjust=False).mean()
    di_plus  = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr
    di_minus = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr
    dx = (abs(di_plus - di_minus) / (di_plus + di_minus + 1e-10)) * 100
    adx = dx.ewm(span=period, adjust=False).mean()
    return adx


def current_adx(df: pd.DataFrame, period: int = 14) -> float:
    """Return the most recent ADX value as a float."""
    try:
        s = adx_series(df, period)
        if s.empty:
            return 0.0
        return float(s.iloc[-1])
    except Exception:
        return 0.0


def find_swing_highs(prices: list, lookback: int = 10):
    """
    Identify swing highs in a list of prices.
    A swing high is a local maximum within ±lookback bars.

    Args:
        prices:   List of price values (e.g. df["high"].tolist())
        lookback: Bars on each side to check

    Yields:
        (index, price) tuples for each swing high found
    """
    n = len(prices)
    for i in range(lookback, n - lookback):
        window = prices[i - lookback: i + lookback + 1]
        if prices[i] == max(window):
            yield i, prices[i]


def find_swing_lows(prices: list, lookback: int = 10):
    """
    Identify swing lows in a list of prices.
    A swing low is a local minimum within ±lookback bars.

    Args:
        prices:   List of price values (e.g. df["low"].tolist())
        lookback: Bars on each side to check

    Yields:
        (index, price) tuples for each swing low found
    """
    n = len(prices)
    for i in range(lookback, n - lookback):
        window = prices[i - lookback: i + lookback + 1]
        if prices[i] == min(window):
            yield i, prices[i]
