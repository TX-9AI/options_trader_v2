"""
notifications/alert_manager.py — Telegram alerts for options_trader.
v1.0 — original release (Twilio SMS)
v1.1 — 2026-06-27 — replaced Twilio SMS with Telegram
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

    def send_startup_alert(self, paper: bool, instrument: str,
                            risk_usd: float, session_limit: int):
        mode = "PAPER" if paper else "LIVE"
        self._send(
            f"🚀 OptionsBot [{mode}] started | "
            f"{instrument} | "
            f"risk=${risk_usd:.0f}/trade | "
            f"CB={session_limit} losses | "
            f"{fmt_et_short()}"
        )

    def send_entry_alert(self, record: dict):
        mode = "PAPER" if record.get("paper_trade") else "LIVE"
        if record.get("is_butterfly"):
            self._send(
                f"🦋 [{mode}] BUTTERFLY {record.get('option_side','').upper()} "
                f"{record.get('center_strike','')} "
                f"±{int((record.get('upper_strike',0) - record.get('center_strike',0)))} "
                f"×{record.get('contracts',0)} "
                f"debit=${record.get('net_debit',0):.2f} "
                f"total=${record.get('total_cost',0):.0f} | "
                f"{record.get('strategy','')} | "
                f"{fmt_et_short()}"
            )
        else:
            self._send(
                f"📈 [{mode}] {record.get('option_side','').upper()} "
                f"{record.get('strike','')} "
                f"×{record.get('contracts',0)} "
                f"@ ${record.get('entry_premium',0):.2f} "
                f"total=${record.get('total_cost',0):.0f} | "
                f"{record.get('strategy','')} "
                f"grade={record.get('setup_grade','')} | "
                f"{fmt_et_short()}"
            )

    def send_exit_alert(self, trade_id: str, setup_type: str,
                         exit_premium: float, entry_premium: float,
                         pnl_usd: float, contracts: int, reason: str):
        pnl_pct = (exit_premium - entry_premium) / entry_premium * 100 \
                  if entry_premium > 0 else 0
        sign    = "+" if pnl_usd >= 0 else ""
        icon    = "✅" if pnl_usd >= 0 else "❌"
        self._send(
            f"{icon} CLOSED {setup_type[:20]} | "
            f"exit=${exit_premium:.2f} | "
            f"pnl={sign}${pnl_usd:.2f} ({sign}{pnl_pct:.1f}%) | "
            f"{reason[:30]} | "
            f"{fmt_et_short()}"
        )

    def send_circuit_breaker_alert(self, session_losses: int, reason: str):
        self._send(
            f"🚨 CIRCUIT BREAKER: {session_losses} session losses | "
            f"TRADING HALTED | "
            f"{reason} | "
            f"Restart: sudo systemctl start optionsbot | "
            f"{fmt_et_short()}"
        )

    def send_regime_alert(self, old_regime: str, new_regime: str,
                           conviction: float, notes: str = ""):
        self._send(
            f"📊 REGIME: {old_regime} → {new_regime} "
            f"({conviction:.0%}) | {notes[:40]} | {fmt_et_short()}"
        )


_alert_manager: Optional[AlertManager] = None


def get_alert_manager() -> AlertManager:
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
    return _alert_manager
