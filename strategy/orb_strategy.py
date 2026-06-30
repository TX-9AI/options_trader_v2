"""
strategy/orb_strategy.py — ORB break-and-retest signal generation.
v1.0 — original release
v1.1 — 2026-06-27 — populate orb_range_high/low on signal so exit_engine
        can apply strategy-aware ORB stop logic
v1.2 — 2026-06-30 — update state check for orb_engine v1.1 rename:
        CONFIRMED_LONG/SHORT -> OPEN_LONG/SHORT

Liquidity-aware ORB logic:

RULE 1 — Named level IS the break level (catalyst, not obstacle):
  If the ORB high/low sits within 0.15% of a named pool (PDH, PDL, session H/L),
  and the break direction is THROUGH that level, this is a high-quality setup.
  The sweep of that level IS the ORB catalyst. Add confluence, don't penalize.

RULE 2 — Named level in path between entry and 50% TP (hard reduce):
  A named pool sitting between entry and the trail-activation level is a known
  reversal zone. Require at least one extra confluence factor, OR block.

RULE 3 — Named level just beyond 100% TP (adjust target, don't block):
  If a named pool sits within 0.5 ORB-widths past the 100% TP, move the target
  to that pool price rather than projecting past it.
"""

import logging
from typing import Optional, List, Tuple

from strategy.base_strategy import BaseOptionsStrategy, OptionsSignal
from analysis.orb_engine import ORBData, ORBState
from analysis.regime_classifier import RegimeState, Regime
from analysis.volatility_engine import VolatilityState
from analysis.liquidity_mapper import LiquidityMap, LiquidityPool
from data.options_chain import OptionsChain
from data.options_chain import get_chain_fetcher
from data.macro_data import MacroSnapshot
from config import FED_DAY_ORB_BOOST, INSTRUMENT

logger = logging.getLogger(__name__)

BREAK_LEVEL_PROXIMITY_PCT   = 0.0015
NAMED_IN_PATH_ORB_WIDTHS    = 1.5
BEYOND_TP_ADJUSTMENT_WIDTHS = 0.5


class ORBStrategy(BaseOptionsStrategy):
    """
    Opening Range Breakout strategy.
    Liquidity-aware: distinguishes catalyst sweeps from obstacle sweeps.
    """

    @property
    def name(self) -> str:
        return "ORBStrategy"

    def generate_signal(self,
                         orb: ORBData,
                         regime: RegimeState,
                         vol_state: VolatilityState,
                         liq_map: LiquidityMap,
                         chain: OptionsChain,
                         macro: MacroSnapshot,
                         current_price: float) -> Optional[OptionsSignal]:

        if orb.state not in (ORBState.OPEN_LONG, ORBState.OPEN_SHORT):
            return None

        direction   = orb.break_direction
        option_side = "call" if direction == "long" else "put"
        break_level = orb.orb_high if direction == "long" else orb.orb_low

        liq_result = self._analyze_liquidity(
            orb, liq_map, current_price, direction, break_level
        )

        if liq_result["block"]:
            logger.info(
                f"ORB BLOCKED — named liquidity pool in path with no extra "
                f"confluence: {liq_result['block_reason']}"
            )
            return None

        target_100 = liq_result.get("adjusted_target", orb.target_100pct)
        target_50  = orb.orb_high + (target_100 - orb.orb_high) * 0.5 \
                     if direction == "long" \
                     else orb.orb_low - (orb.orb_low - target_100) * 0.5

        signal = OptionsSignal(
            strategy_name     = self.name,
            setup_type        = f"ORB {direction.title()}",
            direction         = direction,
            option_side       = option_side,
            underlying_entry  = current_price,
            underlying_stop   = orb.stop_level,
            underlying_target = target_100,
            underlying_tp50   = target_50,
            # ── ORB range boundaries for strategy-aware exit ──────────────────
            orb_range_high    = orb.orb_high,
            orb_range_low     = orb.orb_low,
            regime            = regime.primary_regime,
            vix_at_signal     = macro.vix,
            is_fed_day        = macro.is_fed_day,
            stop_loss_pct     = 0.25,
            tp_pct            = 1.0,
        )

        # ── Base confluence ───────────────────────────────────────────────────
        self._add_confluence(signal, f"ORB break confirmed ({direction})")
        self._add_confluence(signal, "Break+retest pattern (1m body/wick rules)")

        if liq_result["break_is_named_level"]:
            pool_name = liq_result["break_level_name"]
            self._add_confluence(
                signal,
                f"ORB break through named level {pool_name} — sweep catalyst"
            )
            signal.conviction += 0.15

        if direction == "long" and vol_state.price_vs_vwap == "ABOVE":
            self._add_confluence(signal, "Above VWAP — bullish bias")
        elif direction == "short" and vol_state.price_vs_vwap == "BELOW":
            self._add_confluence(signal, "Below VWAP — bearish bias")

        if (direction == "long"  and regime.primary_regime == Regime.TRENDING_BULL) or \
           (direction == "short" and regime.primary_regime == Regime.TRENDING_BEAR):
            self._add_confluence(signal, f"Regime aligned ({regime.primary_regime})")

        if liq_result["path_clear"]:
            self._add_confluence(signal, "Liquidity path clear to target")
        elif liq_result["unnamed_in_path"] > 0:
            signal.notes += (
                f" | {liq_result['unnamed_in_path']} unnamed liq cluster(s) in path"
                f" — grade reduced"
            )

        if liq_result.get("target_adjusted"):
            signal.notes += (
                f" | Target adjusted to {target_100:.2f} "
                f"(named level {liq_result['target_adj_reason']} just beyond TP)"
            )

        if macro.is_fed_day:
            self._add_confluence(
                signal, f"Fed day: {macro.fed_event_name} (+confluence)"
            )
            signal.conviction += FED_DAY_ORB_BOOST

        signal.conviction += regime.conviction * 0.7

        if len(signal.confluence_factors) < 2:
            logger.info("ORB: insufficient confluence — no signal")
            return None

        # ── Strike selection ──────────────────────────────────────────────────
        target_strike = orb.target_strike
        if liq_result.get("target_adjusted"):
            from utils.math_utils import round_to_strike
            from config import STRIKE_INCREMENT
            target_strike = round_to_strike(target_100, STRIKE_INCREMENT)

        contract = get_chain_fetcher().select_orb_strike(
            chain, direction, target_strike
        )
        if contract is None:
            logger.warning("ORB: no valid option contract found")
            return None

        signal.strike        = contract.strike
        signal.expiry        = contract.expiry
        signal.entry_premium = contract.mark
        signal.contract      = contract

        if signal.entry_premium <= 0:
            logger.warning("ORB: option has zero premium — skipping")
            return None

        logger.info(
            f"🎯 ORB SIGNAL {direction.upper()}: "
            f"underlying={current_price:.2f} "
            f"orb={orb.orb_low:.2f}–{orb.orb_high:.2f} "
            f"width={orb.orb_width:.2f} "
            f"option={option_side.upper()} {contract.strike} "
            f"mark=${contract.mark:.2f} delta={contract.delta:.3f} "
            f"stop={orb.stop_level:.2f} target={target_100:.2f} "
            f"break_is_named={liq_result['break_is_named_level']} "
            f"path_clear={liq_result['path_clear']} "
            f"target_adjusted={liq_result.get('target_adjusted', False)} "
            f"fed_day={macro.is_fed_day} "
            f"confluence={signal.confluence_factors}"
        )
        return signal

    # ─── Liquidity Analysis ───────────────────────────────────────────────────

    def _analyze_liquidity(self, orb, liq_map, current_price,
                            direction, break_level) -> dict:
        result = {
            "break_is_named_level": False,
            "break_level_name":     "",
            "block":                False,
            "block_reason":         "",
            "path_clear":           True,
            "named_in_path":        0,
            "unnamed_in_path":      0,
            "target_adjusted":      False,
            "adjusted_target":      orb.target_100pct,
            "target_adj_reason":    "",
        }

        orb_width  = orb.orb_width
        target_100 = orb.target_100pct
        target_50  = orb.target_50pct

        for pool in liq_map.pools:
            if pool.swept:
                continue

            pool_price = pool.price
            is_named   = pool.is_named
            pool_name  = pool.name or "unnamed"

            prox = abs(pool_price - break_level) / max(break_level, 1)
            if is_named and prox <= BREAK_LEVEL_PROXIMITY_PCT:
                result["break_is_named_level"] = True
                result["break_level_name"]     = pool_name
                continue

            is_obstacle_kind = (
                (direction == "long"  and pool.kind == "high") or
                (direction == "short" and pool.kind == "low")
            )
            if not is_obstacle_kind:
                continue

            in_danger_zone = (
                (direction == "long"  and current_price < pool_price < target_50) or
                (direction == "short" and target_50 < pool_price < current_price)
            )
            if in_danger_zone and is_named:
                result["named_in_path"] += 1
                result["path_clear"]     = False

            in_full_path = (
                (direction == "long"  and current_price < pool_price < target_100) or
                (direction == "short" and target_100 < pool_price < current_price)
            )
            if in_full_path and not is_named:
                result["unnamed_in_path"] += 1
                result["path_clear"]       = False

            adj_zone_long  = (direction == "long"  and
                              target_100 < pool_price < target_100 + orb_width * BEYOND_TP_ADJUSTMENT_WIDTHS)
            adj_zone_short = (direction == "short" and
                              target_100 - orb_width * BEYOND_TP_ADJUSTMENT_WIDTHS < pool_price < target_100)

            if is_named and (adj_zone_long or adj_zone_short) and not result["target_adjusted"]:
                result["target_adjusted"]   = True
                result["adjusted_target"]   = pool_price
                result["target_adj_reason"] = pool_name

        if result["named_in_path"] > 0 and not result["break_is_named_level"]:
            result["block"]        = True
            result["block_reason"] = (
                f"Named pool in fakeout zone (entry→50%TP): "
                f"{result['named_in_path']} named level(s)."
            )

        return result
