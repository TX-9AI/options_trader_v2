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
