"""
query.py — OptionsTrader Performance Dashboard
v1.0 — original
v1.1 — 2026-06-29 — Fix: read INSTRUMENT from systemd env via get_runtime_env()
                     config.py fallback is QQQ but live bot may be configured for SPX
Options-specific: strikes, premiums, delta, P&L, butterfly legs, session stats.
"""

import sqlite3
import os
import re
import sys
import subprocess
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

INSTALL_DIR = os.path.expanduser("~/options-trader")
sys.path.insert(0, INSTALL_DIR)

try:
    from config import DB_PATH, SESSION_LOSS_LIMIT
    SERVICE_NAME = "optionsbot"
except Exception:
    DB_PATH            = os.path.join(INSTALL_DIR, "trades.db")
    SESSION_LOSS_LIMIT = 2
    SERVICE_NAME       = "optionsbot"

ET  = ZoneInfo("US/Eastern")
UTC = timezone.utc


def get_runtime_env(key: str, default: str = "") -> str:
    """Read a live environment variable from the systemd service — mirrors status.py."""
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


# Always read instrument and mode from live systemd environment — same as status.py
INSTRUMENT    = get_runtime_env("OT_INSTRUMENT",   "QQQ")
PAPER_TRADING = get_runtime_env("OT_PAPER_TRADING", "True") != "False"
BOT_NAME      = get_runtime_env("OT_BOT_NAME",      "OptionsTrader")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def connect():
    if not os.path.exists(DB_PATH):
        print(f"\n  Database not found at {DB_PATH}")
        print("  Has the bot entered any trades yet?")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def to_et(ts: str) -> str:
    if not ts:
        return "N/A"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(ET).strftime("%m/%d %H:%M ET")
    except Exception:
        return ts[:16]


def now_et():
    return datetime.now(ET)


def today_et_prefix():
    return now_et().strftime("%Y-%m-%d")


def pnl_str(val: float) -> str:
    if val is None:
        return "  N/A"
    return f"+${val:.2f}" if val >= 0 else f"-${abs(val):.2f}"


def pct_str(val: float) -> str:
    if val is None:
        return "  N/A"
    return f"{val:+.1%}"


def bar(pct: float, width: int = 20) -> str:
    filled = max(0, min(width, int(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def sep(char: str = "─", width: int = 62):
    print(char * width)


def get_service_status() -> str:
    try:
        import subprocess
        result = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            capture_output=True, text=True
        )
        status = result.stdout.strip()
        return "🟢 ACTIVE" if status == "active" else f"🔴 {status.upper()}"
    except Exception:
        return "UNKNOWN"


def get_live_price() -> float | None:
    try:
        sys.path.insert(0, INSTALL_DIR)
        from data.market_data import fetch_quote
        return fetch_quote(INSTRUMENT)
    except Exception:
        return None


# ─── Sections ─────────────────────────────────────────────────────────────────

def show_header():
    mode = "📄 PAPER" if PAPER_TRADING else "🔴 LIVE"
    status = get_service_status()
    sep("═")
    print(f"  {BOT_NAME} — PERFORMANCE DASHBOARD")
    print(f"  {now_et().strftime('%Y-%m-%d %H:%M:%S ET')}")
    sep("═")
    print(f"  Service:     {status}")
    print(f"  Instrument:  {INSTRUMENT}")
    print(f"  Mode:        {mode}")
    print()


def show_open_position(conn):
    row = conn.execute(
        "SELECT * FROM trades WHERE status='open' ORDER BY entry_time DESC LIMIT 1"
    ).fetchone()

    sep("═")
    print("  OPEN POSITION")
    sep("═")

    if not row:
        print("  ⏳ No open position.")
        print()
        return

    is_bf      = bool(row["is_butterfly"])
    entry_prem = row["entry_premium"] or 0
    stop_prem  = row["stop_premium"]  or 0
    target_prem= row["target_premium"]or 0
    contracts  = row["contracts"]     or 0
    total_cost = row["total_cost"]    or 0

    # Try to get live option premium
    live_prem  = None
    live_pnl   = None
    live_pnl_pct = None
    try:
        from data.market_data import fetch_quote
        underlying = fetch_quote(INSTRUMENT)
    except Exception:
        underlying = None

    # Position description
    if is_bf:
        pos_desc = (
            f"BUTTERFLY {(row['option_side'] or '').upper()}  "
            f"{row['lower_strike']:.0f} / {row['center_strike']:.0f} / {row['upper_strike']:.0f}"
        )
    else:
        pos_desc = f"{(row['option_side'] or '').upper()}  Strike {row['strike']:.0f}"

    # Live P&L from current_premium stored by position_manager each tick
    current_prem = row["current_premium"] if row["current_premium"] else None
    live_pnl_usd = None
    live_pnl_pct = None
    if current_prem and entry_prem:
        live_pnl_usd = (current_prem - entry_prem) * contracts * 100
        live_pnl_pct = (current_prem - entry_prem) / entry_prem

    print(f"  ID:            {row['trade_id'][:8]}")
    print(f"  Position:      {pos_desc}")
    print(f"  Strategy:      {row['strategy']}  |  Setup: {row['setup_type']}")
    print(f"  Grade:         {row['setup_grade']}  (score={row['setup_score']:.2f})")
    print(f"  Regime:        {row['regime']}")
    print(f"  Expiry:        {row['expiry']}")
    print(f"  Contracts:     {contracts}")
    print()

    print(f"  Entry Premium: ${entry_prem:.2f}/share  (${entry_prem * 100:.2f}/contract)")
    print(f"  Total Cost:    ${total_cost:.2f}")
    if current_prem:
        print(f"  Current Mark:  ${current_prem:.2f}/share  (${current_prem * 100:.2f}/contract)")
    if live_pnl_usd is not None:
        pnl_label = f"+${live_pnl_usd:.2f}" if live_pnl_usd >= 0 else f"-${abs(live_pnl_usd):.2f}"
        pct_label = pct_str(live_pnl_pct)
        print(f"  Unrealized P&L:{pnl_label}  ({pct_label})")
    print(f"  Stop Premium:  ${stop_prem:.2f}  ({pct_str((stop_prem - entry_prem) / entry_prem if entry_prem else 0)} from entry)")
    print(f"  Target:        ${target_prem:.2f}  ({pct_str((target_prem - entry_prem) / entry_prem if entry_prem else 0)} from entry)")

    if row["trail_activation"]:
        print(f"  Trail Trigger: ${row['trail_activation']:.2f}")

    print()

    # Underlying price context
    if underlying:
        print(f"  Underlying:    ${underlying:,.2f}  (live)")
    if row["underlying_entry"]:
        print(f"  Entry Spot:    ${row['underlying_entry']:,.2f}")
    if row["underlying_stop"]:
        print(f"  Spot Stop:     ${row['underlying_stop']:,.2f}")
    if row["underlying_target"]:
        print(f"  Spot Target:   ${row['underlying_target']:,.2f}")

    print()
    print(f"  VIX at Entry:  {row['vix_at_entry']:.1f}" if row["vix_at_entry"] else "  VIX at Entry:  N/A")
    print(f"  Fed Day:       {'Yes ⚠️' if row['is_fed_day'] else 'No'}")
    print(f"  Paper:         {'Yes' if row['paper_trade'] else 'No'}")
    print(f"  Entered:       {to_et(row['entry_time'])}")
    print()


def show_today(conn):
    today = today_et_prefix()
    rows = conn.execute(
        """SELECT * FROM trades
           WHERE status='closed'
           AND date(datetime(entry_time, '-4 hours')) = ?
           ORDER BY exit_time""",
        (today,)
    ).fetchall()

    sep()
    print(f"  TODAY'S TRADES  ({today} ET)")
    sep()

    if not rows:
        print("  No closed trades today.")
        print()
        return

    wins      = [r for r in rows if (r["pnl_usd"] or 0) > 0]
    losses    = [r for r in rows if (r["pnl_usd"] or 0) <= 0]
    total_pnl = sum(r["pnl_usd"] or 0 for r in rows)
    win_rate  = len(wins) / len(rows) * 100 if rows else 0
    total_cost= sum(r["total_cost"] or 0 for r in rows)
    pnl_pct   = total_pnl / total_cost * 100 if total_cost else 0

    print(f"  Trades:        {len(rows)}  ({len(wins)}W / {len(losses)}L)")
    print(f"  Win Rate:      {win_rate:.0f}%  {bar(win_rate, 15)}")
    print(f"  Net P&L:       {pnl_str(total_pnl)}  ({pnl_pct:+.1f}% of capital deployed)")
    if wins:
        print(f"  Best Trade:    {pnl_str(max(r['pnl_usd'] or 0 for r in wins))}")
    if losses:
        print(f"  Worst Trade:   {pnl_str(min(r['pnl_usd'] or 0 for r in losses))}")
    print()

    # Trade detail table
    print(f"  {'ID':<10} {'Type':<8} {'Strike':<14} {'Grade':<6} "
          f"{'Entry':>7} {'Exit':>7} {'P&L':>9} {'P&L%':>7}  Exit Reason")
    sep()
    for r in rows:
        is_bf = bool(r["is_butterfly"])
        if is_bf:
            strike_str = f"{r['center_strike']:.0f} BF"
        else:
            side = (r["option_side"] or "")[:1].upper()
            strike_str = f"{side} {r['strike']:.0f}"

        trade_type = "BUTTERFLY" if is_bf else (r["setup_type"] or "")[:8]
        entry_p    = r["entry_premium"] or 0
        exit_p     = r["exit_premium"]  or 0
        pnl        = r["pnl_usd"]       or 0
        pnl_p      = r["pnl_pct"]       or 0

        print(
            f"  {r['trade_id'][:8]:<10} "
            f"{trade_type:<8} "
            f"{strike_str:<14} "
            f"{r['setup_grade'] or '?':<6} "
            f"${entry_p:>6.2f} "
            f"${exit_p:>6.2f} "
            f"{pnl_str(pnl):>9} "
            f"{pct_str(pnl_p):>7}  "
            f"{(r['exit_reason'] or '')[:28]}"
        )
    print()


def show_alltime(conn):
    rows = conn.execute(
        "SELECT * FROM trades WHERE status='closed' ORDER BY exit_time"
    ).fetchall()

    sep()
    print("  ALL-TIME PERFORMANCE")
    sep()

    if not rows:
        print("  No closed trades yet.")
        print()
        return

    wins         = [r for r in rows if (r["pnl_usd"] or 0) > 0]
    losses       = [r for r in rows if (r["pnl_usd"] or 0) <= 0]
    total_pnl    = sum(r["pnl_usd"] or 0 for r in rows)
    win_rate     = len(wins) / len(rows) * 100 if rows else 0
    avg_win      = sum(r["pnl_usd"] or 0 for r in wins) / len(wins) if wins else 0
    avg_loss     = sum(r["pnl_usd"] or 0 for r in losses) / len(losses) if losses else 0
    total_wins   = sum(r["pnl_usd"] or 0 for r in wins)
    total_losses = abs(sum(r["pnl_usd"] or 0 for r in losses))
    pf           = total_wins / total_losses if total_losses > 0 else 0

    # Max drawdown
    running = peak = max_dd = 0.0
    for r in rows:
        running += (r["pnl_usd"] or 0)
        peak     = max(peak, running)
        max_dd   = max(max_dd, peak - running)

    # Avg hold time
    hold_times = []
    for r in rows:
        try:
            entry = datetime.fromisoformat(r["entry_time"])
            exit_ = datetime.fromisoformat(r["exit_time"])
            hold_times.append((exit_ - entry).total_seconds() / 60)
        except Exception:
            pass
    avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0

    print(f"  Total Trades:    {len(rows)}  ({len(wins)}W / {len(losses)}L)")
    print(f"  Win Rate:        {win_rate:.1f}%  {bar(win_rate)}")
    print(f"  Net P&L:         {pnl_str(total_pnl)}")
    print(f"  Avg Win:         {pnl_str(avg_win)}")
    print(f"  Avg Loss:        {pnl_str(avg_loss)}")
    print(f"  Profit Factor:   {pf:.2f}")
    print(f"  Max Drawdown:    ${max_dd:.2f}")
    print(f"  Avg Hold Time:   {avg_hold:.0f} min")
    print()


def show_by_strategy(conn):
    sep()
    print("  PERFORMANCE BY STRATEGY")
    sep()

    strategies = conn.execute(
        "SELECT DISTINCT strategy FROM trades WHERE status='closed' AND strategy IS NOT NULL"
    ).fetchall()

    if not strategies:
        print("  No closed trades yet.")
        print()
        return

    for strat in strategies:
        name = strat["strategy"]
        rows = conn.execute(
            "SELECT pnl_usd, pnl_pct, total_cost FROM trades WHERE status='closed' AND strategy=?",
            (name,)
        ).fetchall()
        wins     = [r for r in rows if (r["pnl_usd"] or 0) > 0]
        win_rate = len(wins) / len(rows) * 100 if rows else 0
        net_pnl  = sum(r["pnl_usd"] or 0 for r in rows)
        print(
            f"  {name:<24} {len(rows):>3} trades  "
            f"WR={win_rate:.0f}%  {bar(win_rate, 12)}  "
            f"Net={pnl_str(net_pnl)}"
        )
    print()


def show_by_grade(conn):
    sep()
    print("  PERFORMANCE BY SETUP GRADE")
    sep()

    for grade in ["A", "B", "C"]:
        rows = conn.execute(
            "SELECT pnl_usd, pnl_pct FROM trades WHERE status='closed' AND setup_grade=?",
            (grade,)
        ).fetchall()
        if not rows:
            print(f"  Grade {grade}:  No trades yet")
            continue
        wins     = [r for r in rows if (r["pnl_usd"] or 0) > 0]
        win_rate = len(wins) / len(rows) * 100
        net_pnl  = sum(r["pnl_usd"] or 0 for r in rows)
        avg_pct  = sum(r["pnl_pct"] or 0 for r in rows) / len(rows)
        print(
            f"  Grade {grade}:  {len(rows):>3} trades  "
            f"WR={win_rate:.0f}%  {bar(win_rate, 12)}  "
            f"Net={pnl_str(net_pnl)}  AvgPnl%={avg_pct:+.1%}"
        )
    print()


def show_by_setup_type(conn):
    sep()
    print("  PERFORMANCE BY SETUP TYPE")
    sep()

    types = conn.execute(
        "SELECT DISTINCT setup_type FROM trades WHERE status='closed' AND setup_type IS NOT NULL"
    ).fetchall()

    if not types:
        print("  No closed trades yet.")
        print()
        return

    for st in types:
        stype = st["setup_type"]
        rows  = conn.execute(
            "SELECT pnl_usd FROM trades WHERE status='closed' AND setup_type=?",
            (stype,)
        ).fetchall()
        wins     = [r for r in rows if (r["pnl_usd"] or 0) > 0]
        win_rate = len(wins) / len(rows) * 100 if rows else 0
        net_pnl  = sum(r["pnl_usd"] or 0 for r in rows)
        print(
            f"  {stype:<28} {len(rows):>3} trades  "
            f"WR={win_rate:.0f}%  Net={pnl_str(net_pnl)}"
        )
    print()


def show_recent(conn, n: int = 10):
    rows = conn.execute(
        "SELECT * FROM trades WHERE status='closed' ORDER BY exit_time DESC LIMIT ?", (n,)
    ).fetchall()

    sep()
    print(f"  LAST {n} CLOSED TRADES")
    sep()

    if not rows:
        print("  No closed trades yet.")
        print()
        return

    print(f"  {'ID':<10} {'Type':<8} {'Strike':<14} {'Contr':>5} "
          f"{'Entry':>7} {'Exit':>7} {'P&L':>9} {'P&L%':>7}  Reason")
    sep()

    for r in rows:
        is_bf = bool(r["is_butterfly"])
        if is_bf:
            strike_str = f"{r['center_strike']:.0f} BF"
        else:
            side = (r["option_side"] or "")[:1].upper()
            strike_str = f"{side} {r['strike']:.0f}" if r["strike"] else "N/A"

        trade_type = "BUTTERFLY" if is_bf else (r["setup_type"] or "")[:8]
        entry_p    = r["entry_premium"] or 0
        exit_p     = r["exit_premium"]  or 0
        pnl        = r["pnl_usd"]       or 0
        pnl_p      = r["pnl_pct"]       or 0

        print(
            f"  {r['trade_id'][:8]:<10} "
            f"{trade_type:<8} "
            f"{strike_str:<14} "
            f"{r['contracts'] or 0:>5} "
            f"${entry_p:>6.2f} "
            f"${exit_p:>6.2f} "
            f"{pnl_str(pnl):>9} "
            f"{pct_str(pnl_p):>7}  "
            f"{(r['exit_reason'] or '')[:25]}"
        )
    print()


def show_circuit_breakers(conn):
    rows = conn.execute(
        "SELECT * FROM circuit_breaker_events ORDER BY event_time DESC LIMIT 5"
    ).fetchall()

    if not rows:
        return

    sep()
    print("  RECENT CIRCUIT BREAKER EVENTS")
    sep()
    for r in rows:
        print(
            f"  {to_et(r['event_time'])}  "
            f"losses={r['session_losses']}/{SESSION_LOSS_LIMIT}  "
            f"{r['reason'] or ''}"
        )
    print()


def show_regime_log(conn, n: int = 5):
    rows = conn.execute(
        "SELECT * FROM regime_log ORDER BY logged_at DESC LIMIT ?", (n,)
    ).fetchall()

    if not rows:
        return

    sep()
    print(f"  RECENT REGIME CHANGES (last {n})")
    sep()
    for r in rows:
        print(
            f"  {to_et(r['logged_at'])}  "
            f"{r['regime']:<22} "
            f"conviction={r['conviction']:.0%}  "
            f"macro={r['macro_context']}"
        )
    print()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    conn = connect()
    print()
    show_header()
    show_open_position(conn)
    show_today(conn)
    show_alltime(conn)
    show_by_strategy(conn)
    show_by_grade(conn)
    show_by_setup_type(conn)
    show_recent(conn)
    show_circuit_breakers(conn)
    show_regime_log(conn)
    sep("═")
    print()
    conn.close()


if __name__ == "__main__":
    main()
