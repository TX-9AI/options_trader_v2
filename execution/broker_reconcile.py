"""
execution/broker_reconcile.py — LIVE broker⇄DB position reconciliation.
v1.0 — 2026-07-07 — initial: the brokerage is the source of truth for whether a
        position EXISTS; the DB supplies the management plan. Builds a plan of
        keep / adopt / close-phantom / anomaly from broker positions + DB live
        rows. Pure logic (no SDK, no DB writes) so it is fully unit-testable; the
        caller performs the DB writes and alerts. PAPER never calls this — the DB
        is truth there.

Rules (agreed design):
  - A broker position already represented by a LIVE DB row  -> KEEP (manage from
    the existing plan).
  - A broker position with NO DB plan                       -> ADOPT and manage
    on its own merit: entry = broker average_open_price, side from the broker,
    stop = entry * (1 ∓ ADOPTED_STOP_PCT), managed by the generic adopted exit.
  - A LIVE DB row with NO matching broker position          -> PHANTOM: close it
    (broker wins on existence).
  - Pairing is for LABELLING only: a short leg with a matching long (same
    underlying+expiry+type) is tagged as part of a spread; a short with no such
    long is flagged an ANOMALY (per the account's margin reality a naked short
    should be near-impossible) — but it is still adopted and managed, loudly.
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import List, Dict, Optional

from config import ADOPTED_STOP_PCT
from utils.time_utils import ts_for_db

logger = logging.getLogger(__name__)


@dataclass
class ReconcilePlan:
    keep: List[dict]            = field(default_factory=list)  # DB rows confirmed at broker
    adopt: List[dict]           = field(default_factory=list)  # synthesized records to manage+journal
    close_phantom: List[str]    = field(default_factory=list)  # DB trade_ids absent at broker
    anomalies: List[str]        = field(default_factory=list)  # adopted trade_ids that are lone shorts


# ── OCC option symbol parsing ────────────────────────────────────────────────
# Standard 21-char OCC: 6-char root (space-padded) + YYMMDD + C/P + 8-digit
# strike in thousandths. e.g. "AMD   260711C00180000" -> AMD 2026-07-11 call 180.
def parse_occ(symbol: str) -> Optional[dict]:
    if not symbol:
        return None
    s = str(symbol)
    # Tolerate a stray leading dot/streamer form and extra spaces; work off the
    # fixed-width tail (last 15 chars = YYMMDD + C/P + 8-digit strike).
    core = s.lstrip(".").rstrip()
    if len(core) < 15:
        return None
    tail = core[-15:]
    root = core[:-15].strip()
    ymd, cp, strike_raw = tail[:6], tail[6:7].upper(), tail[7:]
    if cp not in ("C", "P") or not ymd.isdigit() or not strike_raw.isdigit():
        return None
    try:
        expiry = f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:6]}"
        strike = int(strike_raw) / 1000.0
    except Exception:
        return None
    return {
        "underlying": root,
        "expiry": expiry,
        "option_side": "call" if cp == "C" else "put",
        "strike": strike,
    }


def _adopt_record(pos: dict) -> Optional[dict]:
    """Synthesize a manageable TradeRecord from one normalized broker position.
    `pos` keys: symbol, underlying, quantity, direction ('Long'/'Short'),
    average_open_price. Returns None if the OCC symbol can't be parsed."""
    occ = parse_occ(pos.get("symbol", ""))
    if occ is None:
        logger.error(f"Adopt: cannot parse broker symbol {pos.get('symbol','')!r} — skipping")
        return None
    is_short = str(pos.get("direction", "")).lower().startswith("short")
    entry    = float(pos.get("average_open_price", 0) or 0)
    contracts = abs(int(float(pos.get("quantity", 0) or 0)))
    # Stop at the same max-loss degree our normal stops respect, sign-correct:
    # a long loses as premium falls, a short as premium rises.
    if is_short:
        stop = round(entry * (1 + ADOPTED_STOP_PCT), 2)
    else:
        stop = round(entry * (1 - ADOPTED_STOP_PCT), 2)
    return {
        "trade_id":          f"adopt-{uuid.uuid4().hex[:12]}",
        "symbol":            pos.get("underlying") or occ["underlying"],
        "strategy":          "ADOPTED",
        "setup_type":        "orphan_adopted",
        "option_side":       occ["option_side"],
        "strike":            occ["strike"],
        "expiry":            occ["expiry"],
        "contracts":         contracts,
        "entry_premium":     entry,
        "total_cost":        round(entry * contracts * 100, 2),
        "stop_premium":      stop,
        "is_short_position": 1 if is_short else 0,
        "option_symbol":     pos.get("symbol", ""),
        "paper_trade":       0,          # adoption only happens live
        "status":            "open",
        "entry_time":        ts_for_db(),
        "notes":             "adopted from broker (no DB plan)",
    }


def _db_leg_symbols(row: dict) -> set:
    """Every option symbol a DB row could be known by (single leg + spread/
    butterfly legs), so a broker leg can be matched to its owning row."""
    keys = ("option_symbol", "short_symbol", "long_symbol",
            "lower_symbol", "center_symbol", "upper_symbol")
    return {row.get(k) for k in keys if row.get(k)}


def build_plan(broker_positions: List[dict], db_live_rows: List[dict]) -> ReconcilePlan:
    """Pure reconciliation. Inputs:
        broker_positions — normalized dicts (see _adopt_record) for OPEN option
                           positions at the broker.
        db_live_rows     — DB rows currently considered live (unexpired open).
    Returns a ReconcilePlan; the caller does the DB writes + alerts."""
    plan = ReconcilePlan()

    # Map every broker leg symbol -> its position for fast lookup.
    broker_by_symbol = {p.get("symbol"): p for p in broker_positions if p.get("symbol")}
    broker_symbols   = set(broker_by_symbol)

    # 1) KEEP vs PHANTOM: each DB live row must have at least one leg present at
    #    the broker, else it no longer exists -> phantom, close it.
    matched_broker_symbols = set()
    for row in db_live_rows:
        legs = _db_leg_symbols(row)
        present = legs & broker_symbols
        if present:
            plan.keep.append(row)
            matched_broker_symbols |= present
        else:
            plan.close_phantom.append(row.get("trade_id", ""))

    # 2) ADOPT: any broker position not already explained by a DB row.
    to_adopt = [p for sym, p in broker_by_symbol.items()
                if sym not in matched_broker_symbols]
    adopted = []
    for pos in to_adopt:
        rec = _adopt_record(pos)
        if rec is not None:
            rec["_direction"] = "short" if rec["is_short_position"] else "long"
            adopted.append(rec)

    # 3) Pairing pass — LABEL only. A short with a matching unmatched long
    #    (same underlying+expiry+side) is part of a spread; a short with none is
    #    an anomaly (still adopted+managed).
    longs_avail = [r for r in adopted if r["_direction"] == "long"]
    for rec in adopted:
        if rec["_direction"] != "short":
            continue
        partner = next(
            (l for l in longs_avail
             if not l.get("_paired")
             and l["symbol"] == rec["symbol"]
             and l["expiry"] == rec["expiry"]
             and l["option_side"] == rec["option_side"]),
            None,
        )
        if partner is not None:
            partner["_paired"] = True
            rec["_paired"] = True
            rec["notes"] = "adopted short — paired with open long (defined risk)"
        else:
            rec["notes"] = "adopted LONE SHORT — no defining long found (anomaly)"
            plan.anomalies.append(rec["trade_id"])

    # strip scratch keys before handing back
    for rec in adopted:
        rec.pop("_direction", None)
        rec.pop("_paired", None)
        plan.adopt.append(rec)

    return plan
