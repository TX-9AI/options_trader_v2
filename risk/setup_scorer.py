"""
risk/setup_scorer.py — Scores and grades options trade signals A/B/C.
Ported from crypto_trader with options-specific strategy profiles.
Grade determines position size multiplier: A=1.5×, B=1.0×, C=0.5×.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from strategy.base_strategy import OptionsSignal
from analysis.regime_classifier import RegimeState, Regime
from analysis.volatility_engine import VolatilityState
from analysis.structure_analyzer import StructureMap
from analysis.liquidity_mapper import LiquidityMap
from data.macro_data import MacroSnapshot
from config import GRADE_SIZE_MULTIPLIER, GRADE_A_MIN_SCORE, GRADE_B_MIN_SCORE
from utils.time_utils import current_session_label

logger = logging.getLogger(__name__)


@dataclass
class SetupScore:
    grade:           str   = "C"
    score:           float = 0.0
    size_multiplier: float = 0.5
    breakdown:       dict  = None

    def __post_init__(self):
        if self.breakdown is None:
            self.breakdown = {}


# ─── Strategy-specific scoring profiles ──────────────────────────────────────

STRATEGY_PROFILES = {
    "ORBStrategy": {
        "score_weights": {
            "regime_conviction":    0.20,
            "orb_quality":          0.30,   # Break clarity, retest quality
            "vwap_alignment":       0.15,
            "liquidity_clear":      0.20,
            "macro_context":        0.15,   # Fed day is a boost here
        },
        "grade_a": 0.78,
        "grade_b": 0.55,
    },
    "SweepReversal": {
        "score_weights": {
            "regime_conviction":    0.25,
            "sweep_quality":        0.35,   # Rejection %, freshness, named level
            "vwap_alignment":       0.10,
            "liquidity_clear":      0.20,
            "macro_context":        0.10,
        },
        "grade_a": 0.75,
        "grade_b": 0.52,
    },
    "ButterflyStrategy": {
        "score_weights": {
            "regime_conviction":    0.30,   # Need clean ranging regime
            "range_quality":        0.35,   # BB width, ADX, time in range
            "vwap_alignment":       0.15,
            "liquidity_clear":      0.10,
            "macro_context":        0.10,
        },
        "grade_a": 0.75,
        "grade_b": 0.52,
    },
    "default": {
        "score_weights": {
            "regime_conviction":    0.30,
            "signal_quality":       0.25,
            "vwap_alignment":       0.15,
            "liquidity_clear":      0.20,
            "macro_context":        0.10,
        },
        "grade_a": 0.78,
        "grade_b": 0.55,
    },
}


class SetupScorer:
    """
    Scores an options signal using strategy-specific weights.
    """

    def score(self,
              signal:    OptionsSignal,
              regime:    RegimeState,
              vol_state: VolatilityState,
              structure: StructureMap,
              liq_map:   LiquidityMap,
              macro:     Optional[MacroSnapshot] = None) -> SetupScore:

        breakdown = {}
        name      = signal.strategy_name
        profile   = STRATEGY_PROFILES.get(name, STRATEGY_PROFILES["default"])
        weights   = profile["score_weights"]
        grade_a   = profile["grade_a"]
        grade_b   = profile["grade_b"]

        # ── 1. Regime Conviction ──────────────────────────────────────────────
        reg_score = regime.conviction
        breakdown["regime_conviction"] = round(reg_score, 3)

        # ── 2. Strategy-specific quality score ───────────────────────────────
        if name == "ORBStrategy":
            quality_score = self._orb_quality(signal, regime, vol_state)
            breakdown["orb_quality"] = round(quality_score, 3)
        elif name == "SweepReversal":
            quality_score = self._sweep_quality(signal, liq_map, regime)
            breakdown["sweep_quality"] = round(quality_score, 3)
        elif name == "ButterflyStrategy":
            quality_score = self._range_quality(regime, vol_state)
            breakdown["range_quality"] = round(quality_score, 3)
        else:
            quality_score = signal.conviction
            breakdown["signal_quality"] = round(quality_score, 3)

        # ── 3. VWAP alignment ─────────────────────────────────────────────────
        vwap_score = 0.5
        if vol_state.vwap > 0:
            if signal.direction == "long" and vol_state.price_vs_vwap == "ABOVE":
                vwap_score = 1.0
            elif signal.direction == "short" and vol_state.price_vs_vwap == "BELOW":
                vwap_score = 1.0
            elif signal.direction == "neutral":
                vwap_score = 0.7   # Butterfly — VWAP matters less
            else:
                vwap_score = 0.25
        breakdown["vwap_alignment"] = round(vwap_score, 3)

        # ── 4. Liquidity path clear ───────────────────────────────────────────
        liq_score = 1.0
        if not signal.is_butterfly:
            pools_blocking = [
                p for p in liq_map.pools
                if not p.swept and (
                    (signal.direction == "long"  and p.kind == "high" and
                     signal.underlying_entry < p.price < signal.underlying_target) or
                    (signal.direction == "short" and p.kind == "low" and
                     signal.underlying_target < p.price < signal.underlying_entry)
                )
            ]
            liq_score -= len(pools_blocking) * 0.25
            liq_score  = max(liq_score, 0.0)
        breakdown["liquidity_clear"] = round(liq_score, 3)

        # ── 5. Macro context ──────────────────────────────────────────────────
        macro_score = 0.5
        if macro:
            if macro.is_fed_day and name == "ORBStrategy":
                macro_score = 1.0   # Fed day boosts ORB
            elif macro.vix_regime == "LOW":
                macro_score = 0.8
            elif macro.vix_regime == "ELEVATED":
                macro_score = 0.3
            elif macro.vix_regime == "CRISIS":
                macro_score = 0.0
            elif macro.vix_regime == "NORMAL":
                macro_score = 0.6
        breakdown["macro_context"] = round(macro_score, 3)

        # ── Weighted total ────────────────────────────────────────────────────
        # Map each weight key to its corresponding breakdown value
        total = 0.0
        for dim, w in weights.items():
            val = breakdown.get(dim, 0.5)
            total += val * w

        # Session time modifier — penalize late-session entries
        session = current_session_label()
        if session == "late_session":
            total *= 0.85

        # ── Grade ─────────────────────────────────────────────────────────────
        if total >= grade_a:
            grade = "A"
        elif total >= grade_b:
            grade = "B"
        else:
            grade = "C"

        multiplier = GRADE_SIZE_MULTIPLIER[grade]

        result = SetupScore(
            grade=grade,
            score=round(total, 3),
            size_multiplier=multiplier,
            breakdown=breakdown
        )

        logger.info(
            f"Setup grade: {grade} score={total:.2f} "
            f"strategy={name} mult={multiplier}x "
            f"breakdown={breakdown}"
        )
        return result

    def _orb_quality(self, signal: OptionsSignal,
                      regime: RegimeState,
                      vol_state: VolatilityState) -> float:
        """
        ORB quality: confluence count, regime alignment, liquidity context.

        Grade penalties:
          - Unnamed clusters in path (flagged in signal.notes): -0.15 each
            → results in grade dropping one letter at the boundary
          - Named level IS the break catalyst: +0.20 (Rule 1 fired)
        """
        base = min(len(signal.confluence_factors) * 0.2, 0.8)

        if signal.is_fed_day:
            base = min(base + 0.2, 1.0)

        # Rule 1 boost: break through named level is the highest-quality ORB
        if "named level" in " ".join(signal.confluence_factors).lower() and \
           "sweep catalyst" in " ".join(signal.confluence_factors).lower():
            base = min(base + 0.20, 1.0)

        # Unnamed clusters in path (Rule 2 partial — not blocked, but penalized)
        notes = signal.notes or ""
        if "unnamed liq cluster" in notes:
            try:
                n = int(notes.split("unnamed liq cluster")[0].strip().split()[-1])
            except (ValueError, IndexError):
                n = 1
            base = max(base - 0.15 * n, 0.0)

        return base

    def _sweep_quality(self, signal: OptionsSignal,
                        liq_map: LiquidityMap,
                        regime: RegimeState) -> float:
        """Sweep quality: rejection %, freshness, named level."""
        if not liq_map.recent_sweep:
            return 0.3
        sweep = liq_map.recent_sweep
        rejection_score = min(sweep.rejection_pct / 0.01, 1.0)
        age_score       = max(0, 1 - (liq_map.sweep_age_bars / 8))
        named_bonus     = 0.15 if sweep.swept_named_level else 0.0
        return min(rejection_score * 0.45 + age_score * 0.4 + named_bonus, 1.0)

    def _range_quality(self, regime: RegimeState,
                        vol_state: VolatilityState) -> float:
        """Ranging quality: low ADX, BB squeeze, stable ATR."""
        adx_score = max(0, 1 - regime.adx / 25)
        bb_score  = max(0, 1 - vol_state.bb_width_pct * 3)
        vol_score = 1.0 if vol_state.atr_state in ("STABLE", "CONTRACTING") else 0.5
        return adx_score * 0.4 + bb_score * 0.4 + vol_score * 0.2


# Singleton
_scorer: Optional[SetupScorer] = None


def get_setup_scorer() -> SetupScorer:
    global _scorer
    if _scorer is None:
        _scorer = SetupScorer()
    return _scorer
