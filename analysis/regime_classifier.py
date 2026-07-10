"""
analysis/regime_classifier.py — Market regime classification.
Ported from crypto_trader v4.2. BtcPersonality removed (equities context).
Added ORB_CONFIRMED as a regime that overlays TRENDING when ORB is confirmed.

v1.3 — 2026-07-09 — COVERAGE FIX: a strongly-trending tape must not fall to
        UNKNOWN. Root cause (regime_log post-mortem, 2026-07-09): during clean,
        held ORB breakouts at ADX 43–50, momentary timeframe de-alignment made
        `aligned_timeframes < 2` kick the tick out of _is_trending; BREAKOUT
        simultaneously failed on a BB re-entry flicker; nothing else matched →
        UNKNOWN → no-trade gate → five high-beta names (AMD, MU, PLTR, AMZN,
        NVDA*) fired ZERO trades on clean breakouts. (*NVDA was a deploy-timing
        casualty, not a classifier miss.)
        FIX: timeframe alignment is CORROBORATION for MARGINAL ADX, not a hard
        gate at any strength. Below ADX_STRONG_SOLO, alignment ≥ 2 is still
        required exactly as before; at/above it, ADX carries the trend on its
        own. The structure-contradiction guard and NEUTRAL-direction rejection
        are unchanged — this widens nothing except the alignment requirement
        for tape that is unambiguously strong. Contained, definitional; the
        persistence (conviction-integrator) redesign subsumes it later.
v1.2 — 2026-07-09 — base-regime definitions corrected + honest abstention.
        (a) RANGING now has POSITIVE conditions (_is_ranging: low ADX, price
            contained INSIDE the bands, not expanding) instead of being the
            silent catch-all "everything else" bucket.
        (b) UNKNOWN is now the TRUE fallback when nothing matches — the
            classifier abstains instead of forcing a RANGING label. UNKNOWN is a
            hard NO-TRADE regime, gated in the strategy dispatch (main.py): when
            regime is UNKNOWN/undefined, NO strategy may fire.
        (c) TRENDING now uses the previously-DEAD `structure` argument: a trend
            must not have CONTRADICTING structure (no LH_LL in a bull, no HH_HL
            in a bear). ADX + alignment established strength; structure confirms
            it's an actual trend and not a structureless spike.
        Definitional (fix-on-principle), not calibration.
v1.1 — 2026-07-08 — SWEEP DEFINITION CORRECTED (location + rejection). A sweep
        is not "a wick past a level." By definition it requires THREE things:
        (1) LOCATION — price at a MAPPED liquidity zone (named pool: PDH/PDL/
            session), within one expected move for the timeframe. Liquidity lives
            at the edges, not in the range interior.
        (2) PENETRATION — price pokes through the zone.
        (3) REJECTION — price is thrown back and HOLDS (reclaimed). Acceptance
            through the level is a BREAKOUT, not a sweep.
        _is_sweep_reversal now enforces all three, so sweep only OUTRANKS
        breakout/ranging when price is AT a mapped zone and rejected it. This
        removes the failure where open-air breakouts (price far above a broken
        level, no reclaim) were stamped SWEEP_REVERSAL and faded. The reclaim/
        location REQUIREMENTS are definitional; the expected-move multiple
        (SWEEP_PROXIMITY_EM) and acceptance count are calibration placeholders.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional
import pandas as pd

from config import ADX_TREND_THRESHOLD, ADX_RANGE_THRESHOLD, REGIME_REASSESS_MINUTES
from analysis.volatility_engine import VolatilityState
from analysis.trend_engine import TrendState
from analysis.structure_analyzer import StructureMap
from analysis.liquidity_mapper import LiquidityMap
from data.macro_data import MacroSnapshot

# ─── Sweep definition calibration (v1.1) ──────────────────────────────────────
# Placeholders, to be tightened as candle-logger sessions accumulate. The reclaim
# and named-zone REQUIREMENTS are definitional (not tunable); these are the knobs.
SWEEP_ACCEPT_CLOSES = 2    # this many closes THROUGH the level ⇒ acceptance ⇒ breakout, not sweep
SWEEP_PROXIMITY_EM  = 1.0  # (follow-up) allowed distance to zone, in expected moves
# ─── Trend coverage calibration (v1.3) ────────────────────────────────────────
ADX_STRONG_SOLO     = 35   # at/above this ADX, trend stands WITHOUT timeframe
                           # alignment (alignment corroborates marginal ADX only);
                           # calibration knob — refine from candle-logger base rates
from utils.time_utils import fmt_et_full

logger = logging.getLogger(__name__)


class Regime:
    TRENDING_BULL      = "TRENDING_BULL"
    TRENDING_BEAR      = "TRENDING_BEAR"
    RANGING            = "RANGING"
    BREAKOUT_VOLATILE  = "BREAKOUT_VOLATILE"
    COMPRESSION        = "COMPRESSION"
    SWEEP_REVERSAL     = "SWEEP_REVERSAL"
    UNKNOWN            = "UNKNOWN"


@dataclass
class RegimeState:
    primary_regime:     str   = Regime.UNKNOWN
    conviction:         float = 0.0
    macro_context:      str   = "NEUTRAL"

    adx:                float = 0.0
    atr_normalized:     float = 0.0
    bb_width_pct:       float = 0.5
    trend_direction:    str   = "NEUTRAL"
    trend_conviction:   float = 0.0
    structure_sequence: str   = "NEUTRAL"
    sweep_recent:       bool  = False
    sweep_age_bars:     int   = 999
    vix_regime:         str   = "UNKNOWN"

    timeframe_alignment: Dict[str, str] = field(default_factory=dict)

    classified_at:      str   = ""
    trigger:            str   = "scheduled"
    notes:              str   = ""

    @property
    def is_trending(self) -> bool:
        return self.primary_regime in (Regime.TRENDING_BULL, Regime.TRENDING_BEAR)

    @property
    def is_bullish(self) -> bool:
        return self.primary_regime == Regime.TRENDING_BULL

    @property
    def is_bearish(self) -> bool:
        return self.primary_regime == Regime.TRENDING_BEAR

    @property
    def is_ranging(self) -> bool:
        return self.primary_regime == Regime.RANGING

    @property
    def is_compression(self) -> bool:
        return self.primary_regime == Regime.COMPRESSION

    @property
    def is_sweep_reversal(self) -> bool:
        return self.primary_regime == Regime.SWEEP_REVERSAL

    @property
    def is_breakout(self) -> bool:
        return self.primary_regime == Regime.BREAKOUT_VOLATILE


class RegimeClassifier:
    """
    Decision hierarchy:
    1. SWEEP_REVERSAL  — highest priority
    2. BREAKOUT_VOLATILE
    3. COMPRESSION
    4. TRENDING_BULL/BEAR
    5. RANGING (default)
    """

    def classify(self, vol_state, trend_state, structure, liq_map,
                 macro=None, trigger="scheduled") -> RegimeState:

        state = RegimeState(
            adx=trend_state.primary_adx,
            atr_normalized=vol_state.atr_normalized,
            bb_width_pct=vol_state.bb_width_pct,
            trend_direction=trend_state.overall_direction,
            trend_conviction=trend_state.overall_conviction,
            structure_sequence=structure.structure_sequence,
            sweep_recent=liq_map.recent_sweep is not None,
            sweep_age_bars=liq_map.sweep_age_bars,
            vix_regime=macro.vix_regime if macro else "UNKNOWN",
            macro_context=macro.macro_context if macro else "NEUTRAL",
            classified_at=fmt_et_full(),
            trigger=trigger,
            timeframe_alignment={tf: v.direction for tf, v in trend_state.votes.items()}
        )

        if self._is_sweep_reversal(liq_map, vol_state, trend_state):
            state.primary_regime = Regime.SWEEP_REVERSAL
            state.conviction     = self._sweep_conviction(liq_map, trend_state)
            state.notes          = self._note_sweep(liq_map)
            return self._finalize(state)

        if self._is_breakout(vol_state, structure, trend_state):
            state.primary_regime = Regime.BREAKOUT_VOLATILE
            state.conviction     = self._breakout_conviction(vol_state, trend_state)
            state.notes          = "ATR expanding, price breaking key level"
            return self._finalize(state)

        if self._is_compression(vol_state):
            state.primary_regime = Regime.COMPRESSION
            state.conviction     = self._compression_conviction(vol_state)
            state.notes          = f"BB squeeze at {vol_state.bb_width_pct:.0%} percentile"
            return self._finalize(state)

        if self._is_trending(trend_state, structure):
            state.primary_regime = Regime.TRENDING_BULL if trend_state.is_bullish else Regime.TRENDING_BEAR
            state.conviction     = self._trend_conviction(trend_state, vol_state, macro)
            state.notes          = (
                f"ADX={trend_state.primary_adx:.1f} "
                f"aligned={trend_state.aligned_timeframes}/{trend_state.total_timeframes}"
            )
            return self._finalize(state)

        if self._is_ranging(vol_state, trend_state):
            state.primary_regime = Regime.RANGING
            state.conviction     = self._ranging_conviction(trend_state, vol_state)
            state.notes          = f"ADX={trend_state.primary_adx:.1f}, contained inside bands"
            return self._finalize(state)

        # TRUE fallback: nothing matched cleanly. ABSTAIN — do not force a label.
        # UNKNOWN is a hard NO-TRADE regime (gated in the strategy dispatch); an
        # unclassified tape is not a tradeable edge, so standing aside is correct.
        state.primary_regime = Regime.UNKNOWN
        state.conviction     = 0.0
        state.notes          = (
            f"unclassified — ADX={trend_state.primary_adx:.1f}, "
            f"bb={vol_state.price_vs_bb}, expanding={vol_state.is_expanding} — NO TRADE"
        )
        return self._finalize(state)

    def _is_sweep_reversal(self, liq_map, vol_state, trend_state) -> bool:
        """A sweep requires all three, by definition (see v1.1 header):
           LOCATION (at a mapped zone, within ~1 expected move),
           PENETRATION (poked through), and REJECTION (reclaimed, not accepted).
        Sweep only outranks breakout/ranging when price is AT a mapped edge."""
        sweep = liq_map.recent_sweep
        if not sweep:
            return False

        # (3) REJECTION — must have been thrown back inside and held. Acceptance
        # through the level (closes_beyond >= threshold) is a breakout, not a sweep.
        if not getattr(sweep, "reclaimed", False):
            return False
        if getattr(sweep, "closes_beyond", 0) >= SWEEP_ACCEPT_CLOSES:
            return False

        # (1) LOCATION — the sweep must be of a MAPPED (named) liquidity zone
        # (PDH/PDL/session H/L). Interior chop that pokes some local high/low is
        # NOT a sweep — liquidity lives at the mapped edges, not in the range.
        # This alone removes the QQQ-style interior fires (empty named level).
        # CALIBRATION FOLLOW-UP: additionally require current price within one
        # expected move (atr_normalized × SWEEP_PROXIMITY_EM) of pool_price. That
        # needs current price plumbed into classify() (a separate small change in
        # the caller); the named-zone gate below already enforces the core of the
        # location truth without it.
        if not getattr(sweep, "swept_named_level", ""):
            return False

        # (2) PENETRATION + freshness/strength (calibration placeholders)
        if liq_map.sweep_age_bars <= 8 and sweep.rejection_pct >= 0.003:
            return True
        if (liq_map.sweep_age_bars <= 3 and
                sweep.rejection_pct >= 0.005 and
                trend_state.primary_adx < 50):
            return True
        return False

    def _is_breakout(self, vol_state, structure, trend_state) -> bool:
        if vol_state.is_expanding and vol_state.price_vs_bb != "INSIDE":
            return True
        if (vol_state.atr_state == "EXPANDING" and
                trend_state.primary_adx > ADX_TREND_THRESHOLD and
                structure.structure_sequence in ("HH_HL", "LH_LL")):
            return True
        return False

    def _is_compression(self, vol_state) -> bool:
        return (vol_state.bb_width_pct <= 0.20 and
                vol_state.atr_state in ("CONTRACTING", "STABLE") and
                not vol_state.is_expanding)

    def _is_trending(self, trend_state, structure) -> bool:
        if trend_state.primary_adx < ADX_TREND_THRESHOLD:
            return False
        if trend_state.overall_direction == "NEUTRAL":
            return False
        # v1.3: alignment corroborates MARGINAL ADX; it is not a hard gate at
        # any strength. At ADX ≥ ADX_STRONG_SOLO the tape is unambiguously
        # strong and a momentary timeframe de-alignment must not drop a held
        # trend to UNKNOWN (the BREAKOUT↔UNKNOWN 15s flip-flop of 2026-07-09).
        if (trend_state.primary_adx < ADX_STRONG_SOLO and
                trend_state.aligned_timeframes < 2):
            return False
        # STRUCTURE confirmation (v1.2): ADX + alignment prove strength, but a
        # trend must not carry CONTRADICTING structure — a bull with LH_LL or a
        # bear with HH_HL is a structureless spike, not a trend. (Neutral/absent
        # structure is allowed to pass on ADX+alignment; tightening to an exact
        # match is a calibration option once sessions accumulate.)
        contra = "LH_LL" if trend_state.is_bullish else "HH_HL"
        if structure.structure_sequence == contra:
            return False
        return True

    def _is_ranging(self, vol_state, trend_state) -> bool:
        """A range has POSITIVE conditions (v1.2), it is not 'everything else':
        no trend strength (ADX below the range threshold), price CONTAINED inside
        the Bollinger envelope, and volatility not expanding. If these do not
        hold and nothing else matched, the regime is UNKNOWN (abstain), not a
        forced RANGING label."""
        if trend_state.primary_adx >= ADX_RANGE_THRESHOLD:
            return False                       # too much directional strength for a range
        if vol_state.is_expanding:
            return False                       # expansion is not ranging
        if vol_state.price_vs_bb != "INSIDE":
            return False                       # price outside the envelope isn't contained
        return True

    def _sweep_conviction(self, liq_map, trend_state) -> float:
        sweep = liq_map.recent_sweep
        if not sweep:
            return 0.3
        rejection_score = min(sweep.rejection_pct / 0.01, 1.0)
        age_score       = max(0, 1 - (liq_map.sweep_age_bars / 8))
        return (rejection_score * 0.5 + age_score * 0.5) * 0.9 + 0.1

    def _breakout_conviction(self, vol_state, trend_state) -> float:
        atr_ratio = vol_state.atr_current / max(vol_state.atr_avg_20, 0.001)
        atr_score = min((atr_ratio - 1) / 0.5, 1.0) if atr_ratio > 1 else 0
        tf_score  = trend_state.aligned_timeframes / max(trend_state.total_timeframes, 1)
        return atr_score * 0.5 + tf_score * 0.5

    def _compression_conviction(self, vol_state) -> float:
        return max(0, 1.0 - vol_state.bb_width_pct) * 0.8 + 0.2

    def _trend_conviction(self, trend_state, vol_state, macro) -> float:
        adx_score  = min(trend_state.primary_adx / 50, 1.0)
        tf_score   = trend_state.aligned_timeframes / max(trend_state.total_timeframes, 1)
        macro_mult = 1.1 if (macro and macro.macro_context == "RISK_ON" and trend_state.is_bullish) else 1.0
        base       = adx_score * 0.5 + tf_score * 0.3 + trend_state.overall_conviction * 0.2
        return min(base * macro_mult, 1.0)

    def _ranging_conviction(self, trend_state, vol_state) -> float:
        adx_score = max(0, 1 - trend_state.primary_adx / ADX_RANGE_THRESHOLD)
        vol_score = 1.0 if vol_state.atr_state == "STABLE" else 0.6
        return adx_score * 0.6 + vol_score * 0.4

    def _note_sweep(self, liq_map) -> str:
        if not liq_map.recent_sweep:
            return ""
        s = liq_map.recent_sweep
        return (f"{s.kind} @ {s.pool_price:.2f} "
                f"rejection={s.rejection_pct:.1%} "
                f"{liq_map.sweep_age_bars} bars ago")

    def _finalize(self, state: RegimeState) -> RegimeState:
        state.classified_at = fmt_et_full()
        logger.info(
            f"REGIME: {state.primary_regime} "
            f"conviction={state.conviction:.2f} "
            f"macro={state.macro_context}"
        )
        return state


_classifier: Optional[RegimeClassifier] = None

def get_regime_classifier() -> RegimeClassifier:
    global _classifier
    if _classifier is None:
        _classifier = RegimeClassifier()
    return _classifier
