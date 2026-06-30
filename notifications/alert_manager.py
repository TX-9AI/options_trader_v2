"""
notifications/alert_manager.py — Telegram alerts for options_trader.
v1.0 — original release (Twilio SMS)
v1.1 — 2026-06-27 — replaced Twilio SMS with Telegram
v1.2 — 2026-06-30 — stripped down to exactly 4 essential alerts:
        bot started, bot stopped, trade entered, trade closed (win/loss).
        Removed regime change spam and circuit breaker noise
        (circuit breaker is implied by no further entry alerts —
        operator can check status.py for the reason if curious).
"""

import logging
from typing import Optional
from utils.time_utils import fmt_et_short

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
                            risk_usd: float, session_limit: int):
        mode = "PAPER" if paper else "LIVE"
        self._send(
            f"\U0001F680 OptionsBot [{mode}] STARTED | "
            f"{instrument} | "
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
        mode = "PAPER" if record.get("paper_trade") else "LIVE"
        if record.get("is_butterfly"):
            self._send(
                f"\U0001F98B [{mode}] BUTTERFLY {record.get('option_side','').upper()} "
                f"{record.get('center_strike','')} "
                f"\u00b1{int((record.get('upper_strike',0) - record.get('center_strike',0)))} "
                f"\u00d7{record.get('contracts',0)} "
                f"debit=${record.get('net_debit',0):.2f} "
                f"total=${record.get('total_cost',0):.0f} | "
                f"{fmt_et_short()}"
            )
        else:
            self._send(
                f"\U0001F4C8 [{mode}] {record.get('option_side','').upper()} "
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
            f"{icon} CLOSED {setup_type[:20]} | "
            f"pnl={sign}${pnl_usd:.2f} | "
            f"{fmt_et_short()}"
        )

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
