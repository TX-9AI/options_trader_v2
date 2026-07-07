"""
data/candle_logger.py — end-of-day 1-minute candle logger.
v1.0 — 2026-07-07 — Pulls 1-min OHLC candles from the SAME DXLink/DXFeed session
        the bot trades on (via get_session()) and writes one CSV per symbol per
        day, in the format the P&L analysis harnesses expect. This is the feed
        the fills/marks/greeks price against — so analysis is done against the
        exact data set the trades executed on, not yfinance (which diverges,
        especially on the 5-min opening range).

Design:
  - Reuses tasty_client.get_session() (auth) and get_loop() (the existing
    background asyncio loop) — no second login, no second event loop.
  - subscribe_candle(symbols, "1m", start_time=<today 09:30 ET>) backfills the
    session's candles from DXFeed, then we drain events until a quiet gap
    (backfill complete) or a hard deadline. Last write wins per candle time
    (DXFeed may re-send/correct a bar).
  - Writes {out_dir}/{YYYY-MM-DD}/{SYMBOL}.csv → timestamp,open,high,low,close,volume
    (timestamps ET ISO). timing_analysis.py reads this directly (--charts <dir>).

FIRST-RUN CHECKLIST (only these two need confirming on one box):
  1. History depth: if backfill returns few/no bars for start_time=09:30, your
     entitlement is thin → run intraday or switch to live-append (see --live-append
     note in README). Same-day intraday is normally available.
  2. Index symbology: equities/ETFs (AMD, UNH, NVDA, QQQ...) use the plain ticker.
     SPX index may need a specific DXFeed symbol — pass it via --symbol-map
     (e.g. --symbol-map SPX=SPX) and verify the SPX file is populated.

Usage:
    python -m data.candle_logger --symbols AMD,UNH,NVDA --out /var/lib/opt/candles
    python -m data.candle_logger --symbols SPX,QQQ --out ./candles --symbol-map SPX=SPX
    python -m data.candle_logger --symbols AMD --out ./candles --date 2026-07-07
"""
import argparse
import asyncio
import csv
import logging
import os
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from tastytrade import DXLinkStreamer          # module-level so tests can patch
from tastytrade.dxfeed import Candle

from data.tasty_client import get_session, get_loop

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

INTERVAL = "1m"
DEFAULT_START_HM = (9, 30)
QUIET_TIMEOUT_S = 8.0          # no events for this long ⇒ backfill complete
MAX_WAIT_S = 180.0            # hard ceiling on the whole collection


def _base_symbol(event_symbol: str) -> str:
    """'AMD{=1m}' -> 'AMD'."""
    return (event_symbol or "").split("{")[0]


async def _collect(session, symbols, interval, start_dt, quiet_timeout, deadline):
    """Subscribe + drain Candle events into {symbol: {time_ms: Candle}}."""
    data = {s: {} for s in symbols}
    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe_candle(symbols, interval, start_time=start_dt)
        loop = asyncio.get_running_loop()
        end = loop.time() + deadline
        while loop.time() < end:
            remaining = end - loop.time()
            try:
                c = await asyncio.wait_for(
                    streamer.get_event(Candle),
                    timeout=min(quiet_timeout, max(0.1, remaining)),
                )
            except asyncio.TimeoutError:
                break                                   # quiet gap ⇒ done
            base = _base_symbol(getattr(c, "event_symbol", ""))
            if base in data and c.time is not None and c.open is not None:
                data[base][int(c.time)] = c             # last write wins
    return data


def _rows_from_candles(by_time, drop_forming_before=None):
    """Sorted [(ts_et, o,h,l,c,v)] from {time_ms: Candle}."""
    rows = []
    for t_ms in sorted(by_time):
        c = by_time[t_ms]
        ts = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).astimezone(ET)
        if drop_forming_before is not None and ts >= drop_forming_before:
            continue                                    # skip still-forming minute
        rows.append((ts, c.open, c.high, c.low, c.close, getattr(c, "volume", None)))
    return rows


def _write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for ts, o, h, l, c, v in rows:
            w.writerow([ts.isoformat(), o, h, l, c, v if v is not None else ""])


def dump_session_candles(symbols, out_dir, date=None, start_hm=DEFAULT_START_HM,
                         interval=INTERVAL, quiet_timeout=QUIET_TIMEOUT_S,
                         max_wait=MAX_WAIT_S, drop_forming=True):
    """Pull today's (or `date`'s) 1-min candles for `symbols` and write CSVs.
    Returns {symbol: (path, n_bars)}."""
    session = get_session()
    d = date or datetime.now(ET).date()
    start_dt = datetime.combine(d, dtime(*start_hm), tzinfo=ET)

    loop = get_loop()
    fut = asyncio.run_coroutine_threadsafe(
        _collect(session, list(symbols), interval, start_dt, quiet_timeout, max_wait),
        loop,
    )
    data = fut.result(timeout=max_wait + 30)

    now_min = datetime.now(ET).replace(second=0, microsecond=0) if drop_forming else None
    out_day = os.path.join(out_dir, d.isoformat())
    os.makedirs(out_day, exist_ok=True)

    written = {}
    for sym in symbols:
        rows = _rows_from_candles(data.get(sym, {}), drop_forming_before=now_min)
        path = os.path.join(out_day, f"{sym}.csv")
        _write_csv(path, rows)
        written[sym] = (path, len(rows))
        if rows:
            logger.info("candle_logger: %s → %s (%d bars, %s–%s ET)",
                        sym, path, len(rows), rows[0][0].strftime("%H:%M"),
                        rows[-1][0].strftime("%H:%M"))
        else:
            logger.warning("candle_logger: %s → 0 bars (check symbology/entitlement)", sym)
    return written


def _parse_symbol_map(items):
    m = {}
    for it in items or []:
        if "=" in it:
            k, v = it.split("=", 1)
            m[k.strip()] = v.strip()
    return m


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True, help="comma-separated tickers")
    ap.add_argument("--out", required=True, help="output base directory")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today ET)")
    ap.add_argument("--symbol-map", nargs="*", default=None,
                    help="override DXFeed symbol, e.g. SPX=SPX")
    ap.add_argument("--start", default="09:30", help="session start HH:MM ET")
    args = ap.parse_args()

    smap = _parse_symbol_map(args.symbol_map)
    symbols = [smap.get(s.strip(), s.strip()) for s in args.symbols.split(",") if s.strip()]
    d = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None
    hh, mm = (int(x) for x in args.start.split(":"))

    written = dump_session_candles(symbols, args.out, date=d, start_hm=(hh, mm))
    total = sum(n for _, n in written.values())
    print(f"candle_logger: wrote {len(written)} files, {total} bars total → {args.out}")
    for sym, (path, n) in written.items():
        print(f"  {sym:<6} {n:>4} bars  {path}")


if __name__ == "__main__":
    main()
