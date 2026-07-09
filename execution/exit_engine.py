"""
execution/exit_engine.py — Strategy-aware exit logic for all options positions.
v1.0 — original release
v1.1 — 2026-06-27 — strategy-aware exit routing:
        ORB:     stop on 1-min close back inside range, trail at 50% TP, no BOS
        Sweep:   BOS on 1-min structure, hard stop 25%
        Butterfly: time/premium exits only, no BOS, no trail
v1.2 — 2026-06-30 — ORB no longer hard-exits at 100% TP. Past 100%, the trail
        tightens to track the nearest unfilled 1-minute Fair Value Gap on the
        underlying (in the trade\'s favor), giving the position room to wick
        back and fill the gap without exiting on every dip, while still
        protecting the bulk of gains if the move actually reverses. FVG
        detection is scoped to 1m data only, matching ORB entry/exit logic
        which is always evaluated on the 1-minute timeframe.
v1.3 — 2026-07-06 — long-option THETA PROTECTION + generalized FVG trail:
        (a) theta-bleed exit — a profitable long is closed when projected time
            decay over THETA_LOOKAHEAD_MIN would erase the current gain (the
            clock, not price, is the threat). Uses live per-contract theta.
        (b) FVG-anchored trailing stop for ALL longs (ORB + Sweep), armed at
            +FVG_TRAIL_ARM_PCT: parks at the far edge of the nearest unfilled
            in-favor 1m FVG (room to continue), runs with the % trail (max wins).
v1.4 — 2026-07-07 — ADOPTED-position exit path: manages a position discovered
        open at the broker on a LIVE restart with no DB plan (see
        broker_reconcile) by the universal core of our rules — sign-correct
        max-loss stop (long/short), long-side profit trail, 15:45 hard close.
        No strategy-specific context required.
v1.5 — 2026-07-07 — theta-bleed REWORK (merged onto the v1.4 adopted-position
        path; supersedes the v1.3 theta logic that shipped inside v1.4). The
        v1.3 check fired on the first green tick: on 07-07 and 07-08, ~50-58 of
        ~77-87 exits were theta_bleed at a median ~60s hold, capping trends
        while the day's P&L came from the trades that reached the trail.
        _theta_bleed is now bounded by four gates: (1) a minimum gain floor,
        (2) a trail ceiling (once armed, the trail owns the trade — theta goes
        silent so trends run), (3) a MIN-HOLD blackout after entry, and (4) a
        corrected per-CALENDAR-day decay projection (v1.3/v1.4 divided by
        RTH_MINUTES=390, overstating projected decay ~3.7x). No call sites
        change; the adopted-position exit path (_evaluate_adopted) is untouched.
v1.6 — 2026-07-09 — ORB UNCONDITIONAL -25% HARD FLOOR (critical risk fix). The
        -25% dollar failsafe is universal by design and sweep/butterfly/adopted
        all enforce it directly (if current_premium <= stop_prem). ORB was the
        lone exception: it routed its floor through _update_trail, which returns
        None below the +50% trail activation — so any ORB trade that never armed
        the trail ran with NO dollar-loss stop and could bleed toward zero while
        the structure stop held (CRM 2026-07-09: -83%, underlying still above the
        range, trail never armed). _evaluate_orb now checks the floor DIRECTLY and
        UNCONDITIONALLY right after the hard-close check, mirroring the other
        three paths. _update_trail is unchanged (its de-arm is correct for the
        TRAIL; the floor no longer depends on it). Known failure mode — the
        adopted path's own comment already flagged _update_trail's de-arm.

Exit triggers by strategy:

  ORB
    1. HARD CLOSE: 15:45 ET
    2. RANGE VIOLATION: 1-min candle closes back inside ORB range
       (close < orb_high for longs, close > orb_low for shorts)
    3. TRAIL (below 100% TP): activates at 50% TP, trails at 75% of current premium
    4. TRAIL (at/past 100% TP): tightens to track the nearest unfilled 1m FVG
       in the trade\'s favor — no hard exit at target, position can keep running

  SWEEP REVERSAL
    1. HARD CLOSE: 15:45 ET
    2. HARD STOP: current premium <= 25% loss
    3. TARGET HIT: 100% TP
    4. BOS EXIT: 1-min break of structure against position
    5. TRAIL: activates at 50% TP

  BUTTERFLY
    1. HARD CLOSE: 15:45 ET
    2. MAX HOLD: 2.5 hours
    3. HARD STOP: net value <= 25% loss
    4. TARGET HIT: 25% of max profit

  ADOPTED (broker-discovered, no DB plan)
    1. HARD CLOSE: 15:45 ET
    2. MAX-LOSS STOP: sign-correct (long: premium <= stop; short: premium >= stop)
    3. LONG PROFIT TRAIL: standard trail to lock gains; short rides to hard close
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, List
from datetime import datetime

import pandas as pd

from tastytrade.order import (
    NewOrder, Leg, OrderAction, OrderType, OrderTimeInForce,
    PriceEffect, InstrumentType
)

from database.trade_logger import TradeRecord, get_trade_logger
from data.tasty_client import get_session, get_account, TastyClientError
from config import (
    PAPER_TRADING, CONTRACT_MULTIPLIER,
    BUTTERFLY_MAX_HOLD_MIN, TRAIL_LOCK_PCT, TRAIL_ACTIVATION_PCT, FVG_MIN_SIZE_PCT,
    THETA_LOOKAHEAD_MIN, RTH_MINUTES, FVG_TRAIL_ARM_PCT, FVG_TRAIL_LOCK_PCT
)
from utils.time_utils import is_hard_close_time, minutes_since, now_utc, fmt_et_short

logger = logging.getLogger(__name__)

# Past 100% TP, the trail locks in this fraction of current premium as a
# floor — tighter than the pre-target trail (75%), protecting gains beyond
# the original target. Used only when no usable 1m FVG is found.
POST_TARGET_TRAIL_LOCK_PCT = 0.85

# ─── Theta-bleed gates (v1.5) ─────────────────────────────────────────────────
# Bound the theta-bleed exit to its legitimate job — a small, stalled winner
# that has had time to develop and still won't reach the trail. Without these
# the check fires on the first green tick (see v1.5 header note).
THETA_MIN_HOLD_MIN       = 20      # blackout: no theta exit in the first N min after entry
THETA_MIN_GAIN_PCT       = 0.10    # gain floor: don't protect a gain smaller than this
MINUTES_PER_CALENDAR_DAY = 1440    # theta greek is $/share/CALENDAR day (not the 390 RTH min)


@dataclass
class ExitDecision:
    should_exit:        bool  = False
    exit_reason:        str   = ""
    new_trail_stop:     Optional[float] = None
    current_pnl_pct:    float = 0.0
    current_pnl_usd:    float = 0.0


@dataclass
class _SimpleFVG:
    """Minimal 1-minute FVG used only for the post-target ORB trail."""
    top:       float
    bottom:    float
    direction: str   # "bullish" or "bearish"
    index:     int


def _find_1m_fvgs(df_1m: pd.DataFrame) -> List["_SimpleFVG"]:
    """
    Detect Fair Value Gaps on the 1-minute timeframe only.
    Same 3-candle imbalance logic as structure_analyzer.py, scoped to 1m
    since ORB entry/exit conditions are always evaluated on 1m.
    Returns most-recent-first.
    """
    gaps: List[_SimpleFVG] = []
    if df_1m is None or len(df_1m) < 3:
        return gaps

    for i in range(2, len(df_1m)):
        # Bullish FVG: candle[i].low > candle[i-2].high
        gap_bot = float(df_1m["high"].iloc[i - 2])
        gap_top = float(df_1m["low"].iloc[i])
        if gap_top > gap_bot:
            size_pct = (gap_top - gap_bot) / gap_bot if gap_bot > 0 else 0
            if size_pct >= FVG_MIN_SIZE_PCT:
                gaps.append(_SimpleFVG(top=gap_top, bottom=gap_bot,
                                        direction="bullish", index=i))

        # Bearish FVG: candle[i].high < candle[i-2].low
        gap_top2 = float(df_1m["low"].iloc[i - 2])
        gap_bot2 = float(df_1m["high"].iloc[i])
        if gap_bot2 < gap_top2:
            size_pct = (gap_top2 - gap_bot2) / gap_top2 if gap_top2 > 0 else 0
            if size_pct >= FVG_MIN_SIZE_PCT:
                gaps.append(_SimpleFVG(top=gap_top2, bottom=gap_bot2,
                                        direction="bearish", index=i))

    return sorted(gaps, key=lambda g: g.index, reverse=True)


def _nearest_unfilled_fvg_in_favor(df_1m: pd.DataFrame, current_price: float,
                                    direction: str) -> Optional["_SimpleFVG"]:
    """
    Find the nearest unfilled 1m FVG below current price for a long
    (bullish gap, price hasn\'t traded back down through it) or above
    current price for a short (bearish gap, price hasn\'t traded back
    up through it). This is the gap the trail should give the trade
    room to wick back into without exiting.
    """
    gaps = _find_1m_fvgs(df_1m)
    if not gaps:
        return None

    candidates = []
    for g in gaps:
        if direction == "long" and g.direction == "bullish" and g.top < current_price:
            candidates.append(g)
        elif direction == "short" and g.direction == "bearish" and g.bottom > current_price:
            candidates.append(g)

    if not candidates:
        return None

    if direction == "long":
        return max(candidates, key=lambda g: g.top)
    else:
        return min(candidates, key=lambda g: g.bottom)


class BOSTracker:
    """
    Tracks 1-minute Break of Structure for sweep reversal trades.
    Long:  tracks highest closing high \u2192 protected HL = low of that candle
           BOS = 1m close below protected HL
    Short: tracks lowest closing low \u2192 protected LH = high of that candle
           BOS = 1m close above protected LH
    """
    def __init__(self, direction: str, entry_price: float):
        self.direction       = direction
        self.entry_price     = entry_price
        self.peak_close      = entry_price
        self.protected_level = None   # HL for longs, LH for shorts

    def update(self, df_1m: pd.DataFrame) -> bool:
        """
        Update structure tracking. Returns True if BOS triggered.
        Uses iloc[-2] \u2014 the last fully closed candle.
        """
        if df_1m is None or len(df_1m) < 3:
            return False

        candle = df_1m.iloc[-2]   # last closed candle
        close  = float(candle["close"])
        high   = float(candle["high"])
        low    = float(candle["low"])

        if self.direction == "long":
            if close > self.peak_close:
                self.peak_close      = close
                self.protected_level = low
                logger.debug(
                    f"BOS long: new HH close={close:.2f} "
                    f"protected_HL={self.protected_level:.2f}"
                )
            if self.protected_level and close < self.protected_level:
                logger.info(
                    f"BOS TRIGGERED (long): close={close:.2f} < "
                    f"protected_HL={self.protected_level:.2f}"
                )
                return True

        else:  # short
            if close < self.peak_close:
                self.peak_close      = close
                self.protected_level = high
                logger.debug(
                    f"BOS short: new LL close={close:.2f} "
                    f"protected_LH={self.protected_level:.2f}"
                )
            if self.protected_level and close > self.protected_level:
                logger.info(
                    f"BOS TRIGGERED (short): close={close:.2f} > "
                    f"protected_LH={self.protected_level:.2f}"
                )
                return True

        return False


class ExitEngine:
    """Evaluates every open options trade on each tick."""

    def __init__(self, paper_trading: bool = PAPER_TRADING):
        self.paper_trading  = paper_trading
        self._trail_stops:  dict = {}
        self._trail_active: dict = {}
        self._bos_trackers: dict = {}   # trade_id \u2192 BOSTracker (sweep only)
        self._post_target_trail: dict = {}   # trade_id \u2192 bool (ORB only)
        self._trade_logger  = get_trade_logger()

    def evaluate(self,
                 record: TradeRecord,
                 current_premium: float,
                 df_1m: Optional[pd.DataFrame] = None,
                 regime: Optional[str] = None) -> ExitDecision:
        """
        Strategy-aware exit evaluation.
        Routes to the appropriate exit logic based on strategy_name.
        regime: current regime string — used for regime-flip exit checks on
                neutral strategies (butterfly, condor) that depend on RANGING.
        """
        strategy = record.get("strategy", "")

        if record.get("is_butterfly"):
            return self._evaluate_butterfly(record, current_premium, regime=regime)
        elif strategy == "IronCondorStrategy":
            return self._evaluate_condor_leg(record, current_premium, regime=regime)
        elif strategy == "ADOPTED":
            return self._evaluate_adopted(record, current_premium)
        elif strategy == "ORBStrategy":
            return self._evaluate_orb(record, current_premium, df_1m)
        else:
            # SweepReversal and any other directional strategies
            return self._evaluate_sweep(record, current_premium, df_1m)

    # \u2500\u2500\u2500 ORB Exit \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def _evaluate_orb(self, record: TradeRecord,
                       current_premium: float,
                       df_1m: Optional[pd.DataFrame]) -> ExitDecision:
        """
        ORB exit logic:
        - Stop: 1-min candle closes back inside ORB range
        - TP:   100% of premium \u2014 past this, trail tightens to track the
                nearest unfilled 1m FVG instead of hard-exiting
        - Trail: activates at 50% TP, trails at 75% of current premium below
                 100%; 85% of current premium (tighter) above 100%
        - No BOS
        """
        decision   = ExitDecision()
        trade_id   = record["trade_id"]
        entry_prem = record["entry_premium"]
        target     = record["target_premium"]
        trail_act  = record["trail_activation"]
        direction  = record.get("direction", "long")

        # P&L
        pnl_pct = (current_premium - entry_prem) / entry_prem if entry_prem > 0 else 0
        pnl_usd = (current_premium - entry_prem) * record["contracts"] * CONTRACT_MULTIPLIER
        decision.current_pnl_pct = pnl_pct
        decision.current_pnl_usd = pnl_usd

        # 1. HARD CLOSE
        if is_hard_close_time():
            decision.should_exit = True
            decision.exit_reason = "hard_close_15:45_ET"
            return decision

        # 1b. HARD STOP — unconditional -25% dollar floor (v1.6). Mirrors the
        #     sweep/butterfly/adopted paths, which check the floor DIRECTLY. ORB
        #     previously relied on _update_trail for this, but that returns None
        #     below the +50% trail activation — so a trade that never armed the
        #     trail had NO floor and could bleed toward zero while the structure
        #     stop held (CRM 2026-07-09: -83%). The floor must fire every tick,
        #     regardless of trail state.
        stop_prem = record.get("stop_premium", 0.0) or (entry_prem * 0.75)
        if stop_prem > 0 and current_premium <= stop_prem:
            decision.should_exit = True
            decision.exit_reason = f"hard_stop_25pct pnl={pnl_pct:.1%}"
            logger.info(
                f"ORB HARD STOP: {trade_id[:8]} prem={current_premium:.2f} "
                f"<= floor={stop_prem:.2f} (pnl={pnl_pct:.1%})"
            )
            return decision

        # 2. RANGE VIOLATION \u2014 1-min candle closes back inside ORB range
        if df_1m is not None and len(df_1m) >= 2:
            orb_high = record.get("orb_range_high", 0.0)
            orb_low  = record.get("orb_range_low", 0.0)
            if orb_high > 0 and orb_low > 0:
                last_close = float(df_1m.iloc[-2]["close"])
                if direction == "long" and last_close < orb_high:
                    decision.should_exit = True
                    decision.exit_reason = (
                        f"orb_range_violation: 1m close {last_close:.2f} "
                        f"back inside range (below {orb_high:.2f})"
                    )
                    logger.info(
                        f"ORB STOP: {trade_id[:8]} 1m close={last_close:.2f} "
                        f"< orb_high={orb_high:.2f} \u2014 breakout failed"
                    )
                    return decision
                elif direction == "short" and last_close > orb_low:
                    decision.should_exit = True
                    decision.exit_reason = (
                        f"orb_range_violation: 1m close {last_close:.2f} "
                        f"back inside range (above {orb_low:.2f})"
                    )
                    logger.info(
                        f"ORB STOP: {trade_id[:8]} 1m close={last_close:.2f} "
                        f"> orb_low={orb_low:.2f} \u2014 breakout failed"
                    )
                    return decision

        # 2b. THETA BLEED \u2014 profitable but time is about to eat the gain
        if self._theta_bleed(record, current_premium, pnl_pct):
            decision.should_exit = True
            decision.exit_reason = f"theta_bleed pnl={pnl_pct:.1%}"
            return decision

        # 3. PAST 100% TP \u2014 switch to tightened FVG-aware trail, no hard exit
        if current_premium >= target:
            if not self._post_target_trail.get(trade_id, False):
                self._post_target_trail[trade_id] = True
                logger.info(
                    f"ORB TARGET REACHED (no hard exit): {trade_id[:8]} "
                    f"pnl={pnl_pct:.1%} \u2014 switching to tightened FVG trail"
                )

            trail_stop = self._update_post_target_trail(
                trade_id, current_premium, record, df_1m, direction
            )
            if trail_stop is not None:
                if current_premium <= trail_stop:
                    decision.should_exit = True
                    decision.exit_reason = f"orb_fvg_trail_stop pnl={pnl_pct:.1%}"
                    return decision
                decision.new_trail_stop = trail_stop
            return decision

        # 4. TRAIL \u2014 below 100% TP: FVG-anchored once armed (+20%), plus the
        #    50% % trail; the higher of the two governs.
        if pnl_pct >= FVG_TRAIL_ARM_PCT:
            self._update_fvg_trail(trade_id, current_premium, record, df_1m, direction)
        trail_stop = self._update_trail(
            trade_id, current_premium, entry_prem, trail_act,
            entry_prem * 0.75  # hard floor = 25% loss
        )
        trail_stop = self._trail_stops.get(trade_id, trail_stop)
        if trail_stop is not None:
            if current_premium <= trail_stop:
                decision.should_exit = True
                decision.exit_reason = f"orb_trail_stop pnl={pnl_pct:.1%}"
                return decision
            decision.new_trail_stop = trail_stop

        return decision

    def _update_post_target_trail(self, trade_id: str, current_premium: float,
                                   record: TradeRecord,
                                   df_1m: Optional[pd.DataFrame],
                                   direction: str) -> Optional[float]:
        """
        Past 100% TP: trail tightens to track the nearest unfilled 1m FVG
        in the trade\'s favor, converted to an equivalent premium floor.
        Falls back to a tightened percentage trail (85% of current premium)
        if no usable FVG is found in the 1m data.
        """
        underlying_entry  = record.get("underlying_entry", 0.0)
        underlying_target = record.get("underlying_target", 0.0)
        entry_prem        = record["entry_premium"]

        fvg_floor_premium = None

        if df_1m is not None and underlying_entry > 0 and underlying_target > 0:
            current_underlying_move = abs(underlying_target - underlying_entry)
            fvg = _nearest_unfilled_fvg_in_favor(
                df_1m,
                current_price=underlying_target,
                direction=direction
            )
            if fvg is not None and current_underlying_move > 0:
                premium_per_point = (current_premium - entry_prem) / current_underlying_move \
                                    if current_underlying_move > 0 else 0
                if direction == "long":
                    underlying_floor = fvg.top
                else:
                    underlying_floor = fvg.bottom

                underlying_distance_from_entry = abs(underlying_floor - underlying_entry)
                fvg_floor_premium = entry_prem + (underlying_distance_from_entry * premium_per_point)

        pct_trail = current_premium * POST_TARGET_TRAIL_LOCK_PCT

        if fvg_floor_premium is not None:
            new_trail = max(fvg_floor_premium, pct_trail)
        else:
            new_trail = pct_trail

        current_trail = self._trail_stops.get(trade_id, entry_prem)
        if new_trail > current_trail:
            self._trail_stops[trade_id] = new_trail
            logger.debug(
                f"ORB post-target trail updated: {trade_id[:8]} "
                f"trail=${self._trail_stops[trade_id]:.2f} "
                f"(fvg_based={fvg_floor_premium is not None})"
            )

        return self._trail_stops.get(trade_id)

    # ─── Long-option theta protection + general FVG trail ─────────────────────
    def _theta_bleed(self, record: TradeRecord, current_premium: float,
                     pnl_pct: float) -> bool:
        """True only when a long has EARNED a real, sub-trail gain that time
        decay is now projected to erase — AND has been given room to develop
        first. Four gates (see v1.5 header) bound what was previously a
        first-green-tick guillotine:
          (1) GAIN FLOOR    — skip a trivial winner (< THETA_MIN_GAIN_PCT).
          (2) TRAIL CEILING — once up >= FVG_TRAIL_ARM_PCT the trail owns the
                              trade; theta stays silent so trends run.
          (3) MIN HOLD      — a THETA_MIN_HOLD_MIN blackout after entry lets the
                              move develop before the clock can cut it.
          (4) DECAY vs GAIN — only then, if projected decay over the lookahead
                              erases the gain, exit. Theta is $/share/CALENDAR
                              day, so scale the lookahead by 1440 min/day.
        Active window: held >= THETA_MIN_HOLD_MIN AND gain in
        [THETA_MIN_GAIN_PCT, FVG_TRAIL_ARM_PCT) AND stalling to theta."""
        # (1) gain floor — a tiny green is not worth protecting
        if pnl_pct < THETA_MIN_GAIN_PCT:
            return False
        # (2) trail ceiling — a running trade belongs to the trail, not theta
        if pnl_pct >= FVG_TRAIL_ARM_PCT:
            return False
        # (3) min-hold blackout — give the move room before the clock can cut it
        entry_time = record.get("entry_time")
        if not entry_time:
            return False                       # can't verify hold ⇒ don't cut
        entry_dt = entry_time if isinstance(entry_time, datetime) else None
        if entry_dt is None:
            try:
                entry_dt = datetime.fromisoformat(str(entry_time))
            except ValueError:
                return False
        if minutes_since(entry_dt) < THETA_MIN_HOLD_MIN:
            return False
        # (4) projected decay vs current gain
        theta = abs(float(record.get("current_theta", 0.0) or 0.0))  # $/share/CAL day
        if theta <= 0:
            return False
        gain_per_share = current_premium - record["entry_premium"]
        if gain_per_share <= 0:
            return False
        proj_decay = theta * (THETA_LOOKAHEAD_MIN / MINUTES_PER_CALENDAR_DAY)
        return proj_decay >= gain_per_share

    def _update_fvg_trail(self, trade_id: str, current_premium: float,
                          record: TradeRecord, df_1m: Optional[pd.DataFrame],
                          direction: str) -> Optional[float]:
        """FVG-anchored trailing stop for a long, armed once profitable. The
        stop parks at the FAR edge of the nearest unfilled in-favor 1m FVG
        (converted to an equivalent premium floor) so the trade has room to pull
        back INTO the gap for continuation; only a move beyond the gap exits.
        Falls back to a % lock of current premium when no usable FVG exists.
        Writes to the shared _trail_stops (highest trail wins)."""
        entry_prem       = record["entry_premium"]
        underlying_entry = record.get("underlying_entry", 0.0)
        fvg_floor_premium = None

        if df_1m is not None and len(df_1m) > 0 and underlying_entry > 0:
            cur_under = float(df_1m["close"].iloc[-1])
            move_from_entry = abs(cur_under - underlying_entry)
            fvg = _nearest_unfilled_fvg_in_favor(df_1m, current_price=cur_under,
                                                 direction=direction)
            if fvg is not None and move_from_entry > 0:
                premium_per_point = (current_premium - entry_prem) / move_from_entry
                # FAR edge → room to wick INTO the gap before exiting
                underlying_floor = fvg.bottom if direction == "long" else fvg.top
                dist = abs(underlying_floor - underlying_entry)
                fvg_floor_premium = entry_prem + dist * premium_per_point

        pct_trail = current_premium * FVG_TRAIL_LOCK_PCT
        new_trail = max(fvg_floor_premium, pct_trail) if fvg_floor_premium is not None else pct_trail

        current_trail = self._trail_stops.get(trade_id, entry_prem)
        if new_trail > current_trail:
            self._trail_stops[trade_id] = new_trail
            logger.debug(
                f"FVG trail: {trade_id[:8]} trail=${self._trail_stops[trade_id]:.2f} "
                f"(fvg_based={fvg_floor_premium is not None})"
            )
        return self._trail_stops.get(trade_id)

    # ─── Sweep Reversal Exit ──────────────────────────────────────────────────

    def _evaluate_sweep(self, record: TradeRecord,
                         current_premium: float,
                         df_1m: Optional[pd.DataFrame]) -> ExitDecision:
        """
        Sweep reversal exit logic:
        - Hard stop: 25% premium loss
        - BOS: 1-min break of structure against position
        - TP: 100% of premium
        - Trail: activates at 50% TP
        """
        decision   = ExitDecision()
        trade_id   = record["trade_id"]
        entry_prem = record["entry_premium"]
        stop_prem  = record["stop_premium"]
        target     = record["target_premium"]
        trail_act  = record["trail_activation"]
        direction  = record.get("direction", "long")

        pnl_pct = (current_premium - entry_prem) / entry_prem if entry_prem > 0 else 0
        pnl_usd = (current_premium - entry_prem) * record["contracts"] * CONTRACT_MULTIPLIER
        decision.current_pnl_pct = pnl_pct
        decision.current_pnl_usd = pnl_usd

        # 1. HARD CLOSE
        if is_hard_close_time():
            decision.should_exit = True
            decision.exit_reason = "hard_close_15:45_ET"
            return decision

        # 2. HARD STOP
        if current_premium <= stop_prem:
            decision.should_exit = True
            decision.exit_reason = f"stop_hit pnl={pnl_pct:.1%}"
            return decision

        # 3. TARGET HIT
        if current_premium >= target:
            decision.should_exit = True
            decision.exit_reason = f"target_hit pnl={pnl_pct:.1%}"
            return decision

        # 4. BOS EXIT \u2014 only once premium is positive (don\'t BOS out of a
        #    healthy retest that hasn\'t moved yet)
        if df_1m is not None and pnl_pct > 0:
            tracker = self._get_bos_tracker(trade_id, direction, entry_prem)
            if tracker.update(df_1m):
                decision.should_exit = True
                decision.exit_reason = f"bos_exit pnl={pnl_pct:.1%}"
                return decision

        # 4b. THETA BLEED \u2014 profitable but time is about to eat the gain
        if self._theta_bleed(record, current_premium, pnl_pct):
            decision.should_exit = True
            decision.exit_reason = f"theta_bleed pnl={pnl_pct:.1%}"
            return decision

        # 5. TRAIL \u2014 FVG-anchored once armed (+20%), plus the 50% % trail; the
        #    higher of the two governs (both write to _trail_stops).
        if pnl_pct >= FVG_TRAIL_ARM_PCT:
            self._update_fvg_trail(trade_id, current_premium, record, df_1m, direction)
        trail_stop = self._update_trail(
            trade_id, current_premium, entry_prem, trail_act, stop_prem
        )
        trail_stop = self._trail_stops.get(trade_id, trail_stop)
        if trail_stop is not None:
            if current_premium <= trail_stop:
                decision.should_exit = True
                decision.exit_reason = f"trail_stop_hit pnl={pnl_pct:.1%}"
                return decision
            decision.new_trail_stop = trail_stop

        return decision

    # \u2500\u2500\u2500 Butterfly Exit \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def _evaluate_butterfly(self, record: TradeRecord,
                             current_premium: float,
                             regime: Optional[str] = None) -> ExitDecision:
        """
        Butterfly exit logic:
        - Regime flip: exit immediately if regime flips to TRENDING
        - Max hold: 2.5 hours
        - Hard stop: net value <= 25% loss
        - Target: 25% of max profit
        - No BOS, no trail
        """
        decision     = ExitDecision()
        trade_id     = record["trade_id"]
        entry_prem   = record["entry_premium"]
        stop_prem    = record["stop_premium"]
        target       = record["target_premium"]
        entry_time   = record["entry_time"]

        pnl_pct = (current_premium - entry_prem) / entry_prem if entry_prem > 0 else 0
        pnl_usd = (current_premium - entry_prem) * record["contracts"] * CONTRACT_MULTIPLIER
        decision.current_pnl_pct = pnl_pct
        decision.current_pnl_usd = pnl_usd

        # 1. HARD CLOSE
        if is_hard_close_time():
            decision.should_exit = True
            decision.exit_reason = "hard_close_15:45_ET"
            return decision

        # 2. REGIME FLIP EXIT — butterfly assumption (neutral/ranging) is broken
        # if the market transitions to a trending regime. Exit immediately rather
        # than waiting for the stop to get hit by the same directional move.
        TRENDING_REGIMES = {"TRENDING_BULL", "TRENDING_BEAR", "BREAKOUT_VOLATILE"}
        if regime and regime in TRENDING_REGIMES:
            decision.should_exit = True
            decision.exit_reason = f"regime_flip_exit: {regime} incompatible with butterfly"
            logger.info(
                f"BUTTERFLY REGIME EXIT: {trade_id[:8]} — "
                f"regime flipped to {regime}, exiting neutral position"
            )
            return decision

        # 3. MAX HOLD
        if entry_time:
            try:
                from datetime import timezone
                entry_dt = datetime.fromisoformat(entry_time)
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                mins_held = minutes_since(entry_dt)
                if mins_held >= BUTTERFLY_MAX_HOLD_MIN:
                    decision.should_exit = True
                    decision.exit_reason = f"butterfly_max_hold({mins_held:.0f}min)"
                    return decision
            except Exception:
                pass

        # 3. HARD STOP
        if current_premium <= stop_prem:
            decision.should_exit = True
            decision.exit_reason = f"stop_hit pnl={pnl_pct:.1%}"
            return decision

        # 4. TARGET HIT
        if current_premium >= target:
            decision.should_exit = True
            decision.exit_reason = f"target_hit pnl={pnl_pct:.1%}"
            return decision

        return decision

    # \u2500\u2500\u2500 Shared Helpers \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def _evaluate_condor_leg(self, record: TradeRecord,
                              current_premium: float,
                              regime: Optional[str] = None) -> ExitDecision:
        """
        Iron condor leg exit logic.
        Each leg is a credit spread — rising spread value = losing money.

        Regime-flip exits are DIRECTION-AWARE:
          - Call spread: only exit on TRENDING_BULL or BREAKOUT_VOLATILE
            (price moving toward short calls). TRENDING_BEAR is favorable — hold.
          - Put spread: only exit on TRENDING_BEAR or BREAKOUT_VOLATILE
            (price moving toward short puts). TRENDING_BULL is favorable — hold.
          - Leg 2 cancellation on favorable flips handled by check_leg_triggers().

        Exits: hard close, adverse regime flip, 25% stop, $0.05 nickel close.
        """
        from config import CONDOR_NICKEL_CLOSE, CONDOR_STOP_LOSS_PCT

        decision    = ExitDecision()
        trade_id    = record["trade_id"]
        entry_prem  = record["entry_premium"]
        option_side = record.get("option_side", "")

        pnl_pct = (entry_prem - current_premium) / entry_prem if entry_prem > 0 else 0
        pnl_usd = (entry_prem - current_premium) * record["contracts"] * CONTRACT_MULTIPLIER
        decision.current_pnl_pct = pnl_pct
        decision.current_pnl_usd = pnl_usd

        if is_hard_close_time():
            decision.should_exit = True
            decision.exit_reason = "hard_close_15:45_ET"
            return decision

        TRENDING_REGIMES = {"TRENDING_BULL", "TRENDING_BEAR", "BREAKOUT_VOLATILE"}
        if regime and regime in TRENDING_REGIMES:
            adverse = False
            if option_side == "call" and regime in ("TRENDING_BULL", "BREAKOUT_VOLATILE"):
                adverse = True
            elif option_side == "put" and regime in ("TRENDING_BEAR", "BREAKOUT_VOLATILE"):
                adverse = True

            if adverse:
                decision.should_exit = True
                decision.exit_reason = f"regime_flip_adverse: {regime} threatens {option_side} spread"
                logger.info(
                    f"CONDOR LEG ADVERSE EXIT: {trade_id[:8]} — "
                    f"{regime} moving into {option_side} short strikes"
                )
                return decision
            else:
                logger.info(
                    f"CONDOR LEG: {regime} flip FAVORABLE for {option_side} spread "
                    f"(pnl={pnl_pct:.1%}) — holding, Leg 2 will be cancelled by strategy"
                )

        stop_level = entry_prem * (1 + CONDOR_STOP_LOSS_PCT)
        if current_premium >= stop_level:
            decision.should_exit = True
            decision.exit_reason = f"condor_stop pnl={pnl_pct:.1%}"
            return decision

        if current_premium <= CONDOR_NICKEL_CLOSE:
            decision.should_exit = True
            decision.exit_reason = f"nickel_close pnl={pnl_pct:.1%}"
            return decision

        return decision

    def _evaluate_adopted(self, record: TradeRecord,
                          current_premium: float) -> ExitDecision:
        """
        Exit logic for an ADOPTED position — one discovered open at the broker on
        a LIVE restart with no DB plan (see broker_reconcile). The original setup
        is unknown, so it is managed by the universal core of our rules:
          - sign-correct max-loss stop (already on the record as stop_premium:
            long = entry*(1-ADOPTED_STOP_PCT); short = entry*(1+ADOPTED_STOP_PCT)),
          - long positions also trail to lock gains (standard trail helper),
          - the 15:45 hard close applies like everything else.
        A position already past its stop when adopted exits on the first tick; a
        healthy one rides. That is the "if red exit, if green manage" behaviour,
        via the normal exit path — no strategy-specific context required.

        A lone adopted SHORT (an anomaly per the account's margin reality) is
        held on a fixed protective stop + hard close only; no ratcheting trail.
        """
        decision   = ExitDecision()
        trade_id   = record["trade_id"]
        entry_prem = record.get("entry_premium", 0) or 0
        stop_prem  = record.get("stop_premium", 0) or 0
        contracts  = record.get("contracts", 0) or 0
        is_short   = bool(record.get("is_short_position", 0))

        # sign-correct P&L: a long gains as premium rises, a short as it falls
        if is_short:
            pnl_pct = (entry_prem - current_premium) / entry_prem if entry_prem > 0 else 0
            pnl_usd = (entry_prem - current_premium) * contracts * CONTRACT_MULTIPLIER
        else:
            pnl_pct = (current_premium - entry_prem) / entry_prem if entry_prem > 0 else 0
            pnl_usd = (current_premium - entry_prem) * contracts * CONTRACT_MULTIPLIER
        decision.current_pnl_pct = pnl_pct
        decision.current_pnl_usd = pnl_usd

        # 1. HARD CLOSE (also enforced by the 15:45 flatten — belt & suspenders)
        if is_hard_close_time():
            decision.should_exit = True
            decision.exit_reason = "hard_close_15:45_ET"
            return decision

        # 2. MAX-LOSS STOP (sign-correct)
        if is_short:
            if stop_prem > 0 and current_premium >= stop_prem:
                decision.should_exit = True
                decision.exit_reason = f"adopted_stop_short pnl={pnl_pct:.1%}"
            # anomalous short: fixed stop + hard close only, no ratcheting trail
            return decision

        if stop_prem > 0 and current_premium <= stop_prem:
            decision.should_exit = True
            decision.exit_reason = f"adopted_stop_long pnl={pnl_pct:.1%}"
            return decision

        # 3. LONG PROFIT TRAIL — once up TRAIL_ACTIVATION_PCT, arm a ratcheting
        #    stop that locks gains and PERSISTS: a pullback to the locked level
        #    exits (unlike the shared _update_trail, which de-arms below the
        #    activation threshold). Ratchets to TRAIL_LOCK_PCT below the high.
        if (not self._trail_active.get(trade_id, False)
                and pnl_pct >= TRAIL_ACTIVATION_PCT):
            self._trail_active[trade_id] = True
            self._trail_stops[trade_id] = entry_prem * (1 + TRAIL_LOCK_PCT)

        if self._trail_active.get(trade_id, False):
            ratchet = current_premium * (1 - TRAIL_LOCK_PCT)
            trail   = max(self._trail_stops.get(trade_id, stop_prem), ratchet)
            self._trail_stops[trade_id] = trail
            decision.new_trail_stop = trail
            if current_premium <= trail:
                decision.should_exit = True
                decision.exit_reason = f"adopted_trail pnl={pnl_pct:.1%}"

        return decision

    def _get_bos_tracker(self, trade_id: str,
                          direction: str,
                          entry_price: float) -> BOSTracker:
        if trade_id not in self._bos_trackers:
            self._bos_trackers[trade_id] = BOSTracker(direction, entry_price)
        return self._bos_trackers[trade_id]

    def _update_trail(self, trade_id: str,
                       current: float, entry: float,
                       trail_activation: float,
                       hard_stop: float) -> Optional[float]:
        if current < trail_activation:
            return None

        if not self._trail_active.get(trade_id, False):
            self._trail_active[trade_id] = True
            initial_trail = entry * (1 + TRAIL_LOCK_PCT)
            self._trail_stops[trade_id] = initial_trail
            logger.info(
                f"TRAIL ACTIVATED: {trade_id[:8]} "
                f"initial_trail=${initial_trail:.2f}"
            )

        current_trail = self._trail_stops.get(trade_id, hard_stop)
        new_trail     = current * 0.75
        if new_trail > current_trail:
            self._trail_stops[trade_id] = new_trail

        return self._trail_stops[trade_id]

    def place_exit_order(self, record: TradeRecord, reason: str) -> bool:
        """Place closing order. Paper mode simulates. Live mode uses SDK."""
        mode         = "PAPER" if self.paper_trading else "LIVE"
        trade_id     = record["trade_id"]
        contracts    = record["contracts"]
        is_butterfly = bool(record.get("is_butterfly", False))

        logger.info(
            f"[{mode}] CLOSING {trade_id[:8]}: {reason} "
            f"contracts={contracts}"
        )

        if self.paper_trading:
            logger.info(f"[PAPER] Simulated close: {trade_id[:8]}")
            return True

        try:
            session = get_session()
            account = get_account()

            if is_butterfly:
                return self._close_butterfly(session, account, record, contracts)
            else:
                return self._close_single_leg(session, account, record, contracts)

        except Exception as e:
            logger.error(f"Exit order failed for {trade_id[:8]}: {e}")
            return False

    def _close_single_leg(self, session, account, record, contracts) -> bool:
        symbol = record.get("option_symbol", "")
        if not symbol:
            logger.error("Cannot close: no option_symbol in record")
            return False

        leg = Leg(
            instrument_type = InstrumentType.EQUITY_OPTION,
            symbol          = symbol,
            action          = OrderAction.SELL_TO_CLOSE,
            quantity        = contracts,
        )
        order = NewOrder(
            time_in_force = OrderTimeInForce.DAY,
            order_type    = OrderType.MARKET,
            legs          = [leg],
        )
        response = account.place_order(session, order, dry_run=False)
        if response.errors:
            logger.error(f"Close order errors: {response.errors}")
            return False
        return True

    def _close_butterfly(self, session, account, record, contracts) -> bool:
        lower_sym  = record.get("lower_symbol", "")
        center_sym = record.get("center_symbol", "")
        upper_sym  = record.get("upper_symbol", "")

        if not all([lower_sym, center_sym, upper_sym]):
            logger.error("Cannot close butterfly: missing leg symbols")
            return False

        legs = [
            Leg(instrument_type=InstrumentType.EQUITY_OPTION,
                symbol=lower_sym,  action=OrderAction.SELL_TO_CLOSE, quantity=contracts),
            Leg(instrument_type=InstrumentType.EQUITY_OPTION,
                symbol=center_sym, action=OrderAction.BUY_TO_CLOSE,  quantity=contracts * 2),
            Leg(instrument_type=InstrumentType.EQUITY_OPTION,
                symbol=upper_sym,  action=OrderAction.SELL_TO_CLOSE, quantity=contracts),
        ]
        order = NewOrder(
            time_in_force = OrderTimeInForce.DAY,
            order_type    = OrderType.MARKET,
            legs          = legs,
        )
        response = account.place_order(session, order, dry_run=False)
        if response.errors:
            logger.error(f"Butterfly close errors: {response.errors}")
            return False
        return True

    def clear_trail(self, trade_id: str):
        self._trail_stops.pop(trade_id, None)
        self._trail_active.pop(trade_id, None)
        self._bos_trackers.pop(trade_id, None)
        self._post_target_trail.pop(trade_id, None)


# Singleton
_exit_engine: Optional[ExitEngine] = None


def get_exit_engine(paper_trading: bool = PAPER_TRADING) -> ExitEngine:
    global _exit_engine
    if _exit_engine is None:
        _exit_engine = ExitEngine(paper_trading)
    return _exit_engine
