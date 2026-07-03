#!/usr/bin/env bash
# ============================================================
#  options_trader v1.5  —  Live Configuration Manager
#  v1.0 — original release
#  v1.1 — 2026-06-27 — replaced SMS/Twilio with Telegram
#  v1.2 — 2026-06-27 — swapped menu order: Telegram now 4, TT credentials now 5
#  v1.3 — 2026-06-27 — fixed menu display lines to match handler order
#  v1.4 — 2026-06-27 — auto restart on exit if changes made, no prompt
#  v1.5 — 2026-07-01 — wipe trades.db on instrument change (paper mode only);
#          ORB range auto-fetched for new instrument via get_orb_range.py
#  v1.6 — 2026-07-02 — add Daily loss cap override menu (OT_DAILY_LOSS_LIMIT)
#
#  Run this anytime to view or change bot settings.
#  Changes take effect on the NEXT bot start — the bot is
#  never restarted automatically to avoid mid-session surprises.
#
#  Usage:
#    ./configure.sh          — interactive menu
#    ./configure.sh --show   — print current config and exit
# ============================================================

SERVICE_NAME="optionsbot"
BOT_DIR="$HOME/options-trader"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# ── Colours ──────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

print_banner() {
    echo ""
    echo -e "${BOLD}${CYAN}============================================================${RESET}"
    echo -e "${BOLD}${CYAN}  options_trader  —  Configuration Manager${RESET}"
    echo -e "${BOLD}${CYAN}============================================================${RESET}"
    echo ""
}

print_ok()   { echo -e "  ${GREEN}✓${RESET}   $1"; }
print_warn() { echo -e "  ${YELLOW}⚠${RESET}   $1"; }
print_info() { echo -e "  ${CYAN}→${RESET}  $1"; }
ask()        { read -p "    $1: " "$2"; }
ask_secret() { read -s -p "    $1: " "$2"; echo ""; }
ask_yn()     {
    while true; do
        read -p "    $1 [y/n]: " yn
        case "$yn" in [Yy]) return 0;; [Nn]) return 1;; esac
    done
}

# ── Read a single Environment= value from the unit file ──────
get_env() {
    sudo grep -oP "(?<=Environment=${1}=).*" "$UNIT_FILE" 2>/dev/null | tail -1 || echo ""
}

# ── Update or add an Environment= line in the unit file ──────
set_env() {
    local key="$1" val="$2"
    if sudo grep -q "Environment=${key}=" "$UNIT_FILE" 2>/dev/null; then
        sudo sed -i "s|Environment=${key}=.*|Environment=${key}=${val}|" "$UNIT_FILE"
    else
        sudo sed -i "/ExecStartPre=/i Environment=${key}=${val}" "$UNIT_FILE"
    fi
}

reload_daemon() {
    sudo systemctl daemon-reload
}

bot_is_running() {
    systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null
}

# ──────────────────────────────────────────────────────────────
# SHOW CURRENT CONFIG
# ──────────────────────────────────────────────────────────────
show_config() {
    local instrument risk paper account

    if [[ ! -f "$UNIT_FILE" ]]; then
        echo -e "  ${RED}Service unit not found.${RESET}"
        echo -e "  Run setup_ec2.sh first to install the bot."
        return 1
    fi

    instrument=$(get_env "OT_INSTRUMENT")
    risk=$(get_env "OT_RISK_USD")
    paper=$(get_env "OT_PAPER_TRADING")
    account=$(get_env "TT_ACCOUNT_NUMBER")
    telegram_token=$(get_env "TELEGRAM_TOKEN")
    telegram_chat=$(get_env "TELEGRAM_CHAT_ID")

    local mode_label
    if [[ "$paper" == "False" ]]; then
        mode_label="${RED}${BOLD}🔴 LIVE — real money${RESET}"
    else
        mode_label="${GREEN}📄 PAPER — simulated fills${RESET}"
    fi

    local status_label
    if bot_is_running; then
        status_label="${GREEN}● running${RESET}"
    else
        status_label="${YELLOW}○ stopped${RESET}"
    fi

    echo -e "  ${BOLD}Current Configuration${RESET}"
    echo -e "  ─────────────────────────────────────────"
    echo -e "  Bot status:     $(echo -e $status_label)"
    echo -e "  Instrument:     ${BOLD}${instrument:-not set}${RESET}"
    echo -e "  Risk per trade: ${BOLD}\$${risk:-not set}${RESET}"
    local dll=$(get_env "OT_DAILY_LOSS_LIMIT")
    echo -e "  Daily loss cap: ${BOLD}\$${dll:-${risk} (default)}${RESET}"
    echo -e "  Trading mode:   $(echo -e $mode_label)"
    echo -e "  TT Account:     ${BOLD}${account:-not set}${RESET}"
    local tg_status
    if [[ -n "$telegram_token" ]]; then
        tg_status="✓ enabled (chat ${telegram_chat})"
    else
        tg_status="— disabled"
    fi
    echo -e "  Telegram:       ${BOLD}${tg_status}${RESET}"
    echo -e "  ─────────────────────────────────────────"
    echo ""

    if bot_is_running; then
        print_warn "Bot is currently running. Changes take effect on next start."
    fi
}

# ──────────────────────────────────────────────────────────────
# MENU ACTIONS
# ──────────────────────────────────────────────────────────────

change_instrument() {
    local current
    current=$(get_env "OT_INSTRUMENT")
    echo ""
    echo -e "  Current instrument: ${BOLD}${current}${RESET}"
    echo ""
    echo -e "  ${BOLD}1. QQQ${RESET}  —  Nasdaq-100 ETF   (\$1 strikes)"
    echo -e "  ${BOLD}2. SPY${RESET}  —  S&P 500 ETF      (\$1 strikes)"
    echo -e "  ${BOLD}3. SPX${RESET}  —  S&P 500 Index    (\$5 strikes)"
    echo ""
    while true; do
        read -p "    Select [1/2/3, or ENTER to keep ${current}]: " choice
        case "${choice:-0}" in
            0) print_info "Unchanged: ${current}"; return ;;
            1) NEW_INST="QQQ"; break ;;
            2) NEW_INST="SPY"; break ;;
            3) NEW_INST="SPX"; break ;;
            *) print_warn "Please enter 1, 2, or 3." ;;
        esac
    done
    set_env "OT_INSTRUMENT"  "$NEW_INST"
    set_env "OT_BOT_NAME"    "OptionsTrader-${NEW_INST}"
    reload_daemon
    print_ok "Instrument updated to ${BOLD}${NEW_INST}${RESET}."
    # Wipe trades.db in paper mode — old trades from a different instrument
    # are meaningless and pollute the P&L dashboard
    local paper
    paper=$(get_env "OT_PAPER_TRADING")
    if [[ "$paper" != "False" ]]; then
        rm -f "$BOT_DIR/trades.db"
        print_ok "Paper trade history cleared (instrument changed)."
    fi
}

change_risk() {
    local current
    current=$(get_env "OT_RISK_USD")
    echo ""
    echo -e "  Current risk per trade: ${BOLD}\$${current}${RESET}"
    echo ""
    while true; do
        read -p "    New risk per trade in \$ [ENTER to keep \$${current}]: " input
        if [[ -z "$input" ]]; then
            print_info "Unchanged: \$${current}"
            return
        fi
        if [[ "$input" =~ ^[0-9]+(\.[0-9]+)?$ ]] && (( $(echo "$input > 0" | bc -l) )); then
            set_env "OT_RISK_USD" "$input"
            reload_daemon
            print_ok "Risk per trade updated to ${BOLD}\$$input${RESET}."
            return
        fi
        print_warn "Please enter a positive number (e.g. 200 or 150.50)."
    done
}

change_daily_loss() {
    local current risk
    current=$(get_env "OT_DAILY_LOSS_LIMIT")
    risk=$(get_env "OT_RISK_USD")
    echo ""
    echo -e "  ${BOLD}Daily loss cap${RESET} — halts NEW entries once the day's NET"
    echo -e "  P&L is down by this amount. Open trades still exit normally."
    echo -e "  Default is one trade's risk (\$${risk})."
    echo -e "  Current: ${BOLD}\$${current:-${risk} (default)}${RESET}"
    echo ""
    while true; do
        read -p "    New cap in \$, 'r' to reset to risk default, ENTER to keep: " input
        if [[ -z "$input" ]]; then
            print_info "Unchanged."
            return
        fi
        if [[ "$input" == "r" ]]; then
            set_env "OT_DAILY_LOSS_LIMIT" "$risk"
            reload_daemon
            print_ok "Daily loss cap reset to per-trade risk (\$${risk})."
            return
        fi
        if [[ "$input" =~ ^[0-9]+(\.[0-9]+)?$ ]] && (( $(echo "$input > 0" | bc -l) )); then
            set_env "OT_DAILY_LOSS_LIMIT" "$input"
            reload_daemon
            print_ok "Daily loss cap updated to ${BOLD}\$$input${RESET}."
            return
        fi
        print_warn "Enter a positive number, 'r' to reset, or ENTER to keep."
    done
}

change_mode() {
    local current
    current=$(get_env "OT_PAPER_TRADING")
    echo ""
    if [[ "$current" == "False" ]]; then
        echo -e "  Current mode: ${RED}${BOLD}🔴 LIVE${RESET}"
        echo ""
        if ask_yn "Switch to PAPER mode?"; then
            set_env "OT_PAPER_TRADING" "True"
            reload_daemon
            print_ok "Switched to ${BOLD}📄 PAPER mode${RESET}."
        else
            print_info "Unchanged: LIVE."
        fi
    else
        echo -e "  Current mode: ${GREEN}📄 PAPER${RESET}"
        echo ""
        print_warn "You are about to enable LIVE TRADING."
        print_warn "Real orders will be placed with real money."
        echo ""
        read -p "    Type  LIVE  to confirm: " confirm
        if [[ "$confirm" == "LIVE" ]]; then
            set_env "OT_PAPER_TRADING" "False"
            reload_daemon
            print_ok "Switched to ${RED}${BOLD}🔴 LIVE mode${RESET}."
        else
            print_info "Confirmation not received — mode unchanged."
        fi
    fi
}

change_tt_credentials() {
    echo ""
    echo -e "  Update your TastyTrade OAuth credentials."
    echo -e "  ${CYAN}Leave blank and press ENTER to keep the current value.${RESET}"
    echo ""

    local current_secret current_token current_account
    current_secret=$(get_env "TT_CLIENT_SECRET")
    current_token=$(get_env "TT_REFRESH_TOKEN")
    current_account=$(get_env "TT_ACCOUNT_NUMBER")

    read -s -p "    New Client Secret  [ENTER to keep current]: " new_secret; echo ""
    read -s -p "    New Refresh Token  [ENTER to keep current]: " new_token;  echo ""
    read -p    "    Account Number     [ENTER to keep ${current_account}]: " new_account

    local changed=false
    if [[ -n "$new_secret" ]]; then
        set_env "TT_CLIENT_SECRET"  "$new_secret";  changed=true; fi
    if [[ -n "$new_token" ]]; then
        set_env "TT_REFRESH_TOKEN"  "$new_token";   changed=true; fi
    if [[ -n "$new_account" ]]; then
        set_env "TT_ACCOUNT_NUMBER" "$new_account"; changed=true; fi

    if [[ "$changed" == "true" ]]; then
        reload_daemon
        print_ok "TastyTrade credentials updated."
    else
        print_info "No credentials changed."
    fi
}

change_telegram() {
    local current_token current_chat
    current_token=$(get_env "TELEGRAM_TOKEN")
    current_chat=$(get_env "TELEGRAM_CHAT_ID")
    echo ""

    if [[ -n "$current_token" ]]; then
        echo -e "  Telegram alerts are currently ${GREEN}enabled${RESET}."
        echo -e "  Chat ID: ${BOLD}${current_chat}${RESET}"
    else
        echo -e "  Telegram alerts are currently ${YELLOW}disabled${RESET}."
    fi

    echo ""
    echo -e "  ${CYAN}Press ENTER on any field to keep the current value.${RESET}"
    echo ""

    read -p "    Bot Token [ENTER = no change]: " new_token
    read -p "    Chat ID   [ENTER = no change, current: ${current_chat}]: " new_chat

    local changed=false
    if [[ -n "$new_token" ]]; then
        set_env "TELEGRAM_TOKEN" "$new_token"
        changed=true
    fi
    if [[ -n "$new_chat" ]]; then
        set_env "TELEGRAM_CHAT_ID" "$new_chat"
        changed=true
    fi

    if [[ "$changed" == "true" ]]; then
        reload_daemon
        print_ok "Telegram settings updated."
    else
        print_info "No changes made."
    fi
}

auto_restart() {
    echo ""
    echo "  Applying changes and restarting bot..."
    sudo systemctl restart "$SERVICE_NAME"
    sleep 4
    if bot_is_running; then
        print_ok "Bot restarted successfully with new settings."
    else
        print_warn "Bot failed to start — check: journalctl -u ${SERVICE_NAME} -n 20"
    fi
}

# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

if [[ "${1:-}" == "--show" ]]; then
    print_banner
    show_config
    exit 0
fi

if [[ ! -f "$UNIT_FILE" ]]; then
    print_banner
    echo -e "  ${RED}No service unit found at ${UNIT_FILE}${RESET}"
    echo -e "  Run setup_ec2.sh first to install and configure the bot."
    echo ""
    exit 1
fi

print_banner
show_config

CHANGED=false
while true; do
    echo -e "  ${BOLD}What would you like to change?${RESET}"
    echo ""
    echo -e "  ${BOLD}1.${RESET}  Instrument          (currently: $(get_env OT_INSTRUMENT))"
    echo -e "  ${BOLD}2.${RESET}  Risk per trade      (currently: \$$(get_env OT_RISK_USD))"
    echo -e "  ${BOLD}3.${RESET}  Paper / Live mode   (currently: $([ "$(get_env OT_PAPER_TRADING)" = "False" ] && echo "🔴 LIVE" || echo "📄 PAPER"))"
    echo -e "  ${BOLD}4.${RESET}  Telegram alerts     (chat: $(get_env TELEGRAM_CHAT_ID))"
    echo -e "  ${BOLD}5.${RESET}  TastyTrade credentials"
    echo -e "  ${BOLD}6.${RESET}  Daily loss cap      (currently: \$$(dll=$(get_env OT_DAILY_LOSS_LIMIT); echo ${dll:-$(get_env OT_RISK_USD)}))"
    echo -e "  ${BOLD}7.${RESET}  Done"
    echo ""
    read -p "    Select [1-7]: " menu_choice

    case "$menu_choice" in
        1) change_instrument; CHANGED=true ;;
        2) change_risk;       CHANGED=true ;;
        3) change_mode;       CHANGED=true ;;
        4) change_telegram;       CHANGED=true ;;
        5) change_tt_credentials; CHANGED=true ;;
        6) change_daily_loss;     CHANGED=true ;;
        7) break ;;
        *) print_warn "Please enter a number between 1 and 7." ;;
    esac
    echo ""
done

if [[ "$CHANGED" == "true" ]]; then
    echo ""
    show_config
    auto_restart
fi

echo ""
