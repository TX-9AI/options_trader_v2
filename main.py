"""
main.py — options_trader v2.2
v1.0 — original release
v2.2 — 2026-07-01 — iron condor legged entry, BB-anchored strikes,
        regime-flip exits, ORB range via get_orb_range.py/orb_range.json,
        fed day trading enabled, ORB cutoff 11AM, condor window 11AM-2PM
v2.3 — 2026-07-02 — fix missing ZoneInfo import causing loop error every tick
v2.4 — 2026-07-02 — remove duplicate _execute_condor_leg (dead 2-arg def shadowed by
        a broken 3-arg def that referenced a non-existent CondorLeg class and
        mark_leg_filled method); single canonical impl on the real OptionsSignal
        API with live TastyTrade placement ported in. ORB range fetch is now
        success-keyed (retries until today's 9:30-9:35 candle is really written)
        and the startup fetch is gated to >= 9:35 ET so it never writes a
        stale prior-day range; instrument read from OT_INSTRUMENT (no systemd
        unit-file parsing).
v2.10 — 2026-07-02 — directional-only instruments (single names): skip iron
        condor and butterfly in the dispatch; ORB + sweep only.
v2.9 — 2026-07-02 — block new entries when the daily loss halt is active
        (day P&L <= -DAILY_LOSS_LIMIT_USD); open positions still exit.
v2.8 — 2026-07-02 — (2a) ORB-window sweep override: when an ORB signal fires but
        a sweep reversal has higher conviction, take the sweep. (2b) pass the
        current regime into the ORB engine for regime-gated re-arm. (#3) run
        the broken-wing roll check when both condor verticals are open.
v2.7 — 2026-07-02 — condor legs are now TRACKED positions: each vertical is
        sized at half the grade budget, written to the trade log, registered
        with the position manager (the only two-position strategy), and
        managed/exited per-side. Replaces the phantom notify-only path.
v2.6 — 2026-07-02 — session loss limit forces a regime reassessment instead of
        halting: main_loop consumes RiskManager.consume_reassess_request() and
        reclassifies with trigger="loss_limit".
v2.5 — 2026-07-02 — ORB range is now three-state (ESTABLISHED/IN_PROGRESS/
        EXPIRED) and always carries the last valid range. Startup fetch runs
        unconditionally (populates last-valid EXPIRED range pre-open); the
        open-poll runs from 9:30 ET and latches only when today's range is
        ESTABLISHED. Flag renamed orb_range_fetched_today -> _established_.
0DTE options bot: ORB, Sweep Reversal, Butterfly
RTH only (9:30–16:00 ET), hard close 15:45 ET.

Run modes:
  python main.py            — interactive startup (prompts instrument, risk $, paper/live)
  python main.py --service  — non-interactive for systemd
"""

import logging
import logging.handlers
import os
import signal
import sys
import time
import traceback
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from config import (
    POLL_INTERVAL_SECONDS, LOG_LEVEL, LOG_FILE, LOG_ROTATION_MB,
    PAPER_TRADING, RISK_PER_TRADE_USD, SESSION_LOSS_LIMIT,
    REGIME_REASSESS_MINUTES, INSTRUMENT, SessionConfig, DIRECTIONAL_ONLY
)


def _setup_logging():
    import os
    root = logging.getLogger()
    if root.handlers:
        return
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_ROTATION_MB * 1024 * 1024, backupCount=5
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    root.setLevel(level)


_setup_logging()
logger = logging.getLogger(__name__)

from utils.time_utils import (
    now_utc, fmt_et_short, minutes_since, is_rth,
    seconds_until_rth_open, is_hard_close_time
)
from data.data_cache import get_cache
from data.macro_data import get_macro_manager

from data.options_chain import get_chain_fetcher

from analysis.volatility_engine import get_volatility_engine
from analysis.trend_engine import get_trend_engine
from analysis.structure_analyzer import get_structure_analyzer
from analysis.liquidity_mapper import get_liquidity_mapper
from analysis.regime_classifier import get_regime_classifier, RegimeState, Regime
from analysis.orb_engine import get_orb_engine, ORBState

from strategy.orb_strategy import ORBStrategy
from strategy.sweep_reversal_strategy import SweepReversalStrategy
from strategy.butterfly_strategy import ButterflyStrategy
from strategy.iron_condor_strategy import IronCondorStrategy

from risk.risk_manager import init_risk_manager, get_risk_manager
from risk.setup_scorer import get_setup_scorer
from risk.session_guard import get_session_guard

from execution.entry_engine import get_entry_engine
from execution.exit_engine import get_exit_engine
from execution.position_manager import get_position_manager

from database.trade_logger import get_trade_logger
from notifications.alert_manager import get_alert_manager


# Strategy singletons
_orb_strategy     = ORBStrategy()
_sweep_strategy   = SweepReversalStrategy()
_butterfly_strategy = ButterflyStrategy()
_iron_condor_strategy = IronCondorStrategy()


class BotState:
    def __init__(self):
        self.last_regime_at:   Optional[datetime] = None
        self.current_regime:   Optional[RegimeState] = None
        self.last_regime_name: str = "UNKNOWN"
        self.tick_count:       int = 0
        self.errors_this_hour: int = 0
        self.paper_trading:    bool = PAPER_TRADING
        self.session_reset_done: bool = False   # Reset once per RTH open
        self.orb_reset_done:   bool = False     # ORB reset once per session
        self.orb_range_established_today: bool = False  # today's ORB range ESTABLISHED


def run_analysis(state: BotState) -> dict:
    """Fetch all market data and run analysis pipeline."""
    cache  = get_cache()
    data   = cache.get_all()
    price  = cache.get_price()
    if price is None:
        raise ValueError("Could not fetch current price")

    df_5m  = data.get("5m")
    df_1m  = data.get("1m")
    df_15m = data.get("15m")
    df_1h  = data.get("1h")

    if df_5m is None or df_5m.empty:
        raise ValueError("No 5m data available")

    df_1h_safe = df_1h if df_1h is not None else df_5m

    vol_state = get_volatility_engine().analyze(df_5m, df_1h_safe, price)
    trend     = get_trend_engine().analyze(data)
    structure = get_structure_analyzer().analyze(df_5m, df_15m, df_1h, price)
    liq_map   = get_liquidity_mapper().analyze(df_5m, df_15m, price)
    macro     = get_macro_manager().get()

    # ORB engine update (every tick during RTH). Pass last-tick regime so the
    # engine can gate its re-arm decision (this runs before reclassification).
    _regime_str = state.current_regime.primary_regime if state.current_regime else None
    orb = get_orb_engine().update(df_5m, df_1m, price, _regime_str)

    # Write ORB state to JSON file so status.py can read it directly
    # without parsing bot.log — eliminates all log-parsing timing issues
    try:
        import json as _json
        _orb_state = {
            "high":    orb.orb_high if orb.orb_high > 0 else None,
            "low":     orb.orb_low  if orb.orb_low  > 0 else None,
            "width":   orb.orb_width,
            "state":   orb.state,
            "attempt": orb.attempt_number,
        }
        _state_path = os.path.join(os.path.dirname(LOG_FILE), "orb_state.json")
        with open(_state_path, "w") as _f:
            _json.dump(_orb_state, _f)
    except Exception:
        pass

    return {
        "price":     price,
        "data":      data,
        "vol":       vol_state,
        "trend":     trend,
        "structure": structure,
        "liq_map":   liq_map,
        "macro":     macro,
        "orb":       orb,
        "df_1m":     df_1m,
    }


def run_regime_classification(ctx: dict, trigger: str, state: BotState) -> RegimeState:
    """Classify current market regime and log transitions."""
    regime = get_regime_classifier().classify(
        vol_state  = ctx["vol"],
        trend_state= ctx["trend"],
        structure  = ctx["structure"],
        liq_map    = ctx["liq_map"],
        macro      = ctx["macro"],
        trigger    = trigger
    )
    state.last_regime_at = now_utc()

    if regime.primary_regime != state.last_regime_name:
        logger.info(
            f"REGIME: {state.last_regime_name} → {regime.primary_regime} "
            f"(conviction={regime.conviction:.2f} trigger={trigger})"
        )
        get_alert_manager().send_regime_alert(
            old_regime = state.last_regime_name,
            new_regime = regime.primary_regime,
            conviction = regime.conviction,
            notes      = regime.notes
        )
        get_trade_logger().log_regime(
            regime        = regime.primary_regime,
            conviction    = regime.conviction,
            macro_context = ctx["macro"].macro_context if ctx["macro"] else "NEUTRAL",
            adx           = regime.adx,
            trigger       = trigger
        )

    state.last_regime_name = regime.primary_regime
    state.current_regime   = regime
    return regime


def _execute_condor_leg(signal: "OptionsSignal", state: BotState):
    """
    Execute a single condor leg (one vertical credit spread) from the
    OptionsSignal produced by IronCondorStrategy.check_leg_triggers().

    Legging model (per strategy design): Leg 1 fires on the side price is
    moving toward first; Leg 2 is queued and only fires after Leg 1 fills and
    only while the regime is still RANGING. If the regime flips before Leg 2,
    the strategy cancels Leg 2 and the filled Leg 1 vertical is managed
    standalone through normal stop/nickel exits. This function just executes
    whichever leg the strategy has decided is ready this tick.

    Paper mode: fills at mid credit. Live mode: places the 2-leg vertical as a
    single CREDIT limit order via TastyTrade (same SDK pattern as entry_engine).
    """
    from config import (CONTRACT_MULTIPLIER, CONDOR_NICKEL_CLOSE,
                        CONDOR_STOP_LOSS_PCT, INSTRUMENT)
    from database.trade_logger import make_record, get_trade_logger
    import uuid

    mode = "PAPER" if state.paper_trading else "LIVE"

    # Short/long contracts for this leg live on the call- or put-side fields.
    if signal.option_side == "call":
        short_contract = signal.short_call_contract
        long_contract  = signal.long_call_contract
    else:
        short_contract = signal.short_put_contract
        long_contract  = signal.long_put_contract

    if short_contract is None or long_contract is None:
        logger.error("Condor leg: missing contracts — cannot execute")
        return

    net_credit   = signal.net_credit
    spread_width = abs(short_contract.strike - long_contract.strike)

    # Size this vertical at HALF the grade budget — each side is independent,
    # so a B-grade $1000 trade becomes two ~$500 verticals.
    sizing = get_risk_manager().compute_condor_leg_size(spread_width, net_credit, "B")
    if not sizing.allowed:
        logger.info(f"Condor leg not sized: {sizing.reject_reason}")
        return
    contracts = sizing.contracts

    if not state.paper_trading:
        # Live 2-leg vertical credit order. NOTE: dry-run test before first live use.
        try:
            from data.tasty_client import get_session, get_account
            from tastytrade.order import (
                NewOrder, Leg, OrderAction, OrderType, OrderTimeInForce,
                PriceEffect, InstrumentType,
            )
            from decimal import Decimal

            session = get_session()
            account = get_account()
            legs = [
                Leg(instrument_type=InstrumentType.EQUITY_OPTION,
                    symbol=short_contract.symbol,
                    action=OrderAction.SELL_TO_OPEN, quantity=contracts),
                Leg(instrument_type=InstrumentType.EQUITY_OPTION,
                    symbol=long_contract.symbol,
                    action=OrderAction.BUY_TO_OPEN, quantity=contracts),
            ]
            order = NewOrder(
                time_in_force = OrderTimeInForce.DAY,
                order_type    = OrderType.LIMIT,
                price         = Decimal(str(round(net_credit, 2))),
                price_effect  = PriceEffect.CREDIT,
                legs          = legs,
            )
            response = account.place_order(session, order, dry_run=False)
            if response.errors:
                logger.error(f"Condor leg order failed: {response.errors}")
                return
            fill_credit = float(getattr(response.order, "price", None) or net_credit)
            order_id    = str(getattr(response.order, "id", "") or "")
        except Exception as e:
            logger.error(f"Condor leg order failed: {e}")
            return
    else:
        fill_credit = net_credit    # paper: fill at mid credit
        order_id    = "PAPER"

    is_leg1  = "Leg 1" in signal.setup_type
    max_loss = (spread_width - fill_credit) * contracts * CONTRACT_MULTIPLIER

    # Register the leg as a TRACKED position so it is managed, exited, and P&L'd.
    # The condor is the ONLY strategy allowed a second concurrent position.
    record = make_record(
        trade_id         = str(uuid.uuid4()),
        symbol           = INSTRUMENT,
        strategy         = "IronCondorStrategy",
        setup_type       = signal.setup_type,
        setup_grade      = "B",
        direction        = "neutral",
        option_side      = signal.option_side,
        is_butterfly     = 0,
        strike           = short_contract.strike,
        short_strike     = short_contract.strike,
        long_strike      = long_contract.strike,
        spread_width     = spread_width,
        credit_received  = fill_credit,
        expiry           = getattr(short_contract, "expiry", ""),
        contracts        = contracts,
        entry_premium    = fill_credit,                # credit basis for exits
        total_cost       = max_loss,
        max_loss         = max_loss,
        stop_premium     = fill_credit * (1 + CONDOR_STOP_LOSS_PCT),
        target_premium   = CONDOR_NICKEL_CLOSE,
        underlying_entry = getattr(signal, "underlying_entry", 0.0),
        regime           = "RANGING",
        vix_at_entry     = getattr(signal, "vix_at_signal", 0.0),
        is_condor_leg    = 1,
        condor_leg_num   = 1 if is_leg1 else 2,
        is_broken_wing   = 0,
        short_symbol     = getattr(short_contract, "symbol", ""),
        long_symbol      = getattr(long_contract, "symbol", ""),
        option_symbol    = getattr(short_contract, "symbol", ""),
        order_id         = order_id,
        paper_trade      = 1 if state.paper_trading else 0,
        status           = "open",
    )
    get_trade_logger().log_entry(record)
    get_position_manager(state.paper_trading).add_condor_leg(record)

    # Advance the plan (DECIDED -> LEG1_FILLED -> COMPLETE).
    _iron_condor_strategy.notify_leg_filled(
        is_leg1        = is_leg1,
        credit         = fill_credit,
        short_contract = short_contract,
        long_contract  = long_contract,
    )

    get_alert_manager()._send(
        f"\U0001F985 [{mode}] {signal.setup_type} | "
        f"sell={short_contract.strike:.0f} buy={long_contract.strike:.0f} "
        f"x{contracts} credit=${fill_credit:.2f} | "
        f"stop=${fill_credit * (1 + CONDOR_STOP_LOSS_PCT):.2f} | "
        f"nickel=${CONDOR_NICKEL_CLOSE:.2f} | maxloss=${max_loss:.0f} | "
        f"{fmt_et_short()}"
    )

    logger.info(
        f"[{mode}] CONDOR LEG EXECUTED (tracked): {signal.setup_type} "
        f"short={short_contract.strike:.0f} long={long_contract.strike:.0f} "
        f"x{contracts} credit=${fill_credit:.2f} max_loss=${max_loss:.0f}"
    )


def attempt_new_entry(ctx: dict, regime: RegimeState, state: BotState):
    """Try to generate and execute a trade signal."""
    session  = get_session_guard()
    risk_mgr = get_risk_manager()
    scorer   = get_setup_scorer()
    entry_eng = get_entry_engine(state.paper_trading)

    # ── Session gate ──────────────────────────────────────────────────────────
    # Daily loss halt: if the day's NET P&L is down by the limit, take no new
    # trades (open positions keep being managed to exit). Override via configure.sh.
    if risk_mgr.is_halted():
        logger.info("Entry blocked: DAILY LOSS LIMIT reached — halted. Override via configure.sh.")
        return

    can_enter, reason = session.can_enter(ctx["macro"])
    if not can_enter:
        logger.debug(f"Entry blocked: {reason}")
        return


    # ── Fetch options chain (shared across strategies) ────────────────────────
    chain = ctx.get("chain") or get_chain_fetcher().fetch_chain()
    if chain is None:
        logger.warning("Could not fetch options chain — skipping entry attempt")
        return

    macro = ctx["macro"]
    signal = None

    # ── Strategy dispatch: regime → strategy ──────────────────────────────────
    # Priority 1: ORB (if confirmed AND regime is trending or breakout)
    orb = ctx["orb"]
    if (orb.state in (ORBState.OPEN_LONG, ORBState.OPEN_SHORT) and
            regime.primary_regime in (
                Regime.TRENDING_BULL, Regime.TRENDING_BEAR,
                Regime.BREAKOUT_VOLATILE, Regime.RANGING, Regime.COMPRESSION
            )):
        orb_sig = _orb_strategy.generate_signal(
            orb           = orb,
            regime        = regime,
            vol_state     = ctx["vol"],
            liq_map       = ctx["liq_map"],
            chain         = chain,
            macro         = macro,
            current_price = ctx["price"]
        )
        if orb_sig:
            signal = orb_sig
            # ORB-window override: during the ORB window, if a sweep reversal is
            # setting up with HIGHER probability than the ORB, take the sweep.
            # A breakout-without-retest is exactly when sweep odds spike.
            if regime.sweep_recent:
                sweep_sig = _sweep_strategy.generate_signal(
                    regime        = regime,
                    vol_state     = ctx["vol"],
                    structure     = ctx["structure"],
                    liq_map       = ctx["liq_map"],
                    chain         = chain,
                    macro         = macro,
                    df_1m         = ctx.get("df_1m"),
                    current_price = ctx["price"]
                )
                if sweep_sig and getattr(sweep_sig, "conviction", 0.0) > getattr(orb_sig, "conviction", 0.0):
                    logger.info(
                        f"ORB-window override: sweep conviction "
                        f"{sweep_sig.conviction:.2f} > ORB {orb_sig.conviction:.2f} — taking sweep"
                    )
                    signal = sweep_sig
            if signal is orb_sig:
                get_orb_engine().mark_triggered()

    # Priority 2: Sweep Reversal
    if signal is None and regime.primary_regime == Regime.SWEEP_REVERSAL:
        signal = _sweep_strategy.generate_signal(
            regime        = regime,
            vol_state     = ctx["vol"],
            structure     = ctx["structure"],
            liq_map       = ctx["liq_map"],
            chain         = chain,
            macro         = macro,
            df_1m         = ctx.get("df_1m"),
            current_price = ctx["price"]
        )

    # Priority 3: Butterfly (Ranging/Compression — requires GEX PINNING)
    # Fed days allowed — bot reaction time is faster and more systematic
    # than manual trading on a volatile FOMC day. Fed day boosts ORB
    # conviction instead of blocking entries.
    if (signal is None and
            not DIRECTIONAL_ONLY and
            regime.primary_regime in (Regime.RANGING, Regime.COMPRESSION) and
            macro.butterfly_allowed):
        signal = _butterfly_strategy.generate_signal(
            regime        = regime,
            vol_state     = ctx["vol"],
            liq_map       = ctx["liq_map"],
            chain         = chain,
            macro         = macro,
            current_price = ctx["price"],
            gex           = ctx.get("gex")
        )

    # Priority 4: Iron Condor — legged entry, RANGING fallback when no GEX pin.
    if not _iron_condor_strategy.has_active_plan:
        # Try to make a condor plan if no other signal fired and regime is RANGING.
        # Skipped for directional-only instruments (single names).
        if (signal is None and
                not DIRECTIONAL_ONLY and
                regime.primary_regime == Regime.RANGING):
            plan = _iron_condor_strategy.decide(
                regime        = regime,
                vol_state     = ctx["vol"],
                chain         = chain,
                macro         = macro,
                current_price = ctx["price"]
            )
            # Plan is informational — no order yet. Leg triggers fire on
            # subsequent ticks via check_leg_triggers().
            if plan:
                logger.info(
                    f"Condor plan active — Leg 1={plan.leg1_side.upper()} "
                    f"trigger@{plan.call_trigger_price if plan.leg1_side == 'call' else plan.put_trigger_price:.0f}"
                )
    else:
        # Active plan: check if a leg should fire this tick
        leg_signal = _iron_condor_strategy.check_leg_triggers(
            regime        = regime,
            chain         = chain,
            current_price = ctx["price"]
        )
        if leg_signal is not None:
            # Route directly to entry — bypasses normal signal/score path
            # since condor legs are credit spreads with their own P&L math
            _execute_condor_leg(leg_signal, state)

    if signal is None:
        logger.info(f"STRATEGY: NO TRADE — regime={regime.primary_regime}")
        return

    if not signal.is_valid:
        logger.warning(f"Invalid signal from {signal.strategy_name}")
        return

    # ── Score and size ─────────────────────────────────────────────────────────
    score  = scorer.score(
        signal    = signal,
        regime    = regime,
        vol_state = ctx["vol"],
        structure = ctx["structure"],
        liq_map   = ctx["liq_map"],
        macro     = macro
    )

    if score is None:
        # Setup scored below the B threshold — there is no C grade.
        # This is not a trade, regardless of available capital.
        logger.info(f"STRATEGY: NO TRADE — {signal.strategy_name} setup below B threshold")
        return

    sizing = risk_mgr.compute_size(
        premium           = signal.entry_premium,
        grade             = score.grade,
        is_butterfly      = signal.is_butterfly,
        net_debit         = signal.net_debit if signal.is_butterfly else 0.0,
        butterfly_half_size = macro.butterfly_half_size if signal.is_butterfly else False
    )

    if not sizing.allowed:
        logger.info(f"Sizing rejected: {sizing.reject_reason}")
        return

    # Populate contract count in signal
    signal.contracts  = sizing.contracts
    signal.total_cost = sizing.total_cost

    # ── Enter trade ───────────────────────────────────────────────────────────
    record = entry_eng.enter(signal=signal, score=score, sizing=sizing)
    if record:
        get_position_manager(state.paper_trading).set_open_position(record)
        get_alert_manager().send_entry_alert(record)
        logger.info(
            f"✅ Entry: {signal.setup_type} "
            f"grade={score.grade} "
            f"contracts={sizing.contracts} "
            f"total=${sizing.total_cost:.2f}"
        )


def handle_session_reset(state: BotState):
    """Reset session-level state at the start of each RTH day."""
    if not state.session_reset_done:
        logger.info("RTH open — resetting session state")
        get_risk_manager().reset_session()
        state.session_reset_done = True
        state.orb_reset_done     = False
        state.orb_range_established_today = False

    if not state.orb_reset_done:
        get_orb_engine().reset_for_session()
        state.orb_reset_done = True
        logger.info("ORB engine reset for new session")

    # Fetch the ORB range only AFTER 9:35 ET when the 9:30-9:35 candle
    # is fully closed and baked. Fetching at 9:30 returns a degenerate
    # candle (high == low == 0 width) because the candle is still forming.
    if not state.orb_range_established_today:
        now_et_dt = datetime.now(ZoneInfo("US/Eastern"))
        if (now_et_dt.hour, now_et_dt.minute) >= (9, 30):
            # Poll from the open: 9:30-9:35 writes IN_PROGRESS, then ESTABLISHED
            # once the candle closes. Latch ONLY on ESTABLISHED (returns True) so
            # we keep polling across IN_PROGRESS/EXPIRED instead of locking in a
            # carried-over range for the session.
            state.orb_range_established_today = _fetch_orb_range()


def handle_hard_close(state: BotState):
    """Force-close open position at 15:45 ET."""
    pos_mgr = get_position_manager(state.paper_trading)
    if not pos_mgr.has_open_position():
        return

    record = pos_mgr.get_open_record()
    if record:
        logger.warning(
            f"HARD CLOSE: forcing exit of {record.get('trade_id','')[:8]} "
            f"at 15:45 ET"
        )
        get_exit_engine(state.paper_trading).place_exit_order(
            record, "hard_close_15:45_ET"
        )


def main_loop(state: BotState):
    pos_mgr = get_position_manager(state.paper_trading)

    while True:
        tick_start  = time.time()
        state.tick_count += 1

        try:
            # ── Pre-RTH: sleep until open ──────────────────────────────────
            if not is_rth():
                if state.session_reset_done:
                    # Day ended — reset flag so it fires again tomorrow
                    state.session_reset_done = False
                secs = seconds_until_rth_open()
                if secs > 120:
                    logger.info(
                        f"Market closed. Next RTH open in "
                        f"{secs/60:.0f} min. Sleeping 60s."
                    )
                    time.sleep(60)
                    continue
                else:
                    logger.info(f"RTH opens in {secs:.0f}s — standing by")
                    time.sleep(max(secs - 5, 5))
                    continue

            # ── RTH session reset ──────────────────────────────────────────
            handle_session_reset(state)

            # ── Hard close check ──────────────────────────────────────────
            if is_hard_close_time():
                handle_hard_close(state)
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # ── Main analysis ─────────────────────────────────────────────
            ctx = run_analysis(state)

            # ── Regime reassessment ───────────────────────────────────────
            should_reassess = (
                state.last_regime_at is None or
                minutes_since(state.last_regime_at) >= REGIME_REASSESS_MINUTES
            )
            # Session loss limit forces an off-schedule reassessment (no halt).
            loss_reassess = get_risk_manager().consume_reassess_request()
            if should_reassess or loss_reassess:
                trigger = "loss_limit" if loss_reassess else "scheduled"
                regime = run_regime_classification(ctx, trigger, state)
            else:
                regime = state.current_regime

            if regime is None:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # ── Compute GEX every tick (used by all strategies + position mgr)
            try:
                from data.options_chain import get_chain_fetcher
                from data.gex_data import compute_gex as _compute_gex
                _gex_chain = get_chain_fetcher().fetch_chain()
                if _gex_chain:
                    ctx["gex"]   = _compute_gex(_gex_chain, ctx["price"])
                    ctx["chain"] = _gex_chain
            except Exception as _gex_err:
                logger.warning(f"GEX tick fetch failed: {_gex_err}")

            # ── Manage open position ──────────────────────────────────────
            if pos_mgr.has_open_position():
                pos_mgr.manage_open_position(
                    chain=ctx.get("chain"),
                    df_1m=ctx.get("df_1m"),
                    regime=regime.primary_regime if regime else None
                )
                # ── Condor Leg 2 check ────────────────────────────────────
                # If Leg 1 is the open position and Leg 2 is still queued,
                # check_leg_triggers() must run here — not in attempt_new_entry()
                # which is blocked by has_open_position(). This is the only
                # path that allows Leg 2 to fire while Leg 1 is already live.
                # Once both legs are filled the condor is a complete 4-leg
                # position and no further leg firing occurs.
                if (_iron_condor_strategy.has_active_plan and
                        _iron_condor_strategy.plan is not None and
                        _iron_condor_strategy.plan.state == "LEG1_FILLED"):
                    leg_signal = _iron_condor_strategy.check_leg_triggers(
                        regime        = regime,
                        chain         = ctx.get("chain"),
                        current_price = ctx["price"]
                    )
                    if leg_signal is not None:
                        _execute_condor_leg(leg_signal, state)

                # ── Broken-wing roll check ────────────────────────────────
                # Both condor verticals open + one side tested → roll the
                # untested side into a BWB if it makes the tested side
                # risk-free. One-time, final adjustment.
                try:
                    from strategy.condor_roll import check_and_execute_roll
                    check_and_execute_roll(pos_mgr, ctx.get("chain"), ctx["price"], state)
                except Exception as _roll_err:
                    logger.warning(f"Roll check failed: {_roll_err}")
            else:
                attempt_new_entry(ctx, regime, state)

            # ── Periodic heartbeat log ────────────────────────────────────
            if state.tick_count % 20 == 0:
                summary = get_trade_logger().today_summary()
                logger.info(
                    f"Tick #{state.tick_count} | "
                    f"{fmt_et_short()} | "
                    f"price=${ctx['price']:,.2f} | "
                    f"regime={regime.primary_regime} ({regime.conviction:.0%}) | "
                    f"orb={ctx['orb'].state} | "
                    f"session: {summary.get('wins',0)}W/"
                    f"{summary.get('losses',0)}L "
                    f"pnl=${summary.get('total_pnl',0):+.2f} | "
                    f"{get_risk_manager().status_report()}"
                )

            state.errors_this_hour = max(0, state.errors_this_hour - 1)

        except Exception as e:
            state.errors_this_hour += 1
            logger.error(f"Loop error (#{state.errors_this_hour}): {e}")
            logger.error(traceback.format_exc())
            if state.errors_this_hour > 30:
                logger.critical("Too many errors — shutting down")
                sys.exit(1)

        elapsed = time.time() - tick_start
        time.sleep(max(0, POLL_INTERVAL_SECONDS - elapsed))


def _recover_open_position(state: BotState):
    """
    Called immediately on every start, restart, and reboot.
    Checks the database for any open position and resumes managing it
    before the main loop begins. Non-negotiable — if money is on the
    line, the bot must be aware of it within seconds of coming online.
    """
    pos_mgr = get_position_manager(state.paper_trading)

    if not pos_mgr.has_open_position():
        logger.info("Startup position check: no open positions found.")
        return

    record = pos_mgr.get_open_record()
    if not record:
        return

    trade_id     = record.get("trade_id", "")[:8]
    strategy     = record.get("strategy", "")
    option_side  = record.get("option_side", "").upper()
    strike       = record.get("strike", 0)
    contracts    = record.get("contracts", 0)
    entry_prem   = record.get("entry_premium", 0)
    total_cost   = record.get("total_cost", 0)
    is_butterfly = bool(record.get("is_butterfly", 0))
    entry_time   = record.get("entry_time", "")

    if is_butterfly:
        position_desc = (
            f"BUTTERFLY {record.get('option_side','').upper()} "
            f"{record.get('lower_strike',0):.0f}/"
            f"{record.get('center_strike',0):.0f}/"
            f"{record.get('upper_strike',0):.0f}"
        )
    else:
        position_desc = f"{option_side} {strike:.0f}"

    logger.warning(
        f"⚠️  OPEN POSITION DETECTED ON STARTUP: "
        f"{position_desc} x{contracts} "
        f"entry=${entry_prem:.2f} total=${total_cost:.2f} "
        f"strategy={strategy} id={trade_id} "
        f"entered={entry_time}"
    )

    # Alert operator immediately — money is on the line
    get_alert_manager()._send(
        f"⚠️ BOT RESTARTED WITH OPEN POSITION: "
        f"{position_desc} x{contracts} "
        f"@ ${entry_prem:.2f}/share (${total_cost:.2f} at risk) | "
        f"{strategy} | Now managing."
    )

    # Set the position in the position manager so the main loop
    # picks it up immediately on the very first tick
    pos_mgr.set_open_position(record)
    logger.info(
        f"Position recovery complete — "
        f"main loop will manage {position_desc} from first tick."
    )



def _fetch_orb_range(instrument: str = "") -> bool:
    """Fetch and write orb_range.json via the standalone get_orb_range.py.

    get_orb_range.py is the single source of truth. It ALWAYS writes the last
    valid range, tagged with one of three states, and returns it via exit code:
        0 = ESTABLISHED (today's, closed) -> return True
        2 = IN_PROGRESS (opening candle forming) -> return False (retry)
        3 = EXPIRED (carrying last RTH range)    -> return False (retry)
        1 = hard error                            -> return False

    Returns True ONLY when today's range is ESTABLISHED, so callers keep polling
    across IN_PROGRESS/EXPIRED until today's candle closes — while status.py and
    the engine always have the last valid range to read in the meantime.
    """
    try:
        import subprocess as _sp
        _symbol = instrument or os.environ.get("OT_INSTRUMENT", INSTRUMENT)
        # main.py lives in the install root; the script is a sibling package.
        _install_dir = os.path.dirname(os.path.abspath(__file__))
        _orb_script = os.path.join(_install_dir, "analysis", "get_orb_range.py")
        _result = _sp.run(
            [sys.executable, _orb_script, _symbol],
            capture_output=True, text=True, timeout=30
        )
        if _result.returncode == 0:
            _line = _result.stdout.splitlines()[0] if _result.stdout.strip() else ""
            logger.info(f"ORB range: {_line}")
            return True
        if _result.returncode == 2:
            logger.debug("ORB range: IN_PROGRESS — today's opening candle forming")
        elif _result.returncode == 3:
            logger.debug("ORB range: EXPIRED — carrying last RTH range, awaiting today's")
        else:
            logger.warning(f"ORB range fetch failed: {_result.stderr.strip()}")
        return False
    except Exception as e:
        logger.warning(f"ORB range fetch skipped: {e}")
        return False


def main():
    service_mode = "--service" in sys.argv

    if service_mode:
        session_config = SessionConfig(
            paper_trading      = PAPER_TRADING,
            instrument         = INSTRUMENT,
            risk_per_trade_usd = RISK_PER_TRADE_USD,
            notes              = "systemd auto-start"
        )
        logger.info(
            f"Service mode: {'PAPER' if PAPER_TRADING else 'LIVE'} | "
            f"{INSTRUMENT} | "
            f"risk=${RISK_PER_TRADE_USD:.0f}/trade | "
            f"session_CB={SESSION_LOSS_LIMIT} losses"
        )
    else:
        session_config = _interactive_startup()

    # Initialize TastyTrade client
    # TastyTrade session initializes lazily on first use via get_session()

    # Initialize risk manager with session params
    risk_mgr = init_risk_manager(
        risk_per_trade = session_config.risk_per_trade_usd,
        paper_trading  = session_config.paper_trading
    )

    state = BotState()
    state.paper_trading = session_config.paper_trading

    # Pre-fetch macro data
    logger.info("Fetching macro data...")
    get_macro_manager().get(force=True)

    get_alert_manager().send_startup_alert(
        paper      = session_config.paper_trading,
        instrument = session_config.instrument,
        risk_usd   = session_config.risk_per_trade_usd,
        session_limit = SESSION_LOSS_LIMIT
    )

    # ── Graceful shutdown alert on SIGTERM/SIGINT ────────────────────────────
    # systemctl stop/restart sends SIGTERM. Without this handler the bot
    # just dies silently with no Telegram notification.
    def _handle_shutdown(signum, frame):
        reason = "systemctl stop/restart" if signum == signal.SIGTERM else "manual interrupt"
        logger.info(f"Shutdown signal received ({reason}) — sending alert and exiting")
        try:
            get_alert_manager().send_shutdown_alert(
                instrument = session_config.instrument,
                reason     = reason
            )
        except Exception as e:
            logger.error(f"Failed to send shutdown alert: {e}")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT,  _handle_shutdown)

    # ── CRITICAL: Recover any open position immediately ─────────────────────
    # Runs before the main loop on every start, restart, or reboot.
    # If the bot went down with money on the line, we resume managing
    # that position within seconds — not waiting for the first loop cycle.
    _recover_open_position(state)

    # ── Fetch ORB range on start/restart ─────────────────────────────────────
    # Runs unconditionally: get_orb_range.py always writes the last valid range
    # tagged ESTABLISHED / IN_PROGRESS / EXPIRED, so status.py and the ORB engine
    # always have a range to read (e.g. Friday's EXPIRED range on a Monday
    # pre-open restart). It is safe pre-open because the engine only ARMS on an
    # ESTABLISHED/today range. We latch only when today's range is ESTABLISHED;
    # otherwise handle_session_reset() keeps polling from the open.
    state.orb_range_established_today = _fetch_orb_range(
        os.environ.get("OT_INSTRUMENT", INSTRUMENT)
    )

    logger.info(
        f"OptionsBot ready | "
        f"{'PAPER' if state.paper_trading else 'LIVE'} | "
        f"{session_config.instrument} | "
        f"risk=${session_config.risk_per_trade_usd:.0f}/trade | "
        f"poll={POLL_INTERVAL_SECONDS}s"
    )

    main_loop(state)


def _interactive_startup() -> SessionConfig:
    """Interactive startup prompt for manual launch."""
    print("\n" + "="*50)
    print("  options_trader v1.0 — Startup Configuration")
    print("="*50)

    # Instrument
    print("\nInstrument:")
    print("  1. QQQ  (Nasdaq ETF, $1 strikes)")
    print("  2. SPY  (S&P 500 ETF, $1 strikes)")
    print("  3. SPX  (S&P 500 Index, $5 strikes)")
    choice = input("Select [1/2/3, default=1]: ").strip() or "1"
    instrument = {"1": "QQQ", "2": "SPY", "3": "SPX"}.get(choice, "QQQ")

    # Risk per trade
    risk_input = input(f"\nRisk per trade in $ [default=200]: ").strip() or "200"
    try:
        risk_usd = float(risk_input)
    except ValueError:
        risk_usd = 200.0

    # Paper vs live
    mode_input = input("\nTrading mode [P=Paper/L=Live, default=P]: ").strip().upper() or "P"
    paper = mode_input != "L"

    print(f"\n{'─'*50}")
    print(f"  Instrument:    {instrument}")
    print(f"  Risk/trade:    ${risk_usd:.0f}")
    print(f"  Mode:          {'PAPER' if paper else '⚠️  LIVE'}")
    print(f"  Session CB:    {SESSION_LOSS_LIMIT} losses → halt")
    print(f"{'─'*50}")

    if not paper:
        confirm = input("\n⚠️  LIVE TRADING — type YES to confirm: ").strip()
        if confirm != "YES":
            print("Defaulting to paper trading.")
            paper = True

    from utils.time_utils import fmt_et_full
    return SessionConfig(
        paper_trading      = paper,
        instrument         = instrument,
        risk_per_trade_usd = risk_usd,
        confirmed_at       = fmt_et_full()
    )


if __name__ == "__main__":
    main()