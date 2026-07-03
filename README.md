# options_trader v2.3 — Vertigo Capital

**QQQ/SPX/SPY 0DTE | TastyTrade | Regime-Aware | GEX-Live | Tracked Legged Condor | Broken-Wing Roll | Net Daily Loss Halt**

Institutional-grade 0DTE options trading bot. Classifies intraday market regime every 15 seconds and deploys the appropriate strategy. GEX (Gamma Exposure) is computed in real time from the live options chain — no external API required. Position sizing is automatic. Supports paper and live trading via TastyTrade SDK.

> **Version note:** v2.3 is a large architectural advance over v2.2 (tracked multi-leg condor, broken-wing roll, net daily-loss halt, rebuilt ORB invalidation model). It is intended to prove out across paper sessions before being promoted to **3.0** as the validated milestone. Treat the condor tracking and the broken-wing roll as new until they have real fills behind them.

---

## Architecture

### Regime Classification

ADX is computed from the **5-minute timeframe**, matching the bot's actual trading horizon. Using a slower timeframe (e.g. 1H) causes trend days to misclassify as RANGING for hours after a breakout has already happened.

The classifier is a **priority hierarchy** — sweep → breakout → compression → trending → ranging. The first condition that matches wins, so a genuine liquidity sweep is preferred over an ORB even inside the ORB window.

| Regime | Strategy |
|--------|----------|
| TRENDING_BULL / TRENDING_BEAR | ORB long call/put (9:30–11:00 AM) |
| BREAKOUT_VOLATILE | ORB long call/put (9:30–11:00 AM) |
| SWEEP_REVERSAL | SweepReversal (OTM gamma play) |
| RANGING | Iron Condor (11:00 AM–2:00 PM), Butterfly fallback (12:00–2:00 PM if GEX PINNING) |
| COMPRESSION | Butterfly (GEX pin-centered, 12:00–2:00 PM) |

Not every regime guarantees a fill (a trending regime with no confirmed ORB, or a compression regime with no GEX pin, may stand aside), but the bot is designed to find at least one valid trade on nearly all trading days.

### Strategies

**ORB (Opening Range Breakout)**
- 5-minute opening range = the 9:30–9:35 ET candle.
- **Range is sourced through the bot's own data layer** (`market_data.fetch_candles`) — the identical feed and symbol mapping the rest of the bot trades on (`^SPX` for SPX). It is no longer fetched from a separate symbol, so the opening range always agrees with the bot's price feed.
- **Three-state range model** written to `orb_range.json`; the file always carries the last valid range and declares its state:
  - `ESTABLISHED` — today's 9:30–9:35 candle has closed. The only tradeable state.
  - `IN_PROGRESS` — the clock is inside 9:30:00–9:34:59; today's range is still forming; the file carries the last valid range meanwhile.
  - `EXPIRED` — pre-open, or today's candle isn't on the feed yet; carries the last RTH range (e.g. Friday's on a Monday pre-open).
  - The engine only arms on `ESTABLISHED`/today — a carried prior-day range can never be traded.
- Entry requires a retest (wick into the range, body stays outside) — no chasing a breakout that never pulls back.
- **Two invalidation rules:**
  - **(a) Runaway breakout** — price runs to the 50%-TP level with no retest → INVALIDATED. This is the setup that most favors a sweep reversal; the ORB stands aside for it.
  - **(b) Retrace** — a 1-minute candle closes back inside the ORB range → INVALIDATED.
- **Regime-gated re-arm:** after a (b) retrace invalidation the engine re-arms and watches for another break **only while the regime is still ORB-friendly (RANGING/COMPRESSION)**. It does **not** re-arm after an (a) runaway (hand-off to sweep) or once the regime has shifted to sweep/trend/breakout. It re-checks each tick, so ORB can come back if the regime returns to friendly before 11:00.
- **ORB-window sweep override:** when an ORB signal fires but a sweep reversal is setting up with higher conviction, the bot takes the sweep. A breakout-without-retest is exactly when sweep odds spike.
- Single-leg long call or long put — strike near the ORB-projected 100% target.
- At 50% TP: trailing stop arms. Past 100% TP: trail tracks the nearest unfilled 1-minute FVG — no hard exit, the position can keep running.
- **ORB entries valid until 11:00 AM ET — HARD cutoff.** At 11:00 the ORB expires regardless of state (including awaiting-retest), and the bot works the other regimes.

**Sweep Reversal**
- Detects liquidity sweeps at key levels (PDH/PDL, equal highs/lows, session H/L).
- OTM options selected by delta targeting (pure gamma play).
- BOS (Break of Structure) exit on the 1-minute chart — candle closes only, no wicks.
- Directional entries cut off at 2:00 PM ET.

**Iron Condor (Legged Entry — Tracked)**
- RANGING regime fallback — fires when no GEX pin is available for a butterfly.
- **Each vertical is a fully tracked position.** The condor is the **only** strategy allowed to hold two positions at once (its two verticals); every other strategy is single-position. Each leg is managed, exited, and P&L'd independently, using credit-spread math (profit as the spread value falls).
- **Half-budget-per-side sizing:** each vertical is sized to half the grade budget. A B-grade $1,000 trade → two ~$500 verticals.
- Strike selection: **Bollinger Band anchored only, no delta.**
  - Short call = lowest liquid strike at/above the BB upper band.
  - Short put = highest liquid strike at/below the BB lower band.
  - Delta deliberately excluded — it is relative to where price sits, not the actual range boundaries.
- Sanity guardrail: short-strike distance must be within 1.2× the ATM straddle expected move.
- **Wing widths: narrow — 5 points on SPX, $5 on QQQ/SPY** (max loss ~$235/contract on a 5-wide SPX vertical, which is what makes half-budget sizing affordable).
- **Legged entry** (`DECIDED → LEG1_FILLED → COMPLETE`): the bot fixes both vertical locations at decision time, fires Leg 1 when price approaches the first short strike, then queues Leg 2 for the opposite side.
  - If the regime flips away from RANGING before a pending leg fires, that leg is cancelled.
  - Already-filled legs are never cancelled — they manage independently.
  - If Leg 2 never fires, Leg 1 runs as a standalone vertical.
- Exit per leg: 25% stop (spread value at 125% of credit) OR $0.05 nickel close.
- Regime-flip exit is **direction-aware**: a call spread only exits on TRENDING_BULL/BREAKOUT_VOLATILE (a bearish flip is favorable — hold); a put spread only exits on TRENDING_BEAR/BREAKOUT_VOLATILE.
- **Entry window: 11:00 AM – 2:00 PM ET.**

**Broken-Wing Roll (Condor Adjustment)** — *new in v2.3*
- When **both** condor verticals are open and price **tests one side**, the bot can roll the **untested** side toward price into a broken-wing butterfly.
- The roll fires **only if it makes the tested side risk-free** — i.e. cumulative credit collected covers the tested side's width:
  ```
  banked_condor_credit + roll_credit - close_cost  >=  tested_side_width
  ```
- The solver pulls live chain marks and takes the **smallest** roll toward price that clears risk-free (least new risk on the rolled side). If no roll achieves risk-free, it doesn't roll — the condor is managed normally.
- **Final form — the roll is a one-time transformation.** Once rolled, every leg is flagged `is_broken_wing` and the bot never adjusts it again: it locks the untested side's gains, removes loss risk on the tested side, and is managed to exit only (stop / nickel). Roll once, stand it, defend it.

**Debit Butterfly (GEX Pin-Centered)**
- Fires only in RANGING or COMPRESSION with a PINNING GEX environment.
- Center strike = GEX pin strike (not ATM).
- Entry gated by proximity: price within 1× the session expected move of the pin.
- Fixed wings: 25 points on SPX, $5 on QQQ/SPY.
- One butterfly per RTH session.
- Regime-flip exit: exits immediately on a flip to TRENDING.
- TP: 20% of max profit | SL: 25% of net debit | 2.5 hr max hold.
- **Entry window: 12:00 PM – 2:00 PM ET.**

### GEX Integration

Computed live from the TastyTrade options chain every 15 seconds. No external scraping.

```
call_gex = gamma x open_interest x 100 x spot_price
put_gex  = gamma x open_interest x 100 x spot_price x -1
net_gex  = call_gex + put_gex (summed across all strikes)
```

Derived levels: call wall, put wall, pin strike, flip strike, GEX environment. GEX centers the butterfly (requires PINNING + proximity), boosts sweep-reversal conviction at walls, and dampens/amplifies ORB conviction. The condor is intentionally not GEX-dependent — it fires specifically when GEX is *not* pinning.

### Regime-Flip Exits

| Position | Exits on |
|----------|----------|
| Butterfly | TRENDING_BULL, TRENDING_BEAR, BREAKOUT_VOLATILE |
| Iron Condor leg | Adverse trend into that side's short strikes (direction-aware) |
| ORB | Range violation (1m close back inside range) — not regime-based |
| Sweep Reversal | BOS on 1m structure — not regime-based |

### Position Sizing (Auto)

Risk per trade configurable via `OT_RISK_USD` (`config.py`).

- Grade A = 1.5× base risk | Grade B = 1.0× base risk.
- **There is no Grade C.** Below-threshold setups return `None` and never fire, regardless of capital.
- **Condor verticals are sized at half the grade budget per side** (two ~$500 verticals on a B-grade $1,000 trade).
- Butterfly sizing halved when VIX is in the 15–20 zone.

### Risk Management — Regime Reassessment & Net Daily Loss Halt

- **Regime reassessment after every losing trade.** A loss is fresh information about whether the current regime read still holds, so each losing exit forces a regime reclassification on the next tick (replaces the old count-based circuit breaker).
- **Net daily loss halt.** New entries are halted once the **day's NET realized P&L** is down by `DAILY_LOSS_LIMIT_USD` (default = one trade's risk). Wins offset losses — a green day keeps trading no matter how many individual losses stack up; only a genuinely red day (net down by the limit) halts.
  - The tally is **seeded from the DB on startup**, so the halt survives restarts within the session.
  - It halts **new entries only** — open positions keep being managed to their exits.
  - **Override:** raise the cap via `configure.sh` → *Daily loss cap* (option 6), or `r` to reset to the risk default. The bot restarts and re-evaluates against the new cap.

### Session Windows

| Strategy | Entry Window | Notes |
|----------|-------------|-------|
| ORB | 9:30 AM – 11:00 AM ET | HARD cutoff at 11:00 — ORB expires, other regimes take over |
| Iron Condor | 11:00 AM – 2:00 PM ET | Takes over when the ORB window closes |
| Butterfly | 12:00 PM – 2:00 PM ET | Narrower window, requires GEX PINNING |
| Sweep Reversal | RTH – 2:00 PM ET | Fires anytime a sweep is detected |
| Hard close | 3:45 PM ET | All positions |
| VIX > 20 | Block butterflies | — |
| Fed day | **Bot trades Fed days** | `is_fed_day` only boosts ORB conviction — entries are not blocked |

---

## Changelog

### v2.3 — 2026-07-02 (→ planned 3.0 once paper-validated)
- **Iron Condor legs are now tracked positions**: each vertical is written to the trade log, registered with the position manager, sized at half the grade budget, and managed/exited/P&L'd independently. The condor is the only strategy allowed two concurrent positions. (Previously legs were logged but never tracked — no exits, no P&L.)
- **Broken-wing roll added**: rolls the untested condor side into a BWB when the premium math makes the tested side risk-free (cumulative credit ≥ tested-side width); smallest-roll solver over live chain marks; one-time final adjustment flagged `is_broken_wing` — no further rolls.
- **Narrow SPX condor wings** 25 → 5 points (max loss ~$235/contract), enabling affordable half-budget-per-side sizing.
- **ORB range rebuilt as a three-state model** (ESTABLISHED / IN_PROGRESS / EXPIRED); always carries the last valid range; engine arms only on ESTABLISHED/today.
- **ORB range source unified**: fetched through `market_data.fetch_candles` (same feed/symbol as the bot, `^SPX` not `^GSPC`) — fixes the opening-range mismatch with the bot's price feed and the chart.
- **ORB 11:00 AM HARD cutoff** — expires even awaiting-retest states so the bot moves to other regimes.
- **Two ORB invalidation rules**: (a) runaway to the 50% TP without a retest (favors sweep), (b) 1m close back inside the range.
- **Regime-gated ORB re-arm**: re-arm after a (b) retrace only while the regime is ORB-friendly; stand down after (a) runaway or once the regime flips.
- **ORB-window sweep override**: take a higher-conviction sweep over the ORB inside the window.
- **Regime reassessment after every losing trade** (replaces count-based circuit breaker).
- **Net daily loss halt**: halts new entries when the day's net P&L is down by the daily loss limit (default = per-trade risk, `OT_DAILY_LOSS_LIMIT`); seeded from the DB (survives restarts); override menu in `configure.sh`.
- **`setup_ec2.sh`**: cleanup of the deploy dir and `install.sh` before dropping to the shell in the install dir with venv active.

### v2.2 — 2026-06-30 (evening session)
- Iron Condor added: legged entry via price-triggered vertical spreads, RANGING fallback.
- BB-anchored strike selection (no delta); legged state machine; regime-flip exits.
- ORB cutoff moved to 11:00 AM; condor exit logic (25% stop, $0.05 nickel).
- `structure_analyzer.py` None-format crash fixed; `orb_engine.py` range persistence fixed; `check_versions.sh` added.

### v2.1 — 2026-06-30
- ADX from 5m; ORB engine rewritten with full state model; ORB range persistence and FVG trail; butterfly overhaul; Grade C eliminated; `status.py` rewritten; Telegram to 4 events; graceful shutdown; `push.sh` hardened.

### v2.0 — 2026-06-27
- GEX live from TastyTrade; strategy-aware exit routing; Telegram replaces Twilio; `configure.sh`, `snapshot.sh` added.

### v1.0 — 2026-06-25
- Initial release.

---

## Deployment

### Web install (mobile / Termius / any SSH client)

```bash
curl -fsSL https://raw.githubusercontent.com/TX-9AI/options_trader_v2/main/install.sh -o install.sh && bash install.sh
```

Have ready:
- TastyTrade Client Secret, Refresh Token, Account Number
- Telegram Bot Token and Chat ID
- GitHub repo (optional — only the source-of-truth server needs this)

`setup_ec2.sh` cleans up the deploy directory and installer on completion and drops you into `~/options-trader` with the venv active.

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
python status.py          # Live status + ORB H/L/width/state + GEX pin + daily-loss banner
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

### Configuration & overrides
```bash
bash configure.sh         # Instrument, risk, mode, Telegram, TT creds, DAILY LOSS CAP override
```

### Verify all fixes are present
```bash
bash check_versions.sh    # Recursive version-header + critical-string checks after a deploy
```

### Push / snapshot
```bash
bash push.sh "your message"
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

Core events: bot started, bot stopped, trade entered, trade closed (P&L), plus broken-wing roll and daily-loss-limit alerts when they fire.

---

## File Structure

```
options_trader_v2/
├── main.py                    # Main loop, regime dispatch, GEX, entry/exit, roll check, daily-loss gate (v2.9)
├── config.py                  # All tunable parameters incl. DAILY_LOSS_LIMIT_USD (v1.4)
├── status.py                  # Live status: ORB state, regime, GEX, strategy, daily-loss banner (v1.10)
├── query.py                   # Performance dashboard
├── check_versions.sh          # Recursive version/fix verification
├── push.sh                    # Git push, self-healing
├── setup_ec2.sh               # EC2 setup + cleanup (v2.6)
├── configure.sh               # Settings + daily-loss-cap override (v1.6)
├── install.sh                 # Web installer
├── snapshot.sh                # Bot state backup
├── analysis/
│   ├── get_orb_range.py       # ORB range fetch — three-state, via bot's own feed (v1.3)
│   ├── orb_engine.py          # ORB state machine — invalidation rules, regime-gated re-arm (v1.7)
│   ├── trend_engine.py        # ADX from 5m
│   ├── structure_analyzer.py  # FVGs, S/R, swings
│   ├── regime_classifier.py
│   ├── volatility_engine.py   # BB bands, VWAP, ATR
│   └── liquidity_mapper.py
├── strategy/
│   ├── orb_strategy.py
│   ├── butterfly_strategy.py
│   ├── iron_condor_strategy.py  # Legged, BB-anchored
│   ├── condor_roll.py           # NEW v2.3 — broken-wing roll solver + executor (v1.0)
│   ├── sweep_reversal_strategy.py
│   └── base_strategy.py
├── execution/
│   ├── exit_engine.py         # Strategy-aware exits, direction-aware condor-leg exits
│   ├── entry_engine.py
│   └── position_manager.py    # Multi-position condor tracking, credit-spread P&L (v1.7)
├── risk/
│   ├── setup_scorer.py        # A/B only, no Grade C
│   ├── risk_manager.py        # Half-budget condor sizing, reassess-every-loss, net daily-loss halt (v1.4)
│   └── session_guard.py
├── data/
│   ├── gex_data.py
│   ├── options_chain.py
│   ├── market_data.py         # Shared candle/quote feed (source of the ORB range)
│   ├── data_cache.py
│   ├── macro_data.py
│   └── tasty_client.py
├── database/trade_logger.py   # Spread columns, get_open_trades(), update_fields() (v1.3)
├── notifications/
│   ├── alert_manager.py
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

- All credentials stored in systemd environment only — never in source files.
- `.gitignore` excludes `credentials.py`, `*.pem`, `orb_range.json`, `orb_state.json`.
- `snapshot.sh` redacts secrets before archiving.
