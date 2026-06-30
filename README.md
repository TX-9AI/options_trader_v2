# options_trader v2.0 — Vertigo Capital

**QQQ/SPX 0DTE | TastyTrade | Regime-Aware | GEX-Live | Strategy-Aware Exits | Auto-Sized**

Institutional-grade 0DTE options trading bot. Classifies intraday market regime every 15 seconds and deploys the appropriate strategy. GEX (Gamma Exposure) is computed in real time from the live options chain — no external API required. Position sizing is automatic. Supports paper and live trading via TastyTrade SDK.

---

## Architecture

### Regime Classification

| Regime | Strategy |
|--------|----------|
| COMPRESSION | ButterflyStrategy (GEX pin-centered) |
| SWEEP_REVERSAL | SweepReversal (OTM gamma play) |
| TRENDING_BULL / TRENDING_BEAR | ORB debit spread |
| RANGING | No new entries |

### Strategies

**ORB (Opening Range Breakout)**
- 5-minute opening range locked at 9:30 ET
- Entry on confirmed 1-minute close outside range + retest
- Long calls or puts leveraging gamma on confirmed directional breakouts
- ORB entries valid until noon ET only

**Sweep Reversal**
- Detects liquidity sweeps at key levels
- OTM options selected by delta targeting (pure gamma play)
- BOS (Break of Structure) exit on 1-minute chart
- Directional entries cut off at 2:00 PM ET

**Debit Butterfly (Compression + GEX Pinning)**
- Fires only in COMPRESSION regime (Bollinger squeeze + low ADX)
- Center strike anchored to GEX pin zone when within $5 of price
- Grade A = PINNING environment + center within $2 of GEX pin
- Grade B = COMPRESSION regime, GEX neutral or moderate
- Blocked entirely when GEX environment = TRENDING
- Entries valid until 3:00 PM ET (late-day pinning window)
- 25% of max profit target | 25% loss stop | 2.5hr max hold

### GEX Integration

Computed live from the TastyTrade options chain every 15 seconds. No external scraping required.

```
call_gex = gamma × open_interest × 100 × spot_price
put_gex  = gamma × open_interest × 100 × spot_price × -1
net_gex  = call_gex + put_gex (summed across all strikes)
```

Derived levels: call wall, put wall, pin strike, flip strike, GEX environment

GEX informs all three strategies:
- **Butterfly** — centers on pin strike, blocked in TRENDING GEX
- **Sweep Reversal** — confluence boost when sweep hits call/put wall
- **ORB** — `DAMPENING` (×0.75 conviction) or `AMPLIFYING` (×1.15 conviction)

### BOS Exit (Directional Trades)

Break of Structure on the 1-minute chart. Candle closes only — no wicks.

- **Long:** tracks highest close from entry. Protected HL = low of candle that made the new high. Close below protected HL = BOS → exit
- **Short:** mirror image — tracks lowest close, protected LH = high of candle making new low
- Hard stop (25% premium loss) still fires first regardless of structure

### Position Sizing (Auto)

Risk per trade configurable in `config.py`

- Grade A = 1.5× base risk | Grade B = 1.0× base risk
- Below minimum score threshold → rejected, no trade (no Grade C)
- Butterfly sizing halved when VIX in 15–20 zone

### Session Rules

| Rule | Value |
|------|-------|
| RTH only | 9:30 AM – 4:00 PM ET |
| Hard close | 3:45 PM ET (all positions) |
| Directional entry cutoff | 2:00 PM ET |
| Butterfly entry cutoff | 3:00 PM ET |
| ORB validity | Until noon ET |
| VIX > 20 | Butterflies blocked |
| Fed day | All entries blocked |

---

## Changelog

### v2.0 — 2026-06-27
- GEX computed live from TastyTrade options chain — no external API
- Pin strike displayed in `status.py`
- Strategy-aware exit routing — ORB, Sweep Reversal, and Butterfly each have distinct exit logic
- ORB stop: 1-min candle close back inside range (not BOS)
- Sweep Reversal: BOS exit on 1-min structure
- Butterfly: time/premium exits only, no BOS, no trail
- Telegram alerts replace Twilio SMS
- Grade C eliminated — below minimum score = no trade
- `configure.sh` — runtime menu with auto-restart
- `snapshot.sh` — dated tarball of running bot state
- `SWEEP_TARGET_DELTA` corrected
- Butterfly entry cutoff moved to config (`BUTTERFLY_ENTRY_CUTOFF_ET`)

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
- TastyTrade Client Secret, Refresh Token, and Account Number (from my.tastytrade.com → Manage → API)
- Telegram Bot Token and Chat ID

### Option 2 — Local install (Windows desktop)

1. Unpack to a local directory
2. Place your EC2 `.pem` key in the project folder
3. Double-click `install.bat`
4. Follow `setup_ec2.sh` prompts

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
python status.py          # Live status + GEX pin
python query.py           # Performance dashboard
journalctl -u optionsbot -f --no-pager | grep -v "tastytrade\|FEED_DATA\|received"
```

### Snapshot (backup running bot)
```bash
bash snapshot.sh
```
Creates a dated tarball of all bot files in `~/snapshots/`. Run before terminating an instance or after a heavy dev session.

### Clean restart
```bash
sudo systemctl stop optionsbot
rm -f ~/options-trader/trades.db ~/options-trader/bot.log
sudo systemctl start optionsbot
```

---

## Telegram Alerts

- Startup, entry, exit, regime change, circuit breaker
- Token and Chat ID stored as systemd environment variables — never in source code
- Configure via `setup_ec2.sh` on install or `configure.sh` at runtime

---

## File Structure

```
options_trader_v1/
├── main.py                    # Main loop, regime dispatch, GEX compute, entry/exit
├── config.py                  # All tunable parameters
├── setup_ec2.sh               # EC2 first-time setup (called by install.sh)
├── install.sh                 # Web installer — curl and run
├── install.bat                # Windows desktop launcher
├── configure.sh               # Runtime configuration menu
├── snapshot.sh                # Snapshot running bot to dated tarball
├── check_sdk.py               # TastyTrade SDK diagnostic tool
├── status.py                  # Live status dashboard
├── query.py                   # Performance dashboard
├── requirements.txt           # Python dependencies
├── analysis/                  # Regime classifier, ORB engine, volatility, structure, liquidity
├── data/                      # TastyTrade client, options chain, GEX calculator, macro
├── database/                  # Trade logger (SQLite)
├── execution/                 # Entry engine, exit engine (BOS), position manager
├── notifications/             # Telegram alerts
├── risk/                      # Risk manager, session guard, setup scorer
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
