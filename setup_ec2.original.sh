#!/bin/bash
# =============================================================================
# setup_ec2.sh — options_trader v2.5 EC2 Setup
# v1.0 — original release
# v2.0 — 2026-06-27 — QQQ/SPX banner, Telegram only, VERSION=2.0
# v2.1 — 2026-06-27 — auto git init on fresh install
# v2.2 — 2026-06-27 — git branch -M main on init
# v2.3 — 2026-06-27 — GitHub token prompt, added to systemd service
# v2.4 — 2026-06-27 — GitHub repo prompt, token only required if repo provided
# v2.5 — 2026-06-30 — strip full URL/protocol from GITHUB_REPO input to prevent
#         doubled "https://github.com/https://github.com/..." remote URLs
#         if the operator pastes a full URL instead of "owner/repo"
#
# QQQ/SPX 0DTE | TastyTrade OAuth | Telegram alerts
# =============================================================================

set -e
export DEBIAN_FRONTEND=noninteractive
export TERM=xterm-256color

INSTALL_DIR="$HOME/options-trader"
DEPLOY_DIR="$HOME/options-trader-deploy"
SERVICE_NAME="optionsbot"
VENV="$INSTALL_DIR/venv"
VERSION="2.5"

exec < /dev/tty

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
RED='\033[0;31m'; BOLD='\033[1m'; RESET='\033[0m'

print_step() { echo -e "\n${BOLD}${GREEN}[ $1 ]${RESET} $2"; }
print_ok()   { echo -e "  ${GREEN}✓${RESET}  $1"; }
print_info() { echo -e "  ${CYAN}→${RESET}  $1"; }
print_warn() { echo -e "  ${YELLOW}⚠${RESET}  $1"; }
ask()        { read -rp "    $1: " "$2"; }
ask_secret() { read -rsp "    $1 (paste, then ENTER): " "$2"; echo ""; }
ask_yn()     {
    while true; do
        read -rp "    $1 [y/n]: " yn
        case "$yn" in [Yy]) return 0;; [Nn]) return 1;; esac
    done
}

echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║     options_trader v${VERSION}  |  Vertigo Capital     ║${RESET}"
echo -e "${BOLD}${CYAN}║     QQQ/SPX 0DTE  |  TastyTrade  |  Telegram       ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Have ready:"
echo "    - TastyTrade Client Secret"
echo "    - TastyTrade Refresh Token"
echo "    - TastyTrade Account Number (e.g. 5WT12345)"
echo "    - Telegram Bot Token & Chat ID"
echo "    - GitHub Personal Access Token"
echo ""
read -rp "  Press ENTER to continue or Ctrl+C to cancel..."

# ─── STEP 1: TRADING MODE ────────────────────────────────────────────────────
print_step "1/8" "Trading Mode"
echo ""
INSTRUMENT="QQQ"
RISK_USD="200"
PAPER_TRADING="True"

printf "    Paper trading? [Y/n, default=Y]: "; read -r PAPER_INPUT
PAPER_INPUT="${PAPER_INPUT:-Y}"
if [[ "$PAPER_INPUT" =~ ^[Nn] ]]; then
    PAPER_TRADING="False"
    print_warn "LIVE TRADING — real orders will be sent to TastyTrade"
else
    PAPER_TRADING="True"
    print_ok "Paper mode"
fi

printf "    Risk per trade USD [200]: "; read -r RISK_INPUT
RISK_USD="${RISK_INPUT:-200}"
print_ok "Instrument: QQQ/SPX | Risk: \$${RISK_USD}/trade | Mode: $([ "$PAPER_TRADING" = "True" ] && echo "PAPER" || echo "LIVE")"

# ─── STEP 2: TASTYTRADE CREDENTIALS ─────────────────────────────────────────
print_step "2/8" "TastyTrade OAuth Credentials"
echo ""
echo -e "  ${BOLD}How to get credentials (2 min):${RESET}"
echo -e "  1. my.tastytrade.com → Manage → API → OAuth Applications"
echo -e "  2. New OAuth Application → all scopes → Create → ${BOLD}save Client Secret${RESET}"
echo -e "  3. Inside app → New Personal OAuth Grant → all scopes → ${BOLD}save Refresh Token${RESET}"
echo -e "  4. Account Number is on the main account page (e.g. 5WT12345)"
echo ""
read -rp "    Press ENTER when ready..."
echo ""

while true; do
    ask_secret "Client Secret" TT_CLIENT_SECRET
    [[ -n "$TT_CLIENT_SECRET" ]] && break
    print_warn "Cannot be empty."
done
while true; do
    ask_secret "Refresh Token" TT_REFRESH_TOKEN
    [[ -n "$TT_REFRESH_TOKEN" ]] && break
    print_warn "Cannot be empty."
done
while true; do
    ask "Account Number (e.g. 5WT12345)" TT_ACCOUNT_NUMBER
    [[ -n "$TT_ACCOUNT_NUMBER" ]] && break
    print_warn "Cannot be empty."
done
print_ok "TastyTrade credentials accepted."

# ─── STEP 3: TELEGRAM ────────────────────────────────────────────────────────
print_step "3/8" "Telegram Alerts"
echo ""
while true; do
    ask_secret "Telegram Bot Token" TELEGRAM_TOKEN
    [[ -n "$TELEGRAM_TOKEN" ]] && break
    print_warn "Cannot be empty."
done
while true; do
    ask "Telegram Chat ID" TELEGRAM_CHAT_ID
    [[ -n "$TELEGRAM_CHAT_ID" ]] && break
    print_warn "Cannot be empty."
done
print_ok "Telegram configured."

# ─── STEP 4: GITHUB REPO & TOKEN ────────────────────────────────────────────
print_step "4/8" "GitHub Repository (optional)"
echo ""
echo -e "  Enter the GitHub repo to link this server to for push.sh."
echo -e "  Format: TX-9AI/options_trader_v2"
echo -e "  (Full URLs are also accepted and will be normalized automatically)"
echo -e "  Press ENTER to skip."
echo ""
GITHUB_REPO=""
GITHUB_TOKEN=""
printf "    GitHub repo [ENTER to skip]: "; read -r GITHUB_REPO

# ── Normalize GITHUB_REPO: strip protocol, host, trailing .git/slash ─────────
# Accepts any of:
#   TX-9AI/options_trader_v2
#   https://github.com/TX-9AI/options_trader_v2
#   https://github.com/TX-9AI/options_trader_v2.git
#   github.com/TX-9AI/options_trader_v2
# Always normalizes to: TX-9AI/options_trader_v2
if [[ -n "$GITHUB_REPO" ]]; then
    GITHUB_REPO="${GITHUB_REPO#https://}"
    GITHUB_REPO="${GITHUB_REPO#http://}"
    GITHUB_REPO="${GITHUB_REPO#github.com/}"
    GITHUB_REPO="${GITHUB_REPO%.git}"
    GITHUB_REPO="${GITHUB_REPO%/}"
fi

if [[ -n "$GITHUB_REPO" ]]; then
    echo ""
    echo -e "  Get token from: github.com → Settings → Developer settings → Tokens (classic)"
    echo ""
    while true; do
        ask_secret "GitHub Personal Access Token" GITHUB_TOKEN
        [[ -n "$GITHUB_TOKEN" ]] && break
        print_warn "Cannot be empty."
    done
    print_ok "GitHub repo: https://github.com/${GITHUB_REPO}"
    print_ok "GitHub token accepted."
else
    print_ok "Skipping GitHub — push.sh will prompt for token when needed."
fi

# ─── STEP 5: SYSTEM PACKAGES ─────────────────────────────────────────────────
print_step "5/8" "System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip python3-venv python-is-python3 git rsync bc sqlite3
print_ok "System packages ready."

# ─── STEP 6: INSTALL FILES ───────────────────────────────────────────────────
print_step "6/8" "Installing bot files"
mkdir -p "$INSTALL_DIR"
rsync -a \
    --exclude='.git' \
    --exclude='*.pem' \
    --exclude='*.bat' \
    --exclude='credentials.py' \
    --exclude='venv' \
    --exclude='trades.db' \
    --exclude='trades.db-shm' \
    --exclude='trades.db-wal' \
    --exclude='bot.log' \
    --exclude='__pycache__' \
    "$DEPLOY_DIR/" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR"/*.sh 2>/dev/null || true

for f in main.py config.py requirements.txt; do
    [ -f "$INSTALL_DIR/$f" ] || { echo "ERROR: $f missing. Aborting."; exit 1; }
done
print_ok "Files installed to ${INSTALL_DIR}"

# ─── STEP 7: PYTHON ENVIRONMENT ──────────────────────────────────────────────
print_step "7/8" "Python environment"
python3 -m venv "$VENV"
source "$VENV/bin/activate"
pip install --upgrade pip -q
pip install -r "$INSTALL_DIR/requirements.txt" -q
pip install yfinance requests -q
print_ok "Dependencies installed."

grep -q "options-trader/venv" ~/.bashrc || echo "source $VENV/bin/activate" >> ~/.bashrc
grep -q "cd ~/options-trader"  ~/.bashrc || echo "cd $INSTALL_DIR"           >> ~/.bashrc

# ─── STEP 8: SYSTEMD SERVICE ─────────────────────────────────────────────────
print_step "8/8" "Configuring systemd service"

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << SVCEOF
[Unit]
Description=options_trader v${VERSION} — QQQ/SPX 0DTE | Vertigo Capital
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${INSTALL_DIR}
Environment=OT_INSTRUMENT=${INSTRUMENT}
Environment=OT_RISK_USD=${RISK_USD}
Environment=OT_PAPER_TRADING=${PAPER_TRADING}
Environment=OT_BOT_NAME=OptionsTrader-${INSTRUMENT}
Environment=TT_CLIENT_SECRET=${TT_CLIENT_SECRET}
Environment=TT_REFRESH_TOKEN=${TT_REFRESH_TOKEN}
Environment=TT_ACCOUNT_NUMBER=${TT_ACCOUNT_NUMBER}
Environment=TELEGRAM_TOKEN=${TELEGRAM_TOKEN}
Environment=TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
Environment=GITHUB_TOKEN=${GITHUB_TOKEN}
Environment=GITHUB_REPO=${GITHUB_REPO}
ExecStartPre=/bin/bash -c 'touch ${INSTALL_DIR}/bot.log ${INSTALL_DIR}/trades.db && chown ${USER}:${USER} ${INSTALL_DIR}/bot.log ${INSTALL_DIR}/trades.db'
ExecStart=${VENV}/bin/python main.py --service
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
SVCEOF

sudo chmod 600 /etc/systemd/system/${SERVICE_NAME}.service
sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}

touch "$INSTALL_DIR/bot.log" "$INSTALL_DIR/trades.db"
chown "${USER}:${USER}" "$INSTALL_DIR/bot.log" "$INSTALL_DIR/trades.db"

# ── Git init — repo-ready on every fresh install ─────────────────────────────
cd "$INSTALL_DIR"
if [ ! -d ".git" ]; then
    git init -q
    git branch -M main 2>/dev/null || git checkout -b main 2>/dev/null || true
    if [[ -n "$GITHUB_REPO" ]]; then
        git remote add origin "https://github.com/${GITHUB_REPO}.git"
        git fetch origin main -q 2>/dev/null || true
        git reset --hard origin/main -q 2>/dev/null || true
        print_ok "Git repo initialized — push.sh ready to use"
    else
        print_ok "Git repo initialized — add remote manually when ready"
    fi
fi

# ── Start bot ─────────────────────────────────────────────────────────────────
print_info "Starting bot..."
sudo systemctl start ${SERVICE_NAME}
sleep 8

STATUS=$(systemctl is-active ${SERVICE_NAME})
if [ "$STATUS" = "active" ]; then
    echo ""
    echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}${GREEN}║          ✅  Setup Complete — Bot Running!          ║${RESET}"
    echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════╝${RESET}"
    echo ""
    echo -e "  Instrument:  QQQ/SPX 0DTE (TastyTrade)"
    echo -e "  Mode:        $([ "$PAPER_TRADING" = "True" ] && echo "📄 PAPER" || echo "🔴 LIVE")"
    echo -e "  Risk:        \$${RISK_USD}/trade"
    echo -e "  TT Account:  ${TT_ACCOUNT_NUMBER}"
    echo -e "  Telegram:    chat ${TELEGRAM_CHAT_ID}"
    echo ""
    echo -e "  Commands:"
    echo -e "    python status.py                   — live status"
    echo -e "    python query.py                    — performance dashboard"
    echo -e "    journalctl -u ${SERVICE_NAME} -f   — live logs"
    echo -e "    bash configure.sh                  — change settings"
    echo -e "    bash push.sh                       — push changes to GitHub"
    echo -e "    bash snapshot.sh                   — snapshot bot state"
    echo ""
    source "${VENV}/bin/activate" && python "$INSTALL_DIR/status.py"
else
    echo ""
    echo -e "${BOLD}${YELLOW}⚠️  Service did not start. Check:${RESET}"
    echo -e "    journalctl -u ${SERVICE_NAME} -n 30 --no-pager"
    echo ""
    journalctl -u ${SERVICE_NAME} -n 20 --no-pager
fi

# Always end in the install dir with venv active
export PATH="$VENV/bin:$PATH"
cd "$INSTALL_DIR"
exec bash --login
