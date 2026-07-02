"""
database/trade_logger.py — Options trade logging (SQLite).
v1.0 — original release
v1.1 — 2026-06-27 — add orb_range_high, orb_range_low, current_premium
        columns to schema for ORB exit logic and live P&L display
v1.2 — 2026-07-02 — condor-leg support: spread columns (short/long strike,
        credit, width, is_condor_leg, condor_leg_num, is_broken_wing,
        short/long symbol) + get_open_trades() for concurrent condor legs.
v1.3 — 2026-07-02 — add generic update_fields() (used by the broken-wing roll
        to flag rolled/tested legs is_broken_wing).
"""

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime, timezone

from config import DB_PATH
from utils.time_utils import ts_for_db, now_utc

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord(dict):
    """
    Options trade record. Inherits from dict so it works as both
    a typed object and a sqlite3.Row-compatible mapping.
    """
    pass


def make_record(**kwargs) -> TradeRecord:
    r = TradeRecord()
    r.update(kwargs)
    return r


class TradeLogger:
    """SQLite-backed trade log for options_trader."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS trades (
        trade_id          TEXT PRIMARY KEY,
        symbol            TEXT,
        strategy          TEXT,
        setup_type        TEXT,
        setup_grade       TEXT,
        setup_score       REAL,
        direction         TEXT,
        option_side       TEXT,
        is_butterfly      INTEGER DEFAULT 0,
        strike            REAL,
        lower_strike      REAL,
        center_strike     REAL,
        upper_strike      REAL,
        expiry            TEXT,
        contracts         INTEGER,
        entry_premium     REAL,
        exit_premium      REAL,
        current_premium   REAL DEFAULT 0.0,
        net_debit         REAL,
        max_profit        REAL,
        total_cost        REAL,
        max_loss          REAL,
        stop_premium      REAL,
        trail_activation  REAL,
        target_premium    REAL,
        underlying_entry  REAL,
        underlying_stop   REAL,
        underlying_target REAL,
        orb_range_high    REAL DEFAULT 0.0,
        orb_range_low     REAL DEFAULT 0.0,
        short_strike      REAL DEFAULT 0.0,
        long_strike       REAL DEFAULT 0.0,
        credit_received   REAL DEFAULT 0.0,
        spread_width      REAL DEFAULT 0.0,
        is_condor_leg     INTEGER DEFAULT 0,
        condor_leg_num    INTEGER DEFAULT 0,
        is_broken_wing    INTEGER DEFAULT 0,
        short_symbol      TEXT,
        long_symbol       TEXT,
        pnl_usd           REAL,
        pnl_pct           REAL,
        regime            TEXT,
        vix_at_entry      REAL,
        is_fed_day        INTEGER DEFAULT 0,
        status            TEXT DEFAULT 'open',
        exit_reason       TEXT,
        order_id          TEXT,
        lower_symbol      TEXT,
        center_symbol     TEXT,
        upper_symbol      TEXT,
        option_symbol     TEXT,
        paper_trade       INTEGER DEFAULT 1,
        entry_time        TEXT,
        exit_time         TEXT,
        notes             TEXT
    );

    CREATE TABLE IF NOT EXISTS circuit_breaker_events (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        event_time      TEXT,
        reason          TEXT,
        session_losses  INTEGER,
        notes           TEXT
    );

    CREATE TABLE IF NOT EXISTS regime_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        logged_at     TEXT,
        regime        TEXT,
        conviction    REAL,
        macro_context TEXT,
        adx           REAL,
        trigger       TEXT
    );
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._ensure_db()

    def _ensure_db(self):
        import os
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(self.SCHEMA)
        # Migrate existing DBs — add columns if missing
        for col, definition in [
            ("current_premium", "REAL DEFAULT 0.0"),
            ("orb_range_high",  "REAL DEFAULT 0.0"),
            ("orb_range_low",   "REAL DEFAULT 0.0"),
            ("short_strike",    "REAL DEFAULT 0.0"),
            ("long_strike",     "REAL DEFAULT 0.0"),
            ("credit_received", "REAL DEFAULT 0.0"),
            ("spread_width",    "REAL DEFAULT 0.0"),
            ("is_condor_leg",   "INTEGER DEFAULT 0"),
            ("condor_leg_num",  "INTEGER DEFAULT 0"),
            ("is_broken_wing",  "INTEGER DEFAULT 0"),
            ("short_symbol",    "TEXT"),
            ("long_symbol",     "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {definition}")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists
        conn.commit()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def log_entry(self, record: TradeRecord):
        """Insert a new open trade into the database."""
        record["entry_time"] = ts_for_db()
        record["status"]     = "open"

        cols         = [k for k in record.keys()]
        values       = [record[k] for k in cols]
        placeholders = ", ".join(["?"] * len(cols))
        col_names    = ", ".join(cols)

        with self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO trades ({col_names}) VALUES ({placeholders})",
                values
            )
        logger.info(f"Trade logged: {record.get('trade_id', '')[:8]} entry")

    def log_exit(self, trade_id: str, exit_price: float,
                  pnl_usd: float, exit_reason: str):
        """Update an open trade with exit details."""
        entry_prem = self._get_field(trade_id, "entry_premium") or 0
        pnl_pct    = (exit_price - entry_prem) / entry_prem if entry_prem > 0 else 0

        with self._connect() as conn:
            conn.execute("""
                UPDATE trades SET
                    status       = 'closed',
                    exit_premium = ?,
                    pnl_usd      = ?,
                    pnl_pct      = ?,
                    exit_reason  = ?,
                    exit_time    = ?
                WHERE trade_id = ?
            """, (exit_price, pnl_usd, pnl_pct,
                  exit_reason, ts_for_db(), trade_id))
        logger.info(
            f"Trade closed: {trade_id[:8]} "
            f"exit=${exit_price:.2f} pnl=${pnl_usd:+.2f}"
        )

    def update_stop(self, trade_id: str, new_stop: float):
        with self._connect() as conn:
            conn.execute(
                "UPDATE trades SET stop_premium=? WHERE trade_id=?",
                (new_stop, trade_id)
            )

    def update_current_premium(self, trade_id: str, premium: float):
        """Update live mark price on the open trade every tick for P&L display."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE trades SET current_premium=? WHERE trade_id=?",
                (premium, trade_id)
            )

    def update_fields(self, trade_id: str, **fields):
        """Generic field updater (used by the broken-wing roll to flag legs)."""
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [trade_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE trades SET {sets} WHERE trade_id=?", vals)

    def get_open_trade(self) -> Optional[TradeRecord]:
        """Return the single open trade if any."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM trades WHERE status='open' ORDER BY entry_time DESC LIMIT 1"
            ).fetchone()
        if row:
            return make_record(**dict(row))
        return None

    def get_open_trades(self) -> List[TradeRecord]:
        """Return ALL open trades (oldest first). Supports concurrent condor
        legs; every other strategy holds at most one at a time."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status='open' ORDER BY entry_time ASC"
            ).fetchall()
        return [make_record(**dict(r)) for r in rows]

    def get_session_losses(self) -> int:
        today = now_utc().strftime("%Y-%m-%d")
        with self._connect() as conn:
            row = conn.execute("""
                SELECT COUNT(*) as n FROM trades
                WHERE status='closed'
                AND pnl_usd < 0
                AND date(entry_time) = ?
            """, (today,)).fetchone()
        return row["n"] if row else 0

    def get_consecutive_losses(self) -> int:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT pnl_usd FROM trades
                WHERE status='closed'
                ORDER BY exit_time DESC
                LIMIT 10
            """).fetchall()
        count = 0
        for row in rows:
            if row["pnl_usd"] < 0:
                count += 1
            else:
                break
        return count

    def log_circuit_breaker(self, reason: str, session_losses: int, notes: str = ""):
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO circuit_breaker_events
                (event_time, reason, session_losses, notes)
                VALUES (?, ?, ?, ?)
            """, (ts_for_db(), reason, session_losses, notes))

    def log_regime(self, regime: str, conviction: float,
                   macro_context: str, adx: float, trigger: str):
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO regime_log
                (logged_at, regime, conviction, macro_context, adx, trigger)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (ts_for_db(), regime, conviction, macro_context, adx, trigger))

    def _get_field(self, trade_id: str, field: str):
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {field} FROM trades WHERE trade_id=?", (trade_id,)
            ).fetchone()
        return row[field] if row else None

    def today_summary(self) -> dict:
        today = now_utc().strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) as losses,
                       COALESCE(SUM(pnl_usd), 0) as total_pnl
                FROM trades
                WHERE status='closed' AND date(entry_time) = ?
            """, (today,)).fetchone()
        return dict(rows) if rows else {}


_trade_logger: Optional[TradeLogger] = None


def get_trade_logger() -> TradeLogger:
    global _trade_logger
    if _trade_logger is None:
        _trade_logger = TradeLogger()
    return _trade_logger
