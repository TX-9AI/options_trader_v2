"""
risk/risk_manager.py — Position sizing and session circuit breaker.
v1.0 — original release
v1.1 — 2026-06-27 — remove TRADE_GRADE_C and Twilio references,
        clean up Grade C sizing logic

Sizing model:
  - Fixed dollar risk per trade (operator-set at startup)
  - Contracts = floor(risk_usd × grade_multiplier / cost_per_contract)
  - cost_per_contract = mark × 100 (single leg) or net_debit × 100 (butterfly)
  - Always whole contracts; minimum 1 if affordable

Session circuit breaker:
  - 2 closed losses in one RTH session → halt session
  - Paper mode: circuit breaker fires and logs, but does NOT halt
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

from config import (
    RISK_PER_TRADE_USD, SESSION_LOSS_LIMIT, GRADE_SIZE_MULTIPLIER,
    CONTRACT_MULTIPLIER, PAPER_TRADING, INSTRUMENT
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
        self._session_losses   = 0
        self._session_halted   = False
        self._cb_fired_ids:    set = set()

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

    def check_circuit_breaker(self) -> CircuitBreakerState:
        state = CircuitBreakerState(
            session_losses=self._session_losses,
            session_halted=self._session_halted
        )
        if self._session_halted:
            state.reason = (
                f"Session circuit breaker: {self._session_losses} losses — "
                f"halted for rest of session. Restart tomorrow."
            )
        return state

    def record_loss(self):
        self._session_losses += 1
        logger.warning(
            f"Session loss #{self._session_losses} recorded "
            f"(limit={SESSION_LOSS_LIMIT})"
        )
        if self._session_losses >= SESSION_LOSS_LIMIT:
            cb_id = f"session_{self._session_losses}"
            if cb_id not in self._cb_fired_ids:
                self._cb_fired_ids.add(cb_id)
                self._fire_circuit_breaker()

    def record_win(self):
        logger.info(
            f"Session win recorded "
            f"(session_losses={self._session_losses})"
        )

    def _fire_circuit_breaker(self):
        self._session_halted = True
        logger.warning(
            f"🚨 SESSION CIRCUIT BREAKER FIRED: "
            f"{self._session_losses} losses in this session "
            f"(limit={SESSION_LOSS_LIMIT}). "
            f"Bot halted until restart."
        )
        if not self._paper_trading:
            import subprocess, os
            service = os.environ.get("OPTIONSBOT_SERVICE", SERVICE_NAME)
            try:
                subprocess.Popen(["sudo", "systemctl", "stop", service])
            except Exception as e:
                logger.error(f"Failed to stop service: {e}")
        else:
            logger.info("[PAPER] Circuit breaker fired — paper mode, not stopping service")

    def reset_session(self):
        self._session_losses = 0
        self._session_halted = False
        self._cb_fired_ids.clear()
        logger.info("Risk manager session reset")

    @property
    def session_losses(self) -> int:
        return self._session_losses

    @property
    def is_halted(self) -> bool:
        return self._session_halted

    def status_report(self) -> str:
        return (
            f"risk=${self._risk_per_trade:.0f}/trade "
            f"session_losses={self._session_losses}/{SESSION_LOSS_LIMIT} "
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
