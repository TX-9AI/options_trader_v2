# options_trader v2.2 — Vertigo Capital

**QQQ/SPX 0DTE | TastyTrade | Regime-Aware | GEX-Live | Strategy-Aware Exits | Auto-Sized**

Institutional-grade 0DTE options trading bot. Classifies intraday market regime every 15 seconds and deploys the appropriate strategy. GEX (Gamma Exposure) is computed in real time from the live options chain — no external API required. Position sizing is automatic. Supports paper and live trading via TastyTrade SDK.

---

## Architecture

### Regime Classification

ADX is computed from the **5-minute timeframe**, matching the bot's actual trading horizon. Using a slower timeframe (e.g. 1H) causes trend days to misclassify as RANGING for hours after a breakout has already happened.

| Regime | Strategy |
|--------|----------|
| TRENDING_BULL / TRENDING_BEAR | ORB long call/put (9:30-11:00 AM) |
| BREAKOUT_VOLATILE | ORB long call/put (9:30-11:00 AM) |
| SWEEP_REVERSAL | SweepReversal (OTM gamma play) |
| RANGING | Iron Condor (11:00 AM-2:00 PM), Butterfly fallback (12:00-2:00 PM if GEX PINNING) |
| COMPRESSION | Butterfly (GEX pin-centered, 12:00-2:00 PM) |

Every regime has a strategy. The bot is designed to find at least one valid trade on nearly all trading days.

### Strategies

**ORB (Opening Range Breakout)**
- 5-minute opening range locked at 9:30-9:35 ET
- Range fetched from historical candle data on every startup — survives restarts and fresh installs at any time of day
- High-conviction setup that typically forms at the session open, independent of trend direction — triggered strictly by break-and-retest rules, no chasing
- States: `RANGING` -> `BREAK_HIGH/LOW_AWAITING_RETEST` -> `OPEN_LONG/SHORT`
- Entry requires retest (wick into the range, body stays outside) — no chasing a breakout that never pulls back
- Failed breaks re-arm the engine for the next attempt (multiple cracks per session)
- Single-leg long call or long put — strike selected near the ORB-projected 100% target
- Past 100% TP: trail tightens to track the nearest unfilled 1-minute FVG — no hard exit at target, position can keep running
- At 50% TP: trailing stop arms and locks in profit
- **ORB entries valid until 11:00 AM ET only**

**Sweep Reversal**
- Detects liquidity sweeps at key levels (PDH/PDL, equal highs/lows, session H/L)
- OTM options selected by delta targeting (pure gamma play)
- BOS (Break of Structure) exit on 1-minute chart — candle closes only, no wicks
- Directional entries cut off at 2:00 PM ET

**Iron Condor (Legged Entry)**
- RANGING regime fallback — fires when no GEX pin is available for a butterfly
- Strike selection: **Bollinger Band anchored only, no delta**
  - Short call = lowest liquid strike at or above BB upper band
  - Short put = highest liquid strike at or below BB lower band
  - BB bands are the structural range boundaries on a ranging day — the only geometrically correct anchor for a neutral credit spread
  - Delta deliberately excluded — it is relative to wherever price happens to sit at decision time, not the actual range boundaries
- Sanity guardrail: short strike distance must be within 1.2x the ATM straddle expected move
- Wing widths: fixed (25pt SPX, $5 QQQ) from the short strikes
- **Legged entry**: bot identifies both vertical spread locations at decision time, then waits for price to reach within 2 strikes of each short strike before firing that leg
  - Leg 1 fires when price approaches the first side's short strike
  - Leg 2 queues after Leg 1 fills, fires when price approaches the opposite side
  - If regime flips away from RANGING before a leg fires, that pending leg is cancelled
  - Already-filled legs are never cancelled — they manage independently
  - If Leg 2 never fires, Leg 1 runs as a standalone vertical
- Exit per leg: 25% stop loss OR $0.05 nickel close (cleaner than expiry assignment risk)
- Regime-flip exit: any non-RANGING regime flip while a leg is open triggers immediate exit
- **Entry window: 11:00 AM - 2:00 PM ET**

**Debit Butterfly (GEX Pin-Centered)**
- Fires only in RANGING or COMPRESSION regime, requires GEX environment to be PINNING
- Center strike = GEX pin strike (not ATM)
- Entry gated by proximity check: price must be within 1x the session expected move of the pin
- Fixed wing widths: 25 points on SPX, $5 on QQQ/SPY
- One butterfly per RTH session
- Regime-flip exit: exits immediately if regime flips to TRENDING
- TP: 20% of max profit | SL: 25% of net debit | 2.5hr max hold
- **Entry window: 12:00 PM - 2:00 PM ET**

### GEX Integration

Computed live from the TastyTrade options chain every 15 seconds. No external scraping required.

```
call_gex = gamma x open_interest x 100 x spot_price
put_gex  = gamma x open_interest x 100 x spot_price x -1
net_gex  = call_gex + put_gex (summed across all strikes)
```

Derived levels: call wall, put wall, pin strike, flip strike, GEX environment

GEX informs all strategies:
- **Butterfly** — centers on pin strike, requires PINNING environment and price proximity
- **Iron Condor** — not GEX-dependent (by design — it fires specifically when GEX is not PINNING)
- **Sweep Reversal** — confluence boost when sweep hits call/put wall
- **ORB** — DAMPENING (x0.75 conviction) or AMPLIFYING (x1.15 conviction)

### Regime-Flip Exits

The bot tracks the current regime on every tick and exits neutral positions when their core assumption breaks:

| Position | Exits on |
|----------|----------|
| Butterfly | TRENDING_BULL, TRENDING_BEAR, BREAKOUT_VOLATILE |
| Iron Condor leg | Any non-RANGING regime |
| ORB | Range violation (1m close back inside range) — not regime-based |
| Sweep Reversal | BOS on 1m structure — not regime-based |

### Position Sizing (Auto)

Risk per trade configurable in `config.py`

- Grade A = 1.5x base risk | Grade B = 1.0x base risk
- **There is no Grade C.** Below-threshold setups return `None` and never fire, regardless of capital.
- Butterfly sizing halved when VIX in 15-20 zone

### Session Windows

| Strategy | Entry Window | Notes |
|----------|-------------|-------|
| ORB | 9:30 AM - 11:00 AM ET | After 11 AM, re-arm continues but no new entries |
| Iron Condor | 11:00 AM - 2:00 PM ET | Takes over when ORB window closes |
| Butterfly | 12:00 PM - 2:00 PM ET | Narrower window, requires GEX PINNING |
| Sweep Reversal | RTH - 2:00 PM ET | Fires anytime a sweep is detected |
| Hard close | 3:45 PM ET | All positions |
| VIX > 20 | Block butterflies | — |
| Fed day | Block all entries | — |

---

## Changelog

### v2.2 — 2026-06-30 (evening session)
- **Iron Condor added**: legged entry via price-triggered vertical spreads, RANGING regime fallback when no GEX pin available for butterfly
- **BB-anchored strike selection**: short strikes placed at/outside Bollinger Band boundaries — no delta involved. Delta is relative to current price, not range structure; BB bands are the correct geometric anchor for a neutral credit spread
- **Legged entry state machine**: DECIDED -> LEG1_FILLED -> COMPLETE, each leg fires independently when price reaches within 2 strikes of the target short strike
- **Regime-flip exits**: butterfly exits immediately on TRENDING regime flip; condor leg exits on any non-RANGING flip — the regime at entry is the core assumption of both trades
- **ORB cutoff moved to 11:00 AM**: tighter ORB window, condor takes over from 11 AM
- **Condor exit logic**: 25% stop loss per leg, $0.05 nickel close (cleaner than holding to expiry assignment risk)
- **`structure_analyzer.py` crash fixed**: None-format crash on nearest_resistance/nearest_support when no S/R levels exist early in session — was silently breaking run_analysis() on every tick
- **`orb_engine.py` range persistence fixed (v2)**: range now set before cutoff check, so restart after 2 PM correctly shows H/L/width + EXPIRED state instead of "Waiting for 9:35"
- **`check_versions.sh`**: recursive version header and critical-string verification script added to repo

### v2.1 — 2026-06-30
- ADX fixed: now sourced from 5m timeframe instead of 1H
- ORB engine rewritten: full state model, re-arms after invalidation or position close
- ORB range persistence: fetched from historical candles on every startup
- ORB FVG trail: past 100% TP, trail tightens to nearest unfilled 1m FVG instead of hard-exiting
- Butterfly overhauled: GEX pin-centered, proximity gate, fixed wings, noon-2PM window, one-per-session
- Grade C eliminated: below-threshold setups return None
- status.py rewritten: structured ORB display, No Trade instead of UNKNOWN
- Telegram alerts reduced to 4 events
- Graceful shutdown alert via SIGTERM/SIGINT handler
- push.sh hardened: self-healing remote URLs, diverged history handling
- setup_ec2.sh: GitHub repo prompt accepts full URL or owner/repo format

### v2.0 — 2026-06-27
- GEX computed live from TastyTrade options chain
- Strategy-aware exit routing
- Telegram alerts replace Twilio SMS
- configure.sh, snapshot.sh added

### v1.0 — 2026-06-25
- Initial release

---

## Deployment

### Option 1 — Web install (mobile / Terminus / any SSH client)

```bash
curl -fsSL https://raw.githubusercontent.com/TX-9AI/options_trader_v2/main/install.sh -o install.sh && bash install.sh
```

Have ready:
- TastyTrade Client Secret, Refresh Token, Account Number
- Telegram Bot Token and Chat ID
- GitHub repo (optional — only the source-of-truth server needs this)

### Multi-server workflow

One server is git-connected (typically QQQ). Develop and patch there, push to GitHub, deploy additional instances (SPX, future symbols) fresh via the install one-liner. Skip the GitHub prompt on follower servers by pressing ENTER.

---

## Key Commands

### Service control
```bash
sudo systemctl start optionsbot
sudo systemctl stop optionsbot
sudo systemctl restart optionsbot
```

### Monitoring
```bash
python status.py          # Live status + ORB H/L/width + GEX pin
python query.py           # Performance dashboard
journalctl -u optionsbot -f --no-pager | grep -v "tastytrade\|FEED_DATA\|received"
```

### Clearing the Python bytecode cache

**Always do this after uploading new code, before restarting the service.**

```bash
cd ~/options-trader
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
sudo systemctl restart optionsbot
```

This is the single most common cause of "I pushed the fix but it's still broken."

### Verify all fixes are present
```bash
bash check_versions.sh
```
Recursively checks every .py and .sh file's version header and runs 28 critical-string checks. Run after any fresh deploy to confirm all fixes actually landed.

### Push changes to GitHub
```bash
bash push.sh "your message"
```

### Snapshot
```bash
bash snapshot.sh
```

### Clean restart (wipes trade history)
```bash
sudo systemctl stop optionsbot
rm -f ~/options-trader/trades.db ~/options-trader/bot.log
sudo systemctl start optionsbot
```

---

## Telegram Alerts

Exactly 4 events:
1. Bot started
2. Bot stopped
3. Trade entered (includes ticker, strikes, credit/debit, total)
4. Trade closed (win/loss with P&L)

---

## File Structure

```
options_trader_v2/
├── main.py                    # Main loop, regime dispatch, GEX, entry/exit
├── config.py                  # All tunable parameters
├── status.py                  # Live status (ORB H/L/width, regime, GEX, strategy)
├── query.py                   # Performance dashboard
├── check_versions.sh          # Recursive version/fix verification
├── fix_structure_analyzer.sh  # One-shot patch script (baked into repo now)
├── push.sh                    # Git push, self-healing
├── setup_ec2.sh               # EC2 setup
├── install.sh                 # Web installer
├── snapshot.sh                # Bot state backup
├── analysis/
│   ├── orb_engine.py          # ORB state machine (v1.2)
│   ├── trend_engine.py        # ADX from 5m (v1.1)
│   ├── structure_analyzer.py  # FVGs, S/R, swings (v1.1 None-crash fix)
│   ├── regime_classifier.py
│   ├── volatility_engine.py   # BB bands, VWAP, ATR
│   └── liquidity_mapper.py
├── strategy/
│   ├── orb_strategy.py
│   ├── butterfly_strategy.py
│   ├── iron_condor_strategy.py  # NEW v2.2 — legged, BB-anchored
│   ├── sweep_reversal_strategy.py
│   └── base_strategy.py         # Extended with 4-leg condor fields
├── execution/
│   ├── exit_engine.py         # v1.3 — regime-flip exits for butterfly/condor
│   ├── entry_engine.py
│   └── position_manager.py    # v1.4 — threads regime to exit engine
├── risk/
│   ├── setup_scorer.py        # A/B only, no Grade C
│   ├── risk_manager.py
│   └── session_guard.py
├── data/
│   ├── gex_data.py
│   ├── options_chain.py
│   ├── market_data.py
│   ├── data_cache.py
│   ├── macro_data.py
│   └── tasty_client.py
├── database/trade_logger.py
├── notifications/
│   ├── alert_manager.py       # 4 events only, ticker included
│   └── telegram_sender.py
└── utils/
    ├── math_utils.py
    └── time_utils.py
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

---

## Security

- All credentials stored in systemd environment only — never in source files
- `.gitignore` excludes `credentials.py` and `*.pem`
- `snapshot.sh` redacts secrets before archiving
