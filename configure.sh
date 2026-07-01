#!/bin/bash
# =============================================================================
# configure.sh — Runtime configuration for crypto-trader
# Changes are staged and applied with a single restart on exit.
# Exception: paper→live toggle stops/wipes DB/restarts immediately (atomic).
# =============================================================================

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="cryptobot"
CONFIG_FILE="$INSTALL_DIR/config.py"
CHANGED=false

cd "$INSTALL_DIR"
source venv/bin/activate 2>/dev/null || true

# ── Helpers ───────────────────────────────────────────────────────────────────
read_instrument() {
    python3 -c "
import sys; sys.path.insert(0, '.')
try:
    from config import TRADING_SYMBOL; print(TRADING_SYMBOL)
except: print('BTC/USD')
" 2>/dev/null
}

read_mode() {
    python3 -c "
import sys; sys.path.insert(0, '.')
try:
    from config import PAPER_TRADING; print('PAPER' if PAPER_TRADING else 'LIVE')
except: print('PAPER')
" 2>/dev/null
}

read_risk() {
    python3 -c "
import sys; sys.path.insert(0, '.')
try:
    from config import RISK_PER_TRADE_USD
    print(f'\${RISK_PER_TRADE_USD:.2f}')
except Exception as e:
    print('?')
" 2>/dev/null
}

CURRENT_INSTRUMENT=$(read_instrument)
CURRENT_MODE=$(read_mode)

while true; do
    echo ""
    echo "╔══════════════════════════════════════════════════════╗"
    echo "║           crypto-trader — Configure                 ║"
    echo "╚══════════════════════════════════════════════════════╝"
    echo ""
    echo "  Current instrument: $CURRENT_INSTRUMENT"
    echo "  Current mode:       $CURRENT_MODE TRADING"
    echo "  Current risk:       $(read_risk) per B grade trade"
    if [ "$CHANGED" = "true" ]; then
        echo ""
        echo "  ⚠  Unsaved changes — select Exit to apply"
    fi
    echo ""
    echo "  1) Change instrument"
    echo "  2) Toggle paper/live trading"
    echo "  3) View current config"
    echo "  4) Change risk per trade (\$)"
    echo "  5) Exit"
    echo ""
    read -rp "Choice: " CHOICE

    case "$CHOICE" in

        1)
            echo ""
            echo "  Select instrument:"
            echo "    1) BTC/USD"
            echo "    2) ETH/USD"
            echo "    3) SOL/USD"
            echo ""
            read -rp "  Choice [1]: " INST_CHOICE
            INST_CHOICE="${INST_CHOICE:-1}"
            case "$INST_CHOICE" in
                2) NEW_SYMBOL="ETH/USD"; NEW_KRAKEN="ETH/USD:BTNL" ;;
                3) NEW_SYMBOL="SOL/USD"; NEW_KRAKEN="SOL/USD:BTNL" ;;
                *) NEW_SYMBOL="BTC/USD"; NEW_KRAKEN="XBT/USD:BTNL" ;;
            esac
            sed -i 's|^TRADING_SYMBOL = "BTC/USD".*|# TRADING_SYMBOL = "BTC/USD"; KRAKEN_SYMBOL = "XBT/USD:BTNL"|g' "$CONFIG_FILE"
            sed -i 's|^TRADING_SYMBOL = "ETH/USD".*|# TRADING_SYMBOL = "ETH/USD"; KRAKEN_SYMBOL = "ETH/USD:BTNL"|g' "$CONFIG_FILE"
            sed -i 's|^TRADING_SYMBOL = "SOL/USD".*|# TRADING_SYMBOL = "SOL/USD"; KRAKEN_SYMBOL = "SOL/USD:BTNL"|g' "$CONFIG_FILE"
            if [ "$NEW_SYMBOL" = "ETH/USD" ]; then
                sed -i 's|^# TRADING_SYMBOL = "ETH/USD".*|TRADING_SYMBOL = "ETH/USD"; KRAKEN_SYMBOL = "ETH/USD:BTNL"|g' "$CONFIG_FILE"
            elif [ "$NEW_SYMBOL" = "SOL/USD" ]; then
                sed -i 's|^# TRADING_SYMBOL = "SOL/USD".*|TRADING_SYMBOL = "SOL/USD"; KRAKEN_SYMBOL = "SOL/USD:BTNL"|g' "$CONFIG_FILE"
            else
                sed -i 's|^# TRADING_SYMBOL = "BTC/USD".*|TRADING_SYMBOL = "BTC/USD"; KRAKEN_SYMBOL = "XBT/USD:BTNL"|g' "$CONFIG_FILE"
            fi
            CURRENT_INSTRUMENT="$NEW_SYMBOL"
            CHANGED=true
            echo "  ✓ Instrument staged: $NEW_SYMBOL (applies on exit)"
            ;;

        2)
            echo ""
            if [ "$CURRENT_MODE" = "PAPER" ]; then
                echo "  ⚠️  WARNING: You are about to switch to LIVE TRADING."
                echo "  Real money will be at risk."
                echo "  The trade history (trades.db) will be cleared so live"
                echo "  P&L starts clean with no paper trades mixed in."
                echo ""
                read -rp "  Type LIVE to confirm: " CONFIRM
                if [ "$CONFIRM" = "LIVE" ]; then
                    python3 -c "
import re
with open('$CONFIG_FILE', 'r') as f:
    c = f.read()
c = re.sub(r'^PAPER_TRADING\s*=.*$', 'PAPER_TRADING             = False', c, flags=re.MULTILINE)
with open('$CONFIG_FILE', 'w') as f:
    f.write(c)
"
                    CURRENT_MODE="LIVE"
                    echo "  Stopping service..."
                    sudo systemctl stop $SERVICE_NAME
                    sleep 2
                    DB_PATH=$(python3 -c "
import sys, os; sys.path.insert(0, '.')
try:
    from config import DB_PATH; print(DB_PATH)
except:
    print(os.path.expanduser('~/crypto-trader/trades.db'))
" 2>/dev/null)
                    [ -f "$DB_PATH" ] && rm -f "$DB_PATH" && echo "  Trade history cleared."
                    echo "  Starting in LIVE mode..."
                    sudo systemctl start $SERVICE_NAME
                    sleep 3
                    echo "  ✅ Now in LIVE trading mode. Real money at risk."
                    CHANGED=false
                else
                    echo "  Cancelled. Still in PAPER mode."
                fi
            else
                python3 -c "
import re
with open('$CONFIG_FILE', 'r') as f:
    c = f.read()
c = re.sub(r'^PAPER_TRADING\s*=.*$', 'PAPER_TRADING             = True', c, flags=re.MULTILINE)
with open('$CONFIG_FILE', 'w') as f:
    f.write(c)
"
                CURRENT_MODE="PAPER"
                CHANGED=true
                echo "  ✓ Switched to PAPER mode. Live history preserved. (applies on exit)"
            fi
            ;;

        3)
            echo ""
            echo "  ── Current Configuration ──────────────────────────"
            python3 -c "
import sys; sys.path.insert(0, '.')
try:
    from config import TRADING_SYMBOL, KRAKEN_SYMBOL, PAPER_TRADING, ACCOUNT_BALANCE_USD, LEVERAGE, RISK_PER_TRADE_USD
    mode = 'PAPER' if PAPER_TRADING else 'LIVE'
    print(f'  Instrument:    {TRADING_SYMBOL}')
    print(f'  Kraken pair:   {KRAKEN_SYMBOL}')
    print(f'  Mode:          {mode}')
    try:
        from data.market_data import get_account_balance
        bal = get_account_balance()
        if bal and bal.get('USD', {}).get('free', 0) > 0:
            live_cash    = bal['USD']['free']
            max_notional = live_cash * LEVERAGE
            print(f'  Balance:       \${live_cash:,.2f}  (live from Kraken)')
            print(f'  Margin limit:  \${max_notional:,.2f}  ({LEVERAGE}x leverage)')
        else:
            print(f'  Balance:       \${ACCOUNT_BALANCE_USD:,.0f}  (from config)')
    except Exception as e:
        print(f'  Balance:       \${ACCOUNT_BALANCE_USD:,.0f}  (from config)')
    print(f'  Risk/Trade:    \${RISK_PER_TRADE_USD:.2f} per B grade trade')
    print(f'  A grade:       \${RISK_PER_TRADE_USD * 1.5:.2f}  |  C grade: \${RISK_PER_TRADE_USD * 0.5:.2f}')
except Exception as e:
    print(f'  Error reading config: {e}')
"
            echo ""
            ;;

        4)
            echo ""
            echo "  Current risk: $(read_risk) per B grade trade"
            echo ""
            echo "  Enter dollar amount (e.g. 100 = \$100 per B grade trade, 1 = \$1 for testing)"
            echo ""
            read -rp "  New risk \$ [ENTER to cancel]: " NEW_RISK
            if [ -z "$NEW_RISK" ]; then
                echo "  Cancelled."
            elif echo "$NEW_RISK" | grep -qE "^[0-9]+(\.[0-9]+)?$" && python3 -c "exit(0 if float('$NEW_RISK') >= 1 else 1)" 2>/dev/null; then
                python3 << PYEOF
import re
with open('$CONFIG_FILE', 'r') as f:
    c = f.read()
c = re.sub(r'^RISK_PER_TRADE_USD\s*=.*$', 'RISK_PER_TRADE_USD        = $NEW_RISK', c, flags=re.MULTILINE)
with open('$CONFIG_FILE', 'w') as f:
    f.write(c)
PYEOF
                CHANGED=true
                echo "  ✓ Risk staged: \$$NEW_RISK per B grade trade (applies on exit)"
            else
                echo "  ⚠  Enter a number \$1 or greater."
            fi
            ;;

        5)
            echo ""
            if [ "$CHANGED" = "true" ]; then
                echo "  Applying changes — restarting service..."
                sudo systemctl restart $SERVICE_NAME
                sleep 3
                if systemctl is-active --quiet $SERVICE_NAME; then
                    echo "  ✅ Service restarted successfully."
                else
                    echo "  ⚠  Service did not start — check: journalctl -u $SERVICE_NAME -n 20"
                fi
            else
                echo "  No changes made."
            fi
            echo ""
            exit 0
            ;;

        *)
            echo "  Invalid choice."
            ;;
    esac
done
