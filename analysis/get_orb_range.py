#!/usr/bin/env python3
"""
get_orb_range.py — Fetch the most recent 9:30-9:35 ET 5-minute candle and
write it to orb_range.json. That's it. Nothing else.

Any file that needs the ORB range reads orb_range.json. No log parsing.
No circular logic. No patchwork.

Output file: ~/options-trader/orb_range.json
{
    "date": "2026-07-01",
    "high": 729.70,
    "low":  725.98,
    "width": 3.72,
    "fetched_at": "2026-07-01 09:36:00 ET"
}

Called by:
  - main.py at startup and on each RTH session reset
  - status.py reads the file directly
  - orb_engine.py reads the file instead of fetching itself
"""

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

ET = ZoneInfo("US/Eastern")
OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orb_range.json")


def fetch_orb_range(symbol: str = "QQQ") -> dict:
    """Fetch the most recent 9:30 ET 5-min candle for the given symbol."""
    df = yf.download(symbol, period="5d", interval="5m", progress=False)
    if df.empty:
        raise ValueError(f"No 5m data returned for {symbol}")

    # Normalize column names (yfinance returns MultiIndex sometimes)
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]

    # Convert index to ET
    df.index = df.index.tz_convert(ET)

    # Find all 9:30 ET candles
    matches = [(ts, row) for ts, row in df.iterrows()
               if ts.hour == 9 and ts.minute == 30]

    if not matches:
        raise ValueError("No 9:30 ET candle found in 5m data")

    # Most recent 9:30 candle
    ts, candle = matches[-1]

    return {
        "date":       ts.strftime("%Y-%m-%d"),
        "high":       round(float(candle["high"]), 4),
        "low":        round(float(candle["low"]),  4),
        "width":      round(float(candle["high"]) - float(candle["low"]), 4),
        "fetched_at": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
        "symbol":     symbol,
    }


def get_instrument_from_env() -> str:
    """Read OT_INSTRUMENT from systemd service environment if available."""
    import subprocess
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "show", "optionsbot", "--property=Environment"],
            capture_output=True, text=True
        )
        import re
        match = re.search(r'OT_INSTRUMENT=([^ ]+)', result.stdout)
        if match:
            return match.group(1)
    except Exception:
        pass
    return os.environ.get("OT_INSTRUMENT", "QQQ")


def main():
    # Command line arg takes priority, then systemd env, then default QQQ
    symbol = sys.argv[1] if len(sys.argv) > 1 else get_instrument_from_env()
    try:
        result = fetch_orb_range(symbol)
        with open(OUTPUT_PATH, "w") as f:
            json.dump(result, f, indent=2)
        print(f"ORB range: {result['symbol']} {result['date']} "
              f"H={result['high']} L={result['low']} W={result['width']}")
        print(f"Written to: {OUTPUT_PATH}")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
