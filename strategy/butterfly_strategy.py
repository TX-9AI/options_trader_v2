"""
strategy/butterfly_strategy.py — Debit butterfly for RANGING/COMPRESSION regimes.
v1.0 — original release
v1.1 — 2026-06-29 — GEX pin center strike, fixed wings by instrument,
        noon-2PM entry window, one-per-session limit, TP at 20%

Entry logic:
  - Only fires between 12:00 PM and 2:00 PM ET
  - Only one butterfly per RTH session
  - Requires GEX environment to be PINNING
  - Price must be within 1× expected move of GEX pin strike
  - Center strike = GEX pin strike (not ATM)
  - Wings: 25 points on SPX, $5 on QQQ/SPY (fixed, not ATR-based)
  - Direction (call vs put) based on VWAP bias

Exit logic:
  - TP: 20% of max profit
  - SL: 25% of net debit
  - Hard exit: 2:00 PM ET (BUTTERFLY_ENTRY_CUTOFF_ET used as hard close)
  - Max hold: 2.5 hours
"""

import logging
import math
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from strategy.base_strategy import BaseOptionsStrategy, OptionsSignal
from analysis.regime_classifier import RegimeState, Regime
from analysis.volatility_engine import VolatilityState
from analysis.liquidity_mapper import LiquidityMap
from data.options_chain import OptionsChain, OptionContract
from data.options_chain import get_chain_fetcher
from data.macro_data import MacroSnapshot
from config import (
    BUTTERFLY_TP_PCT, BUTTERFLY_WING_SPX, BUTTERFLY_WING_QQQ,
    BUTTERFLY_GEX_PIN_PROXIMITY_MULT,
    BUTTERFLY_ENTRY_START_ET, BUTTERFLY_ENTRY_CUTOFF_ET,
    STRIKE_INCREMENT, INSTRUMENT, VIX_BUTTERFLY_DISABLE,
    CONTRACT_MULTIPLIER
)

logger = logging.getLogger(__name__)
ET = ZoneInfo("US/Eastern")


class ButterflyStrategy(BaseOptionsStrategy):
    """
    Debit butterfly strategy — GEX pin centered, noon-2PM window, one per session.
    """

    def __init__(self):
        self._fired_today: bool = False
        self._last_reset_date: Optional[str] = None

    @property
    def name(self) -> str:
        return "ButterflyStrategy"

    def _reset_if_new_day(self):
        """Reset one-per-session flag at start of each RTH day."""
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if self._last_reset_date != today:
            self._fired_today    = False
            self._last_reset_date = today

    def _expected_move(self, underlying: float, vix: float) -> float:
        """
        1× expected move for remaining session time.
        Formula: underlying × VIX% × sqrt(hours_remaining / 6.5) / sqrt(252)
        Called at entry time to compute the proximity threshold dynamically.
        """
        now_et        = datetime.now(ET)
        close_et      = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        hours_remaining = max((close_et - now_et).total_seconds() / 3600, 0.5)
        return (
            underlying
            * (vix / 100)
            * math.sqrt(hours_remaining / 6.5)
            / math.sqrt(252)
        ) * BUTTERFLY_GEX_PIN_PROXIMITY_MULT

    def _wing_width(self) -> int:
        """Fixed wing width in strike increments by instrument."""
        if INSTRUMENT == "SPX":
            return BUTTERFLY_WING_SPX // STRIKE_INCREMENT   # 25pt / 5pt = 5 increments
        return BUTTERFLY_WING_QQQ // STRIKE_INCREMENT       # $5 / $1 = 5 increments

    def generate_signal(self,
                         regime: RegimeState,
                         vol_state: VolatilityState,
                         liq_map: LiquidityMap,
                         chain: OptionsChain,
                         macro: MacroSnapshot,
                         current_price: float,
                         gex=None) -> Optional[OptionsSignal]:
        """
        Generate a butterfly signal when all conditions are met.
        """
        self._reset_if_new_day()

        now_et = datetime.now(ET)
        hm     = (now_et.hour, now_et.minute)

        # ── Gate 1: Entry time window 12:00 PM – 2:00 PM ET ──────────────────
        if hm < BUTTERFLY_ENTRY_START_ET:
            logger.debug(f"Butterfly: too early ({now_et.strftime('%H:%M')} ET — window opens at 12:00)")
            return None
        if hm >= BUTTERFLY_ENTRY_CUTOFF_ET:
            logger.debug(f"Butterfly: past cutoff ({now_et.strftime('%H:%M')} ET)")
            return None

        # ── Gate 2: One per session ───────────────────────────────────────────
        if self._fired_today:
            logger.debug("Butterfly: already fired today — one per session limit")
            return None

        # ── Gate 3: Regime ────────────────────────────────────────────────────
        if regime.primary_regime not in (Regime.RANGING, Regime.COMPRESSION):
            return None

        # ── Gate 4: VIX and Fed day ───────────────────────────────────────────
        if not macro.butterfly_allowed:
            logger.info(f"Butterfly blocked: VIX={macro.vix:.1f} or Fed day={macro.is_fed_day}")
            return None

        # ── Gate 5: GEX must be PINNING ───────────────────────────────────────
        if gex is None or gex.gex_environment != "PINNING":
            logger.info("Butterfly: GEX not PINNING — no edge without pin")
            return None

        pin_strike = gex.pin_strike
        if not pin_strike:
            logger.info("Butterfly: no pin strike available")
            return None

        # ── Gate 6: Price within 1× expected move of pin ──────────────────────
        proximity_threshold = self._expected_move(current_price, macro.vix)
        distance_from_pin   = abs(current_price - pin_strike)

        if distance_from_pin > proximity_threshold:
            logger.info(
                f"Butterfly: price ${current_price:.2f} too far from pin ${pin_strike} "
                f"(distance=${distance_from_pin:.2f} > threshold=${proximity_threshold:.2f}) — skip"
            )
            return None

        # ── Strike selection: center = GEX pin strike ─────────────────────────
        center_strike = pin_strike
        wing_increments = self._wing_width()
        lower_strike  = center_strike - wing_increments * STRIKE_INCREMENT
        upper_strike  = center_strike + wing_increments * STRIKE_INCREMENT

        # ── Direction: call vs put based on VWAP ─────────────────────────────
        direction = self._pick_direction(vol_state, liq_map, current_price)

        # ── Fetch contracts from chain ────────────────────────────────────────
        contracts_list = chain.calls if direction == "call" else chain.puts

        def find_strike(target: float) -> Optional[OptionContract]:
            candidates = [c for c in contracts_list if c.strike == target and c.mark > 0]
            if candidates:
                return candidates[0]
            # Nearest liquid strike
            liquid = [c for c in contracts_list if c.mark > 0]
            if not liquid:
                return None
            return min(liquid, key=lambda c: abs(c.strike - target))

        lower  = find_strike(lower_strike)
        center = find_strike(center_strike)
        upper  = find_strike(upper_strike)

        if not all([lower, center, upper]):
            logger.warning(
                f"Butterfly: could not find all strikes "
                f"{lower_strike}/{center_strike}/{upper_strike}"
            )
            return None

        # ── Net debit and max profit ──────────────────────────────────────────
        net_debit  = lower.mark + upper.mark - 2 * center.mark
        if net_debit <= 0:
            logger.info(f"Butterfly: net debit ≤ 0 ({net_debit:.4f}) — skip")
            return None

        wing_width = upper.strike - center.strike
        max_profit = wing_width - net_debit
        if max_profit <= 0:
            logger.info(
                f"Butterfly: no max profit potential "
                f"(wing={wing_width:.0f} debit={net_debit:.2f})"
            )
            return None

        # ── Build signal ──────────────────────────────────────────────────────
        signal = OptionsSignal(
            strategy_name       = self.name,
            setup_type          = f"Debit {direction.title()} Butterfly",
            direction           = "neutral",
            option_side         = direction,
            is_butterfly        = True,
            butterfly_direction = direction,
            lower_contract      = lower,
            center_contract     = center,
            upper_contract      = upper,
            net_debit           = net_debit,
            max_profit          = max_profit,
            underlying_entry    = current_price,
            underlying_stop     = 0.0,
            underlying_target   = center_strike,
            regime              = regime.primary_regime,
            vix_at_signal       = macro.vix,
            is_fed_day          = macro.is_fed_day,
            stop_loss_pct       = 0.25,
            tp_pct              = BUTTERFLY_TP_PCT,   # 20%
        )

        if macro.butterfly_half_size:
            signal.notes = "VIX 15–20: half size butterfly"

        # ── Confluence ────────────────────────────────────────────────────────
        self._add_confluence(signal, f"Regime: {regime.primary_regime}")
        self._add_confluence(signal, f"GEX pin @ {pin_strike} ({distance_from_pin:.1f}pts away)")
        if regime.adx < 20:
            self._add_confluence(signal, f"Low ADX ({regime.adx:.1f}) — no trend")
        if direction == "call" and vol_state.price_vs_vwap == "ABOVE":
            self._add_confluence(signal, "Above VWAP — bullish lean")
        elif direction == "put" and vol_state.price_vs_vwap == "BELOW":
            self._add_confluence(signal, "Below VWAP — bearish lean")

        signal.conviction = regime.conviction * 0.7

        # ── Mark as fired for today ───────────────────────────────────────────
        self._fired_today = True

        logger.info(
            f"🦋 BUTTERFLY {direction.upper()}: "
            f"strikes={lower.strike}/{center.strike}/{upper.strike} "
            f"center=GEX_PIN@{pin_strike} "
            f"distance=${distance_from_pin:.1f} threshold=${proximity_threshold:.1f} "
            f"net_debit=${net_debit:.2f} max_profit=${max_profit:.2f} "
            f"TP=${max_profit * BUTTERFLY_TP_PCT:.2f} (20%) "
            f"SL=${net_debit * 0.75:.2f} (25% loss) "
            f"VIX={macro.vix:.1f}"
        )
        return signal

    def _pick_direction(self, vol_state: VolatilityState,
                         liq_map: LiquidityMap,
                         current_price: float) -> str:
        if vol_state.vwap > 0:
            if vol_state.price_vs_vwap == "ABOVE":
                return "call"
            elif vol_state.price_vs_vwap == "BELOW":
                return "put"
        if liq_map.recent_sweep:
            sweep = liq_map.recent_sweep
            if sweep.kind == "low_sweep":
                return "call"
            elif sweep.kind == "high_sweep":
                return "put"
        return "call"
