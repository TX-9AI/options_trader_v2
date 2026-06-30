"""
data/gex_data.py — GEX (Gamma Exposure) calculator from live options chain.

Computed directly from the TastyTrade chain we already fetch every 15s.
No external API, no scraping, no rate limits.

GEX Formula (per strike):
    call_gex = gamma × open_interest × 100 × spot_price
    put_gex  = gamma × open_interest × 100 × spot_price × -1  (puts flip sign)
    net_gex  = call_gex + put_gex

Derived outputs:
    net_gex_total   — sum across all strikes. Positive = pinning, negative = trending
    call_wall       — strike with highest cumulative call GEX (resistance)
    put_wall        — strike with highest cumulative put GEX (support)
    pin_strike      — strike where net GEX is maximized (strongest magnet)
    flip_strike     — strike where net GEX crosses zero (trending/pinning boundary)
    gex_environment — "PINNING", "TRENDING", or "NEUTRAL"
    orb_bias        — "DAMPENING" or "AMPLIFYING" (for ORB strategy)

Strategy inputs:
    Butterfly:        center on pin_strike, only enter if gex_environment == PINNING
    Sweep Reversal:   confluence boost if sweep occurs at call_wall or put_wall
    ORB:              orb_bias — DAMPENING = require higher conviction,
                                 AMPLIFYING = lower bar, breakouts get fuel
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, List
from data.options_chain import OptionsChain, OptionContract

logger = logging.getLogger(__name__)

# Minimum open interest to include a strike in GEX calculation
MIN_OI_THRESHOLD = 10

# Net GEX threshold (in millions) to classify environment
# Below this magnitude = NEUTRAL
GEX_NEUTRAL_THRESHOLD_M = 1.0   # $1M net GEX


@dataclass
class StrikeGEX:
    """GEX breakdown for a single strike."""
    strike:     float = 0.0
    call_gex:   float = 0.0   # USD
    put_gex:    float = 0.0   # USD (already negative)
    net_gex:    float = 0.0   # call_gex + put_gex
    call_oi:    int   = 0
    put_oi:     int   = 0


@dataclass
class GEXSnapshot:
    """
    Full GEX snapshot computed from the current options chain.
    Refreshed every 15s in sync with chain fetch.
    """
    # Raw totals
    total_call_gex:     float = 0.0       # Sum of all call GEX (USD)
    total_put_gex:      float = 0.0       # Sum of all put GEX (USD, negative)
    net_gex:            float = 0.0       # total_call + total_put

    # Key levels
    call_wall:          float = 0.0       # Strike with highest call GEX
    put_wall:           float = 0.0       # Strike with highest put GEX magnitude
    pin_strike:         float = 0.0       # Strike with highest net GEX (price magnet)
    flip_strike:        float = 0.0       # Strike where net GEX crosses zero

    # Per-strike breakdown (sorted by strike)
    strikes:            List[StrikeGEX] = field(default_factory=list)

    # Environment classification
    gex_environment:    str = "NEUTRAL"   # "PINNING", "TRENDING", "NEUTRAL"
    orb_bias:           str = "NEUTRAL"   # "DAMPENING", "AMPLIFYING", "NEUTRAL"

    # Meta
    spot_price:         float = 0.0
    computed_at:        str   = ""
    valid:              bool  = False      # False if chain data was insufficient

    def summary(self) -> str:
        return (
            f"GEX={self.net_gex/1e6:.1f}M "
            f"env={self.gex_environment} "
            f"pin=${self.pin_strike:.0f} "
            f"call_wall=${self.call_wall:.0f} "
            f"put_wall=${self.put_wall:.0f} "
            f"flip=${self.flip_strike:.0f} "
            f"orb={self.orb_bias}"
        )


def compute_gex(chain: OptionsChain, spot_price: float) -> GEXSnapshot:
    """
    Compute full GEX snapshot from the current options chain.

    Args:
        chain:       Populated OptionsChain from get_chain_fetcher().fetch_chain()
        spot_price:  Current underlying price (for GEX scaling)

    Returns:
        GEXSnapshot with all derived levels and classifications
    """
    from datetime import datetime, timezone
    snapshot = GEXSnapshot(
        spot_price   = spot_price,
        computed_at  = datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
    )

    if not chain or not chain.calls or not chain.puts:
        logger.warning("GEX: empty chain — cannot compute")
        return snapshot

    # ── Build strike map ──────────────────────────────────────────────────────
    strike_map: dict = {}

    for call in chain.calls:
        if call.gamma == 0:
            continue
        s = call.strike
        if s not in strike_map:
            strike_map[s] = StrikeGEX(strike=s)
        # Use OI if available, otherwise gamma×mark proxy
        # Higher gamma + tighter spread = more dealer hedging at that strike
        oi_proxy = call.open_interest if call.open_interest > 0 else max(1, int(1000 * call.gamma / max(call.mark, 0.01)))
        gex = call.gamma * oi_proxy * 100 * spot_price
        strike_map[s].call_gex += gex
        strike_map[s].call_oi  += oi_proxy

    for put in chain.puts:
        if put.gamma == 0:
            continue
        s = put.strike
        if s not in strike_map:
            strike_map[s] = StrikeGEX(strike=s)
        oi_proxy = put.open_interest if put.open_interest > 0 else max(1, int(1000 * put.gamma / max(put.mark, 0.01)))
        gex = put.gamma * oi_proxy * 100 * spot_price * -1
        strike_map[s].put_gex += gex
        strike_map[s].put_oi  += oi_proxy

    if not strike_map:
        logger.warning("GEX: no strikes with gamma/OI data")
        return snapshot

    # ── Net GEX per strike ────────────────────────────────────────────────────
    for sg in strike_map.values():
        sg.net_gex = sg.call_gex + sg.put_gex

    strikes_sorted = sorted(strike_map.values(), key=lambda s: s.strike)
    snapshot.strikes = strikes_sorted

    # ── Aggregate totals ──────────────────────────────────────────────────────
    snapshot.total_call_gex = sum(s.call_gex for s in strikes_sorted)
    snapshot.total_put_gex  = sum(s.put_gex  for s in strikes_sorted)
    snapshot.net_gex        = snapshot.total_call_gex + snapshot.total_put_gex

    # ── Call wall — strike with highest positive call GEX ─────────────────────
    call_wall_strike = max(strikes_sorted, key=lambda s: s.call_gex, default=None)
    if call_wall_strike:
        snapshot.call_wall = call_wall_strike.strike

    # ── Put wall — strike with highest put GEX magnitude ─────────────────────
    put_wall_strike = min(strikes_sorted, key=lambda s: s.put_gex, default=None)
    if put_wall_strike:
        snapshot.put_wall = put_wall_strike.strike

    # ── Pin strike — highest net GEX (strongest price magnet) ────────────────
    pin = max(strikes_sorted, key=lambda s: s.net_gex, default=None)
    if pin:
        snapshot.pin_strike = pin.strike

    # ── Flip strike — where net GEX crosses zero ──────────────────────────────
    # Walk from low to high strikes, find where net GEX goes from negative to positive
    flip = None
    for i in range(1, len(strikes_sorted)):
        prev = strikes_sorted[i - 1]
        curr = strikes_sorted[i]
        if prev.net_gex < 0 and curr.net_gex >= 0:
            # Flip point — take the strike closer to zero
            if abs(prev.net_gex) < abs(curr.net_gex):
                flip = prev.strike
            else:
                flip = curr.strike
            break
    if flip:
        snapshot.flip_strike = flip
    elif strikes_sorted:
        # No clean flip found — use pin strike as proxy
        snapshot.flip_strike = snapshot.pin_strike

    # ── Environment classification ────────────────────────────────────────────
    net_gex_millions = snapshot.net_gex / 1_000_000

    if net_gex_millions > GEX_NEUTRAL_THRESHOLD_M:
        snapshot.gex_environment = "PINNING"
        snapshot.orb_bias        = "DAMPENING"
    elif net_gex_millions < -GEX_NEUTRAL_THRESHOLD_M:
        snapshot.gex_environment = "TRENDING"
        snapshot.orb_bias        = "AMPLIFYING"
    else:
        snapshot.gex_environment = "NEUTRAL"
        snapshot.orb_bias        = "NEUTRAL"

    snapshot.valid = True

    logger.info(f"GEX computed: {snapshot.summary()}")
    return snapshot


def butterfly_gex_grade(snapshot: GEXSnapshot,
                         center_strike: float,
                         tolerance: float = 2.0) -> str:
    """
    Grade a butterfly entry based on GEX confluence.

    Args:
        snapshot:       Current GEX snapshot
        center_strike:  Proposed butterfly center strike
        tolerance:      Max distance from pin strike for Grade A (default $2)

    Returns:
        "A" — pin strike within tolerance AND PINNING environment
        "B" — PINNING environment but pin strike further away
        "C" — NEUTRAL or TRENDING — skip or heavily reduce size
    """
    if not snapshot.valid:
        return "B"   # No GEX data — default to B, don't block

    if snapshot.gex_environment == "TRENDING":
        return "C"   # Dealers short gamma — butterflies will get run over

    pin_distance = abs(center_strike - snapshot.pin_strike)

    if snapshot.gex_environment == "PINNING" and pin_distance <= tolerance:
        return "A"
    elif snapshot.gex_environment == "PINNING":
        return "B"
    else:
        return "B"   # NEUTRAL — allow but don't boost


def sweep_gex_confluence(snapshot: GEXSnapshot,
                          sweep_price: float,
                          tolerance: float = 2.0) -> bool:
    """
    Check if a sweep occurred at a GEX wall — highest conviction reversal.

    Args:
        snapshot:     Current GEX snapshot
        sweep_price:  Price level where the sweep occurred
        tolerance:    Max distance from wall strike

    Returns:
        True if sweep is at call wall or put wall (strong reversal confirmation)
    """
    if not snapshot.valid:
        return False

    at_call_wall = abs(sweep_price - snapshot.call_wall) <= tolerance
    at_put_wall  = abs(sweep_price - snapshot.put_wall)  <= tolerance

    if at_call_wall or at_put_wall:
        wall = "call_wall" if at_call_wall else "put_wall"
        logger.info(
            f"GEX sweep confluence: sweep at ${sweep_price:.2f} "
            f"near {wall} (${snapshot.call_wall if at_call_wall else snapshot.put_wall:.2f})"
        )
        return True
    return False


def best_butterfly_center(snapshot: GEXSnapshot,
                           current_price: float,
                           max_distance: float = 5.0) -> float:
    """
    Return the best center strike for a butterfly.
    Uses GEX pin strike if within max_distance of current price,
    otherwise falls back to current price (ATM).

    Args:
        snapshot:       Current GEX snapshot
        current_price:  Current underlying price
        max_distance:   Max distance pin strike can be from price

    Returns:
        Optimal center strike price
    """
    if not snapshot.valid or snapshot.pin_strike == 0:
        return current_price

    pin_distance = abs(current_price - snapshot.pin_strike)

    if pin_distance <= max_distance:
        logger.info(
            f"GEX pin center: ${snapshot.pin_strike:.0f} "
            f"(price=${current_price:.2f} distance=${pin_distance:.1f})"
        )
        return snapshot.pin_strike

    logger.debug(
        f"GEX pin ${snapshot.pin_strike:.0f} too far from price "
        f"${current_price:.2f} ({pin_distance:.1f} > {max_distance}) — using ATM"
    )
    return current_price