"""
main.py — options_trader v1.0
0DTE options bot: ORB, Sweep Reversal, Butterfly
RTH only (9:30–16:00 ET), hard close 15:45 ET.

Run modes:
  python main.py            — interactive startup (prompts instrument, risk $, paper/live)
  python main.py --service  — non-interactive for systemd
"""

import logging
import logging.handlers
import signal
import sys
import time
import traceback
from datetime import datetime
from typing import Optional

from config import (
    POLL_INTERVAL_SECONDS, LOG_LEVEL, LOG_FILE, LOG_ROTATION_MB,
    PAPER_TRADING, RISK_PER_TRADE_USD, SESSION_LOSS_LIMIT,
    REGIME_REASSESS_MINUTES, INSTRUMENT, SessionConfig
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

    # ORB engine update (every tick during RTH)
    orb = get_orb_engine().update(df_5m, df_1m, price)

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
    Execute a single condor leg (2-leg vertical spread credit order).
    Bypasses the normal signal/score/size pipeline since condor legs
    are credit spreads with their own P&L math and sizing logic.
    Paper mode: simulates fill, records to DB, notifies via Telegram.
    Live mode: places the vertical spread order via TastyTrade.
    """
    import uuid
    from database.trade_logger import make_record, get_trade_logger
    from notifications.alert_manager import get_alert_manager
    from config import CONTRACT_MULTIPLIER, CONDOR_NICKEL_CLOSE

    mode     = "PAPER" if state.paper_trading else "LIVE"
    trade_id = str(uuid.uuid4())

    # Determine the short and long contracts from the signal
    if signal.option_side == "call":
        short_contract = signal.short_call_contract
        long_contract  = signal.long_call_contract
    else:
        short_contract = signal.short_put_contract
        long_contract  = signal.long_put_contract

    if short_contract is None or long_contract is None:
        logger.error(f"Condor leg: missing contracts — cannot execute")
        return

    net_credit = signal.net_credit
    contracts  = 1  # Default 1 contract — TODO: size via risk manager

    # In live mode, place the actual 2-leg order via TastyTrade
    if not state.paper_trading:
        try:
            from execution.entry_engine import get_entry_engine
            # TODO: implement live condor leg placement in entry_engine
            logger.warning("Condor live order placement not yet implemented — skipping")
            return
        except Exception as e:
            logger.error(f"Condor leg order failed: {e}")
            return

    # Paper mode: simulate fill at mid
    total_credit = net_credit * contracts * CONTRACT_MULTIPLIER

    is_leg1 = "Leg 1" in signal.setup_type
    _iron_condor_strategy.notify_leg_filled(
        is_leg1        = is_leg1,
        credit         = net_credit,
        short_contract = short_contract,
        long_contract  = long_contract
    )

    get_alert_manager()._send(
        f"\U0001F985 [{mode}] {signal.setup_type} | "
        f"sell={short_contract.strike:.0f} buy={long_contract.strike:.0f} "
        f"credit=${net_credit:.2f} | "
        f"stop=${net_credit * (1 + signal.stop_loss_pct):.2f} | "
        f"nickel=${CONDOR_NICKEL_CLOSE:.2f} | "
        f"{fmt_et_short()}"
    )

    logger.info(
        f"[{mode}] CONDOR LEG EXECUTED: {signal.setup_type} "
        f"short={short_contract.strike:.0f} long={long_contract.strike:.0f} "
        f"credit=${net_credit:.2f} total=${total_credit:.2f}"
    )


def attempt_new_entry(ctx: dict, regime: RegimeState, state: BotState):
    """Try to generate and execute a trade signal."""
    session  = get_session_guard()
    risk_mgr = get_risk_manager()
    scorer   = get_setup_scorer()
    entry_eng = get_entry_engine(state.paper_trading)

    # ── Session gate ──────────────────────────────────────────────────────────
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
        signal = _orb_strategy.generate_signal(
            orb           = orb,
            regime        = regime,
            vol_state     = ctx["vol"],
            liq_map       = ctx["liq_map"],
            chain         = chain,
            macro         = macro,
            current_price = ctx["price"]
        )
        if signal:
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
    if (signal is None and
            regime.primary_regime in (Regime.RANGING, Regime.COMPRESSION) and
            macro.butterfly_allowed and
            not macro.is_fed_day):
        signal = _butterfly_strategy.generate_signal(
            regime        = regime,
            vol_state     = ctx["vol"],
            liq_map       = ctx["liq_map"],
            chain         = chain,
            macro         = macro,
            current_price = ctx["price"],
            gex           = ctx.get("gex")
        )

    # Priority 3: Butterfly (Ranging/Compression — requires GEX PINNING)
    if (signal is None and
            regime.primary_regime in (Regime.RANGING, Regime.COMPRESSION) and
            macro.butterfly_allowed and
            not macro.is_fed_day):
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
    # Two modes per tick:
    #   a) No active plan yet: call decide() to evaluate and identify both
    #      vertical spread strike locations. No order placed — just planning.
    #   b) Active plan (DECIDED or LEG1_FILLED): call check_leg_triggers()
    #      to see if price has reached a leg's trigger level. If yes, returns
    #      a signal for that leg. Regime-flip cancellation is also handled here.
    if not _iron_condor_strategy.has_active_plan:
        # Try to make a condor plan if no other signal fired and regime is RANGING
        if (signal is None and
                regime.primary_regime == Regime.RANGING and
                not macro.is_fed_day):
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


def _execute_condor_leg(leg, state: BotState, chain):
    """
    Execute a single condor vertical spread leg (2-leg order).
    Called when check_triggers() signals a leg is ready to fire.
    This bypasses the normal signal/score/size flow since condor legs
    are credit spreads with their own sizing logic (risk = max loss per leg).
    """
    from strategy.iron_condor_strategy import CondorLeg
    from data.tasty_client import get_session, get_account
    from tastytrade.order import (
        NewOrder, Leg, OrderAction, OrderType, OrderTimeInForce,
        PriceEffect, InstrumentType
    )
    from decimal import Decimal
    import uuid

    mode     = "PAPER" if state.paper_trading else "LIVE"
    trade_id = f"CONDOR-{leg.side.upper()}-{uuid.uuid4().hex[:8].upper()}"

    logger.info(
        f"[{mode}] CONDOR LEG: {leg.side.upper()} spread "
        f"{leg.short_strike:.0f}/{leg.long_strike:.0f} "
        f"credit=${leg.credit:.2f} max_loss=${leg.max_loss:.2f} "
        f"id={trade_id}"
    )

    if state.paper_trading:
        # Paper mode: simulate fill at mid credit
        _iron_condor_strategy.mark_leg_filled(leg, leg.credit)
        get_alert_manager()._send(
            f"\U0001F985 [PAPER] CONDOR {leg.side.upper()} LEG FILLED: "
            f"{leg.short_strike:.0f}/{leg.long_strike:.0f} "
            f"credit=${leg.credit:.2f} | "
            f"{fmt_et_short()}"
        )
        return

    # Live execution — 2-leg vertical spread order
    try:
        session = get_session()
        account = get_account()

        if leg.side == "call":
            sell_sym = leg.short_contract.symbol
            buy_sym  = leg.long_contract.symbol
        else:
            sell_sym = leg.short_contract.symbol
            buy_sym  = leg.long_contract.symbol

        legs = [
            Leg(instrument_type=InstrumentType.EQUITY_OPTION,
                symbol=sell_sym, action=OrderAction.SELL_TO_OPEN, quantity=1),
            Leg(instrument_type=InstrumentType.EQUITY_OPTION,
                symbol=buy_sym,  action=OrderAction.BUY_TO_OPEN,  quantity=1),
        ]
        order = NewOrder(
            time_in_force = OrderTimeInForce.DAY,
            order_type    = OrderType.LIMIT,
            price         = Decimal(str(round(leg.credit, 2))),
            price_effect  = PriceEffect.CREDIT,
            legs          = legs,
        )
        response = account.place_order(session, order, dry_run=False)
        if response.errors:
            logger.error(f"Condor leg order failed: {response.errors}")
            return

        fill_credit = float(getattr(response.order, 'price', None) or leg.credit)
        _iron_condor_strategy.mark_leg_filled(leg, fill_credit)
        get_alert_manager()._send(
            f"\U0001F985 CONDOR {leg.side.upper()} LEG FILLED: "
            f"{leg.short_strike:.0f}/{leg.long_strike:.0f} "
            f"credit=${fill_credit:.2f} | "
            f"{fmt_et_short()}"
        )

    except Exception as e:
        logger.error(f"Condor leg execution failed: {e}")


def handle_session_reset(state: BotState):
    """Reset session-level state at the start of each RTH day."""
    if not state.session_reset_done:
        logger.info("RTH open — resetting session state")
        get_risk_manager().reset_session()
        state.session_reset_done = True
        state.orb_reset_done     = False

    if not state.orb_reset_done:
        get_orb_engine().reset_for_session()
        state.orb_reset_done = True
        logger.info("ORB engine reset for new session")


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
            if should_reassess:
                regime = run_regime_classification(ctx, "scheduled", state)
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