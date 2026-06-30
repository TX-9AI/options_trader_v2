"""
strategy/sweep_reversal_strategy.py — Post-liquidity-sweep reversal for options.

Ported from crypto_trader SweepReversalStrategy and adapted for 0DTE options:
- Same sweep detection (PDH/PDL, equal H/L, session H/L)
- Same 1-min BOS confirmation in reversal direction
- Strike selection: closest to 0.20 delta OTM in reversal direction
- Naked long call (low sweep → long) or naked long put (high sweep → short)
- Stop: 25% of premium paid
- TP: 100% of premium at first target; trailing stop at 50%
"""

import logging
from typing import Optional

from strategy.base_strategy import BaseOptionsStrategy, OptionsSignal
from analysis.regime_classifier import RegimeState, Regime
from analysis.volatility_engine import VolatilityState
from analysis.structure_analyzer import StructureMap
from analysis.liquidity_mapper import LiquidityMap, LiquiditySweep
from data.options_chain import OptionsChain
from data.options_chain import get_chain_fetcher
from data.macro_data import MacroSnapshot
from config import SWEEP_TARGET_DELTA, SWEEP_MAX_AGE_BARS
import pandas as pd

logger = logging.getLogger(__name__)


class SweepReversalStrategy(BaseOptionsStrategy):
    """
    After a confirmed liquidity sweep with BOS confirmation,
    buy a naked OTM option (~0.20 delta) in the reversal direction.

    Long reversal (lows swept):   buy OTM call
    Short reversal (highs swept): buy OTM put
    """

    @property
    def name(self) -> str:
        return "SweepReversal"

    def generate_signal(self,
                         regime: RegimeState,
                         vol_state: VolatilityState,
                         structure: StructureMap,
                         liq_map: LiquidityMap,
                         chain: OptionsChain,
                         macro: MacroSnapshot,
                         df_1m: Optional[pd.DataFrame],
                         current_price: float) -> Optional[OptionsSignal]:
        """
        Generate a sweep reversal options signal.

        Args:
            regime:         Current regime (must be SWEEP_REVERSAL)
            vol_state:      Volatility state
            structure:      Market structure (support/resistance)
            liq_map:        Liquidity map with recent sweep
            chain:          0DTE options chain
            macro:          Macro snapshot
            df_1m:          1-min candles for BOS confirmation
            current_price:  Current underlying price

        Returns:
            OptionsSignal or None
        """
        if regime.primary_regime != Regime.SWEEP_REVERSAL:
            return None

        sweep = liq_map.recent_sweep
        if not sweep or not sweep.confirmed:
            return None

        if liq_map.sweep_age_bars > SWEEP_MAX_AGE_BARS:
            logger.debug(f"Sweep too old: {liq_map.sweep_age_bars} bars")
            return None

        # Determine reversal direction from sweep type
        if sweep.kind == "low_sweep":
            return self._long_reversal(
                sweep, regime, vol_state, structure, liq_map,
                chain, macro, df_1m, current_price
            )
        elif sweep.kind == "high_sweep":
            return self._short_reversal(
                sweep, regime, vol_state, structure, liq_map,
                chain, macro, df_1m, current_price
            )
        return None

    def _long_reversal(self, sweep: LiquiditySweep,
                        regime: RegimeState,
                        vol_state: VolatilityState,
                        structure: StructureMap,
                        liq_map: LiquidityMap,
                        chain: OptionsChain,
                        macro: MacroSnapshot,
                        df_1m: Optional[pd.DataFrame],
                        current_price: float) -> Optional[OptionsSignal]:
        """Low swept → buy OTM call."""

        # Price must have recovered above the swept level
        if current_price <= sweep.pool_price:
            logger.debug("Sweep long: price not recovered above swept level")
            return None

        # Don't enter too far from the sweep (>2% away = missed it)
        recovery_pct = (current_price - sweep.sweep_price) / max(sweep.sweep_price, 1)
        if recovery_pct > 0.02:
            logger.debug(f"Sweep long: too far from sweep ({recovery_pct:.1%})")
            return None

        # BOS confirmation: 1m candle structure shows bullish shift
        if not self._confirm_bos(df_1m, "long", current_price):
            logger.debug("Sweep long: no 1m BOS confirmation")
            return None

        signal = OptionsSignal(
            strategy_name    = self.name,
            setup_type       = "Sweep Reversal Long (low sweep → call)",
            direction        = "long",
            option_side      = "call",
            underlying_entry = current_price,
            underlying_stop  = sweep.sweep_price * 0.999,  # Just below sweep extreme
            underlying_target = current_price + (current_price - sweep.sweep_price) * 1.5,
            underlying_tp50  = current_price + (current_price - sweep.sweep_price) * 0.75,
            regime           = regime.primary_regime,
            vix_at_signal    = macro.vix,
            is_fed_day       = macro.is_fed_day,
            stop_loss_pct    = 0.25,
            tp_pct           = 1.0,
        )

        # ── Confluence ────────────────────────────────────────────────────────
        self._add_confluence(signal,
            f"Low sweep confirmed ({sweep.rejection_pct:.1%} rejection)"
        )
        if liq_map.sweep_age_bars <= 3:
            self._add_confluence(signal, "Fresh sweep (≤3 bars)")
        elif liq_map.sweep_age_bars <= 6:
            self._add_confluence(signal, "Recent sweep (≤6 bars)")

        if vol_state.vwap > 0 and current_price > vol_state.vwap:
            self._add_confluence(signal, "Recovered above VWAP")

        if sweep.swept_named_level:
            self._add_confluence(signal, f"Named level swept: {sweep.swept_named_level}")
        elif liq_map.prev_day_low and abs(sweep.pool_price - liq_map.prev_day_low) / max(sweep.pool_price, 1) < 0.003:
            self._add_confluence(signal, "PDL swept")

        if structure.nearest_support and abs(current_price - structure.nearest_support) / current_price < 0.005:
            self._add_confluence(signal, "At structure support")

        if regime.conviction >= 0.65:
            self._add_confluence(signal, f"High conviction ({regime.conviction:.0%})")

        if len(signal.confluence_factors) < 2:
            logger.debug("Sweep long: insufficient confluence")
            return None

        signal.conviction = regime.conviction

        # ── Strike selection: 0.20 delta OTM call ────────────────────────────
        contract = get_chain_fetcher().select_sweep_strike(chain, "long", SWEEP_TARGET_DELTA)
        if contract is None:
            logger.warning("Sweep long: no suitable OTM call found")
            return None

        signal.strike        = contract.strike
        signal.expiry        = contract.expiry
        signal.entry_premium = contract.mark
        signal.contract      = contract

        logger.info(
            f"🔥 SWEEP REVERSAL LONG: "
            f"price={current_price:.2f} "
            f"pool={sweep.pool_price:.2f} swept_to={sweep.sweep_price:.2f} "
            f"call_strike={contract.strike} mark=${contract.mark:.2f} "
            f"delta={contract.delta:.3f} "
            f"confluence={signal.confluence_factors}"
        )
        return signal

    def _short_reversal(self, sweep: LiquiditySweep,
                         regime: RegimeState,
                         vol_state: VolatilityState,
                         structure: StructureMap,
                         liq_map: LiquidityMap,
                         chain: OptionsChain,
                         macro: MacroSnapshot,
                         df_1m: Optional[pd.DataFrame],
                         current_price: float) -> Optional[OptionsSignal]:
        """High swept → buy OTM put."""

        if current_price >= sweep.pool_price:
            logger.debug("Sweep short: price not rejected below swept level")
            return None

        recovery_pct = (sweep.sweep_price - current_price) / max(sweep.sweep_price, 1)
        if recovery_pct > 0.02:
            logger.debug(f"Sweep short: too far from sweep ({recovery_pct:.1%})")
            return None

        if not self._confirm_bos(df_1m, "short", current_price):
            logger.debug("Sweep short: no 1m BOS confirmation")
            return None

        signal = OptionsSignal(
            strategy_name    = self.name,
            setup_type       = "Sweep Reversal Short (high sweep → put)",
            direction        = "short",
            option_side      = "put",
            underlying_entry = current_price,
            underlying_stop  = sweep.sweep_price * 1.001,
            underlying_target = current_price - (sweep.sweep_price - current_price) * 1.5,
            underlying_tp50  = current_price - (sweep.sweep_price - current_price) * 0.75,
            regime           = regime.primary_regime,
            vix_at_signal    = macro.vix,
            is_fed_day       = macro.is_fed_day,
            stop_loss_pct    = 0.25,
            tp_pct           = 1.0,
        )

        # ── Confluence ────────────────────────────────────────────────────────
        self._add_confluence(signal,
            f"High sweep confirmed ({sweep.rejection_pct:.1%} rejection)"
        )
        if liq_map.sweep_age_bars <= 3:
            self._add_confluence(signal, "Fresh sweep (≤3 bars)")
        elif liq_map.sweep_age_bars <= 6:
            self._add_confluence(signal, "Recent sweep (≤6 bars)")

        if vol_state.vwap > 0 and current_price < vol_state.vwap:
            self._add_confluence(signal, "Rejected below VWAP")

        if sweep.swept_named_level:
            self._add_confluence(signal, f"Named level swept: {sweep.swept_named_level}")
        elif liq_map.prev_day_high and abs(sweep.pool_price - liq_map.prev_day_high) / max(sweep.pool_price, 1) < 0.003:
            self._add_confluence(signal, "PDH swept")

        if structure.nearest_resistance and abs(current_price - structure.nearest_resistance) / current_price < 0.005:
            self._add_confluence(signal, "At structure resistance")

        if regime.conviction >= 0.65:
            self._add_confluence(signal, f"High conviction ({regime.conviction:.0%})")

        if len(signal.confluence_factors) < 2:
            logger.debug("Sweep short: insufficient confluence")
            return None

        signal.conviction = regime.conviction

        # ── Strike selection: 0.20 delta OTM put ─────────────────────────────
        contract = get_chain_fetcher().select_sweep_strike(chain, "short", SWEEP_TARGET_DELTA)
        if contract is None:
            logger.warning("Sweep short: no suitable OTM put found")
            return None

        signal.strike        = contract.strike
        signal.expiry        = contract.expiry
        signal.entry_premium = contract.mark
        signal.contract      = contract

        logger.info(
            f"🔥 SWEEP REVERSAL SHORT: "
            f"price={current_price:.2f} "
            f"pool={sweep.pool_price:.2f} swept_to={sweep.sweep_price:.2f} "
            f"put_strike={contract.strike} mark=${contract.mark:.2f} "
            f"delta={contract.delta:.3f} "
            f"confluence={signal.confluence_factors}"
        )
        return signal

    def _confirm_bos(self, df_1m: Optional[pd.DataFrame],
                      direction: str, current_price: float) -> bool:
        """
        Confirm 1-min Break of Structure in the reversal direction.
        BOS = price closes above the most recent swing high (long)
              or below the most recent swing low (short)
        Uses closed candles only (iloc[-2] and before).
        """
        if df_1m is None or len(df_1m) < 5:
            return False

        # Look at last 5 closed 1m candles (exclude the current forming candle)
        recent = df_1m.iloc[-6:-1]

        if direction == "long":
            # BOS: current price above the high of any of the last 5 candles
            recent_high = float(recent["high"].max())
            return current_price > recent_high

        else:  # short
            # BOS: current price below the low of any of the last 5 candles
            recent_low = float(recent["low"].min())
            return current_price < recent_low
