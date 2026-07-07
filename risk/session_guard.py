"""
risk/session_guard.py — Session boundary enforcement.
v1.2 — 2026-07-07 — ORB-formation lockout: no entries until the 9:30–9:35 ET
        opening-range candle has CLOSED (is_orb_complete → time >= 9:35:00).
        Universal floor across ALL strategies that guarantees nothing fires
        during the opening candle (9:30:00–9:34:59). Closes the sweep-reversal
        hole specifically: the sweep's ORB-break gate returns True while the
        range is still unestablished — i.e. exactly this window — so without
        this floor a sweep could pass can_enter() and fire pre-9:35. The gate
        is a FLOOR, not a delay: it opens the instant the range candle closes
        (9:35:00 sharp), so a break registered on/after that close is
        unaffected and every strategy that hinges on the opening-range candle
        still fires on time.
v1.0 — original release
v1.1 — 2026-06-27 — use BUTTERFLY_ENTRY_CUTOFF_ET from config (15:00)
        instead of hardcoded 15:30

Entry gates (evaluated in order; first failing gate blocks):
  - RTH — 9:30–16:00 ET, weekdays
  - ORB-formation lockout — no entries before 9:35 ET (opening range must close)
  - Hard close — no new entries at/after 15:45 ET
  - Entry cutoff — Standard (ORB, SweepReversal) 2:00 PM ET; Butterfly 3:00 PM ET
  - Macro — VIX-crisis lockout
"""

import logging
from typing import Optional
from datetime import datetime, time as dtime

from utils.time_utils import (
    is_rth, is_orb_complete, is_hard_close_time, is_past_entry_cutoff,
    now_et, fmt_et_short, seconds_until_rth_open
)
from data.macro_data import MacroSnapshot
from config import BUTTERFLY_ENTRY_CUTOFF_ET

logger = logging.getLogger(__name__)

# Convert config tuple (15, 0) to time object
_BUTTERFLY_CUTOFF = dtime(BUTTERFLY_ENTRY_CUTOFF_ET[0], BUTTERFLY_ENTRY_CUTOFF_ET[1])


class SessionGuard:
    """
    Gate keeper for all session-level rules.
    Called at the start of each attempt_new_entry() loop.
    """

    def can_enter(self, macro: Optional[MacroSnapshot] = None,
                  is_butterfly: bool = False) -> tuple:
        """
        Check all pre-entry gates.

        Args:
            macro:        Current macro snapshot
            is_butterfly: True for butterfly — allowed until BUTTERFLY_ENTRY_CUTOFF_ET

        Returns:
            (allowed: bool, reason: str)
        """
        # ── RTH gate ──────────────────────────────────────────────────────────
        if not is_rth():
            return False, f"outside RTH ({fmt_et_short()})"

        # ── ORB-formation lockout ─────────────────────────────────────────────
        # No entries until the 9:30–9:35 ET opening-range candle has CLOSED.
        # This is the universal floor for EVERY strategy: the ORB itself cannot
        # fire pre-9:35 (no established range), but the sweep reversal otherwise
        # could — its ORB-break gate (_sweep_broke_orb) returns True while the
        # range is unestablished, i.e. exactly this window. is_orb_complete() is
        # True at >= 9:35:00, so this OPENS the gate the instant the opening
        # candle closes and never delays a break registered on/after that close.
        if not is_orb_complete():
            return False, f"opening range still forming (<9:35 ET) — no entries ({fmt_et_short()})"

        # ── Hard close ────────────────────────────────────────────────────────
        if is_hard_close_time():
            return False, "past 15:45 ET hard close — no new entries"

        # ── Entry cutoff ──────────────────────────────────────────────────────
        if is_past_entry_cutoff():
            if not is_butterfly:
                return False, "past 14:00 ET entry cutoff — no new 0DTE entries"
            if now_et().time() >= _BUTTERFLY_CUTOFF:
                return False, f"past {_BUTTERFLY_CUTOFF.strftime('%H:%M')} ET butterfly cutoff"

        # ── Macro gates ───────────────────────────────────────────────────────
        if macro and not macro.new_entries_allowed:
            return False, f"VIX crisis ({macro.vix:.1f}) — no new entries"

        return True, ""

    def must_close_all(self) -> bool:
        return is_hard_close_time()

    def seconds_to_open(self) -> float:
        return seconds_until_rth_open()

    def log_session_state(self, macro: Optional[MacroSnapshot] = None):
        allowed, reason = self.can_enter(macro)
        logger.info(
            f"Session [{fmt_et_short()}]: "
            f"rth={is_rth()} "
            f"entry={'OK' if allowed else 'BLOCKED: ' + reason} "
            f"hard_close={is_hard_close_time()} "
            f"vix={macro.vix:.1f if macro else 'N/A'} "
            f"fed_day={macro.is_fed_day if macro else False}"
        )


_session_guard: Optional[SessionGuard] = None


def get_session_guard() -> SessionGuard:
    global _session_guard
    if _session_guard is None:
        _session_guard = SessionGuard()
    return _session_guard
