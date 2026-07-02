"""
strategy/condor_roll.py — Broken-wing roll of a live iron condor.
v1.0 — 2026-07-02 — initial build.

When BOTH condor verticals are open and price tests ONE side, a professional
adjustment is to roll the UNTESTED side toward price to collect additional
credit. If the cumulative credit collected covers the tested side's width, the
tested side becomes RISK-FREE — the structure is now a broken-wing butterfly.

Risk-free condition (the whole point):
    total_credit_collected  >=  tested_side_width
    (both in per-share terms; ×100 is the dollar figure)

    where total_credit_collected = banked_condor_credit
                                   + roll_credit (new untested vertical)
                                   - close_cost  (buying back the old untested vertical)

This module:
  1. classify_tested()      — which side is being tested, which is untested.
  2. find_risk_free_roll()  — pure premium math over live chain marks; finds the
                              smallest roll of the untested side that makes the
                              tested side risk-free (smallest roll = least new
                              risk on the rolled side).
  3. check_and_execute_roll() — orchestrator: detect, solve, and (only if a
                              risk-free roll exists) execute it, marking the
                              result a broken wing.

HARD CONTRACT: the roll is the FINAL adjustment. Once rolled, every leg is
flagged is_broken_wing=1 and this module never touches it again — it is managed
to exit only (stop / target / nickel), no further rolls.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Optional, List, Tuple

from config import STRIKE_INCREMENT, CONTRACT_MULTIPLIER
from utils.time_utils import fmt_et_short

logger = logging.getLogger(__name__)


@dataclass
class RollPlan:
    tested_side:        str    # "call" or "put" — the threatened side (goes risk-free)
    untested_side:      str    # the side we roll toward price
    new_short_strike:   float
    new_long_strike:    float
    new_short_symbol:   str
    new_long_symbol:    str
    roll_credit:        float  # per-share credit of the new untested vertical
    close_cost:         float  # per-share debit to buy back the old untested vertical
    total_credit_after: float  # cumulative per-share credit after the roll
    tested_width:       float
    risk_free:          bool
    contracts:          int


def _mark_at(contracts, strike: float) -> Optional[float]:
    return next((c.mark for c in contracts if c.strike == strike and c.mark > 0), None)


def _contract_at(contracts, strike: float):
    return next((c for c in contracts if c.strike == strike and c.mark > 0), None)


def classify_tested(legs: List[dict], current_price: float,
                    proximity_strikes: int = 1) -> Tuple[Optional[dict], Optional[dict]]:
    """Return (tested_leg, untested_leg). A side is 'tested' when price is within
    proximity_strikes of that side's short strike (or beyond it)."""
    call_leg = next((l for l in legs if l.get("option_side") == "call"), None)
    put_leg  = next((l for l in legs if l.get("option_side") == "put"),  None)
    if not (call_leg and put_leg):
        return None, None

    prox = proximity_strikes * STRIKE_INCREMENT
    if current_price >= call_leg["short_strike"] - prox:
        return call_leg, put_leg      # call tested, put untested
    if current_price <= put_leg["short_strike"] + prox:
        return put_leg, call_leg      # put tested, call untested
    return None, None


def find_risk_free_roll(tested_leg: dict, untested_leg: dict, chain,
                        current_price: float,
                        banked_credit: float) -> Optional[RollPlan]:
    """Solve for the smallest roll of the untested side that makes the tested
    side risk-free. Returns the best RollPlan found (risk_free flag set), or
    None if the chain can't be priced."""
    tested_side   = tested_leg["option_side"]
    untested_side = untested_leg["option_side"]
    tested_width  = float(tested_leg["spread_width"])
    wing          = float(untested_leg["spread_width"])
    contracts     = int(untested_leg.get("contracts", 1))

    u_list = chain.puts if untested_side == "put" else chain.calls

    # Cost to buy back the existing (cheap, far-OTM) untested vertical.
    old_short_m = _mark_at(u_list, untested_leg["short_strike"])
    old_long_m  = _mark_at(u_list, untested_leg["long_strike"])
    if old_short_m is None or old_long_m is None:
        return None
    close_cost = max(old_short_m - old_long_m, 0.0)

    # Candidate short strikes for the rolled vertical, marching from the current
    # short strike TOWARD price (roll up for puts, down for calls). Smallest roll
    # that reaches risk-free wins → least new risk on the rolled side.
    inc = STRIKE_INCREMENT
    candidates: List[float] = []
    if untested_side == "put":
        k = untested_leg["short_strike"] + inc
        while k <= current_price:
            candidates.append(k); k += inc
    else:  # call side rolled down toward price
        k = untested_leg["short_strike"] - inc
        while k >= current_price:
            candidates.append(k); k -= inc

    best: Optional[RollPlan] = None
    for new_short in candidates:
        new_long = new_short - wing if untested_side == "put" else new_short + wing
        ns = _contract_at(u_list, new_short)
        nl = _contract_at(u_list, new_long)
        if ns is None or nl is None:
            continue
        roll_credit = ns.mark - nl.mark
        if roll_credit <= 0:
            continue
        total_after = banked_credit + roll_credit - close_cost
        plan = RollPlan(
            tested_side=tested_side, untested_side=untested_side,
            new_short_strike=new_short, new_long_strike=new_long,
            new_short_symbol=ns.symbol, new_long_symbol=nl.symbol,
            roll_credit=roll_credit, close_cost=close_cost,
            total_credit_after=total_after, tested_width=tested_width,
            risk_free=(total_after >= tested_width), contracts=contracts,
        )
        # Track the best (highest cumulative credit) and return the FIRST one
        # that is risk-free (smallest roll toward price).
        if best is None or plan.total_credit_after > best.total_credit_after:
            best = plan
        if plan.risk_free:
            return plan
    return best


def check_and_execute_roll(pos_mgr, chain, current_price: float, state) -> bool:
    """If both condor verticals are open and one side is tested, roll the
    untested side into a broken wing — but ONLY if that roll makes the tested
    side risk-free. Returns True if a roll was executed."""
    if chain is None:
        return False

    legs = [r for r in pos_mgr.get_open_records() if r.get("is_condor_leg")]
    if len(legs) != 2:
        return False
    # Final-form guard: never touch a position that has already been rolled.
    if any(r.get("is_broken_wing") for r in legs):
        return False

    tested, untested = classify_tested(legs, current_price)
    if tested is None:
        return False

    banked_credit = sum(float(l.get("credit_received", l.get("entry_premium", 0.0)))
                        for l in legs)
    plan = find_risk_free_roll(tested, untested, chain, current_price, banked_credit)
    if plan is None or not plan.risk_free:
        # No roll available that removes tested-side risk — manage normally.
        return False

    return _execute_roll(pos_mgr, tested, untested, plan, state)


def _execute_roll(pos_mgr, tested: dict, untested: dict,
                  plan: RollPlan, state) -> bool:
    """Close the old untested vertical, open the rolled vertical, and flag the
    whole structure a broken wing (final form — no further adjustments)."""
    from database.trade_logger import make_record, get_trade_logger
    from execution.exit_engine import get_exit_engine
    from notifications.alert_manager import get_alert_manager
    from config import INSTRUMENT, CONDOR_STOP_LOSS_PCT, CONDOR_NICKEL_CLOSE

    tl        = get_trade_logger()
    mode      = "PAPER" if state.paper_trading else "LIVE"
    contracts = plan.contracts

    try:
        # ── 1. Close the OLD untested vertical (buy it back) ──────────────────
        if not state.paper_trading:
            ok = get_exit_engine(False).place_exit_order(untested, "rolled_to_broken_wing")
            if not ok:
                logger.error("Roll aborted — could not close untested vertical")
                return False
        old_credit = float(untested.get("credit_received", untested.get("entry_premium", 0.0)))
        pnl_close  = (old_credit - plan.close_cost) * contracts * CONTRACT_MULTIPLIER
        tl.log_exit(untested["trade_id"], exit_price=plan.close_cost,
                    pnl_usd=pnl_close, exit_reason="rolled_to_broken_wing")
        pos_mgr.remove_record(untested["trade_id"])

        # ── 2. Open the ROLLED untested vertical (the new risk side) ──────────
        #      (live order placement mirrors _execute_condor_leg; paper fills at mid)
        new_width  = abs(plan.new_short_strike - plan.new_long_strike)
        new_maxloss = (new_width - plan.roll_credit) * contracts * CONTRACT_MULTIPLIER
        rolled = make_record(
            trade_id        = str(uuid.uuid4()),
            symbol          = INSTRUMENT,
            strategy        = "IronCondorStrategy",
            setup_type      = f"BWB rolled {plan.untested_side} vertical",
            setup_grade     = "B",
            direction       = "neutral",
            option_side     = plan.untested_side,
            strike          = plan.new_short_strike,
            short_strike    = plan.new_short_strike,
            long_strike     = plan.new_long_strike,
            spread_width    = new_width,
            credit_received = plan.roll_credit,
            contracts       = contracts,
            entry_premium   = plan.roll_credit,
            total_cost      = new_maxloss,
            max_loss        = new_maxloss,
            stop_premium    = plan.roll_credit * (1 + CONDOR_STOP_LOSS_PCT),
            target_premium  = CONDOR_NICKEL_CLOSE,
            regime          = "RANGING",
            is_condor_leg   = 1,
            is_broken_wing  = 1,                       # FINAL FORM
            short_symbol    = plan.new_short_symbol,
            long_symbol     = plan.new_long_symbol,
            option_symbol   = plan.new_short_symbol,
            paper_trade     = 1 if state.paper_trading else 0,
            status          = "open",
        )
        tl.log_entry(rolled)
        pos_mgr.add_condor_leg(rolled)

        # ── 3. Flag the TESTED (now risk-free) vertical a broken wing too ─────
        tl.update_fields(tested["trade_id"], is_broken_wing=1)
        tested["is_broken_wing"] = 1

        get_alert_manager()._send(
            f"\U0001F98B [{mode}] ROLLED TO BROKEN WING | "
            f"{plan.tested_side} side now RISK-FREE "
            f"(credit ${plan.total_credit_after:.2f} >= width ${plan.tested_width:.2f}) | "
            f"rolled {plan.untested_side} to {plan.new_short_strike:.0f}/{plan.new_long_strike:.0f} "
            f"for ${plan.roll_credit:.2f} | final form | {fmt_et_short()}"
        )
        logger.info(
            f"[{mode}] BROKEN-WING ROLL: {plan.tested_side} side risk-free "
            f"(cum credit ${plan.total_credit_after:.2f} >= width ${plan.tested_width:.2f}); "
            f"rolled {plan.untested_side} -> {plan.new_short_strike:.0f}/{plan.new_long_strike:.0f} "
            f"credit ${plan.roll_credit:.2f}. FINAL FORM — no further adjustments."
        )
        return True

    except Exception as e:
        logger.error(f"Broken-wing roll failed: {e}")
        return False
