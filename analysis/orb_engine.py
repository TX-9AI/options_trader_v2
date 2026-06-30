"""
analysis/orb_engine.py — Opening Range Breakout state machine.
v1.0 — original release
v1.1 — 2026-06-30 — full state model rewrite:
        RANGING -> BREAK_*_AWAITING_RETEST -> OPEN_LONG/SHORT -> closed -> RANGING
        INVALIDATED re-arms back to RANGING instead of ending the session.
v1.2 — 2026-06-30 — fix: ORB range was never being set if the bot started
        or restarted after the 14:00 ET entry cutoff, because the cutoff
        check ran BEFORE the range-setting step and immediately returned
        EXPIRED. The range (high/low/width) should always be available
        for display and reference even after the cutoff — only NEW
        ENTRIES should be blocked past 14:00 ET, not the range itself.
        Range is now set first (from historical data, regardless of time),
        then the cutoff check determines entry eligibility separately.

ORB rules (exact):
  - Range defined by 9:30-9:35 ET candle (first 5-min candle) high and low
  - BREAK: 1-min candle CLOSE outside the ORB (not just a wick)
  - RETEST: a subsequent 1-min candle WICKS INTO the ORB but the BODY closes outside
  - CONFIRMED: break + retest both satisfied = valid ORB entry signal -> OPEN_LONG/SHORT
  - Stop: 1-min close beyond the BODY of the breakout candle (not the wick)
  - TP100: ORB width projected from the break level
  - TP50 (trail activation): 50% of TP100
  - No entries after 14:00 ET — but the range itself is always set/displayed
  - No chasing: a breakout with no retest wick is NOT confirmed
  - Multiple attempts per session allowed: a failed/invalidated break re-arms
"""

import logging
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
import pandas as pd

from utils.time_utils import now_et, is_orb_complete, is_past_entry_cutoff, ET
from utils.math_utils import orb_width, orb_breakout_target, orb_strike_selection
from config import (
    ORB_BREAK_BUFFER, ORB_MAX_RETEST_BARS, STRIKE_INCREMENT, INSTRUMENT
)

logger = logging.getLogger(__name__)


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
    """ORB state for the current session."""
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
    """
    State machine that tracks the ORB through the session.
    The range itself (high/low/width) is always set from historical data
    as soon as it's available, regardless of time of day. Only NEW ENTRY
    attempts are blocked past the 14:00 ET cutoff.
    """

    def __init__(self):
        self._data = ORBData()

    @property
    def data(self) -> ORBData:
        return self._data

    def reset_for_session(self):
        self._data = ORBData()
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

    def update(self, df_5m: pd.DataFrame, df_1m: pd.DataFrame,
               current_price: float) -> ORBData:
        d = self._data

        if d.state in (ORBState.OPEN_LONG, ORBState.OPEN_SHORT):
            return d

        # Step 1: Set the ORB range ALWAYS, regardless of cutoff status.
        if d.orb_high == 0.0 and d.orb_low == 0.0:
            self._set_orb_range(df_5m)

        # Step 2: Determine entry eligibility separately from range display
        past_cutoff = is_past_entry_cutoff()
        d.entries_expired = past_cutoff

        if past_cutoff and d.state not in (
            ORBState.BREAK_HIGH_AWAITING_RETEST, ORBState.BREAK_LOW_AWAITING_RETEST
        ):
            if d.state != ORBState.EXPIRED:
                d.state = ORBState.EXPIRED
                logger.info(
                    f"ORB: past 14:00 ET entry cutoff — state EXPIRED "
                    f"(range remains: {d.orb_low:.2f}-{d.orb_high:.2f})"
                )
            return d

        # Step 3: Watch for break while in RANGING (only if not past cutoff)
        if d.state == ORBState.RANGING and not past_cutoff:
            self._check_for_break(df_1m)

        # Step 4: Watch for retest while awaiting confirmation
        if d.state in (ORBState.BREAK_HIGH_AWAITING_RETEST, ORBState.BREAK_LOW_AWAITING_RETEST):
            self._check_for_retest(df_1m)

        # Step 5: Re-arm after invalidation
        if d.state == ORBState.INVALIDATED and not past_cutoff:
            self._rearm()
        elif d.state == ORBState.INVALIDATED and past_cutoff:
            d.state = ORBState.EXPIRED
            logger.info("ORB: invalidated past cutoff — state EXPIRED, no re-arm")

        return d

    def notify_position_closed(self):
        d = self._data
        if d.state in (ORBState.OPEN_LONG, ORBState.OPEN_SHORT):
            if is_past_entry_cutoff():
                d.state = ORBState.EXPIRED
                logger.info("ORB position closed past cutoff — state EXPIRED, no re-arm")
            else:
                logger.info("ORB position closed — re-arming for next attempt this session")
                self._rearm()

    def _set_orb_range(self, df_5m: pd.DataFrame):
        """Extract the ORB high/low from the most recent 9:30-9:35 ET candle."""
        d = self._data
        if df_5m is None or df_5m.empty:
            return

        orb_candle = self._get_orb_candle(df_5m)
        if orb_candle is None:
            logger.debug("ORB candle not found in 5m data yet")
            return

        d.orb_high  = float(orb_candle["high"])
        d.orb_low   = float(orb_candle["low"])
        d.orb_width = d.orb_high - d.orb_low

        if d.state == ORBState.WAITING:
            d.state = ORBState.RANGING

        logger.info(
            f"ORB range set: high={d.orb_high:.2f} "
            f"low={d.orb_low:.2f} "
            f"width={d.orb_width:.2f}"
        )

    def _get_orb_candle(self, df_5m: pd.DataFrame) -> Optional[pd.Series]:
        """Return the most recent 9:30 ET 5-min candle in the historical data."""
        try:
            idx = df_5m.index
            matches = []
            for i, ts in enumerate(idx):
                ts_et = ts if hasattr(ts, 'hour') else ts.astimezone(ET)
                if ts_et.hour == 9 and ts_et.minute == 30:
                    matches.append(i)
            if matches:
                most_recent_idx = matches[-1]
                return df_5m.iloc[most_recent_idx]

            today_et = now_et().date()
            same_day_candles = [
                (i, ts) for i, ts in enumerate(idx)
                if ts.date() == today_et
            ]
            if same_day_candles:
                first_idx = same_day_candles[0][0]
                return df_5m.iloc[first_idx]

            if len(idx) > 0:
                last_date = idx[-1].date()
                last_day_candles = [
                    (i, ts) for i, ts in enumerate(idx)
                    if ts.date() == last_date
                ]
                if last_day_candles:
                    first_idx = last_day_candles[0][0]
                    return df_5m.iloc[first_idx]
        except Exception as e:
            logger.debug(f"ORB candle lookup error: {e}")
        return None

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
            d.target_strike      = orb_strike_selection(
                d.orb_high, d.orb_low, "long", STRIKE_INCREMENT
            )
            d.attempt_number     += 1
            d.state               = ORBState.BREAK_HIGH_AWAITING_RETEST
            logger.info(
                f"ORB BREAK HIGH (attempt #{d.attempt_number}): close={close:.2f} "
                f"above ORB_HIGH={d.orb_high:.2f} "
                f"target={d.target_100pct:.2f} stop_body={d.stop_level:.2f} "
                f"strike={d.target_strike} — awaiting retest"
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
            d.target_strike      = orb_strike_selection(
                d.orb_high, d.orb_low, "short", STRIKE_INCREMENT
            )
            d.attempt_number     += 1
            d.state               = ORBState.BREAK_LOW_AWAITING_RETEST
            logger.info(
                f"ORB BREAK LOW (attempt #{d.attempt_number}): close={close:.2f} "
                f"below ORB_LOW={d.orb_low:.2f} "
                f"target={d.target_100pct:.2f} stop_body={d.stop_level:.2f} "
                f"strike={d.target_strike} — awaiting retest"
            )

    def _check_for_retest(self, df_1m: pd.DataFrame):
        d = self._data
        if df_1m is None or len(df_1m) < 2:
            return

        d.bars_since_break += 1

        if d.bars_since_break > ORB_MAX_RETEST_BARS:
            d.state = ORBState.INVALIDATED
            logger.info(
                f"ORB: retest timeout after {d.bars_since_break} bars — "
                f"INVALIDATED (no chase — re-arming for next attempt)"
            )
            return

        candle = df_1m.iloc[-2]
        close  = float(candle["close"])
        open_  = float(candle["open"])
        high   = float(candle["high"])
        low    = float(candle["low"])

        body_high = max(open_, close)
        body_low  = min(open_, close)

        if d.break_direction == "long":
            wick_into_range = low < d.orb_high
            body_outside    = body_low >= d.orb_high * 0.999
            invalidated     = close < d.orb_high

            if wick_into_range and body_outside:
                d.state        = ORBState.OPEN_LONG
                d.confirmed_at = str(now_et())
                logger.info(
                    f"ORB CONFIRMED LONG (attempt #{d.attempt_number}): "
                    f"retest wick to {low:.2f} "
                    f"body_low={body_low:.2f} above ORB_HIGH={d.orb_high:.2f}"
                )
            elif invalidated:
                d.state = ORBState.INVALIDATED
                logger.info(
                    f"ORB INVALIDATED (attempt #{d.attempt_number}): "
                    f"close={close:.2f} back inside ORB "
                    f"(orb_high={d.orb_high:.2f}) — re-arming for next attempt"
                )

        else:
            wick_into_range = high > d.orb_low
            body_outside    = body_high <= d.orb_low * 1.001
            invalidated     = close > d.orb_low

            if wick_into_range and body_outside:
                d.state        = ORBState.OPEN_SHORT
                d.confirmed_at = str(now_et())
                logger.info(
                    f"ORB CONFIRMED SHORT (attempt #{d.attempt_number}): "
                    f"retest wick to {high:.2f} "
                    f"body_high={body_high:.2f} below ORB_LOW={d.orb_low:.2f}"
                )
            elif invalidated:
                d.state = ORBState.INVALIDATED
                logger.info(
                    f"ORB INVALIDATED (attempt #{d.attempt_number}): "
                    f"close={close:.2f} back inside ORB "
                    f"(orb_low={d.orb_low:.2f}) — re-arming for next attempt"
                )

    def mark_triggered(self):
        self.notify_position_closed()

    @property
    def is_confirmed(self) -> bool:
        return self._data.state in (ORBState.OPEN_LONG, ORBState.OPEN_SHORT)

    @property
    def direction(self) -> str:
        d = self._data
        if d.state == ORBState.OPEN_LONG:
            return "long"
        if d.state == ORBState.OPEN_SHORT:
            return "short"
        return ""


_orb_engine: Optional[ORBEngine] = None


def get_orb_engine() -> ORBEngine:
    global _orb_engine
    if _orb_engine is None:
        _orb_engine = ORBEngine()
    return _orb_engine
