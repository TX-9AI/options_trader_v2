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
v1.4 — 2026-07-06 — DEFINITIVE realized-P&L primitive: realized_pnl_today()
        (single source of truth for the daily-loss circuit breaker, status and
        query) with ET-correct session bucketing; today_summary() routed through
        it so displays and the halt can never disagree.
v1.5 — 2026-07-07 — expiry-aware orphan handling: get_open_trades_live() (open
        rows not yet expired — 0DTE and weeklies alike, plus unknown-expiry rows
        kept for safety) and close_expired_open_trades() (auto-close ONLY rows
        whose expiry date has passed — a weekly with time left is left alone).
        Keyed on the stored expiry (YYYY-MM-DD), never on entry date, so a
        multi-day weekly is never mistaken for a same-day ghost.
v1.6 — 2026-07-07 — broker reconciliation support: is_short_position column
        (schema + migration) so an adopted short survives a re-restart; and
        close_phantom() to close a DB row the broker no longer shows (broker is
        the source of truth for existence on live).
"""

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime, timezone, timedelta

from config import DB_PATH
from utils.time_utils import ts_for_db, now_utc, now_et, ET

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
        is_short_position INTEGER DEFAULT 0,
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
            ("is_short_position", "INTEGER DEFAULT 0"),
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

    def get_open_trades_today(self) -> List[TradeRecord]:
        """Deprecated alias — retained for safety. Prefer get_open_trades_live().
        A 0DTE bot that also trades weeklies can hold a position across sessions
        (expiry days out), so 'entered today' is the WRONG liveness test; use
        expiry instead."""
        return self.get_open_trades_live()

    @staticmethod
    def _expiry_date(expiry) -> str:
        """Normalize a stored expiry to 'YYYY-MM-DD'. Entries store this format
        already; tolerate a stray full timestamp. Returns '' if unknown."""
        if not expiry:
            return ""
        s = str(expiry).strip()
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
        try:
            return datetime.fromisoformat(s).strftime("%Y-%m-%d")
        except Exception:
            return ""

    def get_open_trades_live(self) -> List[TradeRecord]:
        """Open rows that have NOT expired — expiry today or later (0DTE AND
        weeklies), plus any row whose expiry is unknown (kept deliberately: never
        abandon a possibly-live position). These are what startup recovery
        resumes managing."""
        today_et = now_et().strftime("%Y-%m-%d")
        out = []
        for r in self.get_open_trades():
            exp = self._expiry_date(r.get("expiry", ""))
            if exp == "" or exp >= today_et:
                out.append(r)
        return out

    def close_expired_open_trades(
        self, exit_reason: str = "expired_orphan_autoclosed"
    ) -> List[TradeRecord]:
        """Reconcile TRULY EXPIRED orphans only: any status='open' row whose
        expiry date has passed (expiry < today ET). A weekly still in its life is
        left ALONE — its expiry is in the future. Rows with an unknown expiry are
        also left open (never guess a live position dead). Each closed row gets
        status='closed', an explicit exit_reason, exit_time now, and pnl_usd
        forced to 0.0 (true settlement is unknowable — flag for manual review) so
        it leaves 'open' and is never 'recovered' again. Returns the rows closed
        (for alerting)."""
        today_et = now_et().strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status='open'"
            ).fetchall()
            expired = []
            for r in rows:
                exp = self._expiry_date(r["expiry"])
                if exp != "" and exp < today_et:
                    expired.append(make_record(**dict(r)))
            if expired:
                ids = [r["trade_id"] for r in expired]
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE trades SET status='closed', "
                    f"exit_reason=?, exit_time=?, "
                    f"pnl_usd=COALESCE(pnl_usd, 0.0) "
                    f"WHERE trade_id IN ({placeholders})",
                    [exit_reason, ts_for_db(), *ids]
                )
        for r in expired:
            logger.warning(
                f"Auto-closed EXPIRED orphan {r.get('trade_id','')[:8]} "
                f"(expiry {self._expiry_date(r.get('expiry',''))}, "
                f"{str(r.get('option_side','')).upper()} {r.get('strike',0)}) "
                f"— {exit_reason}"
            )
        return expired

    def close_phantom(self, trade_id: str,
                      reason: str = "phantom_closed_at_broker") -> None:
        """Close a DB row the broker no longer shows open. On LIVE, the broker is
        the source of truth for existence: if a row is open in our DB but absent
        at the broker, it has closed there (or never truly filled) and we must
        stop 'managing' it. pnl_usd is forced to 0.0 (the real fill is unknown —
        flag for review) with an explicit reason."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE trades SET status='closed', exit_reason=?, exit_time=?, "
                "pnl_usd=COALESCE(pnl_usd, 0.0) WHERE trade_id=?",
                (reason, ts_for_db(), trade_id),
            )
        logger.warning(f"Closed phantom {trade_id[:8]} — {reason}")

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

    # ── DEFINITIVE closed-P&L accounting ──────────────────────────────────────
    # Single source of truth for realized (closed) P&L. Every consumer — the
    # daily-loss circuit breaker, status.py, query.py, EOD — references THESE
    # methods, never an in-memory copy and never a parallel re-sum. That is what
    # lets any bot reference its definitive day P&L immediately and survive any
    # restart: the number lives in trades.db, not in process memory.
    @staticmethod
    def _et_date(iso_ts: str) -> str:
        """ET calendar date ('YYYY-MM-DD') for a stored UTC ISO timestamp.
        Bucketing by ET (not UTC) so a late-session trade never lands on the
        wrong day."""
        try:
            dt = datetime.fromisoformat(iso_ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(ET).strftime("%Y-%m-%d")
        except Exception:
            return ""

    def _closed_today_rows(self) -> List[sqlite3.Row]:
        """Closed trades whose ET session date is today. Coarse UTC prefilter
        keeps the scan tiny; the exact match is done by ET date in Python."""
        today_et = now_et().strftime("%Y-%m-%d")
        lower = (now_utc() - timedelta(days=1)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM trades
                WHERE status='closed' AND pnl_usd IS NOT NULL
                AND date(entry_time) >= ?
            """, (lower,)).fetchall()
        return [r for r in rows if self._et_date(r["entry_time"]) == today_et]

    def realized_pnl_today(self) -> float:
        """DEFINITIVE realized net closed P&L for today's ET session (wins
        offset losses). This is the number the daily loss limit gates on."""
        return float(sum((r["pnl_usd"] or 0.0) for r in self._closed_today_rows()))

    def today_summary(self) -> dict:
        """Counts + net P&L for today's ET session. total_pnl is identical to
        realized_pnl_today() — one computation, so displays and the circuit
        breaker can never disagree."""
        rows = self._closed_today_rows()
        wins   = sum(1 for r in rows if (r["pnl_usd"] or 0) > 0)
        losses = sum(1 for r in rows if (r["pnl_usd"] or 0) < 0)
        total_pnl = float(sum((r["pnl_usd"] or 0.0) for r in rows))
        return {"total": len(rows), "wins": wins, "losses": losses,
                "total_pnl": total_pnl}


_trade_logger: Optional[TradeLogger] = None


def get_trade_logger() -> TradeLogger:
    global _trade_logger
    if _trade_logger is None:
        _trade_logger = TradeLogger()
    return _trade_logger
