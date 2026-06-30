# options_trader v2.1 — Vertigo Capital

**QQQ/SPX 0DTE | TastyTrade | Regime-Aware | GEX-Live | Strategy-Aware Exits | Auto-Sized**

Institutional-grade 0DTE options trading bot. Classifies intraday market regime every 15 seconds and deploys the appropriate strategy. GEX (Gamma Exposure) is computed in real time from the live options chain — no external API required. Position sizing is automatic. Supports paper and live trading via TastyTrade SDK.

---

## Architecture

### Regime Classification

ADX is computed from the **5-minute timeframe**, matching the bot's actual trading horizon. Using a slower timeframe (e.g. 1H) causes trend days to misclassify as RANGING for hours after a breakout has already happened.

| Regime | Strategy |
|--------|----------|
| COMPRESSION | ButterflyStrategy (GEX pin-centered) |
| SWEEP_REVERSAL | SweepReversal (OTM gamma play) |
| TRENDING_BULL / TRENDING_BEAR | ORB debit spread |
| RANGING | No new entries |

### Strategies

**ORB (Opening Range Breakout)**
- 5-minute opening range locked at 9:30-9:35 ET
- Range is fetched from historical candle data on every startup — not dependent on the bot being alive during the live 9:30 candle. A restart at noon, or a fresh install after 9:35 ET, immediately recovers the correct range instead of waiting for the next day.
- States: `RANGING` -> `BREAK_HIGH/LOW_AWAITING_RETEST` -> `OPEN_LONG/SHORT`
- Entry requires a genuine retest (wick into the range, body stays outside) — no chasing a breakout that never pulls back
- A failed/invalidated break re-arms the engine to watch for the next attempt, up to three or more tries per session until the entry cutoff
- ORB entries valid until noon ET only

**Sweep Reversal**
- Detects liquidity sweeps at key levels
- OTM options selected by delta targeting (pure gamma play)
- BOS (Break of Structure) exit on 1-minute chart
- Directional entries cut off at 2:00 PM ET

**Debit Butterfly (GEX Pin-Centered)**
- Fires only in RANGING or COMPRESSION regime
- Center strike = the GEX pin strike (not ATM) — requires GEX environment to be PINNING
- Entry gated by a volatility-based proximity check: price must be within 1x the expected move for the remaining session (scales with VIX) of the pin strike. Too far from the pin = no edge = no trade.
- Fixed wing widths: 25 points on SPX, $5 on QQQ/SPY (not ATR-scaled)
- One butterfly per RTH session — no second attempt same day
- Entry window: 12:00 PM - 2:00 PM ET only
- TP: 20% of max profit | SL: 25% of net debit | 2.5hr max hold

### GEX Integration

Computed live from the TastyTrade options chain every 15 seconds. No external scraping required.

```
call_gex = gamma x open_interest x 100 x spot_price
put_gex  = gamma x open_interest x 100 x spot_price x -1
net_gex  = call_gex + put_gex (summed across all strikes)
```

Derived levels: call wall, put wall, pin strike, flip strike, GEX environment

GEX informs all three strategies:
- **Butterfly** — centers on pin strike, requires PINNING environment and price proximity to the pin
- **Sweep Reversal** — confluence boost when sweep hits call/put wall
- **ORB** — `DAMPENING` (x0.75 conviction) or `AMPLIFYING` (x1.15 conviction)

### BOS Exit (Directional Trades)

Break of Structure on the 1-minute chart. Candle closes only — no wicks.

- **Long:** tracks highest close from entry. Protected HL = low of candle that made the new high. Close below protected HL = BOS -> exit
- **Short:** mirror image — tracks lowest close, protected LH = high of candle making new low
- Hard stop (25% premium loss) still fires first regardless of structure

### Position Sizing (Auto)

Risk per trade configurable in `config.py`

- Grade A = 1.5x base risk | Grade B = 1.0x base risk
- **There is no Grade C.** A setup scoring below the B threshold is not a trade — `setup_scorer.py` returns `None` and the bot logs `STRATEGY: NO TRADE`, regardless of available capital. A marginal setup never gets downsized into a smaller position; it simply doesn't fire.
- Butterfly sizing halved when VIX in 15-20 zone

### Session Rules

| Rule | Value |
|------|-------|
| RTH only | 9:30 AM - 4:00 PM ET |
| Hard close | 3:45 PM ET (all positions) |
| Directional entry cutoff | 2:00 PM ET |
| Butterfly entry window | 12:00 PM - 2:00 PM ET |
| ORB validity | Until noon ET |
| VIX > 20 | Butterflies blocked |
| Fed day | All entries blocked |

---

## Changelog

### v2.1 — 2026-06-30
- **ADX fixed**: now sourced from 5m timeframe instead of 1H — trend days no longer misclassify as RANGING
- **ORB engine rewritten**: full state model (`RANGING` -> `BREAK_*_AWAITING_RETEST` -> `OPEN_LONG/SHORT`), re-arms after invalidation or position close instead of ending the session after one attempt
- **ORB range persistence fixed**: fetched from historical candles on every startup, survives restarts and fresh installs at any time of day
- **Butterfly overhaul**: GEX pin-centered strikes, volatility-based proximity gate, fixed wing widths by instrument, noon-2PM entry window, one-per-session limit, TP reduced to 20%
- **Grade C eliminated**: below-threshold setups return `None` and never trade, regardless of capital — was previously only blocked by accident when capital happened to be insufficient
- **`status.py` rewritten**: structured ORB high/low/width/state display instead of fragile log-string matching; "No Trade" replaces "UNKNOWN" when nothing fires
- **Telegram alerts reduced to 4 events**: bot started, bot stopped, trade entered, trade closed (win/loss). Regime-change spam and circuit-breaker noise removed.
- **Graceful shutdown alert** added via SIGTERM/SIGINT handler — `systemctl stop`/`restart` now sends a Telegram notification instead of dying silently
- **`push.sh` hardened**: auto-repairs doubled/malformed remote URLs, handles diverged git history cleanly with a force-push prompt instead of leaving the working tree mid-conflict
- **`setup_ec2.sh`**: GitHub repo prompt now accepts a full URL or `owner/repo` format interchangeably

### v2.0 — 2026-06-27
- GEX computed live from TastyTrade options chain — no external API
- Pin strike displayed in `status.py`
- Strategy-aware exit routing — ORB, Sweep Reversal, and Butterfly each have distinct exit logic
- ORB stop: 1-min candle close back inside range (not BOS)
- Sweep Reversal: BOS exit on 1-min structure
- Butterfly: time/premium exits only, no BOS, no trail
- Telegram alerts replace Twilio SMS
- `configure.sh` — runtime menu with auto-restart
- `snapshot.sh` — dated tarball of running bot state
- `SWEEP_TARGET_DELTA` corrected

### v1.0 — 2026-06-25
- Initial release

---

## Deployment

### Option 1 — Web install (mobile / Terminus / any SSH client)

SSH into a fresh EC2 T2 micro (Ubuntu) and run:

```bash
curl -fsSL https://raw.githubusercontent.com/TX-9AI/options_trader_v2/main/install.sh -o install.sh && bash install.sh
```

Have ready:
- TastyTrade Client Secret, Refresh Token, and Account Number (from my.tastytrade.com -> Manage -> API)
- Telegram Bot Token and Chat ID
- GitHub repo (optional, only the designated "source of truth" server needs this — e.g. QQQ. Accepts `owner/repo` or a full URL.)

### Option 2 — Local install (Windows desktop)

1. Unpack to a local directory
2. Place your EC2 `.pem` key in the project folder
3. Double-click `install.bat`
4. Follow `setup_ec2.sh` prompts

### Multi-server workflow

Only one server should be git-connected (the "source of truth" — typically QQQ). Develop and patch there, push to GitHub, then deploy any additional instances (SPX, future symbols) fresh via the install one-liner above. This avoids merge conflicts between servers entirely — each fresh deploy just pulls the latest verified code, no git history to reconcile. Skip the GitHub prompt during `setup_ec2.sh` on any "follower" server by pressing ENTER.

---

## Key Commands

### Service control
```bash
sudo systemctl start optionsbot
sudo systemctl stop optionsbot
sudo systemctl restart optionsbot
sudo systemctl status optionsbot
```

### Runtime configuration
```bash
bash configure.sh
```
Interactive menu — change instrument, risk, paper/live mode, Telegram settings, TastyTrade credentials. Auto-restarts on exit if changes were made.

### Monitoring
```bash
python status.py          # Live status + ORB H/L/width + GEX pin
python query.py           # Performance dashboard
journalctl -u optionsbot -f --no-pager | grep -v "tastytrade\|FEED_DATA\|received"
```

### Pushing changes (source-of-truth server only)
```bash
bash push.sh                        # auto commit message
bash push.sh "your commit message"  # custom message
```
Self-repairs malformed remote URLs and handles diverged history with a clean force-push prompt — never leaves the working tree in a conflict state.

### Clearing the Python bytecode cache

**Always do this after pulling new code, before restarting the service.** Python will silently load stale compiled `.pyc` files from `__pycache__` otherwise, and your fix won't actually take effect even though the source file is correct.

```bash
cd ~/options-trader
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
sudo systemctl restart optionsbot
```

This is the single most common cause of "I pushed the fix but it's still broken" — the running process is still executing the old bytecode.

### Snapshot (backup running bot)
```bash
bash snapshot.sh
```
Creates a dated tarball of all bot files in `~/snapshots/`. Run before terminating an instance or after a heavy dev session.

### Clean restart (wipes trade history)
```bash
sudo systemctl stop optionsbot
rm -f ~/options-trader/trades.db ~/options-trader/bot.log
sudo systemctl start optionsbot
```

---

## Telegram Alerts

Exactly 4 events, nothing else:
1. Bot started
2. Bot stopped (including graceful shutdown on `systemctl stop`/`restart`)
3. Trade entered
4. Trade closed (win/loss with P&L)

No regime-change spam, no circuit-breaker pings, no periodic status updates. Check `status.py` directly if you want more detail.

Token and Chat ID stored as systemd environment variables — never in source code. Configure via `setup_ec2.sh` on install or `configure.sh` at runtime.

---

## File Structure

```
options_trader_v2/
├── main.py                    # Main loop, regime dispatch, GEX compute, entry/exit
├── config.py                  # All tunable parameters
├── setup_ec2.sh               # EC2 first-time setup (called by install.sh)
├── install.sh                 # Web installer — curl and run
├── install.bat                # Windows desktop launcher
├── configure.sh               # Runtime configuration menu
├── push.sh                    # Git push with self-healing remote/history handling
├── snapshot.sh                # Snapshot running bot to dated tarball
├── check_sdk.py               # TastyTrade SDK diagnostic tool
├── status.py                  # Live status dashboard (ORB H/L/width, regime, GEX)
├── query.py                   # Performance dashboard
├── requirements.txt           # Python dependencies
├── analysis/                  # Regime classifier, ORB engine, trend (5m ADX), volatility, structure, liquidity
├── data/                      # TastyTrade client, options chain, GEX calculator, macro
├── database/                  # Trade logger (SQLite)
├── execution/                 # Entry engine, exit engine (BOS), position manager
├── notifications/             # Telegram alerts (4 events only)
├── risk/                      # Risk manager, session guard, setup scorer (A/B only, no C)
├── strategy/                  # ORB, SweepReversal, Butterfly
└── utils/                     # Math, time utilities
```

---

## Dependencies

```
tastytrade
yfinance
pandas
numpy
requests
tzdata
```

Install: `pip install -r requirements.txt`

---

## Security

- Credentials (TastyTrade tokens, Telegram token) are stored only in the systemd service environment — never in source files
- `.gitignore` excludes `credentials.py` and `*.pem`
- `snapshot.sh` redacts all secrets from environment dumps before archiving
