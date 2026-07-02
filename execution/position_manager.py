"""
execution/position_manager.py — Manages the single open options position.
v1.0 — original release
v1.1 — 2026-06-27 — pass df_1m to exit_engine.evaluate() for strategy-aware
        ORB range violation and BOS exits
v1.2 — 2026-06-29 — use live chain marks in paper mode for accurate P&L display;
        butterfly mark computed from lower + upper - 2×center legs
v1.3 — 2026-06-30 — notify ORB engine when an ORB trade closes so it re-arms
        and watches for the next breakout attempt this session
v1.4 — 2026-06-30 — pass current regime to exit_engine.evaluate() so regime-flip
        exits can fire for butterfly and condor leg positions
v1.5 — 2026-07-02 — multi-position support for legged condors: hold up to two
        verticals at once (condor ONLY; every other strategy stays single),
        manage each independently, mark a leg as short_mark - long_mark, and
        invert P&L sign for credit spreads.
v1.6 — 2026-07-02 — add remove_record() for the broken-wing roll (drops the old
        untested vertical when it is rolled).
"""

import logging
from typing import Optional, List

import pandas as pd

from database.trade_logger import TradeRecord, get_trade_logger
from execution.exit_engine import get_exit_engine, ExitDecision
from data.tasty_client import get_client, TastyClientError
from risk.risk_manager import get_risk_manager
from notifications.alert_manager import get_alert_manager
from config import PAPER_TRADING, PAPER_FILL_SLIPPAGE_PCT, CONTRACT_MULTIPLIER

logger = logging.getLogger(__name__)


class PositionManager:
    """
    Manages the bot's single open position (one trade at a time).
    Fetches live option premium, evaluates exits, and closes when triggered.
    """

    def __init__(self, paper_trading: bool = PAPER_TRADING):
        self.paper_trading = paper_trading
        self._open_records: List[TradeRecord] = []
        self._trade_logger = get_trade_logger()

    def has_open_position(self) -> bool:
        if self._open_records:
            return True
        # Fresh process (restart): reload any open trades from the DB.
        trades = self._trade_logger.get_open_trades()
        if trades:
            self._open_records = trades
            return True
        return False

    def set_open_position(self, record: TradeRecord):
        """Single-position strategies (ORB, sweep, butterfly): exactly one."""
        self._open_records = [record]

    def add_condor_leg(self, record: TradeRecord):
        """The condor is the ONLY strategy allowed a second concurrent position
        (its two verticals). Appends rather than replacing."""
        self._open_records.append(record)

    def get_open_record(self) -> Optional[TradeRecord]:
        return self._open_records[0] if self._open_records else None

    def get_open_records(self) -> List[TradeRecord]:
        return list(self._open_records)

    def remove_record(self, trade_id: str):
        """Drop a record from active management (used by the broken-wing roll
        when it closes the old untested vertical)."""
        self._open_records = [r for r in self._open_records
                              if r.get("trade_id") != trade_id]

    def manage_open_position(self,
                              df_1m: Optional[pd.DataFrame] = None,
                              chain=None,
                              regime: Optional[str] = None) -> bool:
        """Manage every open position this tick. Normally one; for a legged
        condor there can be two verticals open at once, each managed
        independently (a tested side exits on its own; the untested side
        stays)."""
        if not self._open_records:
            self._open_records = self._trade_logger.get_open_trades()
            if not self._open_records:
                return False

        still_open: List[TradeRecord] = []
        for record in list(self._open_records):
            if self._manage_one(record, df_1m, chain, regime):
                still_open.append(record)
        self._open_records = still_open
        return len(self._open_records) > 0

    def _manage_one(self, record: TradeRecord,
                    df_1m: Optional[pd.DataFrame],
                    chain, regime: Optional[str]) -> bool:
        """Manage one record. Returns True if it should remain open."""
        trade_id = record["trade_id"]

        current_premium = self._fetch_current_premium(record, chain)
        if current_premium is None:
            logger.warning(
                f"Could not fetch premium for {trade_id[:8]} — skipping tick"
            )
            return True

        self._trade_logger.update_current_premium(trade_id, current_premium)

        exit_eng = get_exit_engine(self.paper_trading)
        decision = exit_eng.evaluate(record, current_premium, df_1m=df_1m, regime=regime)

        if decision.new_trail_stop is not None:
            self._trade_logger.update_stop(trade_id, decision.new_trail_stop)
            record["stop_premium"] = decision.new_trail_stop

        if decision.should_exit:
            closed = self._execute_exit(record, decision, current_premium)
            return not closed   # drop if closed; keep (retry) if the order failed

        logger.debug(
            f"Position [{trade_id[:8]}]: "
            f"premium=${current_premium:.2f} "
            f"pnl={decision.current_pnl_pct:.1%} "
            f"(${decision.current_pnl_usd:+.2f})"
        )
        return True

    def _fetch_current_premium(self, record: TradeRecord,
                                chain=None) -> Optional[float]:
        """
        Fetch current mark price for the option(s).
        Uses chain if available — even in paper mode for accurate P&L display.
        Butterfly mark = lower + upper - 2×center.
        Falls back to entry premium in paper mode if chain unavailable.
        """
        is_butterfly = bool(record.get("is_butterfly", False))

        if chain is not None:
            try:
                side           = record.get("option_side", "call")
                contracts_list = chain.calls if side == "call" else chain.puts

                if record.get("is_condor_leg") or record.get("strategy") == "IronCondorStrategy":
                    short_s = record.get("short_strike", 0)
                    long_s  = record.get("long_strike",  0)
                    short_m = next((c.mark for c in contracts_list if c.strike == short_s and c.mark > 0), None)
                    long_m  = next((c.mark for c in contracts_list if c.strike == long_s  and c.mark > 0), None)
                    if short_m is not None and long_m is not None:
                        return short_m - long_m   # current spread value (credit basis)
                elif is_butterfly:
                    lower_s  = record.get("lower_strike",  0)
                    center_s = record.get("center_strike", 0)
                    upper_s  = record.get("upper_strike",  0)
                    lower_m  = next((c.mark for c in contracts_list if c.strike == lower_s  and c.mark > 0), None)
                    center_m = next((c.mark for c in contracts_list if c.strike == center_s and c.mark > 0), None)
                    upper_m  = next((c.mark for c in contracts_list if c.strike == upper_s  and c.mark > 0), None)
                    if None not in (lower_m, center_m, upper_m):
                        return lower_m + upper_m - 2 * center_m
                else:
                    strike = record.get("strike", 0)
                    match  = next(
                        (c for c in contracts_list if c.strike == strike and c.mark > 0),
                        None
                    )
                    if match:
                        return match.mark
            except Exception:
                pass

        if self.paper_trading:
            return record.get("entry_premium", 0.0)

        client = get_client()
        try:
            if is_butterfly:
                lower_sym  = record.get("lower_symbol",  "")
                center_sym = record.get("center_symbol", "")
                upper_sym  = record.get("upper_symbol",  "")

                lower_mark  = self._get_option_mark(client, lower_sym)
                center_mark = self._get_option_mark(client, center_sym)
                upper_mark  = self._get_option_mark(client, upper_sym)

                if None in (lower_mark, center_mark, upper_mark):
                    return None
                return lower_mark + upper_mark - 2 * center_mark
            else:
                symbol = record.get("option_symbol", "")
                return self._get_option_mark(client, symbol)

        except Exception as e:
            logger.error(f"Premium fetch error: {e}")
            return None

    def _get_option_mark(self, client, symbol: str) -> Optional[float]:
        if not symbol:
            return None
        try:
            data  = client.get(f"/market-data/quotes/{symbol}")
            quote = data.get("data", {})
            bid   = float(quote.get("bid", 0) or 0)
            ask   = float(quote.get("ask", 0) or 0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            return float(quote.get("mark", 0) or quote.get("last", 0) or 0) or None
        except Exception:
            return None

    def _execute_exit(self, record: TradeRecord,
                       decision: ExitDecision,
                       current_premium: float) -> bool:
        """Place the exit, log/alert, record win/loss. Returns True if the
        position closed, False if the order failed (caller retries)."""
        trade_id = record["trade_id"]

        exit_eng = get_exit_engine(self.paper_trading)
        success  = exit_eng.place_exit_order(record, decision.exit_reason)

        if not success:
            logger.error(f"Exit order failed for {trade_id[:8]} — will retry next tick")
            return False

        entry_prem    = record["entry_premium"]
        contracts     = record["contracts"]
        # Credit spreads (condor legs) profit when the spread value FALLS, so the
        # P&L sign is inverted vs a debit trade.
        is_condor_leg = (bool(record.get("is_condor_leg"))
                         or record.get("strategy") == "IronCondorStrategy")
        if is_condor_leg:
            pnl_per_share = entry_prem - current_premium
        else:
            pnl_per_share = current_premium - entry_prem
        pnl_usd       = pnl_per_share * contracts * CONTRACT_MULTIPLIER

        self._trade_logger.log_exit(
            trade_id    = trade_id,
            exit_price  = current_premium,
            pnl_usd     = pnl_usd,
            exit_reason = decision.exit_reason,
        )

        risk_mgr = get_risk_manager()
        if pnl_usd >= 0:
            risk_mgr.record_win()
        else:
            risk_mgr.record_loss()

        get_alert_manager().send_exit_alert(
            trade_id      = trade_id,
            setup_type    = record.get("setup_type", ""),
            exit_premium  = current_premium,
            entry_premium = entry_prem,
            pnl_usd       = pnl_usd,
            contracts     = contracts,
            reason        = decision.exit_reason,
        )

        exit_eng.clear_trail(trade_id)

        # ── Re-arm ORB engine if this was an ORB trade ─────────────────────────
        # Allows the engine to watch for another breakout attempt this session
        # rather than treating one trade as the end of the ORB opportunity.
        if "ORB" in record.get("strategy", ""):
            try:
                from analysis.orb_engine import get_orb_engine
                get_orb_engine().notify_position_closed()
            except Exception as e:
                logger.warning(f"Could not re-arm ORB engine: {e}")

        logger.info(
            f"✅ Position closed: {trade_id[:8]} "
            f"exit=${current_premium:.2f} "
            f"pnl=${pnl_usd:+.2f} "
            f"reason={decision.exit_reason}"
        )
        return True


# Singleton
_position_manager: Optional[PositionManager] = None


def get_position_manager(paper_trading: bool = PAPER_TRADING) -> PositionManager:
    global _position_manager
    if _position_manager is None:
        _position_manager = PositionManager(paper_trading)
    return _position_manager
