"""
config.py — options_trader v1.3
v1.0 — original release
v1.1 — 2026-06-27 — remove Twilio, fix SWEEP_TARGET_DELTA to 0.08,
        remove Grade C, add BUTTERFLY_ENTRY_CUTOFF_ET
v1.2 — 2026-06-29 — butterfly overhaul: fixed wings by instrument,
        GEX pin proximity gate (1x expected move), noon-2PM entry window,
        one-per-session limit, TP reduced to 20%
v1.3 — 2026-07-02 — narrow SPX condor wings 25->5 so each vertical is
        affordable (max loss ~$235/contract), enabling half-budget-per-side
        condor sizing.

All secrets come from environment variables — never from hardcoded values
or editable files. The setup_ec2.sh script writes them into the systemd
unit so the bot has them at runtime without any manual file editing.

Tunable strategy parameters live here and are safe to commit.
"""

import os
from dataclasses import dataclass
from typing import Optional


# ─── ENVIRONMENT HELPERS ──────────────────────────────────────────────────────

def _require(key: str) -> str:
    val = os.environ.get(key, "")
    if not val:
        raise EnvironmentError(
            f"\n\n  ❌  Missing required environment variable: {key}\n"
            f"      Run setup_ec2.sh to configure this bot properly.\n"
            f"      For local dev, export {key}='...' in your shell.\n"
        )
    return val

def _optional(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ─── TASTYTRADE CREDENTIALS (from environment) ────────────────────────────────

def get_tt_client_secret()  -> str: return _require("TT_CLIENT_SECRET")
def get_tt_refresh_token()  -> str: return _require("TT_REFRESH_TOKEN")
def get_tt_account_number() -> str: return _require("TT_ACCOUNT_NUMBER")


# ─── TELEGRAM ALERTS (from environment) ──────────────────────────────────────

def get_telegram_token()    -> str: return _optional("TELEGRAM_TOKEN")
def get_telegram_chat_id()  -> str: return _optional("TELEGRAM_CHAT_ID")

def telegram_configured() -> bool:
    return bool(get_telegram_token() and get_telegram_chat_id())


# ─── INSTRUMENT SELECTION ─────────────────────────────────────────────────────

INSTRUMENT          = os.environ.get("OT_INSTRUMENT", "QQQ")

STRIKE_INCREMENTS = {
    "QQQ": 1,
    "SPY": 1,
    "SPX": 5,
}
STRIKE_INCREMENT    = STRIKE_INCREMENTS.get(INSTRUMENT, 1)
CONTRACT_MULTIPLIER = 100

# ─── ACCOUNT & RISK ───────────────────────────────────────────────────────────

RISK_PER_TRADE_USD  = float(os.environ.get("OT_RISK_USD", "200"))
MAX_LOSS_PCT        = 0.25
SESSION_LOSS_LIMIT  = 2

# ─── PAPER TRADING ────────────────────────────────────────────────────────────

PAPER_TRADING       = os.environ.get("OT_PAPER_TRADING", "True") != "False"

# ─── VIX / IV THRESHOLDS ──────────────────────────────────────────────────────

VIX_LOW_THRESHOLD           = 15
VIX_ELEVATED_THRESHOLD      = 20
VIX_CRISIS_THRESHOLD        = 30
VIX_BUTTERFLY_DISABLE       = 20
VIX_BUTTERFLY_HALF_SIZE     = 15
VIX_NO_ENTRY_THRESHOLD      = 30
IV_RANK_HIGH                = 50

# ─── SESSION / TIME RULES ─────────────────────────────────────────────────────

TIMEZONE                    = "US/Eastern"
RTH_OPEN_ET                 = (9, 30)
RTH_CLOSE_ET                = (16, 0)
HARD_CLOSE_ET               = (15, 45)
NO_ENTRY_AFTER_ET           = (11, 0)   # ORB entries only valid until 11:00 AM ET
BUTTERFLY_ENTRY_CUTOFF_ET   = (15, 0)
BUTTERFLY_ENTRY_START_ET    = (12, 0)   # No butterfly entries before noon
ORB_WINDOW_MINUTES          = 5

# ─── ORB STRATEGY ─────────────────────────────────────────────────────────────

ORB_BREAK_BUFFER            = 0.05
ORB_MAX_RETEST_BARS         = 12
ORB_TP_MULTIPLIER           = 1.0
ORB_TRAIL_ACTIVATION        = 0.50
FED_DAY_ORB_BOOST           = 0.20

# ─── SWEEP REVERSAL STRATEGY ──────────────────────────────────────────────────

SWEEP_TARGET_DELTA          = 0.08
SWEEP_DELTA_TOLERANCE       = 0.03
SWEEP_MIN_REJECTION_PCT     = 0.003
SWEEP_MAX_AGE_BARS          = 8

# ─── BUTTERFLY STRATEGY ───────────────────────────────────────────────────────

BUTTERFLY_TP_PCT            = 0.20   # 20% of max profit
BUTTERFLY_MAX_HOLD_MIN      = 150

# Fixed wing widths by instrument
BUTTERFLY_WING_SPX          = 25     # 25-point wings on SPX
BUTTERFLY_WING_QQQ          = 5      # $5 wings on QQQ/SPY

# GEX pin proximity gate: price must be within 1x expected move of pin
# Formula: underlying × VIX% × sqrt(hours_remaining/6.5) / sqrt(252)
# Computed at runtime in butterfly_strategy.py
BUTTERFLY_GEX_PIN_PROXIMITY_MULT = 1.0  # Multiplier on expected move

# ─── IRON CONDOR STRATEGY ─────────────────────────────────────────────────────
# Fallback for RANGING regime when no GEX pin is available for a butterfly.
# Delta-based strike selection is primary; expected-move guardrail is a
# sanity check only, not a parallel sizing method.

CONDOR_SHORT_DELTA          = 0.22   # Target delta for short strikes
CONDOR_DELTA_TOLERANCE      = 0.05   # Acceptable deviation from target delta
CONDOR_WING_WIDTH_SPX       = 5      # Narrow wings — affordable verticals (was 25)
CONDOR_WING_WIDTH_QQQ       = 5      # Fixed wing width in points on QQQ/SPY
CONDOR_EXPECTED_MOVE_GUARDRAIL_MULT = 1.2  # Short strikes must be within this x EM
CONDOR_PROXIMITY_STRIKES    = 2      # Strikes inside the short strike that trigger entry
                                     # (2 strikes = 10pt on SPX, $2 on QQQ — scales naturally)
CONDOR_STOP_LOSS_PCT        = 0.25   # Exit if spread value rises to 125% of credit received
CONDOR_NICKEL_CLOSE         = 0.05   # Close leg when spread value decays to $0.05
CONDOR_ENTRY_START_ET       = (11, 0)   # No condor entries before 11 AM (after ORB window closes)
CONDOR_ENTRY_CUTOFF_ET      = (14, 0)   # Standard entry cutoff

# ─── EXIT MANAGEMENT ──────────────────────────────────────────────────────────

TRAIL_ACTIVATION_PCT        = 0.50
TRAIL_LOCK_PCT              = 0.25
POLL_INTERVAL_SECONDS       = 15

# ─── REGIME CLASSIFICATION ────────────────────────────────────────────────────

ADX_TREND_THRESHOLD         = 25
ADX_RANGE_THRESHOLD         = 20
ATR_EXPANSION_MULTIPLIER    = 1.5
BB_WIDTH_COMPRESSION_PCT    = 0.20
SWEEP_REJECTION_CANDLES     = 3
EQUAL_LEVEL_PCT             = 0.001
REGIME_REASSESS_MINUTES     = 5

# ─── SETUP SCORING ────────────────────────────────────────────────────────────

GRADE_A_MIN_SCORE           = 0.78
GRADE_B_MIN_SCORE           = 0.55
GRADE_SIZE_MULTIPLIER       = {"A": 1.5, "B": 1.0}

# ─── VOLATILITY / TREND ───────────────────────────────────────────────────────

ATR_PERIOD                  = 14
ATR_STOP_MULTIPLIER         = 1.5
BB_PERIOD                   = 20
BB_STD                      = 2.0
EMA_FAST                    = 9
EMA_MID                     = 21
EMA_SLOW                    = 50
EMA_ANCHOR                  = 200

# ─── LIQUIDITY MAPPING ────────────────────────────────────────────────────────

EQUAL_HIGH_LOW_LOOKBACK     = 50
IMBALANCE_MIN_SIZE_PCT      = 0.002
LIQUIDITY_BUFFER_PCT        = 0.003

# ─── SIGNAL VALIDATION ────────────────────────────────────────────────────────

MIN_RRR                     = 1.3
VWAP_FILTER_ACTIVE          = True
MIN_TF_CONFLUENCE           = 1
ENTRY_COOLDOWN_MINUTES      = 5

# ─── STRUCTURE ANALYSIS ───────────────────────────────────────────────────────

SWING_LOOKBACK              = 10
MIN_SWING_SIZE_ATR          = 0.5
FVG_MIN_SIZE_PCT            = 0.001
SR_TOUCH_MIN                = 2
SR_ZONE_PCT                 = 0.002
ORDER_BLOCK_LOOKBACK        = 20

# ─── ORDER EXECUTION ──────────────────────────────────────────────────────────

LIMIT_RETRY_SECONDS         = 30
LIMIT_IMPROVE_TICKS         = 1
PAPER_FILL_SLIPPAGE_PCT     = 0.01

# ─── TASTYTRADE API ───────────────────────────────────────────────────────────

TT_BASE_URL                 = "https://api.tastytrade.com"
TT_PAPER_BASE_URL           = "https://api.cert.tastyworks.com"

# ─── MACRO / FED CALENDAR ─────────────────────────────────────────────────────

FOREX_FACTORY_URL           = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
MACRO_FETCH_INTERVAL_MIN    = 60
FED_EVENT_KEYWORDS          = ["FOMC", "Fed", "Federal Funds Rate", "Powell"]

# ─── TIMEFRAMES ───────────────────────────────────────────────────────────────

TIMEFRAMES = {
    "1d":  {"candles": 10,  "role": "bias"},
    "1h":  {"candles": 50,  "role": "structure"},
    "15m": {"candles": 50,  "role": "trend"},
    "5m":  {"candles": 100, "role": "entry_context"},
    "1m":  {"candles": 60,  "role": "trigger"},
}

CACHE_STALENESS_SECONDS = {
    "1d":  3600,
    "1h":  300,
    "15m": 120,
    "5m":  30,
    "1m":  10,
}

# ─── NOTIFICATIONS ────────────────────────────────────────────────────────────

NOTIFY_ON_ENTRY             = True
NOTIFY_ON_EXIT              = True
NOTIFY_ON_CIRCUIT_BREAK     = True
NOTIFY_ON_REGIME_CHANGE     = True

# ─── DATABASE & LOGGING ───────────────────────────────────────────────────────

DB_PATH                     = os.path.expanduser("~/options-trader/trades.db")
LOG_LEVEL                   = "INFO"
LOG_FILE                    = os.path.expanduser("~/options-trader/bot.log")
LOG_ROTATION_MB             = 50

# ─── BOT IDENTITY ─────────────────────────────────────────────────────────────

BOT_NAME                    = os.environ.get("OT_BOT_NAME", "OptionsTrader")


@dataclass
class SessionConfig:
    """Runtime session config — populated at startup."""
    paper_trading:      bool    = True
    instrument:         str     = "QQQ"
    risk_per_trade_usd: float   = 200.0
    notes:              str     = ""
    confirmed_at:       Optional[str] = None
