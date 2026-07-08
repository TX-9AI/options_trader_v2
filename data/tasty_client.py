"""
data/tasty_client.py — TastyTrade session via the official tastytrade SDK.

Uses OAuth (client_secret + refresh_token) — never username/password.
Credentials come exclusively from environment variables set by setup_ec2.sh.

The SDK is async-native for streaming. We wrap the session in a thread-safe
singleton. Synchronous SDK calls (orders, chain fetching, market data) work
directly without async. The DXLinkStreamer (for Greeks/quotes) uses a
background async loop.
"""

import asyncio
import logging
import threading
from typing import Optional

from tastytrade import Session, Account

from config import get_tt_client_secret, get_tt_refresh_token, get_tt_account_number

logger = logging.getLogger(__name__)

# ─── Background event loop (for DXLinkStreamer async calls) ───────────────────

_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread]   = None


def _start_background_loop():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


def get_loop() -> asyncio.AbstractEventLoop:
    """Return the background event loop, starting it if needed."""
    global _loop, _loop_thread
    if _loop is None or not _loop.is_running():
        _loop_thread = threading.Thread(
            target=_start_background_loop,
            name="tt-async-loop",
            daemon=True
        )
        _loop_thread.start()
        import time; time.sleep(0.1)
    return _loop


def run_async(coro):
    """
    Run an async coroutine from synchronous code using the background loop.
    Blocks until the coroutine completes and returns its result.
    """
    loop   = get_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=30)


# ─── Session management ────────────────────────────────────────────────────────

_session: Optional[Session]  = None
_account: Optional[Account]  = None
_session_lock = threading.Lock()


def get_session() -> Session:
    """
    Return the active TastyTrade session, creating it if needed.
    Thread-safe. Credentials come from environment variables.
    """
    global _session
    with _session_lock:
        if _session is None:
            _session = _create_session()
    return _session


def _create_session() -> Session:
    client_secret = get_tt_client_secret()
    refresh_token = get_tt_refresh_token()

    logger.info("Connecting to TastyTrade...")
    session = Session(client_secret, refresh_token)
    logger.info("TastyTrade session established")
    return session


def get_account() -> Account:
    """
    Return the active TastyTrade Account object, creating it if needed.
    Uses TT_ACCOUNT_NUMBER env var to select the correct account.
    """
    global _account
    with _session_lock:
        if _account is None:
            session        = get_session()
            account_number = get_tt_account_number()
            _account       = Account.get(session, account_number)
            logger.info(f"TastyTrade account loaded: {account_number}")
    return _account


def get_account_number() -> str:
    return get_tt_account_number()


def get_open_option_positions() -> list:
    """
    LIVE broker option positions, normalized for reconciliation (see
    execution/broker_reconcile.py). Returns a list of dicts:
        {symbol, underlying, quantity, direction, average_open_price}
    for OPEN option legs only (quantity != 0). The brokerage is the source of
    truth for whether a position exists; this is how we ask it.

    NEVER call on paper — there is no broker to query. Raises TastyClientError
    on failure so the caller can fall back to DB-only rather than trade blind.

    Version-robust: Account.get_positions is synchronous on tastytrade 12.x but
    a coroutine on 13.x — we detect and run it on the background loop if needed.

    NOTE: field access is verified against tastytrade 13.0.0 and the stable 12.x
    fields, but the deployed pin is >=12.4.0 — run this once against a live box
    and eyeball the output before relying on it for real orders.
    """
    account = get_account()
    session = get_session()
    try:
        raw = account.get_positions(session)
        if asyncio.iscoroutine(raw):
            raw = run_async(raw)
    except Exception as e:
        raise TastyClientError(f"get_positions failed: {e}") from e

    out = []
    for p in raw or []:
        try:
            itype = getattr(p, "instrument_type", "")
            itype = getattr(itype, "value", itype)          # enum -> str if needed
            if "Option" not in str(itype):
                continue                                     # options only
            qty = int(abs(float(getattr(p, "quantity", 0) or 0)))
            direction = str(getattr(p, "quantity_direction", "") or "")
            if qty == 0 or direction.lower() == "zero":
                continue                                     # closed leg
            out.append({
                "symbol":             getattr(p, "symbol", "") or "",
                "underlying":         getattr(p, "underlying_symbol", "") or "",
                "quantity":           qty,
                "direction":          direction,             # 'Long' / 'Short'
                "average_open_price": float(getattr(p, "average_open_price", 0) or 0),
            })
        except Exception as e:
            logger.error(
                f"Skipping unparseable broker position "
                f"{getattr(p, 'symbol', '?')}: {e}"
            )
            continue

    logger.info(f"Broker reports {len(out)} open option position(s)")
    return out


def reset_session():
    """Force a new session and account to be created on the next call."""
    global _session, _account
    with _session_lock:
        _session = None
        _account = None
    logger.info("TastyTrade session reset")


# ─── Backwards-compatibility aliases ──────────────────────────────────────────

class TastyClientError(Exception):
    """Raised when a TastyTrade API call fails."""
    pass


def get_client():
    """
    Legacy alias — returns the active Session object.
    New code should use get_session() and get_account() directly.
    """
    return get_session()
