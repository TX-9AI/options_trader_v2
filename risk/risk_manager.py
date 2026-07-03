"""
risk/risk_manager.py — Position sizing and session circuit breaker.
v1.0 — original release
v1.1 — 2026-06-27 — remove TRADE_GRADE_C and Twilio references,
        clean up Grade C sizing logic
v1.4 — 2026-07-02 — (a) regime reassessment after EVERY losing trade (not just
        at a count threshold). (b) NET daily loss halt: track session net P&L
        (seeded from today's closed trades so it survives restarts) and halt
        NEW entries when day P&L <= -DAILY_LOSS_LIMIT_USD. Wins offset losses —
        a green day keeps trading. Open positions still exit normally.
v1.3 — 2026-07-02 — add compute_condor_leg_size(): sizes ONE condor vertical
        at HALF the grade budget (each side gets half), against the spread
        max-loss = (width - credit) x 100. Enables two independent verticals.
v1.2 — 2026-07-02 — session loss limit no longer halts the session. Hitting
        SESSION_LOSS_LIMIT now REQUESTS a regime reassessment (consumed by the
        main loop) instead of stopping the service. Rationale: a 2-loss count
        breaker was too blunt — it would kill sessions that are still net
        profitable. Removed _fire_circuit_breaker()/systemctl-stop and the
        session_halted semantics.

Sizing model:
  - Fixed dollar risk per trade (operator-set at startup)
  - Contracts = floor(risk_usd × grade_multiplier / cost_per_contract)
  - cost_per_contract = mark × 100 (single leg) or net_debit × 100 (butterfly)
  - Always whole contracts; minimum 1 if affordable

Session loss limit (NOT a halt):
  - Reaching SESSION_LOSS_LIMIT losses in an RTH session sets a one-shot
    reassessment request. main_loop consumes it and forces a fresh regime
    classification. Trading continues; the bot re-reads the market.
  - NOTE (live): this intentionally removes the hard stop. For live capital a
    separate $-based session backstop is advisable — not implemented here.
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

from config import (
    RISK_PER_TRADE_USD, SESSION_LOSS_LIMIT, GRADE_SIZE_MULTIPLIER,
    CONTRACT_MULTIPLIER, PAPER_TRADING, INSTRUMENT, DAILY_LOSS_LIMIT_USD
)
from utils.time_utils import fmt_et_short
from utils.math_utils import contracts_from_risk

logger = logging.getLogger(__name__)

SERVICE_NAME = "optionsbot"


@dataclass
class SizingResult:
    contracts:          int   = 0
    cost_per_contract:  float = 0.0
    total_cost:         float = 0.0
    max_loss:           float = 0.0
    grade:              str   = "B"
    grade_multiplier:   float = 1.0
    allowed:            bool  = True
    reject_reason:      str   = ""


@dataclass
class CircuitBreakerState:
    session_halted: bool  = False
    session_losses: int   = 0
    reason:         str   = ""

    @property
    def any_active(self) -> bool:
        return self.session_halted


class RiskManager:
    """
    Options-specific risk manager.
    Sizes positions in whole contracts based on fixed dollar risk.
    Tracks session losses and halts on circuit breaker.
    """

    def __init__(self, risk_per_trade: float = RISK_PER_TRADE_USD,
                 paper_trading: bool = PAPER_TRADING):
        self._risk_per_trade   = risk_per_trade
        self._paper_trading    = paper_trading
        self._session_losses     = 0
        self._session_halted     = False
        self._reassess_requested = False
        self._session_pnl_usd    = 0.0
        self._daily_loss_limit   = DAILY_LOSS_LIMIT_USD
        self._seeded             = False

    def update_risk(self, risk_usd: float):
        self._risk_per_trade = risk_usd

    @property
    def risk_per_trade(self) -> float:
        return self._risk_per_trade

    def compute_size(self,
                     premium: float,
                     grade: str = "B",
                     is_butterfly: bool = False,
                     net_debit: float = 0.0,
                     butterfly_half_size: bool = False) -> SizingResult:
        """
        Calculate whole contract count.

        Args:
            premium:             Option mark price (single leg)
            grade:               Setup grade (A or B — C is rejected upstream)
            is_butterfly:        True for butterfly (use net_debit)
            net_debit:           Net debit for butterfly (per share)
            butterfly_half_size: True when VIX 15-20 (halve butterfly size)
        """
        result = SizingResult(grade=grade)

        cost_per_share = net_debit if is_butterfly else premium
        if cost_per_share <= 0:
            result.allowed       = False
            result.reject_reason = "zero_premium"
            return result

        cost_per_contract = cost_per_share * CONTRACT_MULTIPLIER
        grade_mult        = GRADE_SIZE_MULTIPLIER.get(grade, 1.0)

        if is_butterfly and butterfly_half_size:
            grade_mult = grade_mult * 0.5

        count = contracts_from_risk(
            self._risk_per_trade, cost_per_contract, grade_mult
        )

        if count < 1:
            result.allowed       = False
            result.reject_reason = (
                f"insufficient_capital: need ${cost_per_contract:.2f}/contract, "
                f"risk=${self._risk_per_trade * grade_mult:.2f}"
            )
            return result

        total_cost = count * cost_per_contract

        result.contracts         = count
        result.cost_per_contract = cost_per_contract
        result.total_cost        = total_cost
        result.max_loss          = total_cost
        result.grade_multiplier  = grade_mult
        result.allowed           = True

        logger.info(
            f"Position size: {count} contract(s) × ${cost_per_contract:.2f} "
            f"= ${total_cost:.2f} total "
            f"grade={grade} mult={grade_mult}x "
            f"{'[BUTTERFLY HALF-SIZE]' if is_butterfly and butterfly_half_size else ''}"
        )
        return result

    def compute_condor_leg_size(self, spread_width: float, credit: float,
                                 grade: str = "B") -> SizingResult:
        """Size ONE condor vertical (credit spread) at HALF the grade budget.

        Each side of the condor is budgeted independently at half of the normal
        per-trade risk, so a B-grade $1000 trade becomes two $500 verticals.
        Max loss per contract for a credit spread = (width - credit) x 100.
        """
        result = SizingResult(grade=grade)

        max_loss_per_contract = (spread_width - credit) * CONTRACT_MULTIPLIER
        if max_loss_per_contract <= 0:
            result.allowed       = False
            result.reject_reason = "non_positive_max_loss (credit >= width)"
            return result

        grade_mult  = GRADE_SIZE_MULTIPLIER.get(grade, 1.0)
        half_budget = self._risk_per_trade * grade_mult * 0.5

        count = int(half_budget // max_loss_per_contract)
        if count < 1:
            result.allowed       = False
            result.reject_reason = (
                f"insufficient_capital: vertical max_loss="
                f"${max_loss_per_contract:.0f} > half_budget=${half_budget:.0f}"
            )
            return result

        result.contracts        = count
        result.cost_per_contract = max_loss_per_contract
        result.total_cost       = count * max_loss_per_contract
        result.max_loss         = count * max_loss_per_contract
        result.grade_multiplier = grade_mult
        result.allowed          = True

        logger.info(
            f"Condor leg size: {count} vertical(s) x max_loss "
            f"${max_loss_per_contract:.0f} = ${result.total_cost:.0f} "
            f"(half budget=${half_budget:.0f}, grade={grade})"
        )
        return result

    def check_circuit_breaker(self) -> CircuitBreakerState:
        self._ensure_seeded()
        return CircuitBreakerState(
            session_losses=self._session_losses,
            session_halted=self._session_halted,
        )

    def _ensure_seeded(self):
        """Seed session net P&L from today's closed trades so the daily loss
        halt survives restarts within the same session."""
        if self._seeded:
            return
        self._seeded = True
        try:
            from database.trade_logger import get_trade_logger
            summary = get_trade_logger().today_summary()
            self._session_pnl_usd = float(summary.get("total_pnl", 0.0) or 0.0)
            if self._session_pnl_usd <= -self._daily_loss_limit:
                self._session_halted = True
        except Exception:
            pass

    def record_loss(self, pnl_usd: float = 0.0):
        self._ensure_seeded()
        self._session_losses += 1
        self._session_pnl_usd += pnl_usd          # negative for a loss
        # Reassess the regime after EVERY losing trade — a loss is fresh
        # information about whether the current regime read still holds.
        self._reassess_requested = True
        logger.warning(
            f"Session loss #{self._session_losses} (${pnl_usd:+.0f}) — "
            f"day P&L ${self._session_pnl_usd:+.0f}; forcing regime reassessment"
        )
        self._check_daily_loss_limit()

    def record_win(self, pnl_usd: float = 0.0):
        self._ensure_seeded()
        self._session_pnl_usd += pnl_usd
        logger.info(
            f"Session win (${pnl_usd:+.0f}) — day P&L ${self._session_pnl_usd:+.0f}"
        )

    def _check_daily_loss_limit(self):
        """Halt NEW entries when the day's NET P&L is down by the daily loss
        limit (default = per-trade risk). Wins offset losses, so a green day
        keeps trading. Open positions keep being managed to exit."""
        if self._session_halted:
            return
        if self._session_pnl_usd <= -self._daily_loss_limit:
            self._session_halted = True
            logger.warning(
                f"\U0001F6D1 DAILY LOSS LIMIT HIT: day P&L "
                f"${self._session_pnl_usd:+.0f} <= -${self._daily_loss_limit:.0f}. "
                f"Halting NEW entries. Override via configure.sh."
            )
            try:
                from notifications.alert_manager import get_alert_manager
                get_alert_manager()._send(
                    f"\U0001F6D1 DAILY LOSS LIMIT HIT — day P&L "
                    f"${self._session_pnl_usd:+.0f} (limit ${self._daily_loss_limit:.0f}). "
                    f"New entries halted. Override via configure.sh."
                )
            except Exception:
                pass

    def is_halted(self) -> bool:
        self._ensure_seeded()
        return self._session_halted

    def consume_reassess_request(self) -> bool:
        """Edge-triggered. True once after a loss requested a reassessment."""
        if self._reassess_requested:
            self._reassess_requested = False
            return True
        return False

    def reset_session(self):
        self._session_losses     = 0
        self._session_halted     = False
        self._reassess_requested = False
        self._session_pnl_usd    = 0.0
        self._seeded             = True   # fresh session starts flat
        logger.info("Risk manager session reset")

    @property
    def session_losses(self) -> int:
        return self._session_losses

    def status_report(self) -> str:
        return (
            f"risk=${self._risk_per_trade:.0f}/trade "
            f"day_pnl=${self._session_pnl_usd:+.0f} "
            f"daily_limit=${self._daily_loss_limit:.0f} "
            f"halted={self._session_halted}"
        )


_risk_manager: Optional[RiskManager] = None


def init_risk_manager(risk_per_trade: float = RISK_PER_TRADE_USD,
                      paper_trading: bool = PAPER_TRADING) -> RiskManager:
    global _risk_manager
    _risk_manager = RiskManager(risk_per_trade, paper_trading)
    return _risk_manager


def get_risk_manager() -> RiskManager:
    global _risk_manager
    if _risk_manager is None:
        _risk_manager = RiskManager()
    return _risk_manager
