"""
analysis/orb_engine.py — Opening Range Breakout state machine.
v1.0 — original release
v1.1 — 2026-06-30 — full state model rewrite
v1.2 — 2026-06-30 — fix cutoff check running before range-setting
v1.3 — 2026-07-01 — ORB range now read from orb_range.json (written by
        analysis/get_orb_range.py). Single source of truth — no yfinance
        calls inside the engine, no log parsing, no circular logic.
v1.4 — 2026-07-02 — fix _range_date comparison: now stored as string from
        JSON date field so today check works correctly and engine stops
        reloading orb_range.json every tick after range is set.
v1.7 — 2026-07-02 — regime-gated re-arm: after a (b) close-inside invalidation,
        re-arm and watch for another break ONLY while the regime is still
        ORB-friendly (RANGING/COMPRESSION). Do NOT re-arm after an (a) runaway
        (hand off to sweep) or once the regime has shifted to sweep/trend/
        breakout. Tracks invalidation_reason to distinguish the two.
v1.6 — 2026-07-02 — 11:00 ET HARD cutoff (expire even awaiting-retest, so the
        bot moves to other regimes after 11:00) + two explicit invalidation
        rules: (a) price runs to the 50% TP with no retest (runaway breakout,
        favors sweep reversal); (b) a 1m candle closes back inside the ORB
        range. Replaces the 2PM/exempt-retest behavior.
v1.5 — 2026-07-02 — honor the orb_range.json "status" field. Only an
        ESTABLISHED range dated today is loaded and armed (WAITING->RANGING).
        EXPIRED (last RTH) and IN_PROGRESS (opening candle still forming)
        ranges are ignored for trading, so the engine can never break out on
        a carried-over prior-day range.
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
    ORB_BREAK_BUFFER, ORB_MAX_RETEST_BARS, STRIKE_INCREMENT, INSTRUMENT,
    NO_ENTRY_AFTER_ET
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
    invalidation_reason: str  = ""   # 'runaway' | 'close_inside' | 'timeout'


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
        """Load the ORB range from orb_range.json — single source of truth.

        Only an ESTABLISHED range dated today is armed for trading. EXPIRED
        (last RTH) and IN_PROGRESS (opening candle forming) states are ignored
        so the engine never breaks out on a carried-over prior-day range.
        """
        d = self._data
        try:
            with open(ORB_RANGE_FILE) as f:
                data = json.load(f)
            status = str(data.get("status", "")).upper()
            date   = data.get("date")
            today  = now_et().strftime("%Y-%m-%d")

            if status != "ESTABLISHED" or date != today:
                logger.debug(
                    f"ORB range not established for today "
                    f"(status={status or 'NONE'} date={date}) — engine waits"
                )
                return

            high  = float(data["high"])
            low   = float(data["low"])
            width = float(data["width"])
            if high > 0 and low > 0:
                d.orb_high  = high
                d.orb_low   = low
                d.orb_width = width
                self._range_date = today
                if d.state == ORBState.WAITING:
                    d.state = ORBState.RANGING
                logger.info(
                    f"ORB range ESTABLISHED: high={high:.2f} low={low:.2f} "
                    f"width={width:.2f} date={date}"
                )
        except Exception as e:
            logger.debug(f"ORB range file not ready: {e}")

    def update(self, df_5m: pd.DataFrame, df_1m: pd.DataFrame,
               current_price: float, regime: Optional[str] = None) -> ORBData:
        d = self._data

        if d.state in (ORBState.OPEN_LONG, ORBState.OPEN_SHORT):
            return d

        # Load range from file if not yet set for today
        today = now_et().strftime("%Y-%m-%d")
        if self._range_date != today or d.orb_high == 0.0:
            self._load_range_from_file()

        now = now_et()
        past_orb_cutoff = (now.hour, now.minute) >= NO_ENTRY_AFTER_ET
        d.entries_expired = past_orb_cutoff

        # 11:00 ET HARD cutoff — the ORB window is over. Expire regardless of
        # state (including awaiting-retest), so the bot moves on to other
        # regimes (sweep reversal, condor, butterfly). A confirmed OPEN position
        # returned early above and is untouched (it exits on its own rules).
        if past_orb_cutoff:
            if d.state != ORBState.EXPIRED:
                d.state = ORBState.EXPIRED
                logger.info(
                    f"ORB: past 11:00 ET cutoff — EXPIRED "
                    f"(range: {d.orb_low:.2f}-{d.orb_high:.2f})"
                )
            return d

        if d.state == ORBState.RANGING:
            self._check_for_break(df_1m)

        if d.state in (ORBState.BREAK_HIGH_AWAITING_RETEST, ORBState.BREAK_LOW_AWAITING_RETEST):
            self._check_for_retest(df_1m)

        if d.state == ORBState.INVALIDATED:
            # Re-arm ONLY after a (b) close-inside invalidation AND while the
            # regime is still ORB-friendly. After an (a) runaway, or once the
            # regime has shifted to sweep/trend/breakout, stand down so the bot
            # works the other regime's setup instead. Re-checked each tick, so
            # ORB can re-arm later if the regime returns to friendly before 11:00.
            ORB_FRIENDLY = ("RANGING", "COMPRESSION", "UNKNOWN")
            orb_friendly = (regime is None) or (regime in ORB_FRIENDLY)
            if d.invalidation_reason == "close_inside" and orb_friendly:
                self._rearm()
            else:
                logger.debug(
                    f"ORB dormant after '{d.invalidation_reason}' invalidation "
                    f"(regime={regime}) — deferring to other strategies"
                )

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
            d.invalidation_reason = "timeout"
            logger.info(f"ORB: retest timeout — INVALIDATED")
            return

        candle    = df_1m.iloc[-2]
        close     = float(candle["close"])
        open_     = float(candle["open"])
        high      = float(candle["high"])
        low       = float(candle["low"])
        body_high = max(open_, close)
        body_low  = min(open_, close)

        if d.break_direction == "long":
            # (a) Runaway breakout — ran to the 50% TP with no retest → invalidate.
            # This is the setup that most favors a sweep reversal instead.
            if high >= d.target_50pct:
                d.state = ORBState.INVALIDATED
                d.invalidation_reason = "runaway"
                logger.info(
                    f"ORB INVALIDATED: ran to 50% TP ({d.target_50pct:.2f}) "
                    f"without retest — runaway breakout (favors sweep reversal)"
                )
                return
            if low < d.orb_high and body_low >= d.orb_high * 0.999:
                d.state        = ORBState.OPEN_LONG
                d.confirmed_at = str(now_et())
                logger.info(f"ORB CONFIRMED LONG (attempt #{d.attempt_number}): wick={low:.2f} body_low={body_low:.2f}")
            # (b) Retrace into range — 1m candle closes back inside the ORB range.
            elif close < d.orb_high:
                d.state = ORBState.INVALIDATED
                d.invalidation_reason = "close_inside"
                logger.info(f"ORB INVALIDATED: 1m close={close:.2f} back inside range")
        else:
            # (a) Runaway breakout (short) — ran to the 50% TP with no retest.
            if low <= d.target_50pct:
                d.state = ORBState.INVALIDATED
                d.invalidation_reason = "runaway"
                logger.info(
                    f"ORB INVALIDATED: ran to 50% TP ({d.target_50pct:.2f}) "
                    f"without retest — runaway breakout (favors sweep reversal)"
                )
                return
            if high > d.orb_low and body_high <= d.orb_low * 1.001:
                d.state        = ORBState.OPEN_SHORT
                d.confirmed_at = str(now_et())
                logger.info(f"ORB CONFIRMED SHORT (attempt #{d.attempt_number}): wick={high:.2f} body_high={body_high:.2f}")
            # (b) Retrace into range — 1m candle closes back inside the ORB range.
            elif close > d.orb_low:
                d.state = ORBState.INVALIDATED
                d.invalidation_reason = "close_inside"
                logger.info(f"ORB INVALIDATED: 1m close={close:.2f} back inside range")

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
