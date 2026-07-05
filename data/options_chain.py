"""
data/options_chain.py — Options chain data from TastyTrade SDK.
v1.1 — 2026-07-03 — delta-band sweep selection (caller passes a strength-scaled
        target delta); ORB nearest-strike snap breaks toward higher/lower delta.

Chain fetching:  get_option_chain(session, symbol) → dict[date, list[Option]]
Greeks/marks:    DXLinkStreamer subscription for real-time Greeks and quotes

Workflow:
  1. get_option_chain() returns all Option objects (no pricing)
  2. Filter to today's expiry (0DTE)
  3. Subscribe DXLinkStreamer to get Greeks (delta, IV) and Quote (bid/ask/mark)
  4. Build OptionsChain with fully populated OptionContract objects
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional, List, Dict
from decimal import Decimal

from tastytrade.instruments import get_option_chain, Option as TTOption, OptionType
from tastytrade.dxfeed import Greeks, Quote
from tastytrade import DXLinkStreamer

from data.tasty_client import get_session, get_loop, run_async, TastyClientError
from config import (
    INSTRUMENT, STRIKE_INCREMENT, CONTRACT_MULTIPLIER,
    SWEEP_DELTA_TOLERANCE, ORB_STRIKE_DELTA_BIAS
)
from utils.math_utils import round_to_strike, floor_to_strike, ceil_to_strike

logger = logging.getLogger(__name__)


@dataclass
class OptionContract:
    """A single option contract with pricing and greeks."""
    symbol:         str     = ""    # OCC symbol
    underlying:     str     = ""
    expiry:         str     = ""    # YYYY-MM-DD
    option_type:    str     = ""    # "C" or "P"
    strike:         float   = 0.0
    bid:            float   = 0.0
    ask:            float   = 0.0
    mark:           float   = 0.0
    delta:          float   = 0.0
    gamma:          float   = 0.0
    theta:          float   = 0.0
    vega:           float   = 0.0
    iv:             float   = 0.0
    open_interest:  int     = 0
    volume:         int     = 0
    streamer_symbol: str    = ""    # DXFeed streamer symbol


@dataclass
class OptionsChain:
    """Full 0DTE chain snapshot."""
    underlying:     str = ""
    expiry:         str = ""
    spot_price:     float = 0.0
    iv_rank:        float = 0.0
    calls:          List[OptionContract] = field(default_factory=list)
    puts:           List[OptionContract] = field(default_factory=list)


class OptionsChainFetcher:
    """
    Fetches the 0DTE options chain from TastyTrade and populates
    Greeks/marks via DXLinkStreamer.
    """

    def fetch_chain(self, symbol: str = INSTRUMENT,
                    expiry: Optional[str] = None) -> Optional[OptionsChain]:
        """
        Fetch the 0DTE options chain with Greeks and marks.

        Args:
            symbol: Underlying symbol (QQQ, SPY, SPX)
            expiry: YYYY-MM-DD — defaults to today (0DTE)

        Returns:
            OptionsChain or None on failure
        """
        today      = date.today()
        target_date = date.fromisoformat(expiry) if expiry else today
        today_str  = target_date.isoformat()

        try:
            session = get_session()

            # Step 1: Get full option chain (all expirations, no pricing)
            chain_map = run_async(get_option_chain(session, symbol))

            if not chain_map:
                logger.warning(f"Empty option chain for {symbol}")
                return None

            # Step 2: Find today's expiration
            options_today: List[TTOption] = chain_map.get(target_date, [])

            if not options_today:
                # Try to find the nearest available expiry
                available = sorted(chain_map.keys())
                future    = [d for d in available if d >= target_date]
                if future:
                    target_date   = future[0]
                    today_str     = target_date.isoformat()
                    options_today = chain_map[target_date]
                    logger.info(f"No 0DTE for {today}, using nearest: {today_str}")
                else:
                    logger.warning(f"No options expiring on or after {today_str}")
                    return None

            logger.info(f"Found {len(options_today)} options for {symbol} {today_str}")

            # Step 3: Build OptionContract list (no pricing yet)
            calls_raw: List[OptionContract] = []
            puts_raw:  List[OptionContract] = []

            for opt in options_today:
                oc = OptionContract(
                    symbol       = opt.symbol,
                    underlying   = symbol,
                    expiry       = today_str,
                    option_type  = "C" if opt.option_type == OptionType.CALL else "P",
                    strike       = float(opt.strike_price),
                    streamer_symbol = opt.streamer_symbol or "",
                )
                if opt.option_type == OptionType.CALL:
                    calls_raw.append(oc)
                else:
                    puts_raw.append(oc)

            # Step 4: Fetch Greeks and quotes via DXLinkStreamer
            streamer_syms = [
                oc.streamer_symbol for oc in calls_raw + puts_raw
                if oc.streamer_symbol
            ]

            if streamer_syms:
                greeks_map, quote_map = self._fetch_greeks_and_quotes(
                    session, streamer_syms
                )
                self._apply_market_data(calls_raw, greeks_map, quote_map)
                self._apply_market_data(puts_raw,  greeks_map, quote_map)

            # Step 5: Sort by strike
            calls_raw.sort(key=lambda c: c.strike)
            puts_raw.sort(key=lambda c: c.strike)

            chain = OptionsChain(
                underlying = symbol,
                expiry     = today_str,
                calls      = calls_raw,
                puts       = puts_raw,
            )

            # Populate spot_price from ATM call (nearest delta to 0.50)
            liquid_calls = [c for c in chain.calls if c.delta > 0 and c.mark > 0]
            if liquid_calls:
                try:
                    atm = min(liquid_calls, key=lambda c: abs(c.delta - 0.50))
                    chain.spot_price = atm.strike
                except Exception:
                    pass

            logger.info(
                f"Chain built: {symbol} {today_str} "
                f"calls={len(chain.calls)} puts={len(chain.puts)} "
                f"spot~=${chain.spot_price:.0f}"
            )
            return chain

        except Exception as e:
            logger.error(f"Failed to fetch chain for {symbol}: {e}")
            return None

    def _fetch_greeks_and_quotes(
        self,
        session,
        streamer_symbols: List[str],
        timeout: float = 10.0
    ) -> tuple:
        """
        Subscribe to DXLinkStreamer and collect one Greeks + Quote
        event per symbol. Returns (greeks_map, quote_map) dicts
        keyed by streamer_symbol.
        """
        loop = get_loop()
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_fetch_greeks_quotes(session, streamer_symbols, timeout),
                loop
            )
            return future.result(timeout=timeout + 5)
        except Exception as e:
            logger.warning(f"Streamer fetch failed: {e}")
            return {}, {}

    async def _async_fetch_greeks_quotes(
        self,
        session,
        streamer_symbols: List[str],
        timeout: float
    ) -> tuple:
        """Async: open streamer, subscribe, collect one event per symbol."""
        greeks_map: Dict[str, Greeks] = {}
        quote_map:  Dict[str, Quote]  = {}

        try:
            async with DXLinkStreamer(session) as streamer:
                # Subscribe to both Greeks and Quote
                await streamer.subscribe(Greeks, streamer_symbols)
                await streamer.subscribe(Quote,  streamer_symbols)

                needed   = set(streamer_symbols)
                deadline = asyncio.get_event_loop().time() + timeout

                while needed and asyncio.get_event_loop().time() < deadline:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break

                    try:
                        # Try Greeks
                        greek = await asyncio.wait_for(
                            streamer.get_event(Greeks), timeout=0.5
                        )
                        if greek and greek.event_symbol in needed:
                            greeks_map[greek.event_symbol] = greek
                    except asyncio.TimeoutError:
                        pass

                    try:
                        # Try Quote
                        quote = await asyncio.wait_for(
                            streamer.get_event(Quote), timeout=0.5
                        )
                        if quote and quote.event_symbol in needed:
                            quote_map[quote.event_symbol] = quote
                    except asyncio.TimeoutError:
                        pass

                    # Check if we have both for all symbols
                    done = needed & set(greeks_map.keys()) & set(quote_map.keys())
                    needed -= done

        except Exception as e:
            logger.warning(f"DXLinkStreamer error: {e}")

        logger.debug(
            f"Streamer collected greeks={len(greeks_map)} quotes={len(quote_map)}"
        )
        return greeks_map, quote_map

    def _apply_market_data(
        self,
        contracts: List[OptionContract],
        greeks_map: Dict[str, Greeks],
        quote_map:  Dict[str, Quote]
    ):
        """Apply Greeks and Quote data to OptionContract objects."""
        for oc in contracts:
            sym = oc.streamer_symbol

            greek = greeks_map.get(sym)
            if greek:
                oc.delta = float(greek.delta or 0)
                oc.gamma = float(greek.gamma or 0)
                oc.theta = float(greek.theta or 0)
                oc.vega  = float(greek.vega  or 0)
                oc.iv    = float(greek.volatility or 0)

            quote = quote_map.get(sym)
            if quote:
                bid = float(quote.bid_price or 0)
                ask = float(quote.ask_price or 0)
                oc.bid  = bid
                oc.ask  = ask
                oc.mark = (bid + ask) / 2 if bid > 0 and ask > 0 else (bid or ask)

    # ─── Strike Selection ─────────────────────────────────────────────────────

    def select_orb_strike(self, chain: OptionsChain, direction: str,
                           target_strike: int,
                           delta_bias: str = ORB_STRIKE_DELTA_BIAS
                           ) -> Optional[OptionContract]:
        """Nearest liquid strike to target_strike. When strikes bracket the target
        equally, break toward the higher- (more ITM/participation) or lower-
        (further OTM) |delta| per delta_bias."""
        contracts = chain.calls if direction == "long" else chain.puts
        candidates = [c for c in contracts if c.mark > 0.05]
        if not candidates:
            logger.warning("No liquid contracts in chain")
            return None
        min_dist = min(abs(c.strike - target_strike) for c in candidates)
        nearest = [c for c in candidates
                   if abs(c.strike - target_strike) <= min_dist + 1e-6]
        if len(nearest) == 1:
            best = nearest[0]
        elif delta_bias == "lower":
            best = min(nearest, key=lambda c: abs(c.delta))
        else:  # "higher" — more ITM / more directional participation
            best = max(nearest, key=lambda c: abs(c.delta))
        logger.info(
            f"ORB strike: {best.option_type} {best.strike} "
            f"mark=${best.mark:.2f} delta={best.delta:.3f} (bias={delta_bias})"
        )
        return best

    def select_sweep_strike(self, chain: OptionsChain,
                             direction: str,
                             target_delta: float,
                             tolerance: float = SWEEP_DELTA_TOLERANCE
                             ) -> Optional[OptionContract]:
        """Select the OTM strike whose |delta| is nearest target_delta (the caller
        scales target_delta by reversal strength). Prefers strikes within
        +/- tolerance of the target; falls back to the nearest available."""
        pool = chain.calls if direction == "long" else chain.puts
        liquid = [c for c in pool if c.mark > 0.05 and 0.0 < abs(c.delta) <= 0.55]
        if not liquid:
            return None
        band = [c for c in liquid if abs(abs(c.delta) - target_delta) <= tolerance]
        pick_from = band if band else liquid
        best = min(pick_from, key=lambda c: abs(abs(c.delta) - target_delta))
        logger.info(
            f"Sweep strike: {best.option_type} {best.strike} "
            f"mark=${best.mark:.2f} delta={best.delta:.3f} "
            f"(target={target_delta:.2f}{'' if band else ', band empty->nearest'})"
        )
        return best

    def select_butterfly_strikes(self, chain: OptionsChain,
                                  direction: str,
                                  current_price: float,
                                  wing_width_strikes: int
                                  ) -> Optional[Dict[str, OptionContract]]:
        """Select center (ATM) ± wing_width butterfly strikes."""
        center_strike = round_to_strike(current_price, STRIKE_INCREMENT)
        lower_strike  = center_strike - wing_width_strikes * STRIKE_INCREMENT
        upper_strike  = center_strike + wing_width_strikes * STRIKE_INCREMENT

        contracts = chain.calls if direction == "call" else chain.puts

        def find_strike(target: int) -> Optional[OptionContract]:
            candidates = [c for c in contracts if c.mark > 0 and c.strike == target]
            if candidates:
                return candidates[0]
            liquid = [c for c in contracts if c.mark > 0]
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

        logger.info(
            f"Butterfly: {direction.upper()} "
            f"{lower.strike}/{center.strike}/{upper.strike} "
            f"marks={lower.mark:.2f}/{center.mark:.2f}/{upper.mark:.2f}"
        )
        return {"lower": lower, "center": center, "upper": upper}

    def get_iv_rank(self, chain: OptionsChain) -> float:
        """Estimate IV rank. Falls back to ATM call IV."""
        if chain.iv_rank > 0:
            return chain.iv_rank
        if chain.calls:
            atm = min(chain.calls, key=lambda c: abs(c.delta - 0.5))
            return atm.iv * 100
        return 0.0


# Singleton
_fetcher: Optional[OptionsChainFetcher] = None


def get_chain_fetcher() -> OptionsChainFetcher:
    global _fetcher
    if _fetcher is None:
        _fetcher = OptionsChainFetcher()
    return _fetcher