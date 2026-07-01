"""
analysis/orb_engine.py — Opening Range Breakout state machine.
v1.0 — original release
v1.1 — 2026-06-30 — full state model rewrite
v1.2 — 2026-06-30 — fix cutoff check running before range-setting
v1.3 — 2026-07-01 — ORB range now read from orb_range.json (written by
        analysis/get_orb_range.py). Single source of truth — no yfinance
        calls inside the engine, no log parsing, no circular logic.
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional
from datetime import datetime
import pandas as pd

from utils.time_utils import now_et, is_past_entry_cutoff
from utils.math_utils import orb_strike_selection
from config import (
    ORB_BREAK_BUFFER, ORB_MAX_RETEST_BARS, STRIKE_INCREMENT, INSTRUMENT
)

logger = logging.getLogger(__name__)

ORB_RANGE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orb_range.json")


class ORBState:
    WAITING                    = "WAITING"
    RANGING                    = "RANGING"
    BREAK_HIGH_AWAITING_RETEST = "BREAK_HIGH_AWAITING_RETEST"
    BREAK_LOW_AWAITING_RETEST  = "BREAK_LOW_AWAITING_RETEST"
    INVALIDATED                = "INVALIDATED"
    OPEN_LONG                  = "OPEN_LONG"
    OPEN_SHORT                 = "OPEN_SHORT"
    EXPIRED                    = "EXPIRED"


@dataclass
class ORBData:
    state:              str   = ORBState.WAITING
    orb_high:           float = 0.0
    orb_low:            float = 0.0
    orb_width:          float = 0.0
    break_candle_high:  float = 0.0
    break_candle_low:   float = 0.0
    break_candle_close: float = 0.0
    break_direction:    str   = ""
    bars_since_break:   int   = 0
    target_100pct:      float = 0.0
    target_50pct:       float = 0.0
    stop_level:         float = 0.0
    target_strike:      int   = 0
    confirmed_at:       str   = ""
    attempt_number:     int   = 0
    entries_expired:    bool  = False


class ORBEngine:

    def __init__(self):
        self._data = ORBData()
        self._range_date = None

    @property
    def data(self) -> ORBData:
        return self._data

    def reset_for_session(self):
        self._data = ORBData()
        self._range_date = None
        logger.info("ORB engine reset for new session")

    def _rearm(self):
        d = self._data
        orb_high, orb_low, orb_width_val = d.orb_high, d.orb_low, d.orb_width
        attempt = d.attempt_number
        self._data = ORBData()
        self._data.orb_high       = orb_high
        self._data.orb_low        = orb_low
        self._data.orb_width      = orb_width_val
        self._data.state          = ORBState.RANGING
        self._data.attempt_number = attempt
        logger.info(
            f"ORB re-armed for next attempt (#{attempt + 1}): "
            f"watching range {orb_low:.2f}-{orb_high:.2f}"
        )

    def _load_range_from_file(self):
        """Load ORB range from orb_range.json — the single source of truth."""
        d = self._data
        try:
            with open(ORB_RANGE_FILE) as f:
                data = json.load(f)
            high  = float(data["high"])
            low   = float(data["low"])
            width = float(data["width"])
            date  = data["date"]
            if high > 0 and low > 0:
                d.orb_high  = high
                d.orb_low   = low
                d.orb_width = width
                self._range_date = date
                if d.state == ORBState.WAITING:
                    d.state = ORBState.RANGING
                logger.info(
                    f"ORB range set: high={high:.2f} low={low:.2f} "
                    f"width={width:.2f} date={date}"
                )
        except Exception as e:
            logger.debug(f"ORB range file not ready: {e}")

    def update(self, df_5m: pd.DataFrame, df_1m: pd.DataFrame,
               current_price: float) -> ORBData:
        d = self._data

        if d.state in (ORBState.OPEN_LONG, ORBState.OPEN_SHORT):
            return d

        # Load range from file if not yet set for today
        today = now_et().strftime("%Y-%m-%d")
        if self._range_date != today or d.orb_high == 0.0:
            self._load_range_from_file()

        past_cutoff = is_past_entry_cutoff()
        d.entries_expired = past_cutoff

        if past_cutoff and d.state not in (
            ORBState.BREAK_HIGH_AWAITING_RETEST, ORBState.BREAK_LOW_AWAITING_RETEST
        ):
            if d.state != ORBState.EXPIRED:
                d.state = ORBState.EXPIRED
                logger.info(
                    f"ORB: past entry cutoff — EXPIRED "
                    f"(range: {d.orb_low:.2f}-{d.orb_high:.2f})"
                )
            return d

        if d.state == ORBState.RANGING and not past_cutoff:
            self._check_for_break(df_1m)

        if d.state in (ORBState.BREAK_HIGH_AWAITING_RETEST, ORBState.BREAK_LOW_AWAITING_RETEST):
            self._check_for_retest(df_1m)

        if d.state == ORBState.INVALIDATED and not past_cutoff:
            self._rearm()
        elif d.state == ORBState.INVALIDATED and past_cutoff:
            d.state = ORBState.EXPIRED

        return d

    def notify_position_closed(self):
        d = self._data
        if d.state in (ORBState.OPEN_LONG, ORBState.OPEN_SHORT):
            if is_past_entry_cutoff():
                d.state = ORBState.EXPIRED
            else:
                logger.info("ORB position closed — re-arming for next attempt")
                self._rearm()

    def _check_for_break(self, df_1m: pd.DataFrame):
        d = self._data
        if df_1m is None or len(df_1m) < 2:
            return
        candle = df_1m.iloc[-2]
        close  = float(candle["close"])
        open_  = float(candle["open"])
        buffer = d.orb_high * ORB_BREAK_BUFFER / 100

        if close > d.orb_high + buffer:
            d.break_direction    = "long"
            d.break_candle_close = close
            d.break_candle_high  = max(open_, close)
            d.break_candle_low   = min(open_, close)
            d.bars_since_break   = 0
            d.target_100pct      = d.orb_high + d.orb_width
            d.target_50pct       = d.orb_high + d.orb_width * 0.5
            d.stop_level         = d.break_candle_low
            d.target_strike      = orb_strike_selection(d.orb_high, d.orb_low, "long", STRIKE_INCREMENT)
            d.attempt_number    += 1
            d.state              = ORBState.BREAK_HIGH_AWAITING_RETEST
            logger.info(
                f"ORB BREAK HIGH (attempt #{d.attempt_number}): close={close:.2f} "
                f"above {d.orb_high:.2f} target={d.target_100pct:.2f} strike={d.target_strike}"
            )
        elif close < d.orb_low - buffer:
            d.break_direction    = "short"
            d.break_candle_close = close
            d.break_candle_high  = max(open_, close)
            d.break_candle_low   = min(open_, close)
            d.bars_since_break   = 0
            d.target_100pct      = d.orb_low - d.orb_width
            d.target_50pct       = d.orb_low - d.orb_width * 0.5
            d.stop_level         = d.break_candle_high
            d.target_strike      = orb_strike_selection(d.orb_high, d.orb_low, "short", STRIKE_INCREMENT)
            d.attempt_number    += 1
            d.state              = ORBState.BREAK_LOW_AWAITING_RETEST
            logger.info(
                f"ORB BREAK LOW (attempt #{d.attempt_number}): close={close:.2f} "
                f"below {d.orb_low:.2f} target={d.target_100pct:.2f} strike={d.target_strike}"
            )

    def _check_for_retest(self, df_1m: pd.DataFrame):
        d = self._data
        if df_1m is None or len(df_1m) < 2:
            return
        d.bars_since_break += 1
        if d.bars_since_break > ORB_MAX_RETEST_BARS:
            d.state = ORBState.INVALIDATED
            logger.info(f"ORB: retest timeout — INVALIDATED (re-arming)")
            return

        candle    = df_1m.iloc[-2]
        close     = float(candle["close"])
        open_     = float(candle["open"])
        high      = float(candle["high"])
        low       = float(candle["low"])
        body_high = max(open_, close)
        body_low  = min(open_, close)

        if d.break_direction == "long":
            if low < d.orb_high and body_low >= d.orb_high * 0.999:
                d.state        = ORBState.OPEN_LONG
                d.confirmed_at = str(now_et())
                logger.info(f"ORB CONFIRMED LONG (attempt #{d.attempt_number}): wick={low:.2f} body_low={body_low:.2f}")
            elif close < d.orb_high:
                d.state = ORBState.INVALIDATED
                logger.info(f"ORB INVALIDATED: close={close:.2f} back inside range")
        else:
            if high > d.orb_low and body_high <= d.orb_low * 1.001:
                d.state        = ORBState.OPEN_SHORT
                d.confirmed_at = str(now_et())
                logger.info(f"ORB CONFIRMED SHORT (attempt #{d.attempt_number}): wick={high:.2f} body_high={body_high:.2f}")
            elif close > d.orb_low:
                d.state = ORBState.INVALIDATED
                logger.info(f"ORB INVALIDATED: close={close:.2f} back inside range")

    def mark_triggered(self):
        self.notify_position_closed()

    @property
    def is_confirmed(self) -> bool:
        return self._data.state in (ORBState.OPEN_LONG, ORBState.OPEN_SHORT)

    @property
    def direction(self) -> str:
        if self._data.state == ORBState.OPEN_LONG:  return "long"
        if self._data.state == ORBState.OPEN_SHORT: return "short"
        return ""


_orb_engine: Optional[ORBEngine] = None

def get_orb_engine() -> ORBEngine:
    global _orb_engine
    if _orb_engine is None:
        _orb_engine = ORBEngine()
    return _orb_engine
