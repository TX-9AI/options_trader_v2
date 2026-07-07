"""Offline self-test for data/candle_logger.py — patches the streamer with a
fake that emits synthetic Candle events, proving collect→dedupe→CSV works and
the output is readable by the analysis harness. No network / no creds."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import data.candle_logger as cl

ET = ZoneInfo("America/New_York")


class FakeCandle:
    def __init__(self, sym, t_ms, o, h, l, c, v=100):
        self.event_symbol, self.time = sym, t_ms
        self.open, self.high, self.low, self.close, self.volume = o, h, l, c, v


class FakeStreamer:
    """Mimics `async with DXLinkStreamer(session)` + subscribe_candle/get_event."""
    queue = []          # set by the test
    def __init__(self, session): pass
    async def __aenter__(self): self._i = 0; return self
    async def __aexit__(self, *a): return False
    async def subscribe_candle(self, symbols, interval, start_time=None, **kw):
        self._syms, self._interval, self._start = symbols, interval, start_time
    async def get_event(self, cls):
        if self._i < len(FakeStreamer.queue):
            ev = FakeStreamer.queue[self._i]; self._i += 1
            return ev
        await asyncio.sleep(9999)          # no more events ⇒ wait_for times out


def _ms(h, m):
    return int(datetime(2026, 7, 7, h, m, tzinfo=ET).astimezone(timezone.utc).timestamp() * 1000)


def main():
    fails = []
    # synthetic session: AMD 09:36–09:39, with a duplicate/corrected 09:37 (last wins),
    # a None-open snapshot marker (must be ignored), and an unrelated symbol.
    FakeStreamer.queue = [
        FakeCandle("AMD{=1m}", _ms(9, 36), 557.0, 557.4, 556.8, 557.2),
        FakeCandle("AMD{=1m}", _ms(9, 37), 557.2, 557.9, 557.1, 557.8),
        FakeCandle("AMD{=1m}", _ms(9, 37), 557.2, 558.3, 557.1, 558.1),   # correction, last wins
        FakeCandle("AMD{=1m}", None, None, None, None, None),              # snapshot marker, skip
        FakeCandle("AMD{=1m}", _ms(9, 38), 558.1, 558.5, 557.6, 557.7),
        FakeCandle("XXX{=1m}", _ms(9, 38), 1.0, 1.0, 1.0, 1.0),           # not requested, ignore
    ]

    cl.DXLinkStreamer = FakeStreamer                    # patch
    cl.QUIET_TIMEOUT_S = 0.5

    tmp = tempfile.mkdtemp()
    data = asyncio.get_event_loop().run_until_complete(
        cl._collect(session=None, symbols=["AMD", "NVDA"], interval="1m",
                    start_dt=datetime(2026, 7, 7, 9, 30, tzinfo=ET),
                    quiet_timeout=0.5, deadline=10)
    )

    def check(label, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        if not cond: fails.append(label)

    check("AMD collected 3 unique bars (dupe merged, None skipped)", len(data["AMD"]) == 3)
    check("NVDA present but empty (no events)", data.get("NVDA") == {})
    check("XXX (unrequested) not collected", "XXX" not in data)
    # last-write-wins on the 09:37 correction
    c37 = data["AMD"][_ms(9, 37)]
    check("09:37 correction applied (high=558.3)", float(c37.high) == 558.3)

    # rows + CSV (drop_forming disabled so we keep all)
    rows = cl._rows_from_candles(data["AMD"], drop_forming_before=None)
    check("rows sorted ascending by time", [r[0] for r in rows] == sorted(r[0] for r in rows))
    path = os.path.join(tmp, "AMD.csv")
    cl._write_csv(path, rows)
    check("CSV written", os.path.exists(path))

    # harness can read it
    sys.path.insert(0, "/home/claude/pnl_analytics")
    import timing_analysis as ta
    charts = ta.load_charts(tmp)
    ok = ("AMD" in charts and len(charts["AMD"]) == 3
          and list(charts["AMD"].columns[:5]) == ["timestamp", "open", "high", "low", "close"]
          and charts["AMD"]["ts"].notna().all())
    check("timing_analysis.load_charts reads it (3 rows, ET ts parsed)", ok)

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURE(S): " + "; ".join(fails)))
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    main()
