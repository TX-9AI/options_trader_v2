"""
main.py — options_trader v2.11
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
v2.11 — 2026-07-07 — durable 15:45 flatten + expiry-aware recovery. handle_hard_
        close now routes through pos_mgr.flatten_all() so EVERY open record
        (both condor legs) is truly closed in the DB + P&L booked (the old path
        called place_exit_order directly and never wrote status='closed'),
        retries every tick to 16:00, and pages once on failure. Startup recovery
        keys on EXPIRY, not entry date (the bot trades weeklies): sweep only
        genuinely expired orphans, resume every still-live row, and flag a
        CARRIED-overnight position. Restart alerts self-identify (box symbol +
        fresh-boot vs service-restart from /proc/uptime).
v2.12 — 2026-07-07 — LIVE broker reconciliation wired into recovery: the broker
        is the source of truth for existence. _reconcile_with_broker() queries
        open positions, KEEPs DB-planned rows confirmed there, ADOPTs+journals
        broker positions with no DB plan (managed by the ADOPTED exit path),
        and closes PHANTOM DB rows the broker no longer shows. FAIL-SAFE: a
        failed or empty broker read never closes anything — falls back to
        DB-only recovery. Paper is unchanged (no broker query).
v2.13 — 2026-07-07 — INTRADAY broker reconcile (LIVE + enabled): every 30 min
        across RTH with the last sweep at 15:30, a leg-role-aware check catches
        positions the broker closed mid-session — especially a SHORT leg
        auto-closed while the long remains (loud alarm, close the broken record,
        adopt the surviving long so the 15:45 flatten handles it cleanly). Only
        inspects rows we already manage; fail-safe on a bad/empty read.
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
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

from config import (
    POLL_INTERVAL_SECONDS, LOG_LEVEL, LOG_FILE, LOG_ROTATION_MB,
    PAPER_TRADING, RISK_PER_TRADE_USD, SESSION_LOSS_LIMIT,
    REGIME_REASSESS_MINUTES, INSTRUMENT, SessionConfig, DIRECTIONAL_ONLY,
    NO_ENTRY_AFTER_ET, BROKER_RECONCILE_ENABLED
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
    now_utc, now_et, fmt_et_short, minutes_since, is_rth,
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
        self.hard_close_alerted: bool = False   # alerted once on a failed 15:45 flatten
        self.last_reconcile_slot: Optional[str] = None  # last intraday broker-reconcile slot done


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
    # without parsing bot.log — eliminates all log-parsing timing issues.
    # Includes the disarm reason, break latches, live price and the 11:00
    # cutoff flag so status can render the true engine state (DISARMED / EXPIRED
    # / price-vs-range) rather than inferring it from the clock.
    try:
        import json as _json
        _eng = get_orb_engine()
        _now_et = now_et()
        _orb_state = {
            "high":       orb.orb_high if orb.orb_high > 0 else None,
            "low":        orb.orb_low  if orb.orb_low  > 0 else None,
            "width":      orb.orb_width,
            "state":      orb.state,
            "attempt":    orb.attempt_number,
            "reason":     orb.invalidation_reason,
            "broke_high": _eng.broke_high,
            "broke_low":  _eng.broke_low,
            "price":      price,
            "past_cutoff": (_now_et.hour, _now_et.minute) >= NO_ENTRY_AFTER_ET,
            "updated_at": _now_et.strftime("%Y-%m-%d %H:%M:%S ET"),
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
    # Priority 1: ORB — only when the engine has a CONFIRMED break+retest.
    # Note: the engine defers OPEN when regime==SWEEP_REVERSAL (sweeps take
    # priority), so an OPEN state here already implies a non-sweep regime and a
    # real ORB setup. Sweep reversal is handled at Priority 2.
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
    """Force-close every open position at 15:45 ET — durably.

    Routes through pos_mgr.flatten_all(), which closes ALL open records (both
    condor legs) via the full exit accounting so each DB row is actually marked
    closed and booked — not just an order submitted. The main loop calls this
    every tick from 15:45 to 16:00, so an incomplete close is retried
    automatically; a persistent failure pages once (before the 16:00 stop turns
    it into an overnight orphan).
    """
    pos_mgr = get_position_manager(state.paper_trading)
    if not pos_mgr.has_open_position():
        state.hard_close_alerted = False   # nothing open — clear any prior page
        return

    instrument = os.environ.get("OT_INSTRUMENT", INSTRUMENT)
    failed = pos_mgr.flatten_all("hard_close_15:45_ET")

    if not failed:
        logger.info("HARD CLOSE complete — all positions flat.")
        state.hard_close_alerted = False
        return

    logger.error(
        f"HARD CLOSE INCOMPLETE [{instrument}]: {len(failed)} still open "
        f"{[t[:8] for t in failed]} — retrying every tick until 16:00"
    )
    if not state.hard_close_alerted:
        get_alert_manager().send_hard_close_failure_alert(instrument, failed)
        state.hard_close_alerted = True


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

            # ── Intraday broker reconcile (LIVE + enabled) ─────────────────
            # Every 30 min across RTH, last sweep at 15:30 — catches a broker-
            # side leg closure (e.g. shorts auto-closed) before the 15:45
            # flatten acts. Fires once per slot; fail-safe on a bad/empty read.
            if not state.paper_trading and BROKER_RECONCILE_ENABLED:
                slot = _intraday_reconcile_slot(now_et())
                if slot and slot != state.last_reconcile_slot:
                    state.last_reconcile_slot = slot
                    _intraday_reconcile(
                        state, os.environ.get("OT_INSTRUMENT", INSTRUMENT)
                    )

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


# Below this many seconds of system uptime, a startup is treated as a fresh
# instance boot (EC2 stop/start or reboot); above it, a service-only restart
# (systemctl restart / crash / deploy while the box was already up).
BOOT_UPTIME_THRESHOLD_S = 180


def _boot_kind() -> str:
    """Classify why the bot just started, for restart self-identification.
    Fresh instance boot vs service-only restart, read from /proc/uptime.
    Best-effort: returns a generic 'restart' if uptime can't be read."""
    try:
        with open("/proc/uptime") as fh:
            uptime_s = float(fh.read().split()[0])
        return "fresh boot" if uptime_s < BOOT_UPTIME_THRESHOLD_S else "service restart"
    except Exception:
        return "restart"


def _describe_position(record: dict) -> str:
    """One-line, self-identifying description of an open row (used by both the
    recovery alert and the stale-orphan sweep alert)."""
    side = str(record.get("option_side", "")).upper()
    if bool(record.get("is_butterfly", 0)):
        return (
            f"BUTTERFLY {side} "
            f"{record.get('lower_strike',0):.0f}/"
            f"{record.get('center_strike',0):.0f}/"
            f"{record.get('upper_strike',0):.0f}"
        )
    if record.get("is_condor_leg") or record.get("strategy") == "IronCondorStrategy":
        return (f"CONDOR {side} "
                f"{record.get('short_strike',0):.0f}/{record.get('long_strike',0):.0f}")
    return f"{side} {record.get('strike',0):.0f}"


def _intraday_reconcile_slot(now):
    """30-minute RTH reconcile slot key, or None outside the window. Slots at
    :00 and :30 from 09:30 to 15:30 (last = 15:30, 15 min before the 15:45
    flatten). A slot stays 'current' until 15:45, so a late tick still catches
    the final 15:30 sweep; nothing fires after that (the flatten takes over)."""
    if now.weekday() >= 5:
        return None
    t = now.time()
    if t < dtime(9, 30) or t >= dtime(15, 45):
        return None
    boundary = 0 if now.minute < 30 else 30
    hh, mm = now.hour, boundary
    if (hh, mm) > (15, 30):      # cap the last slot at 15:30
        hh, mm = 15, 30
    return f"{now:%Y-%m-%d} {hh:02d}:{mm:02d}"


def _intraday_reconcile(state: BotState, instrument: str):
    """
    LIVE intraday broker-truth check (gated by BROKER_RECONCILE_ENABLED). Detects
    positions the broker closed out from under us DURING the session — especially
    a SHORT leg auto-closed while the long remains — and reacts before the 15:45
    flatten. It only inspects rows WE already manage (it does not adopt brand-new
    broker positions intraday, so a manual trade you place is left alone).

    FAIL-SAFE: a failed or empty broker read changes nothing.
    """
    trade_logger = get_trade_logger()
    try:
        from data.tasty_client import get_open_option_positions
        broker = get_open_option_positions()
    except Exception as e:
        logger.error(f"Intraday reconcile: broker read failed ({e}) — no action.")
        return

    open_rows = trade_logger.get_open_trades_live()
    if not open_rows:
        return
    if not broker:
        logger.warning(
            "Intraday reconcile: broker empty while DB shows open rows — "
            "inconclusive, no action (fail-safe)."
        )
        get_alert_manager().send_reconcile_unavailable_alert(instrument, "empty read (intraday)")
        return

    from execution.broker_reconcile import leg_roles, _adopt_record
    broker_by_sym = {p["symbol"]: p for p in broker if p.get("symbol")}
    broker_syms   = set(broker_by_sym)

    changed  = False
    phantoms = []
    for rec in open_rows:
        rid = rec.get("trade_id", "")
        short_syms, long_syms = leg_roles(rec)
        all_syms = short_syms | long_syms
        if not all_syms:
            continue

        # whole position gone at the broker -> phantom
        if not (all_syms & broker_syms):
            trade_logger.close_phantom(rid, reason="phantom_intraday")
            phantoms.append(rid)
            changed = True
            continue

        # SHORT gone while a LONG remains -> broker closed our protection
        short_present = bool(short_syms & broker_syms)
        long_present  = bool(long_syms & broker_syms)
        if short_syms and not short_present and long_present:
            trade_logger.close_phantom(rid, reason="short_closed_by_broker")
            surviving = []
            for sym in (long_syms & broker_syms):
                adopted = _adopt_record(broker_by_sym[sym])
                if adopted:
                    trade_logger.log_entry(adopted)
                    surviving.append(_describe_position(adopted))
            changed = True
            get_alert_manager().send_short_leg_closed_alert(
                instrument  = instrument,
                closed_desc = _describe_position(rec),
                surviving   = ", ".join(surviving) or "(long leg)",
            )
            logger.error(
                f"SHORT LEG CLOSED BY BROKER [{instrument}] {_describe_position(rec)} "
                f"-> adopted surviving long(s): {surviving}"
            )

    if phantoms:
        get_alert_manager().send_phantom_closed_alert(instrument, phantoms)
    if changed:
        # re-sync in-memory management to the corrected DB truth
        get_position_manager(state.paper_trading).set_open_positions(
            trade_logger.get_open_trades_live()
        )


def _reconcile_with_broker(state: BotState, live_rows: list,
                           restart_type: str, instrument: str) -> list:
    """
    LIVE-only: reconcile the DB's live rows against the broker, which is the
    source of truth for whether a position EXISTS. Returns the final list of
    records to manage (kept DB rows + adopted broker positions). Journals adopts,
    closes phantoms, and alerts.

    FAIL-SAFE: on ANY broker read failure — or an empty read while the DB still
    shows live rows — return the DB rows unchanged and close NOTHING. A bad or
    empty read must never be interpreted as "the broker is flat", which would
    close real positions.
    """
    trade_logger = get_trade_logger()
    try:
        from data.tasty_client import get_open_option_positions
        broker = get_open_option_positions()
    except Exception as e:
        logger.error(f"Broker reconcile unavailable ({e}) — DB-only recovery, closed nothing.")
        get_alert_manager().send_reconcile_unavailable_alert(instrument, "read failed")
        return live_rows

    if not broker:
        if live_rows:
            logger.warning(
                "Broker returned NO option positions while the DB shows live rows — "
                "inconclusive; DB-only recovery, closed nothing."
            )
            get_alert_manager().send_reconcile_unavailable_alert(instrument, "empty read")
        return live_rows

    from execution.broker_reconcile import build_plan
    plan = build_plan(broker, live_rows)

    # Phantoms: open in our DB but absent at the broker -> close (broker wins).
    for tid in plan.close_phantom:
        trade_logger.close_phantom(tid)
    if plan.close_phantom:
        get_alert_manager().send_phantom_closed_alert(instrument, plan.close_phantom)

    # Adopts: journal into our system of record + alert (loud for a lone short).
    anomaly_ids = set(plan.anomalies)
    for rec in plan.adopt:
        trade_logger.log_entry(rec)
        get_alert_manager().send_adopted_alert(
            instrument    = instrument,
            position_desc = _describe_position(rec),
            contracts     = int(rec.get("contracts", 0) or 0),
            entry_premium = float(rec.get("entry_premium", 0) or 0),
            is_short      = bool(rec.get("is_short_position")),
            anomaly       = rec.get("trade_id") in anomaly_ids,
            restart_type  = restart_type,
        )
        logger.warning(
            f"ADOPTED [{instrument}] {_describe_position(rec)} "
            f"({'short' if rec.get('is_short_position') else 'long'}) "
            f"id={rec.get('trade_id','')[:8]}"
        )

    return list(plan.keep) + list(plan.adopt)


def _recover_open_position(state: BotState, restart_type: str = ""):
    """
    Called immediately on every start, restart, and reboot, before the main loop.

    Step 1 — reconcile only TRULY EXPIRED orphans. A position's liveness is its
    EXPIRY, not its entry date: this bot also trades weeklies (nearest expiry can
    be days out), so a row entered on a prior session may still be a live
    contract today. Only rows whose expiry has actually passed are dead; those
    are closed in the DB up front so nothing manages a ghost.

    Step 2 — resume EVERY still-live open row (0DTE or weekly). If a position
    survived overnight (a weekly held, or one that leaked past the 15:45 flatten
    / a hard kill), it is identified and managed immediately, and flagged as
    CARRIED so it can't be missed.
    """
    pos_mgr = get_position_manager(state.paper_trading)
    trade_logger = get_trade_logger()
    instrument = os.environ.get("OT_INSTRUMENT", INSTRUMENT)

    # ── Step 1: sweep only genuinely EXPIRED orphans ─────────────────────────
    expired = trade_logger.close_expired_open_trades()
    if expired:
        descs = [_describe_position(r) for r in expired]
        logger.warning(
            f"Startup: auto-closed {len(expired)} EXPIRED orphan(s) [{instrument}]: "
            f"{', '.join(descs)}"
        )
        get_alert_manager().send_orphan_cleared_alert(
            instrument=instrument, descs=descs, restart_type=restart_type
        )

    # ── Step 2: resume every still-live (unexpired) position ─────────────────
    live = trade_logger.get_open_trades_live()

    # LIVE ONLY, and only when explicitly enabled: the broker is the source of
    # truth for what's actually open. (Paper has no broker to query; and even on
    # live this stays OFF until OT_BROKER_RECONCILE=True, so it can't fire before
    # get_open_option_positions() has been verified on a live box.)
    if not state.paper_trading and BROKER_RECONCILE_ENABLED:
        live = _reconcile_with_broker(state, live, restart_type, instrument)

    if not live:
        logger.info("Startup position check: no live positions to resume.")
        return

    pos_mgr.set_open_positions(live)

    # The recovery/carried alert covers DB-PLANNED rows only; adopted positions
    # already got their own adopted alerts inside the reconcile.
    db_planned = [r for r in live if r.get("strategy") != "ADOPTED"]
    if not db_planned:
        logger.info("Recovery: only adopted positions to manage (already alerted).")
        return

    # A position whose entry ET date is before today survived a session boundary.
    today_et = now_et().strftime("%Y-%m-%d")
    carried  = any(
        trade_logger._et_date(r.get("entry_time", "")) not in ("", today_et)
        for r in db_planned
    )

    descs         = [_describe_position(r) for r in db_planned]
    position_desc = " + ".join(descs)
    contracts     = sum(int(r.get("contracts", 0) or 0) for r in db_planned)
    total_cost    = sum(float(r.get("total_cost", 0) or 0) for r in db_planned)
    lead          = db_planned[0]
    entry_prem    = float(lead.get("entry_premium", 0) or 0)
    strategy      = lead.get("strategy", "")
    trade_ids     = ",".join(r.get("trade_id", "")[:8] for r in db_planned)

    logger.warning(
        f"⚠️  {'CARRIED' if carried else 'LIVE'} POSITION RECOVERED ON STARTUP "
        f"[{instrument}]: {position_desc} x{contracts} "
        f"entry=${entry_prem:.2f} total=${total_cost:.2f} "
        f"strategy={strategy} id={trade_ids} ({restart_type or 'restart'})"
    )
    get_alert_manager().send_recovery_alert(
        instrument   = instrument,
        position_desc = position_desc,
        contracts    = contracts,
        entry_premium = entry_prem,
        total_cost   = total_cost,
        strategy     = strategy,
        restart_type = restart_type,
        carried      = carried,
    )
    logger.info(
        f"Position recovery complete — main loop will manage "
        f"{position_desc} from first tick."
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

    # Classify this start (fresh instance boot vs service restart) so every
    # alert below can self-identify what kind of restart just happened.
    restart_type = _boot_kind()

    get_alert_manager().send_startup_alert(
        paper      = session_config.paper_trading,
        instrument = session_config.instrument,
        risk_usd   = session_config.risk_per_trade_usd,
        session_limit = SESSION_LOSS_LIMIT,
        restart_type = restart_type,
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
    _recover_open_position(state, restart_type)

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