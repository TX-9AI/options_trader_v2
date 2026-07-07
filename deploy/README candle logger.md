# candle_logger — daily 1-min OHLC from the same feed you trade on

Logs 1-minute candles from the bot's **DXLink/DXFeed** session (the feed your
fills, marks, and greeks price against) to one CSV per symbol per day, in the
format the analysis harnesses read. Purpose: evaluate trades against the exact
data set they executed on, instead of yfinance (which diverges, especially on
the 5-min opening range).

No new dependency — `requirements.txt` already pins `tastytrade>=12.4.0`.
Reuses `data.tasty_client.get_session()` + `get_loop()` (no second login/loop).

## Install (per bot box)
1. Ship `data/candle_logger.py` with the rest of the repo (`push.sh --deploy`).
2. Edit `candle-logger.service`: set `User`, `WorkingDirectory`, `EnvironmentFile`,
   the venv python path, and **`--symbols` to this box's sharded tickers**.
3. Install the units:
   ```
   sudo cp deploy/candle-logger.service deploy/candle-logger.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now candle-logger.timer
   systemctl list-timers candle-logger.timer      # confirm next run = 16:05 ET
   ```

## First run — verify TWO things (the only real unknowns)
Run it manually once during or just after a session:
```
python -m data.candle_logger --symbols <this box's syms> --out /var/lib/opt_trader/candles --date $(date +%F)
```
Then check `/var/lib/opt_trader/candles/<date>/`:

1. **History depth** — each requested symbol should have a file with the full
   session (~09:30→close). If files are near-empty, `start_time=09:30` backfill
   is limited by your market-data entitlement → run intraday, or switch to
   live-append (keep a streamer open through RTH and append each closed 1-min
   candle; the streamer is already open for greeks/quotes, so it's cheap).
2. **Index symbology** — equities/ETFs (AMD, UNH, NVDA, QQQ…) use the plain
   ticker and should just work. If an **SPX** file is empty, pass its DXFeed
   symbol: `--symbol-map SPX=SPX` (try the plain form first; this is the one
   symbol to pin down, same class as the old ^GSPC/^SPX issue).

## Use with the analysis
```
python3 timing_analysis.py trades.db --charts /var/lib/opt_trader/candles/<date>/
```
Optional: have the fleet rsync `<date>/` dirs to the control server for a single
central pull.

## Output format
`<out>/<YYYY-MM-DD>/<SYMBOL>.csv` → `timestamp,open,high,low,close,volume`
(timestamps ET ISO). `timing_analysis.load_charts` reads this directly.

Validated offline with `test_candle_logger.py` (fake streamer): collection,
duplicate/correction handling (last-write-wins), snapshot-marker skipping,
sorting, CSV writing, and harness round-trip all pass. The only unvalidated
path is the live DXFeed call itself — hence the first-run check above.
