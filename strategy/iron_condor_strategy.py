"""
strategy/iron_condor_strategy.py — Legged Iron Condor for RANGING regime.
v1.0 — 2026-06-30 — initial release (simultaneous entry placeholder)
v1.1 — 2026-06-30 — full redesign: legged entry via price-triggered verticals.
v1.2 — 2026-07-02 — docstring/comment cleanup: strike selection is BB-band
        anchored ONLY (no delta anywhere in the code). Removed stale
        "delta-primary" / "delta as secondary" / "falls back to delta-primary"
        wording that contradicted the implementation and the architecture
        decision. No logic change.

Strategy design:
  At decision time, the bot identifies both vertical spread strike locations
  by anchoring the short strikes to the Bollinger Band boundaries (short call
  at/just outside the BB upper band, short put at/just outside the BB lower
  band), with an ATM straddle-based expected-move sanity guardrail. No delta
  targeting is used. No order is placed yet.

  Leg 1 fires when price reaches within CONDOR_PROXIMITY_STRIKES of the
  first side's short strike — whichever side price is moving toward first.
  (e.g. short call at 7545 → leg 1 fires when price hits 7540, 2 strikes away)

  Leg 2 is queued after Leg 1 fills. It fires when price reaches within
  CONDOR_PROXIMITY_STRIKES of the opposite side's short strike.

  Invalidation: if regime flips away from RANGING (to ANY other regime)
  before a leg fires, that pending leg is permanently cancelled. An
  already-filled leg stays open and manages independently — it is NEVER
  cancelled after the order is placed.

  Exit per leg: 25% stop loss OR close at $0.05 (nickel) — whichever
  comes first. No take-profit target, no trail, no BOS. Hold to nickel
  or stop, independently per leg.

  If Leg 2 never fires (price never approached the second side before
  close), Leg 1 remains as a standalone vertical and manages the same way.

State machine:
  IDLE -> DECIDED (both strikes identified, watching for Leg 1 trigger)
       -> LEG1_TRIGGERED (Leg 1 order placed, waiting for fill)
       -> LEG1_FILLED (Leg 1 live, Leg 2 queued, watching for Leg 2 trigger)
       -> LEG2_TRIGGERED (Leg 2 order placed, waiting for fill)
       -> COMPLETE (both legs filled — full iron condor assembled)
  Any state -> CANCELLED (regime flipped before a pending leg fired)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List, Tuple
from zoneinfo import ZoneInfo

from strategy.base_strategy import BaseOptionsStrategy, OptionsSignal
from analysis.regime_classifier import RegimeState, Regime
from analysis.volatility_engine import VolatilityState, VolatilityEngine
from data.macro_data import MacroSnapshot
from config import (
    CONDOR_WING_WIDTH_SPX, CONDOR_WING_WIDTH_QQQ,
    CONDOR_EXPECTED_MOVE_GUARDRAIL_MULT,
    CONDOR_PROXIMITY_STRIKES,
    CONDOR_NICKEL_CLOSE, CONDOR_STOP_LOSS_PCT,
    CONDOR_ENTRY_START_ET, CONDOR_ENTRY_CUTOFF_ET,
    STRIKE_INCREMENT, INSTRUMENT, VIX_BUTTERFLY_DISABLE
)

logger = logging.getLogger(__name__)
ET = ZoneInfo("US/Eastern")


class CondorState:
    IDLE         = "IDLE"          # No active condor plan
    DECIDED      = "DECIDED"       # Strikes identified, watching for Leg 1 trigger
    LEG1_FILLED  = "LEG1_FILLED"   # Leg 1 live, Leg 2 queued
    COMPLETE     = "COMPLETE"      # Both legs filled
    CANCELLED    = "CANCELLED"     # Regime flipped before a pending leg fired
    EXPIRED      = "EXPIRED"       # Past entry cutoff


@dataclass
class CondorPlan:
    """
    The full condor plan computed at decision time.
    Both verticals are identified upfront; legs fire independently as
    price visits each side's trigger level.
    """
    # Call spread (upper side)
    short_call_strike:  float = 0.0
    long_call_strike:   float = 0.0
    call_trigger_price: float = 0.0   # Price level that fires Leg 1 or Leg 2

    # Put spread (lower side)
    short_put_strike:   float = 0.0
    long_put_strike:    float = 0.0
    put_trigger_price:  float = 0.0

    # Which side is Leg 1 (the one price is more likely to hit first)
    leg1_side:          str   = ""    # "call" or "put"
    leg2_side:          str   = ""

    # Expected move at decision time (for logging/reference)
    expected_move:      float = 0.0
    underlying_at_decision: float = 0.0

    # Actual contracts (populated when legs fill)
    leg1_short: Optional[OptionContract] = None
    leg1_long:  Optional[OptionContract] = None
    leg2_short: Optional[OptionContract] = None
    leg2_long:  Optional[OptionContract] = None

    leg1_credit: float = 0.0
    leg2_credit: float = 0.0

    state: str = CondorState.IDLE
    decided_at: str = ""
    leg1_filled_at: str = ""


class IronCondorStrategy(BaseOptionsStrategy):
    """
    Legged iron condor — price-triggered vertical spreads.
    Each leg fires independently when price visits that side's trigger level.
    """

    def __init__(self):
        self._plan: Optional[CondorPlan] = None
        self._last_reset_date: Optional[str] = None

    @property
    def name(self) -> str:
        return "IronCondorStrategy"

    def _reset_if_new_day(self):
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if self._last_reset_date != today:
            self._plan            = None
            self._last_reset_date = today

    @property
    def has_active_plan(self) -> bool:
        return (self._plan is not None and
                self._plan.state in (CondorState.DECIDED, CondorState.LEG1_FILLED))

    @property
    def plan(self) -> Optional[CondorPlan]:
        return self._plan

    def _wing_width(self) -> int:
        if INSTRUMENT == "SPX":
            return CONDOR_WING_WIDTH_SPX // STRIKE_INCREMENT
        return CONDOR_WING_WIDTH_QQQ // STRIKE_INCREMENT

    def _expected_move_from_straddle(self, chain: OptionsChain,
                                      underlying: float) -> float:
        """ATM straddle = ATM call mark + ATM put mark. Most accurate EM basis."""
        try:
            atm_call = min(
                [c for c in chain.calls if c.mark > 0],
                key=lambda c: abs(c.strike - underlying)
            )
            atm_put = min(
                [c for c in chain.puts if c.mark > 0],
                key=lambda c: abs(c.strike - underlying)
            )
            if atm_call.mark > 0 and atm_put.mark > 0:
                return atm_call.mark + atm_put.mark
        except Exception:
            pass
        return 0.0

    def _select_by_band(self, contracts: List[OptionContract],
                         band_level: float, side: str) -> Optional[OptionContract]:
        """
        BB-anchored strike selection — no delta involvement.
        Finds the nearest liquid strike at or outside the BB boundary:
          - Call side: lowest strike that is >= bb_upper
          - Put side:  highest strike that is <= bb_lower
        If no liquid strike exists outside the band (very tight chain),
        returns the nearest liquid strike to the band level as fallback.
        Delta is deliberately not used here — it is relative to current
        price, not the structural range boundary, and would place strikes
        incorrectly depending on where price happens to sit at decision time.
        """
        candidates = [c for c in contracts if c.mark > 0.01]
        if not candidates:
            return None

        if side == "call":
            outside = [c for c in candidates if c.strike >= band_level]
            if outside:
                return min(outside, key=lambda c: c.strike)
            # Fallback: nearest liquid strike to band
            return min(candidates, key=lambda c: abs(c.strike - band_level))
        else:  # put
            outside = [c for c in candidates if c.strike <= band_level]
            if outside:
                return max(outside, key=lambda c: c.strike)
            return min(candidates, key=lambda c: abs(c.strike - band_level))

    def _find_contract_at_strike(self, contracts: List[OptionContract],
                                  target_strike: float) -> Optional[OptionContract]:
        """Find contract at exact strike, or nearest with a valid mark."""
        exact = [c for c in contracts if c.strike == target_strike and c.mark > 0]
        if exact:
            return exact[0]
        liquid = [c for c in contracts if c.mark > 0]
        if not liquid:
            return None
        return min(liquid, key=lambda c: abs(c.strike - target_strike))

    def decide(self, regime: RegimeState, vol_state: VolatilityState,
               chain: OptionsChain, macro: MacroSnapshot,
               current_price: float) -> Optional[CondorPlan]:
        """
        Evaluate whether to plan an iron condor. If conditions are met,
        identify both vertical spreads and set up the plan. No orders placed.
        Returns the plan if one was created, None otherwise.
        """
        self._reset_if_new_day()

        now_et = datetime.now(ET)
        hm     = (now_et.hour, now_et.minute)

        if hm < CONDOR_ENTRY_START_ET or hm >= CONDOR_ENTRY_CUTOFF_ET:
            return None

        if self._plan is not None:
            return None  # Already have an active plan this session

        if regime.primary_regime != Regime.RANGING:
            return None

        if macro.vix >= VIX_BUTTERFLY_DISABLE:
            logger.info(f"Condor blocked: VIX={macro.vix:.1f} above threshold")
            return None

        # Compute expected move
        em = self._expected_move_from_straddle(chain, current_price)
        if em <= 0:
            logger.debug("Condor: could not compute expected move")
            return None

        # BB-anchored strike selection — no delta involvement.
        # Short call placed at or just outside the BB upper band — structurally
        # correct for a ranging day since the BB band IS the range boundary.
        # Short put placed at or just outside the BB lower band. If no liquid
        # strike exists near a band, the leg is skipped (return None) — there
        # is no delta fallback.
        bb_upper = vol_state.bb_upper if vol_state.bb_upper > 0 else current_price + em
        bb_lower = vol_state.bb_lower if vol_state.bb_lower > 0 else current_price - em

        if bb_upper <= current_price or bb_lower >= current_price:
            logger.info("Condor: BB bands not usable (price outside bands) — skip")
            return None

        short_call = self._select_by_band(chain.calls, bb_upper, "call")
        short_put  = self._select_by_band(chain.puts,  bb_lower, "put")

        if short_call is None or short_put is None:
            logger.debug("Condor: could not find short strikes near BB bands")
            return None

        # Guardrail: sanity check that strikes aren't beyond 1.2x expected move
        call_dist = short_call.strike - current_price
        put_dist  = current_price - short_put.strike
        guardrail = em * CONDOR_EXPECTED_MOVE_GUARDRAIL_MULT

        if max(call_dist, put_dist) > guardrail:
            logger.info(
                f"Condor: BB-selected strikes exceed expected move guardrail "
                f"({guardrail:.1f}pt) — unusual skew, skip"
            )
            return None

        # Wing widths (fixed, instrument-appropriate)
        wing = self._wing_width()
        long_call_strike = short_call.strike + wing * STRIKE_INCREMENT
        long_put_strike  = short_put.strike  - wing * STRIKE_INCREMENT

        # Trigger prices: 2 strikes (CONDOR_PROXIMITY_STRIKES) from short strike
        # Call spread: fires when price rises to within 2 strikes of short call
        call_trigger = short_call.strike - CONDOR_PROXIMITY_STRIKES * STRIKE_INCREMENT
        # Put spread: fires when price drops to within 2 strikes of short put
        put_trigger  = short_put.strike  + CONDOR_PROXIMITY_STRIKES * STRIKE_INCREMENT

        # Determine which leg is more likely to fill first based on current price
        # — whichever side's trigger is closer to current price is Leg 1
        call_trigger_dist = abs(current_price - call_trigger)
        put_trigger_dist  = abs(current_price - put_trigger)

        if call_trigger_dist <= put_trigger_dist:
            leg1_side = "call"
            leg2_side = "put"
        else:
            leg1_side = "put"
            leg2_side = "call"

        plan = CondorPlan(
            short_call_strike  = short_call.strike,
            long_call_strike   = long_call_strike,
            call_trigger_price = call_trigger,
            short_put_strike   = short_put.strike,
            long_put_strike    = long_put_strike,
            put_trigger_price  = put_trigger,
            leg1_side          = leg1_side,
            leg2_side          = leg2_side,
            expected_move      = em,
            underlying_at_decision = current_price,
            state              = CondorState.DECIDED,
            decided_at         = now_et.strftime("%H:%M ET")
        )

        self._plan = plan

        logger.info(
            f"\U0001F985 CONDOR PLANNED: "
            f"call_spread={short_call.strike:.0f}/{long_call_strike:.0f} "
            f"(trigger@{call_trigger:.0f}) "
            f"put_spread={long_put_strike:.0f}/{short_put.strike:.0f} "
            f"(trigger@{put_trigger:.0f}) "
            f"leg1={leg1_side.upper()} "
            f"EM=${em:.2f} VIX={macro.vix:.1f} "
            f"bb_upper={bb_upper:.2f} bb_lower={bb_lower:.2f}"
        )
        return plan

    def check_leg_triggers(self, regime: RegimeState,
                            chain: OptionsChain,
                            current_price: float) -> Optional[OptionsSignal]:
        """
        Called every tick when a condor plan is active.
        Returns an OptionsSignal if a leg should fire now, None otherwise.
        Also cancels pending legs if regime has flipped away from RANGING.
        """
        plan = self._plan
        if plan is None:
            return None

        # Invalidation: regime flipped away from RANGING
        if regime.primary_regime != Regime.RANGING:
            if plan.state == CondorState.DECIDED:
                # Leg 1 never fired — abandon everything
                logger.info(
                    f"Condor CANCELLED before Leg 1: regime flipped to "
                    f"{regime.primary_regime}"
                )
                plan.state = CondorState.CANCELLED
                self._plan = None
                return None
            elif plan.state == CondorState.LEG1_FILLED:
                # Leg 1 is already live — keep it. Cancel only Leg 2.
                logger.info(
                    f"Condor Leg 2 CANCELLED: regime flipped to "
                    f"{regime.primary_regime} — Leg 1 remains open"
                )
                plan.state = CondorState.COMPLETE  # Mark as done adding legs
                return None

        now_et = datetime.now(ET)
        hm = (now_et.hour, now_et.minute)
        if hm >= CONDOR_ENTRY_CUTOFF_ET:
            if plan.state == CondorState.DECIDED:
                logger.info("Condor: past cutoff, Leg 1 never fired — abandoned")
                plan.state = CondorState.EXPIRED
                self._plan = None
            elif plan.state == CondorState.LEG1_FILLED:
                logger.info("Condor: past cutoff, Leg 2 never fired — Leg 1 standalone")
                plan.state = CondorState.COMPLETE
            return None

        # Check which leg should fire
        if plan.state == CondorState.DECIDED:
            # Check Leg 1 trigger
            if plan.leg1_side == "call":
                triggered = current_price >= plan.call_trigger_price
            else:
                triggered = current_price <= plan.put_trigger_price

            if triggered:
                return self._build_leg_signal(plan, plan.leg1_side, chain, is_leg1=True)

        elif plan.state == CondorState.LEG1_FILLED:
            # Check Leg 2 trigger
            if plan.leg2_side == "call":
                triggered = current_price >= plan.call_trigger_price
            else:
                triggered = current_price <= plan.put_trigger_price

            if triggered:
                return self._build_leg_signal(plan, plan.leg2_side, chain, is_leg1=False)

        return None

    def notify_leg_filled(self, is_leg1: bool, credit: float,
                          short_contract: OptionContract,
                          long_contract: OptionContract):
        """Call from entry_engine after a condor leg order fills."""
        if self._plan is None:
            return
        plan = self._plan
        if is_leg1:
            plan.state         = CondorState.LEG1_FILLED
            plan.leg1_credit   = credit
            plan.leg1_short    = short_contract
            plan.leg1_long     = long_contract
            plan.leg1_filled_at = datetime.now(ET).strftime("%H:%M ET")
            logger.info(
                f"Condor Leg 1 FILLED ({plan.leg1_side.upper()}): "
                f"credit=${credit:.2f} — queuing Leg 2 ({plan.leg2_side.upper()})"
            )
        else:
            plan.state       = CondorState.COMPLETE
            plan.leg2_credit = credit
            plan.leg2_short  = short_contract
            plan.leg2_long   = long_contract
            logger.info(
                f"Condor Leg 2 FILLED ({plan.leg2_side.upper()}): "
                f"credit=${credit:.2f} — full condor assembled "
                f"total_credit=${plan.leg1_credit + credit:.2f}"
            )

    def _build_leg_signal(self, plan: CondorPlan, side: str,
                           chain: OptionsChain,
                           is_leg1: bool) -> Optional[OptionsSignal]:
        """Build an OptionsSignal for a single condor leg (vertical spread)."""
        if side == "call":
            contracts = chain.calls
            short_strike = plan.short_call_strike
            long_strike  = plan.long_call_strike
            leg_label    = "Call Credit Spread"
        else:
            contracts = chain.puts
            short_strike = plan.short_put_strike
            long_strike  = plan.long_put_strike
            leg_label    = "Put Credit Spread"

        short_contract = self._find_contract_at_strike(contracts, short_strike)
        long_contract  = self._find_contract_at_strike(contracts, long_strike)

        if short_contract is None or long_contract is None:
            logger.warning(
                f"Condor Leg {'1' if is_leg1 else '2'}: "
                f"could not find {side} spread contracts "
                f"({short_strike}/{long_strike})"
            )
            return None

        net_credit = short_contract.mark - long_contract.mark
        if net_credit <= 0:
            logger.info(
                f"Condor: {leg_label} credit <= 0 ({net_credit:.2f}) — skip"
            )
            return None

        wing_width = abs(long_strike - short_strike)
        max_loss   = wing_width - net_credit

        leg_num = "1" if is_leg1 else "2"
        signal = OptionsSignal(
            strategy_name     = self.name,
            setup_type        = f"Condor Leg {leg_num}: {leg_label}",
            direction         = "neutral",
            option_side       = side,
            is_iron_condor    = True,
            # Use the short/long contract fields for this leg
            short_call_contract  = short_contract if side == "call" else None,
            long_call_contract   = long_contract  if side == "call" else None,
            short_put_contract   = short_contract if side == "put"  else None,
            long_put_contract    = long_contract  if side == "put"  else None,
            net_credit           = net_credit,
            max_loss_condor      = max_loss,
            underlying_entry     = plan.underlying_at_decision,
            regime               = Regime.RANGING,
            stop_loss_pct        = CONDOR_STOP_LOSS_PCT,
            tp_pct               = 0.0,   # No TP — hold to nickel or stop
            notes                = (
                f"Condor leg {leg_num}/{2} | "
                f"EM=${plan.expected_move:.2f} | "
                f"{'Leg 2 queued after fill' if is_leg1 else 'Full condor on fill'}"
            )
        )

        self._add_confluence(signal, f"RANGING regime — condor leg {leg_num}")
        self._add_confluence(
            signal,
            f"Price reached trigger ({plan.call_trigger_price if side == 'call' else plan.put_trigger_price:.0f}) — "
            f"{CONDOR_PROXIMITY_STRIKES} strikes from short"
        )

        logger.info(
            f"\U0001F985 CONDOR LEG {leg_num} SIGNAL ({side.upper()}): "
            f"sell={short_strike:.0f} buy={long_strike:.0f} "
            f"credit=${net_credit:.2f} max_loss=${max_loss:.2f} "
            f"stop=${net_credit * (1 + CONDOR_STOP_LOSS_PCT):.2f} "
            f"nickel_close=${CONDOR_NICKEL_CLOSE:.2f}"
        )
        return signal

    def reset_plan(self):
        """Clear the active plan (e.g. end of session)."""
        self._plan = None

    # generate_signal required by ABC — routes to decide() for initial call
    def generate_signal(self, *args, **kwargs) -> Optional[OptionsSignal]:
        """
        For the condor, main.py calls decide() and check_leg_triggers()
        separately rather than using generate_signal() directly.
        This stub satisfies the ABC requirement.
        """
        return None
