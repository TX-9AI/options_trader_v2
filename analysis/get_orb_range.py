#!/usr/bin/env python3
"""
analysis/get_orb_range.py — Resolve the opening range for the instrument and
write it to orb_range.json with an explicit STATE. Always writes the last
valid range so consumers always have something to show; the state tells them
what that range represents.

CRITICAL: the opening candle is fetched through the bot's own data layer
(data.market_data.fetch_candles) — the SAME source and symbol mapping the rest
of the bot uses (e.g. SPX -> ^SPX). Earlier versions fetched ^GSPC directly via
yfinance, a DIFFERENT Yahoo symbol than the bot's ^SPX feed, so the ORB range
never matched the bot's price feed or the operator's chart. Never fetch here
with a private symbol map again — always go through fetch_candles.

Three states only (the "call-out"):
    ESTABLISHED  — today's 9:30-9:35 ET candle is closed. high/low/date are
                   today's. The only tradeable state.
    IN_PROGRESS  — now is inside today's opening candle (09:30:00-09:34:59 ET).
                   Today's range is still forming; high/low/date carry the LAST
                   valid RTH range until the candle closes.
    EXPIRED      — no today range yet (pre-open, or today's candle not on the
                   feed yet). high/low/date carry the LAST valid RTH range
                   (e.g. Friday's on a Monday pre-open).

v1.0 — original — ^GSPC via direct yfinance, most-recent 9:30 candle, no guard.
v1.1 — 2026-07-02 — strict today-only gating (wrote nothing before ready).
v1.2 — 2026-07-02 — three-state model; always write the last valid range.
v1.3 — 2026-07-02 — SOURCE FIX: fetch the opening candle via
        data.market_data.fetch_candles() so the range uses the identical feed
        and symbol as the bot (^SPX for SPX, not ^GSPC). Removed the private
        yfinance import and SYMBOL_MAP entirely.

Exit codes (consumed by main._fetch_orb_range):
    0 = ESTABLISHED   1 = hard error (no data / write failure)
    2 = IN_PROGRESS   3 = EXPIRED
"""

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# Make the install root importable so this standalone script can reuse the
# bot's own data layer instead of duplicating symbol maps / fetch logic.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data.market_data import fetch_candles

ET = ZoneInfo("US/Eastern")
OUTPUT_PATH = os.path.expanduser("~/options-trader/orb_range.json")

STATUS_ESTABLISHED = "ESTABLISHED"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_EXPIRED     = "EXPIRED"

EXIT_CODE = {
    STATUS_ESTABLISHED: 0,
    STATUS_IN_PROGRESS: 2,
    STATUS_EXPIRED:     3,
}

# Enough 5m candles to always include at least one prior trading day's open
# (for the EXPIRED/IN_PROGRESS carry) even across a weekend.
ORB_CANDLE_LOOKBACK = 200


def resolve_symbol() -> str:
    """argv[1] -> OT_INSTRUMENT env -> QQQ."""
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    return os.environ.get("OT_INSTRUMENT", "QQQ")


def _candle_to_range(ts, row, status: str, symbol: str, now: datetime) -> dict:
    high = float(row["high"])
    low = float(row["low"])
    return {
        "status":     status,
        "date":       ts.strftime("%Y-%m-%d"),
        "high":       round(high, 4),
        "low":        round(low, 4),
        "width":      round(high - low, 4),
        "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S ET"),
        "symbol":     symbol,
    }


def resolve_orb_range(symbol: str) -> dict:
    """Return the last valid opening range tagged with its state.

    Raises ValueError only when there is no usable data at all.
    """
    now = datetime.now(ET)
    today = now.date()
    # Today's opening candle is forming during 09:30:00-09:34:59 ET.
    in_opening_window = (now.hour == 9 and 30 <= now.minute <= 34)

    # SAME feed/symbol as the bot (SPX -> ^SPX). ET-indexed OHLCV, lowercase cols.
    df = fetch_candles(symbol, "5m", ORB_CANDLE_LOOKBACK)
    if df is None or df.empty:
        raise ValueError(f"no 5m data returned for {symbol}")

    opens = [(ts, row) for ts, row in df.iterrows()
             if ts.hour == 9 and ts.minute == 30]
    todays = [(ts, row) for ts, row in opens if ts.date() == today]
    priors = [(ts, row) for ts, row in opens if ts.date() < today]

    def last_valid_prior():
        for ts, row in reversed(priors):
            if float(row["high"]) > float(row["low"]):
                return ts, row
        return None

    # ── IN_PROGRESS: inside today's opening window — carry last valid range ──
    if in_opening_window:
        pv = last_valid_prior()
        if pv is None:
            raise ValueError("no prior valid opening range to carry (IN_PROGRESS)")
        return _candle_to_range(pv[0], pv[1], STATUS_IN_PROGRESS, symbol, now)

    # ── ESTABLISHED: past the window and today's candle is present + valid ──
    if todays:
        ts, row = todays[-1]
        if float(row["high"]) > float(row["low"]):
            return _candle_to_range(ts, row, STATUS_ESTABLISHED, symbol, now)
        # Today's candle present but degenerate — fall through to EXPIRED.

    # ── EXPIRED: pre-open, or today's candle not on the feed yet ──
    pv = last_valid_prior()
    if pv is None:
        raise ValueError("no valid opening range found in lookback window")
    return _candle_to_range(pv[0], pv[1], STATUS_EXPIRED, symbol, now)


def main():
    symbol = resolve_symbol()
    try:
        result = resolve_orb_range(symbol)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
        with open(OUTPUT_PATH, "w") as f:
            json.dump(result, f, indent=2)
    except Exception as e:
        print(f"ERROR writing {OUTPUT_PATH}: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"ORB range [{result['status']}]: {result['symbol']} {result['date']} "
          f"H={result['high']} L={result['low']} W={result['width']}")
    print(f"Written to: {OUTPUT_PATH}")
    sys.exit(EXIT_CODE.get(result["status"], 1))


if __name__ == "__main__":
    main()
