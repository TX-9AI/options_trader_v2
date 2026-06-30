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
    "EXPIRED":                     "Expired (past 2PM cutoff)",
    "UNKNOWN":                     "Unknown",
}


def get_regime_and_orb():
    log_path = os.path.join(INSTALL_DIR, "bot.log")
    regime    = "UNKNOWN"
    strategy  = "UNKNOWN"
    gex_pin   = None
    gex_env   = None

    orb = {
        "high":    None,
        "low":     None,
        "width":   None,
        "state":   "UNKNOWN",
        "attempt": 0,
    }

    if not os.path.exists(log_path):
        return regime, strategy, orb, gex_pin, gex_env

    try:
        result = subprocess.run(
            ["tail", "-1000", log_path],
            capture_output=True, text=True
        )
        lines = result.stdout.strip().split("\n")

        for line in reversed(lines):
            if "REGIME:" in line and regime == "UNKNOWN":
                parts = line.split("REGIME:")
                if len(parts) > 1:
                    regime = parts[1].strip().split()[0]

            if "STRATEGY TRANSITION:" in line and strategy == "UNKNOWN":
                parts = line.split("\u2192")
                if len(parts) > 1:
                    strategy = parts[1].strip().split()[0].rstrip(")")

            if orb["high"] is None and "ORB range set:" in line:
                try:
                    h = re.search(r"high=([\d.]+)", line)
                    l = re.search(r"low=([\d.]+)", line)
                    w = re.search(r"width=([\d.]+)", line)
                    if h: orb["high"]  = float(h.group(1))
                    if l: orb["low"]   = float(l.group(1))
                    if w: orb["width"] = float(w.group(1))
                except Exception:
                    pass

            if orb["state"] == "UNKNOWN":
                if "ORB CONFIRMED LONG" in line:
                    orb["state"] = "OPEN_LONG"
                elif "ORB CONFIRMED SHORT" in line:
                    orb["state"] = "OPEN_SHORT"
                elif "ORB BREAK HIGH" in line:
                    orb["state"] = "BREAK_HIGH_AWAITING_RETEST"
                    m = re.search(r"attempt #(\d+)", line)
                    if m: orb["attempt"] = int(m.group(1))
                elif "ORB BREAK LOW" in line:
                    orb["state"] = "BREAK_LOW_AWAITING_RETEST"
                    m = re.search(r"attempt #(\d+)", line)
                    if m: orb["attempt"] = int(m.group(1))
                elif "ORB INVALIDATED" in line:
                    orb["state"] = "INVALIDATED"
                elif "retest timeout" in line:
                    orb["state"] = "INVALIDATED"
                elif "ORB: past 14:00" in line:
                    orb["state"] = "EXPIRED"
                elif "ORB re-armed" in line:
                    orb["state"] = "RANGING"
                    m = re.search(r"attempt #(\d+)", line)
                    if m: orb["attempt"] = int(m.group(1))
                elif "ORB range set:" in line:
                    orb["state"] = "RANGING"
                elif "ORB engine reset" in line:
                    orb["state"] = "WAITING"

            if "GEX computed:" in line and gex_pin is None:
                try:
                    if "pin=$" in line:
                        gex_pin = line.split("pin=$")[1].split()[0].rstrip(")")
                    if "env=" in line:
                        gex_env = line.split("env=")[1].split()[0]
                except Exception:
                    pass

            if (regime != "UNKNOWN" and strategy != "UNKNOWN"
                    and orb["state"] != "UNKNOWN" and orb["high"] is not None
                    and gex_pin is not None):
                break

    except Exception:
        pass

    return regime, strategy, orb, gex_pin, gex_env


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
    print()
    sep()

    regime, strategy, orb, gex_pin, gex_env = get_regime_and_orb()
    print(f"  \U0001F4CA Regime:      {regime}")
    print(f"  \U0001F3AF Strategy:    {strategy}")

    if orb["high"] is not None and orb["low"] is not None:
        print(f"  \u23F1  ORB High:    {orb['high']:.2f}")
        print(f"      ORB Low:     {orb['low']:.2f}")
        print(f"      ORB Width:   {orb['width']:.2f}")
        state_label = ORB_STATE_LABELS.get(orb["state"], orb["state"])
        attempt_str = f"  (attempt #{orb['attempt']})" if orb["attempt"] > 0 else ""
        print(f"      State:       {state_label}{attempt_str}")
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
        if losses >= SESSION_LOSS_LIMIT:
            cb_warning = "  \u26A0  CIRCUIT BREAKER FIRED"
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
