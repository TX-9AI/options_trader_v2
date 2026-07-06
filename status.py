"""
status.py — Live bot status snapshot.
v1.0 — original release
v1.1 — 2026-06-27 — read INSTRUMENT and PAPER_TRADING from systemd env
        so status.py reflects live config, not config.py defaults
v1.2 — 2026-06-27 — fix systemd env parsing with regex to handle long token values
v1.3 — 2026-06-27 — remove lookahead from regex, Environment= prefix was blocking match
v1.4 — 2026-06-30 — fix ORB state display: read structured ORB data (high/low/width/
        state/attempt) from bot.log instead of fragile string matching against
        state names that no longer exist (CONFIRMED_LONG -> OPEN_LONG, etc).
        Always show ORB H/L/width once range is set, regardless of state.
v1.5 — 2026-07-02 — remove early break from log scan so regime is always
        found regardless of log line order.
v1.6 — 2026-07-02 — read regime from database (regime_log table) instead
        of log parsing — reliable across restarts and outside RTH.
v1.7 — 2026-07-02 — fix regime_log query: ORDER BY logged_at not timestamp.
v1.8 — 2026-07-02 — consume the orb_range.json "status" field (ESTABLISHED/
        IN_PROGRESS/EXPIRED) instead of inventing ORB state from the clock.
        Only an ESTABLISHED range dated today is shown as live; EXPIRED and
        IN_PROGRESS ranges are labeled as such with their date, so a carried
        prior-session range can never be shown as "watching for break".
v1.9 — 2026-07-02 — reword loss-limit banner: the limit now forces a regime
        reassessment (session continues), not a halt.
v1.10 — 2026-07-02 — banner reflects the NET daily loss halt (day P&L <= -limit).
v1.12 — 2026-07-06 — read authoritative orb_state.json (live engine state:
        disarm reason, break latches, price, 11:00 cutoff) instead of clock
        inference/log-scraping. Adds a live Price line, shows DISARMED (runaway
        past 50% TP) and EXPIRED (past 11:00) truthfully, and reports price vs
        range instead of always saying "inside range, waiting".
v1.11 — 2026-07-03 — show live Risk per trade ($ from OT_RISK_USD) under Mode.

Run: python status.py

Shows: service state, instrument, mode, regime, ORB range + state,
open position (with current premium & P&L), and session summary.
Read-only — never modifies anything.
"""

import os
import re
import sys
import sqlite3
import subprocess
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET  = ZoneInfo("US/Eastern")
UTC = timezone.utc

INSTALL_DIR  = os.path.expanduser("~/options-trader")
SERVICE_NAME = "optionsbot"
sys.path.insert(0, INSTALL_DIR)


def get_runtime_env(key: str, default: str = "") -> str:
    """Read a live environment variable from the systemd service."""
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "show", SERVICE_NAME, "--property=Environment"],
            capture_output=True, text=True
        )
        match = re.search(rf'{re.escape(key)}=([^ ]+)', result.stdout)
        if match:
            return match.group(1)
    except Exception:
        pass
    return os.environ.get(key, default)


try:
    from config import DB_PATH, SESSION_LOSS_LIMIT, BOT_NAME
except Exception:
    DB_PATH            = os.path.join(INSTALL_DIR, "trades.db")
    SESSION_LOSS_LIMIT = 2
    BOT_NAME           = "OptionsTrader"

INSTRUMENT    = get_runtime_env("OT_INSTRUMENT", "QQQ")
PAPER_TRADING = get_runtime_env("OT_PAPER_TRADING", "True") != "False"
RISK_PER_TRADE = get_runtime_env("OT_RISK_USD", "200")


def now_et():
    return datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")

def to_et(ts):
    if not ts:
        return "N/A"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    except Exception:
        return ts[:16]

def sep(char="─", w=54):
    print(char * w)

def pct(val):
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.1%}"

def usd(val):
    if val >= 0:
        return f"+${val:,.2f}"
    else:
        return f"-${abs(val):,.2f}"


def check_service():
    try:
        r = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            capture_output=True, text=True
        )
        active = r.stdout.strip() == "active"
        return active, r.stdout.strip()
    except Exception:
        return False, "unknown"


ORB_STATE_LABELS = {
    "WAITING":                     "Waiting for 9:35 ET range",
    "RANGING":                     "Inside range, watching for break",
    "BREAK_HIGH_AWAITING_RETEST":  "Broke HIGH, awaiting retest",
    "BREAK_LOW_AWAITING_RETEST":   "Broke LOW, awaiting retest",
    "INVALIDATED":                 "Invalidated, re-arming",
    "OPEN_LONG":                   "OPEN LONG (confirmed)",
    "OPEN_SHORT":                  "OPEN SHORT (confirmed)",
    "EXPIRED":                     "Expired (past 11:00 ET cutoff)",
    "IN_PROGRESS":                 "Opening range forming (9:30-9:35 ET)",
    "EXPIRED_RANGE":               "Last session's range - today NOT established",
    "NOT_ESTABLISHED":             "Today's range not established",
    "UNKNOWN":                     "Unknown",
}


def get_regime_and_orb():
    log_path = os.path.join(INSTALL_DIR, "bot.log")
    regime    = "UNKNOWN"
    strategy  = "UNKNOWN"
    gex_pin   = None
    gex_env   = None

    # ── ORB state: prefer orb_state.json (authoritative LIVE engine state) ────
    # Written every tick by run_analysis(); carries the true state incl. disarm
    # reason, break latches, live price and the 11:00 cutoff — no clock guessing,
    # no log parsing. Falls back to orb_range.json (+11:00 clock) only if the
    # state file isn't present yet.
    orb = {
        "high": None, "low": None, "width": None, "state": "UNKNOWN",
        "attempt": 0, "reason": "", "broke_high": False, "broke_low": False,
        "price": None, "past_cutoff": False,
    }
    import json
    orb_state_file = os.path.join(INSTALL_DIR, "orb_state.json")
    orb_range_file = os.path.join(INSTALL_DIR, "orb_range.json")

    _have_state = False
    if os.path.exists(orb_state_file):
        try:
            with open(orb_state_file) as f:
                sd = json.load(f)
            orb.update({
                "high": sd.get("high"), "low": sd.get("low"), "width": sd.get("width"),
                "state": sd.get("state", "UNKNOWN"), "attempt": sd.get("attempt", 0),
                "reason": sd.get("reason", "") or "", "broke_high": sd.get("broke_high", False),
                "broke_low": sd.get("broke_low", False), "price": sd.get("price"),
                "past_cutoff": sd.get("past_cutoff", False),
            })
            _have_state = True
        except Exception:
            pass

    if os.path.exists(orb_range_file):
        try:
            with open(orb_range_file) as f:
                rd = json.load(f)
            orb["range_status"] = str(rd.get("status", "")).upper()
            orb["range_date"]   = rd.get("date")
            if orb["high"]  is None: orb["high"]  = rd.get("high")
            if orb["low"]   is None: orb["low"]   = rd.get("low")
            if orb["width"] is None: orb["width"] = rd.get("width")

            if not _have_state:
                # Fallback only — infer from the clock using the REAL 11:00 cutoff
                today = datetime.now(ET).strftime("%Y-%m-%d")
                if orb["range_status"] == "ESTABLISHED" and orb["range_date"] == today:
                    now = datetime.now(ET); hm = (now.hour, now.minute)
                    if not (9 <= now.hour < 16) or hm >= (11, 0):
                        orb["state"] = "EXPIRED"; orb["past_cutoff"] = hm >= (11, 0)
                    elif hm >= (9, 35):
                        orb["state"] = "RANGING"
                    else:
                        orb["state"] = "WAITING"
                elif orb["range_status"] == "IN_PROGRESS":
                    orb["state"] = "IN_PROGRESS"
                elif orb["range_status"] == "EXPIRED":
                    orb["state"] = "EXPIRED_RANGE"
                else:
                    orb["state"] = "NOT_ESTABLISHED"
        except Exception:
            pass

    if not os.path.exists(log_path):
        return regime, strategy, orb, gex_pin, gex_env

    # Get regime from database — most reliable source
    regime = get_latest_regime()

    try:
        result = subprocess.run(
            ["tail", "-1000", log_path],
            capture_output=True, text=True
        )
        lines = result.stdout.strip().split("\n")

        for line in reversed(lines):
            if regime == "UNKNOWN":
                if "REGIME:" in line and "→" in line:
                    parts = line.split("REGIME:")
                    if len(parts) > 1:
                        regime = parts[1].strip().split()[0]
                elif "STRATEGY: NO TRADE" in line and "regime=" in line:
                    m = re.search(r"regime=(\S+)", line)
                    if m: regime = m.group(1)

            if "STRATEGY TRANSITION:" in line and strategy == "UNKNOWN":
                parts = line.split("\u2192")
                if len(parts) > 1:
                    strategy = parts[1].strip().split()[0].rstrip(")")

            if "STRATEGY: NO TRADE" in line and strategy == "UNKNOWN":
                strategy = "No Trade"

            # ORB state is taken from orb_state.json (authoritative) above — no
            # log-scan refinement needed.

            if "GEX computed:" in line and gex_pin is None:
                try:
                    if "pin=$" in line:
                        gex_pin = line.split("pin=$")[1].split()[0].rstrip(")")
                    if "env=" in line:
                        gex_env = line.split("env=")[1].split()[0]
                except Exception:
                    pass

            # Don't break early — scan all lines to ensure regime is found
            # even when it appears near the bottom of the log

    except Exception:
        pass

    return regime, strategy, orb, gex_pin, gex_env


def get_latest_regime():
    """Read most recent regime from database — reliable, no log parsing."""
    if not os.path.exists(DB_PATH):
        return "UNKNOWN"
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT regime FROM regime_log ORDER BY logged_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row["regime"] if row else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def get_open_trade():
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY entry_time DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def get_session_summary():
    if not os.path.exists(DB_PATH):
        return None
    today = datetime.now(ET).strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl_usd), 0)                     as net_pnl,
                COALESCE(MAX(pnl_usd), 0)                     as best,
                COALESCE(MIN(pnl_usd), 0)                     as worst
            FROM trades
            WHERE status='closed' AND date(entry_time) = ?
        """, (today,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def main():
    print()
    sep("\u2550")
    mode_label = "PAPER" if PAPER_TRADING else "LIVE"
    print(f"  {BOT_NAME} \u2014 STATUS")
    print(f"  {now_et()}")
    sep("\u2550")
    print()

    running, svc_status = check_service()
    svc_icon = "\U0001F7E2" if running else "\U0001F534"
    print(f"  {svc_icon} Service:      {svc_status.upper()}")
    print(f"  \U0001F4CD Instrument:  {INSTRUMENT}")
    mode_icon = "\U0001F4C4" if PAPER_TRADING else "\U0001F534"
    print(f"  {mode_icon} Mode:         {mode_label}")
    try:
        _risk_disp = f"{float(RISK_PER_TRADE):.0f}"
    except Exception:
        _risk_disp = RISK_PER_TRADE
    print(f"  \U0001F4B5 Risk:         ${_risk_disp}")
    print()
    sep()

    regime, strategy, orb, gex_pin, gex_env = get_regime_and_orb()
    print(f"  \U0001F4CA Regime:      {regime}")
    print(f"  \U0001F3AF Strategy:    {strategy}")

    # Live underlying price (from orb_state.json, written each tick)
    _price = orb.get("price")
    if _price:
        print(f"  \U0001F4B2 Price:       {_price:.2f}")

    if orb["high"] is not None and orb["low"] is not None:
        print(f"  \u23F1  ORB High:    {orb['high']:.2f}")
        print(f"      ORB Low:     {orb['low']:.2f}")
        print(f"      ORB Width:   {orb['width']:.2f}")

        # Truthful state label straight from the engine (orb_state.json).
        st = orb["state"]
        reason = orb.get("reason", "")
        if st == "EXPIRED" or orb.get("past_cutoff"):
            state_label = "\u26D4 EXPIRED — past 11:00 ET cutoff (no ORB entries)"
        elif st == "INVALIDATED" and reason == "runaway":
            state_label = "\U0001F6D1 DISARMED — ran past 50% TP, no retest (favors sweep)"
        elif st == "INVALIDATED" and reason == "close_inside":
            state_label = "Invalidated (closed back inside) — re-arming"
        elif st == "INVALIDATED" and reason == "timeout":
            state_label = "Invalidated (retest timeout) — dormant"
        elif st in ("OPEN_LONG", "OPEN_SHORT"):
            state_label = ORB_STATE_LABELS.get(st, st)
        elif st in ("BREAK_HIGH_AWAITING_RETEST", "BREAK_LOW_AWAITING_RETEST"):
            state_label = ORB_STATE_LABELS.get(st, st)
        elif st == "RANGING":
            # show where price sits vs the range so "inside/broke out" is honest
            if _price is not None and _price > orb["high"]:
                state_label = "Broke ABOVE range — awaiting retest/close"
            elif _price is not None and _price < orb["low"]:
                state_label = "Broke BELOW range — awaiting retest/close"
            else:
                state_label = "Inside range, awaiting break"
        else:
            state_label = ORB_STATE_LABELS.get(st, st)

        # break latches (which side has registered a 1m close-out)
        bh, bl = orb.get("broke_high"), orb.get("broke_low")
        brk = []
        if bh: brk.append("H")
        if bl: brk.append("L")
        brk_note = f"  [broke: {'/'.join(brk)}]" if brk else ""

        attempt_str = f"  (attempt #{orb['attempt']})" if orb["attempt"] > 0 else ""
        if orb.get("range_status"):
            rs = orb["range_status"]
            date_note = f"  [{orb.get('range_date')}]" if rs != "ESTABLISHED" else ""
            print(f"      Range:       {rs}{date_note}")
        print(f"      State:       {state_label}{attempt_str}{brk_note}")
    else:
        print(f"  \u23F1  ORB:         Waiting for 9:35 ET range to be set")

    if gex_pin:
        gex_icon = "\U0001F4CC" if gex_env == "PINNING" else "\U0001F4C8" if gex_env == "TRENDING" else "\u2796"
        print(f"  {gex_icon} GEX pin:     ${gex_pin}  ({gex_env})")
    print()
    sep()

    trade = get_open_trade()
    if trade:
        is_butterfly = bool(trade.get("is_butterfly", 0))
        entry_prem   = trade.get("entry_premium", 0) or 0
        stop_prem    = trade.get("stop_premium",  0) or 0
        target_prem  = trade.get("target_premium", 0) or 0
        trail_prem   = trade.get("trail_activation", 0) or 0
        contracts    = trade.get("contracts", 0) or 0
        total_cost   = trade.get("total_cost", 0) or 0
        direction    = trade.get("direction", "").upper()
        strategy_name = trade.get("strategy", "")
        grade        = trade.get("setup_grade", "?")
        option_side  = trade.get("option_side", "").upper()
        strike       = trade.get("strike", 0) or 0
        expiry       = trade.get("expiry", "")
        current_prem = trade.get("current_premium") or entry_prem
        pnl_usd      = (current_prem - entry_prem) * contracts * 100 if entry_prem else 0.0
        pnl_icon = "\U0001F4C8" if pnl_usd >= 0 else "\U0001F4C9"

        if is_butterfly:
            net_debit  = trade.get("net_debit", 0) or 0
            max_profit = trade.get("max_profit", 0) or 0
            lower_s    = trade.get("lower_strike", 0) or 0
            center_s   = trade.get("center_strike", 0) or 0
            upper_s    = trade.get("upper_strike", 0) or 0
            print(f"  \U0001F98B OPEN BUTTERFLY \u2014 {option_side}")
            print(f"     Strikes:    {lower_s:.0f} / {center_s:.0f} / {upper_s:.0f}")
            print(f"     Net debit:  ${net_debit:.2f}/share")
            print(f"     Max profit: ${max_profit:.2f}/share  (TP @ 20%: ${max_profit*0.20:.2f})")
            print(f"     Contracts:  {contracts}")
            print(f"     Total cost: ${total_cost:.2f}")
            if current_prem != entry_prem:
                print(f"     Current:    ${current_prem:.2f}/share  ({usd(pnl_usd)})")
            print(f"     Stop:       < ${stop_prem:.2f}/share  (25% loss)")
        else:
            print(f"  {pnl_icon} OPEN {direction}  \u2014  {option_side} {strike:.0f}")
            print(f"     Expiry:     {expiry}")
            print(f"     Entry:      ${entry_prem:.2f}/share")
            if current_prem != entry_prem:
                print(f"     Current:    ${current_prem:.2f}/share  ({usd(pnl_usd)})")
            print(f"     Stop:       ${stop_prem:.2f}/share  (25% loss)")
            print(f"     Trail at:   ${trail_prem:.2f}/share  (50% TP)")
            print(f"     Target:     ${target_prem:.2f}/share  (100% TP)")
            print(f"     Contracts:  {contracts}  \u00d7  $100  =  ${total_cost:.2f} at risk")

        print(f"     Grade:      {grade}  |  {strategy_name}")
        print(f"     Entered:    {to_et(trade.get('entry_time', ''))}")
        print(f"     Regime:     {trade.get('regime', '')}")
    else:
        print("  \u23F3 No open position")

    print()
    sep()

    s = get_session_summary()
    today_label = datetime.now(ET).strftime("%Y-%m-%d")
    print(f"  TODAY'S SESSION  ({today_label} ET)")
    print()
    if s and s["total"] > 0:
        wins   = s["wins"]   or 0
        losses = s["losses"] or 0
        total  = s["total"]  or 0
        pnl    = s["net_pnl"] or 0
        best   = s["best"]   or 0
        worst  = s["worst"]  or 0
        wr     = wins / total * 100 if total else 0
        cb_warning = ""
        from config import DAILY_LOSS_LIMIT_USD
        if pnl <= -DAILY_LOSS_LIMIT_USD:
            cb_warning = (f"  \U0001F6D1  DAILY LOSS LIMIT HIT "
                          f"(day P&L ${pnl:+.0f} <= -${DAILY_LOSS_LIMIT_USD:.0f}) "
                          f"\u2192 new entries halted (override via configure.sh)")
        print(f"  Trades:       {total}  ({wins}W / {losses}L)")
        print(f"  Win rate:     {wr:.0f}%")
        print(f"  Net P&L:      {usd(pnl)}")
        print(f"  Best trade:   {usd(best)}")
        print(f"  Worst trade:  {usd(worst)}")
        if cb_warning:
            print()
            print(f"  {cb_warning}")
    else:
        print("  No closed trades yet today.")

    print()
    sep("\u2550")
    print()


if __name__ == "__main__":
    main()
