"""
strategy/base_strategy.py — Abstract base and OptionsSignal for all strategies.
v1.0 — original release
v1.1 — 2026-06-27 — added orb_range_high/low fields to OptionsSignal for
        strategy-aware exit routing in exit_engine.py
v1.2 — 2026-06-30 — added 4-leg fields for IronCondorStrategy (RANGING
        regime fallback when no GEX pin available for butterfly)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List

from data.options_chain import OptionContract, OptionsChain
from analysis.orb_engine import ORBData


@dataclass
class OptionsSignal:
    """
    A candidate options trade proposal.
    Validated and sized before reaching execution.
    """
    # ── Strategy identity ────────────────────────────────────────────────
    strategy_name:  str   = ""
    setup_type:     str   = ""

    # ── Direction ─────────────────────────────────────────────────────
    direction:      str   = ""      # "long" or "short" (of the UNDERLYING)
    option_side:    str   = ""      # "call" or "put"

    # ── Underlying price levels ─────────────────────────────────────────────
    underlying_entry:   float = 0.0
    underlying_stop:    float = 0.0
    underlying_target:  float = 0.0
    underlying_tp50:    float = 0.0

    # ── ORB range boundaries (ORB trades only) ──────────────────────────────
    orb_range_high: float = 0.0
    orb_range_low:  float = 0.0

    # ── Option details (single-leg) ───────────────────────────────────────
    strike:         float = 0.0
    expiry:         str   = ""
    entry_premium:  float = 0.0
    contract:       Optional[OptionContract] = None

    # ── Butterfly legs (3-leg) ─────────────────────────────────────────
    is_butterfly:        bool  = False
    lower_contract:      Optional[OptionContract] = None
    center_contract:     Optional[OptionContract] = None
    upper_contract:      Optional[OptionContract] = None
    butterfly_direction: str   = ""
    net_debit:           float = 0.0
    max_profit:          float = 0.0

    # ── Iron Condor legs (4-leg) ─────────────────────────────────────
    # Credit spread: sell short put + buy long put (lower side)
    #                sell short call + buy long call (upper side)
    is_iron_condor:       bool  = False
    short_put_contract:   Optional[OptionContract] = None
    long_put_contract:    Optional[OptionContract] = None
    short_call_contract:  Optional[OptionContract] = None
    long_call_contract:   Optional[OptionContract] = None
    net_credit:           float = 0.0   # Total credit received (premium collected)
    max_loss_condor:      float = 0.0   # Wing width - net credit (per side, worse side)
    expected_move:        float = 0.0   # ATM straddle-derived expected move at entry
    expected_move_mult:   float = 0.0   # short strike distance / expected move (guardrail check)

    # ── Risk / sizing ────────────────────────────────────────────────
    contracts:      int   = 0
    total_cost:     float = 0.0
    max_loss:       float = 0.0
    stop_loss_pct:  float = 0.25
    tp_pct:         float = 1.0

    # ── Quality ─────────────────────────────────────────────────────
    confluence_factors: List[str] = field(default_factory=list)
    conviction:     float = 0.0
    setup_grade:    str   = "B"

    # ── Context ──────────────────────────────────────────────────────
    regime:         str   = ""
    vix_at_signal:  float = 0.0
    is_fed_day:     bool  = False
    notes:          str   = ""

    @property
    def is_orb(self) -> bool:
        return self.strategy_name == "ORBStrategy"

    @property
    def is_sweep(self) -> bool:
        return self.strategy_name == "SweepReversal"

    @property
    def is_valid(self) -> bool:
        if self.is_butterfly:
            return (
                self.butterfly_direction in ("call", "put") and
                self.net_debit > 0 and
                self.lower_contract is not None and
                self.center_contract is not None and
                self.upper_contract is not None
            )
        if self.is_iron_condor:
            return (
                self.net_credit > 0 and
                self.short_put_contract is not None and
                self.long_put_contract is not None and
                self.short_call_contract is not None and
                self.long_call_contract is not None
            )
        return (
            self.option_side in ("call", "put") and
            self.strike > 0 and
            self.entry_premium > 0 and
            self.underlying_entry > 0
        )

    def stop_premium(self) -> float:
        """Premium level at which we exit (25% loss)."""
        if self.is_butterfly:
            return self.net_debit * (1 - self.stop_loss_pct)
        if self.is_iron_condor:
            # For a credit spread, "loss" means the spread VALUE rises
            # (we sold it, so rising value = losing money). Stop level
            # is expressed here as the spread value at which we exit.
            return self.net_credit * (1 + self.stop_loss_pct)
        return self.entry_premium * (1 - self.stop_loss_pct)

    def trail_activation_premium(self) -> float:
        """Premium level at which trailing stop activates (50% TP)."""
        if self.is_butterfly:
            return self.net_debit + self.max_profit * 0.5
        if self.is_iron_condor:
            # Condor profits as the spread value DECAYS toward zero.
            # 50% TP = spread value has decayed to 50% of credit received.
            return self.net_credit * 0.5
        return self.entry_premium * (1 + self.tp_pct * 0.5)

    def target_premium(self) -> float:
        """Full TP premium target."""
        if self.is_butterfly:
            return self.net_debit + self.max_profit * self.tp_pct
        if self.is_iron_condor:
            # TP = spread value has decayed to (1 - tp_pct) of credit.
            # e.g. tp_pct=0.50 means close at 50% of max profit captured,
            # i.e. spread value has fallen to 50% of the credit received.
            return self.net_credit * (1 - self.tp_pct)
        return self.entry_premium * (1 + self.tp_pct)


class BaseOptionsStrategy(ABC):
    """Abstract base for all options strategies."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def generate_signal(self, *args, **kwargs) -> Optional[OptionsSignal]: ...

    def _add_confluence(self, signal: OptionsSignal, factor: str):
        signal.confluence_factors.append(factor)
