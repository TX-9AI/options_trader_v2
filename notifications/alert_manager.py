"""
notifications/alert_manager.py — Telegram alerts for options_trader.
v1.0 — original release (Twilio SMS)
v1.1 — 2026-06-27 — replaced Twilio SMS with Telegram
v1.2 — 2026-06-30 — stripped down to exactly 4 essential alerts:
        bot started, bot stopped, trade entered, trade closed (win/loss).
        Removed regime change spam and circuit breaker noise
        (circuit breaker is implied by no further entry alerts —
        operator can check status.py for the reason if curious).
v1.3 — 2026-07-05 — added send_daily_summary(): a deliberate end-of-day P&L
        rollup sent at ~15:50 ET BEFORE the control server's shutdown sweep.
        Fee-adjusted net is the headline number. This is the 5th alert, and
        it is intentional (once/day, not spam). It is a pure formatter: the
        caller (the EOD task) computes the summary dict from the trade DB.
v1.4 — 2026-07-07 — restart & flatten self-identification: send_startup_alert()
        carries restart_type ('fresh boot' vs 'service restart'); new
        send_recovery_alert() (a live position resumed, with box symbol and a
        CARRIED-overnight flag for a weekly held across sessions),
        send_orphan_cleared_alert() (expired ghosts swept, starting flat), and
        send_hard_close_failure_alert() (position still open past 15:45 —
        retrying to 16:00, needs a manual check). Replaces the old symbol-less
        raw _send in main.py.
v1.5 — 2026-07-07 — broker-reconcile alerts: send_adopted_alert() (a position
        discovered open at the broker with no DB plan, now adopted+managed; a
        lone short raises it to a loud anomaly), send_phantom_closed_alert()
        (DB rows the broker no longer shows, closed), and
        send_reconcile_unavailable_alert() (broker read failed/empty — fell back
        to DB-only recovery, closed nothing).
"""

import logging
from typing import Optional
from utils.time_utils import fmt_et_short
from config import INSTRUMENT

logger = logging.getLogger(__name__)


class AlertManager:
    def __init__(self):
        try:
            from notifications.telegram_sender import TelegramSender
            self._tg      = TelegramSender()
            self._enabled = True
        except Exception as e:
            logger.warning(f"Telegram not available: {e}")
            self._tg      = None
            self._enabled = False

    def _send(self, msg: str):
        if self._enabled and self._tg:
            try:
                self._tg.send(msg)
            except Exception as e:
                logger.error(f"Telegram send failed: {e}")
        logger.info(f"ALERT: {msg}")

    # ── 1. Bot started ──────────────────────────────────────────────────────

    def send_startup_alert(self, paper: bool, instrument: str,
                            risk_usd: float, session_limit: int,
                            restart_type: str = ""):
        mode = "PAPER" if paper else "LIVE"
        rt   = f" | {restart_type}" if restart_type else ""
        self._send(
            f"\U0001F680 OptionsBot [{mode}] STARTED | "
            f"{instrument}{rt} | "
            f"{fmt_et_short()}"
        )

    # ── 1b. Open position resumed after a restart ───────────────────────────

    def send_recovery_alert(self, instrument: str, position_desc: str,
                            contracts: int, entry_premium: float,
                            total_cost: float, strategy: str,
                            restart_type: str = "", carried: bool = False):
        """A LIVE (unexpired) position was found in the DB on startup and is
        being resumed. Self-identifies the box (instrument) and the restart type.
        `carried=True` means it survived from a PRIOR session (a weekly held
        overnight, or a position that leaked past the 15:45 flatten) — surfaced
        louder because it must be looked at, not just noted."""
        rt   = f" after {restart_type}" if restart_type else ""
        head = ("\u26A0\uFE0F OptionsBot CARRIED POSITION (from prior session)"
                if carried else "\u26A0\uFE0F OptionsBot RESUMED POSITION")
        self._send(
            f"{head} | {instrument} | "
            f"{position_desc} \u00d7{contracts} @ ${entry_premium:.2f} "
            f"(${total_cost:.2f} at risk) | {strategy}{rt} | now managing | "
            f"{fmt_et_short()}"
        )

    # ── 1c. Expired orphans auto-cleared on startup ─────────────────────────

    def send_orphan_cleared_alert(self, instrument: str, descs: list,
                                  restart_type: str = ""):
        """Expired open rows (their expiry date had passed) were auto-closed on
        startup. Distinct from a recovery — nothing live is being managed; the
        bot is starting flat. P&L on these is unrecorded (settlement unknown)."""
        n      = len(descs)
        joined = ", ".join(descs) if descs else "-"
        rt     = f" after {restart_type}" if restart_type else ""
        self._send(
            f"\U0001F9F9 OptionsBot cleared {n} expired orphan(s) | {instrument} | "
            f"{joined} | expiry had passed — auto-closed{rt} | starting flat | "
            f"{fmt_et_short()}"
        )

    # ── 1d. Hard-close (15:45) could not flatten a position ─────────────────

    def send_hard_close_failure_alert(self, instrument: str, trade_ids: list):
        """A position is STILL OPEN past the 15:45 hard cutoff and the forced
        close is not completing. The bot keeps retrying every tick until 16:00,
        but this needs a manual check before the EOD stop, or it becomes an
        overnight orphan."""
        ids = ", ".join(str(t)[:8] for t in trade_ids) if trade_ids else "-"
        self._send(
            f"\U0001F6A8 HARD CLOSE INCOMPLETE | {instrument} | "
            f"{len(trade_ids)} position(s) still OPEN past 15:45 — retrying to "
            f"16:00 | {ids} | MANUAL CHECK before the box is stopped | "
            f"{fmt_et_short()}"
        )

    # ── 1e. Broker reconciliation (LIVE) ────────────────────────────────────

    def send_adopted_alert(self, instrument: str, position_desc: str,
                           contracts: int, entry_premium: float,
                           is_short: bool = False, anomaly: bool = False,
                           restart_type: str = ""):
        """A position was found open at the broker with no DB plan and has been
        adopted + is now managed on its own merit. A lone short (anomaly=True) is
        raised to a loud 🚨 — per the account's margin reality it should be
        near-impossible, so it warrants eyes."""
        rt   = f" after {restart_type}" if restart_type else ""
        side = "SHORT" if is_short else "long"
        if anomaly:
            self._send(
                f"\U0001F6A8 ADOPTED LONE SHORT (anomaly) | {instrument} | "
                f"{position_desc} \u00d7{contracts} @ ${entry_premium:.2f} | "
                f"no defining long found — managing on its own merit{rt} | "
                f"CHECK margin/broker | {fmt_et_short()}"
            )
        else:
            self._send(
                f"\U0001F91D OptionsBot ADOPTED POSITION ({side}) | {instrument} | "
                f"{position_desc} \u00d7{contracts} @ ${entry_premium:.2f} | "
                f"no DB plan — now managing{rt} | {fmt_et_short()}"
            )

    def send_phantom_closed_alert(self, instrument: str, trade_ids: list):
        """DB rows the broker no longer shows open, closed (broker wins on
        existence). Informational — we stopped managing positions that aren't
        actually there."""
        ids = ", ".join(str(t)[:8] for t in trade_ids) if trade_ids else "-"
        self._send(
            f"\U0001F9F9 Closed {len(trade_ids)} phantom(s) | {instrument} | "
            f"{ids} | in DB but not at broker | {fmt_et_short()}"
        )

    def send_reconcile_unavailable_alert(self, instrument: str, reason: str = ""):
        """The broker position read failed or came back empty while the DB shows
        live rows. We fell back to DB-only recovery and closed NOTHING — never
        treat a bad read as 'broker is flat'."""
        why = f" ({reason})" if reason else ""
        self._send(
            f"\u26A0\uFE0F Broker reconcile unavailable{why} | {instrument} | "
            f"DB-only recovery, no positions closed | verify manually | "
            f"{fmt_et_short()}"
        )

    # ── 2. Bot stopped ──────────────────────────────────────────────────────

    def send_shutdown_alert(self, instrument: str, reason: str = ""):
        reason_str = f" | {reason}" if reason else ""
        self._send(
            f"\U0001F534 OptionsBot STOPPED | "
            f"{instrument}{reason_str} | "
            f"{fmt_et_short()}"
        )

    # ── 3. Trade entered ─────────────────────────────────────────────────────

    def send_entry_alert(self, record: dict):
        mode   = "PAPER" if record.get("paper_trade") else "LIVE"
        ticker = record.get("symbol", INSTRUMENT)
        if record.get("is_butterfly"):
            self._send(
                f"\U0001F98B [{mode}] {ticker} BUTTERFLY {record.get('option_side','').upper()} "
                f"{record.get('center_strike','')} "
                f"\u00b1{int((record.get('upper_strike',0) - record.get('center_strike',0)))} "
                f"\u00d7{record.get('contracts',0)} "
                f"debit=${record.get('net_debit',0):.2f} "
                f"total=${record.get('total_cost',0):.0f} | "
                f"{fmt_et_short()}"
            )
        else:
            self._send(
                f"\U0001F4C8 [{mode}] {ticker} {record.get('option_side','').upper()} "
                f"{record.get('strike','')} "
                f"\u00d7{record.get('contracts',0)} "
                f"@ ${record.get('entry_premium',0):.2f} "
                f"total=${record.get('total_cost',0):.0f} | "
                f"{fmt_et_short()}"
            )

    # ── 4. Trade closed — win/loss ───────────────────────────────────────────

    def send_exit_alert(self, trade_id: str, setup_type: str,
                         exit_premium: float, entry_premium: float,
                         pnl_usd: float, contracts: int, reason: str):
        sign = "+" if pnl_usd >= 0 else ""
        icon = "\u2705" if pnl_usd >= 0 else "\u274C"
        self._send(
            f"{icon} {INSTRUMENT} CLOSED {setup_type[:20]} | "
            f"pnl={sign}${pnl_usd:.2f} | "
            f"{fmt_et_short()}"
        )

    # ── 5. End-of-day P&L summary (sent before shutdown sweep) ───────────────

    def send_daily_summary(self, summary: dict):
        """
        One deliberate EOD rollup. Called by the 15:50 ET EOD task AFTER
        positions are closed and orphans rechecked, BEFORE the control server
        stops the box.

        Expected `summary` keys (all optional; safe defaults applied):
            instrument : str   (defaults to config.INSTRUMENT)
            paper      : bool
            n_trades   : int
            wins       : int
            losses     : int
            gross_pnl  : float   (before fees)
            fees       : float   (total fees paid, positive number)
            net_pnl    : float   (gross minus fees — the headline)
            best       : float   (best single-trade net pnl)
            worst      : float   (worst single-trade net pnl)
            orphans    : int     (open/orphaned positions found at EOD; 0 = clean)
            note       : str     (optional freeform, e.g. 'circuit breaker hit')
        """
        instrument = summary.get("instrument", INSTRUMENT)
        mode       = "PAPER" if summary.get("paper", True) else "LIVE"
        n          = int(summary.get("n_trades", 0))
        wins       = int(summary.get("wins", 0))
        losses     = int(summary.get("losses", 0))
        gross      = float(summary.get("gross_pnl", 0.0))
        fees       = float(summary.get("fees", 0.0))
        net        = float(summary.get("net_pnl", gross - fees))
        orphans    = int(summary.get("orphans", 0))
        note       = summary.get("note", "")

        icon = "\u2705" if net >= 0 else "\u274C"
        net_s   = f"{'+' if net >= 0 else '-'}${abs(net):.2f}"
        gross_s = f"{'+' if gross >= 0 else '-'}${abs(gross):.2f}"

        lines = [
            f"\U0001F4CA {instrument} DAILY P&L [{mode}] {icon}",
            f"Trades: {n}  ({wins}W / {losses}L)",
            f"Net: {net_s}   (gross {gross_s}, fees -${abs(fees):.2f})",
        ]

        if n > 0 and ("best" in summary or "worst" in summary):
            best  = float(summary.get("best", 0.0))
            worst = float(summary.get("worst", 0.0))
            lines.append(f"Best {best:+.2f} · Worst {worst:+.2f}")

        # Orphan status is a safety signal — always surface it explicitly.
        if orphans > 0:
            lines.append(f"\u26A0\uFE0F {orphans} orphaned position(s) found — CHECK before restart!")
        else:
            lines.append("Orphans: none \u2713")

        if note:
            lines.append(f"_{note}_")

        lines.append(fmt_et_short())
        self._send("\n".join(lines))

    # ── Suppressed — kept as no-ops so existing callers don't break ─────────

    def send_circuit_breaker_alert(self, session_losses: int, reason: str):
        """Suppressed. Check status.py for circuit breaker state if curious."""
        logger.info(
            f"Circuit breaker fired (not sent to Telegram): "
            f"{session_losses} losses — {reason}"
        )

    def send_regime_alert(self, old_regime: str, new_regime: str,
                           conviction: float, notes: str = ""):
        """Suppressed. Regime changes are too frequent to be useful as alerts."""
        pass


_alert_manager: Optional[AlertManager] = None


def get_alert_manager() -> AlertManager:
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
    return _alert_manager
