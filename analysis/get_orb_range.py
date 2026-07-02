#!/usr/bin/env python3
"""
analysis/get_orb_range.py — Resolve the opening-range for the instrument and
write it to orb_range.json with an explicit STATE. Always writes the last
valid range so consumers always have something to show; the state tells them
what that range represents.

Three states only (the "call-out"):
    ESTABLISHED  — today's 9:30-9:35 ET candle is closed. high/low/date are
                   today's. This is the only tradeable state.
    IN_PROGRESS  — right now is inside today's opening candle (09:30:00-09:34:59
                   ET). Today's range is still forming; high/low/date carry the
                   LAST valid RTH range until the candle closes.
    EXPIRED      — no today range yet (pre-open, or today's candle not on the
                   feed yet). high/low/date carry the LAST valid RTH range
                   (e.g. Friday's on a Monday pre-open).

v1.0 — original — most-recent 9:30 candle in a 5d window, no date/state guard
        (wrote yesterday's range as today's before 9:35). Instrument resolved
        via a sudo `systemctl show` subprocess.
v1.1 — 2026-07-02 — strict today-only + completeness gating; wrote nothing
        before the candle was ready.
v1.2 — 2026-07-02 — replace strict/refuse behavior with the three-state model
        above. Always write the last valid range; classify it ESTABLISHED /
        IN_PROGRESS / EXPIRED. Instrument from argv[1] -> OT_INSTRUMENT -> QQQ.

Exit codes (consumed by main._fetch_orb_range):
    0 = ESTABLISHED   1 = hard error (no data / write failure)
    2 = IN_PROGRESS   3 = EXPIRED

Output file: ~/options-trader/orb_range.json
{
    "status": "ESTABLISHED",
    "date": "2026-07-01",
    "high": 729.70,
    "low":  725.98,
    "width": 3.72,
    "fetched_at": "2026-07-01 09:36:00 ET",
    "symbol": "QQQ"
}
"""

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

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

SYMBOL_MAP = {"QQQ": "QQQ", "SPY": "SPY", "SPX": "^GSPC"}


def resolve_symbol() -> str:
    """argv[1] -> OT_INSTRUMENT env -> QQQ. No systemd/subprocess."""
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    return os.environ.get("OT_INSTRUMENT", "QQQ")


def _candle_to_range(ts, candle, status: str, symbol: str, now: datetime) -> dict:
    high = float(candle["high"])
    low = float(candle["low"])
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

    yf_symbol = SYMBOL_MAP.get(symbol.upper(), symbol)
    df = yf.download(yf_symbol, period="5d", interval="5m", progress=False)
    if df.empty:
        raise ValueError(f"no 5m data returned for {symbol}")

    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(ET)
    else:
        df.index = df.index.tz_convert(ET)

    # All 9:30 opening candles in the window, split into today's vs prior days'.
    opens = [(ts, row) for ts, row in df.iterrows()
             if ts.hour == 9 and ts.minute == 30]
    todays = [(ts, row) for ts, row in opens if ts.date() == today]
    priors = [(ts, row) for ts, row in opens if ts.date() < today]

    def last_valid_prior():
        # Most recent completed prior-day opening candle with a real range.
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
        raise ValueError("no valid opening range found in 5d window")
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
