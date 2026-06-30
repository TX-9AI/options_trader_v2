"""
execution/exit_engine.py — Strategy-aware exit logic for all options positions.
v1.0 — original release
v1.1 — 2026-06-27 — strategy-aware exit routing:
        ORB:     stop on 1-min close back inside range, trail at 50% TP, no BOS
        Sweep:   BOS on 1-min structure, hard stop 25%
        Butterfly: time/premium exits only, no BOS, no trail

Exit triggers by strategy:

  ORB
    1. HARD CLOSE: 15:45 ET
    2. RANGE VIOLATION: 1-min candle closes back inside ORB range
       (close < orb_high for longs, close > orb_low for shorts)
    3. TARGET HIT: 100% TP
    4. TRAIL: activates at 50% TP, trails at 75% of current premium

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
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
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
    BUTTERFLY_MAX_HOLD_MIN, TRAIL_LOCK_PCT
)
from utils.time_utils import is_hard_close_time, minutes_since, now_utc, fmt_et_short

logger = logging.getLogger(__name__)


@dataclass
class ExitDecision:
    should_exit:        bool  = False
    exit_reason:        str   = ""
    new_trail_stop:     Optional[float] = None
    current_pnl_pct:    float = 0.0
    current_pnl_usd:    float = 0.0


class BOSTracker:
    """
    Tracks 1-minute Break of Structure for sweep reversal trades.
    Long:  tracks highest closing high → protected HL = low of that candle
           BOS = 1m close below protected HL
    Short: tracks lowest closing low → protected LH = high of that candle
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
        Uses iloc[-2] — the last fully closed candle.
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
        self._bos_trackers: dict = {}   # trade_id → BOSTracker (sweep only)
        self._trade_logger  = get_trade_logger()

    def evaluate(self,
                 record: TradeRecord,
                 current_premium: float,
                 df_1m: Optional[pd.DataFrame] = None) -> ExitDecision:
        """
        Strategy-aware exit evaluation.
        Routes to the appropriate exit logic based on strategy_name.
        """
        strategy = record.get("strategy", "")

        if record.get("is_butterfly"):
            return self._evaluate_butterfly(record, current_premium)
        elif strategy == "ORBStrategy":
            return self._evaluate_orb(record, current_premium, df_1m)
        else:
            # SweepReversal and any other directional strategies
            return self._evaluate_sweep(record, current_premium, df_1m)

    # ─── ORB Exit ─────────────────────────────────────────────────────────────

    def _evaluate_orb(self, record: TradeRecord,
                       current_premium: float,
                       df_1m: Optional[pd.DataFrame]) -> ExitDecision:
        """
        ORB exit logic:
        - Stop: 1-min candle closes back inside ORB range
        - TP:   100% of premium
        - Trail: activates at 50% TP
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

        # 2. RANGE VIOLATION — 1-min candle closes back inside ORB range
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
                        f"< orb_high={orb_high:.2f} — breakout failed"
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
                        f"> orb_low={orb_low:.2f} — breakout failed"
                    )
                    return decision

        # 3. TARGET HIT
        if current_premium >= target:
            decision.should_exit = True
            decision.exit_reason = f"target_hit pnl={pnl_pct:.1%}"
            return decision

        # 4. TRAIL — activates at 50% TP, locks gains
        trail_stop = self._update_trail(
            trade_id, current_premium, entry_prem, trail_act,
            entry_prem * 0.75  # hard floor = 25% loss
        )
        if trail_stop is not None:
            if current_premium <= trail_stop:
                decision.should_exit = True
                decision.exit_reason = f"orb_trail_stop pnl={pnl_pct:.1%}"
                return decision
            decision.new_trail_stop = trail_stop

        return decision

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

        # 4. BOS EXIT — only once premium is positive (don't BOS out of a
        #    healthy retest that hasn't moved yet)
        if df_1m is not None and pnl_pct > 0:
            tracker = self._get_bos_tracker(trade_id, direction, entry_prem)
            if tracker.update(df_1m):
                decision.should_exit = True
                decision.exit_reason = f"bos_exit pnl={pnl_pct:.1%}"
                return decision

        # 5. TRAIL
        trail_stop = self._update_trail(
            trade_id, current_premium, entry_prem, trail_act, stop_prem
        )
        if trail_stop is not None:
            if current_premium <= trail_stop:
                decision.should_exit = True
                decision.exit_reason = f"trail_stop_hit pnl={pnl_pct:.1%}"
                return decision
            decision.new_trail_stop = trail_stop

        return decision

    # ─── Butterfly Exit ───────────────────────────────────────────────────────

    def _evaluate_butterfly(self, record: TradeRecord,
                             current_premium: float) -> ExitDecision:
        """
        Butterfly exit logic:
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

        # 2. MAX HOLD
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

    # ─── Shared Helpers ───────────────────────────────────────────────────────

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


# Singleton
_exit_engine: Optional[ExitEngine] = None


def get_exit_engine(paper_trading: bool = PAPER_TRADING) -> ExitEngine:
    global _exit_engine
    if _exit_engine is None:
        _exit_engine = ExitEngine(paper_trading)
    return _exit_engine
