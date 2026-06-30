#!/bin/bash
# =============================================================================
# check_versions.sh — Recursively verify version headers and key fixes
# across the entire options_trader project.
#
# Excludes: venv, __pycache__, .git, *.pem, trades.db*, bot.log, snapshots
#
# Run from ~/options-trader
# =============================================================================

cd "$(dirname "$0")" || exit 1

echo ""
echo "============================================================"
echo "  RECURSIVE VERSION HEADER CHECK — $(date)"
echo "  Directory: $(pwd)"
echo "============================================================"
echo ""

# Find every .py and .sh file in the project, excluding noise
FILES=$(find . \
    -type d \( -name venv -o -name __pycache__ -o -name .git -o -name snapshots \) -prune \
    -o -type f \( -name "*.py" -o -name "*.sh" \) -print \
    | sed 's|^\./||' \
    | sort)

TOTAL=0
for f in $FILES; do
    TOTAL=$((TOTAL+1))
    echo "------------------------------------------------------------"
    echo "FILE: $f"
    echo "  Last modified: $(stat -c '%y' "$f" 2>/dev/null || stat -f '%Sm' "$f" 2>/dev/null)"
    echo "  Size: $(wc -l < "$f") lines"
    echo "  Header:"
    head -12 "$f" | sed 's/^/    /'
    echo ""
done

echo "============================================================"
echo "  TOTAL FILES SCANNED: $TOTAL"
echo "============================================================"
echo ""
echo "============================================================"
echo "  CRITICAL FIX CHECKS — today's session"
echo "============================================================"
echo ""

check() {
    local file="$1"
    local pattern="$2"
    local label="$3"
    if [ -f "$file" ] && grep -q "$pattern" "$file" 2>/dev/null; then
        echo "  \u2713 PRESENT: $label  (in $file)"
    else
        echo "  \u2717 MISSING: $label  (expected in $file)"
    fi
}

check "main.py"                          "ORBState.OPEN_LONG"           "ORB state fix (OPEN_LONG not CONFIRMED_LONG)"
check "main.py"                          "STRATEGY: NO TRADE"           "NO TRADE log line"
check "main.py"                          "send_shutdown_alert"          "Shutdown alert hook"
check "main.py"                          "signal.SIGTERM"               "SIGTERM handler"
check "main.py"                          "score is None"                "Handles scorer returning None (no Grade C)"
check "analysis/orb_engine.py"           "OPEN_LONG"                    "ORB engine state rename"
check "analysis/orb_engine.py"           "_rearm"                       "ORB re-arm logic"
check "analysis/orb_engine.py"           "matches\[-1\]"                "Most-recent 9:30 candle fetch (not oldest)"
check "analysis/trend_engine.py"         'tf == "5m"'                   "ADX from 5m timeframe"
check "status.py"                        "ORB High"                    "Structured ORB display"
check "status.py"                        "No Trade"                    "No Trade display string"
check "notifications/alert_manager.py"   "send_shutdown_alert"          "Shutdown alert method"
check "notifications/alert_manager.py"   "INSTRUMENT"                   "Ticker in alerts"
check "notifications/alert_manager.py"   "send_regime_alert"            "Regime alert present (should be no-op/pass)"
check "risk/setup_scorer.py"             "return None"                  "Grade C elimination (returns None)"
check "risk/setup_scorer.py"             "Optional\[SetupScore\]"       "Score return type updated"
check "strategy/butterfly_strategy.py"   "gex_environment"              "GEX field name fix"
check "strategy/butterfly_strategy.py"   "BUTTERFLY_WING_SPX"           "Fixed wing widths"
check "strategy/butterfly_strategy.py"   "_fired_today"                 "One butterfly per session"
check "strategy/orb_strategy.py"         "ORBState.OPEN_LONG"           "ORB strategy state rename"
check "execution/exit_engine.py"         "POST_TARGET_TRAIL_LOCK_PCT"   "FVG trail past 100% TP"
check "execution/exit_engine.py"         "_find_1m_fvgs"                "1m FVG detection"
check "execution/position_manager.py"    "notify_position_closed"       "ORB re-arm hook on position close"
check "config.py"                        "BUTTERFLY_WING_SPX"           "Butterfly config constants"
check "config.py"                        "BUTTERFLY_ENTRY_START_ET"     "Butterfly noon entry window"
check "push.sh"                          "Detected malformed remote"    "Self-healing remote URL"
check "push.sh"                          "diverged"                    "Diverged history handling"
check "setup_ec2.sh"                     'GITHUB_REPO#https://'         "GitHub URL normalization"

echo ""
echo "============================================================"
echo "  GIT STATE"
echo "============================================================"
git log --oneline -10 2>/dev/null
echo ""
echo "Remote:"
git remote get-url origin 2>/dev/null
echo ""
echo "Uncommitted changes:"
git status --short 2>/dev/null
echo ""
echo "============================================================"
echo "  DONE"
echo "============================================================"
