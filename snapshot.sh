#!/bin/bash
# ============================================================
#  snapshot.sh — Vertigo Capital Bot Snapshot Tool
#  v1.2 — current
#  v1.0 — original (hardcoded bot names, replaced)
#  v1.1 — 2026-06-27 — fully dynamic, no hardcoded bot names
#  v1.1 — 2026-06-27 — fully dynamic, no hardcoded bot names
#  v1.2 — 2026-06-27 — skip deploy dirs, fix IP fetch method
#
#  Usage:
#    bash snapshot.sh              — auto-detect and snapshot
#    bash snapshot.sh --no-status  — skip status.py output
#
#  Output: ~/snapshots/INSTRUMENT_YYYY-MM-DD_vX.X_HHMM.tar.gz
# ============================================================

set -e

BOLD='\033[1m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'
YELLOW='\033[1;33m'; RESET='\033[0m'

SNAPSHOT_DIR="$HOME/snapshots"
mkdir -p "$SNAPSHOT_DIR"

# ── Discover bot directory (skip deploy dirs) ────────────────
BOT_DIR=""
for dir in "$HOME"/*/; do
    # Skip deploy staging directories
    [[ "$dir" == *"-deploy"* ]] && continue
    if [ -f "${dir}main.py" ] && [ -f "${dir}config.py" ]; then
        BOT_DIR="${dir%/}"
        break
    fi
done

if [ -z "$BOT_DIR" ]; then
    echo -e "${YELLOW}⚠  No bot directory found. Aborting.${RESET}"
    exit 1
fi

# ── Discover service name ─────────────────────────────────────
SERVICE=""
for svc in $(systemctl list-units --type=service --state=active --no-legend | awk '{print $1}'); do
    unit_dir=$(systemctl show "$svc" --property=WorkingDirectory 2>/dev/null | cut -d= -f2)
    if [ "$unit_dir" = "$BOT_DIR" ]; then
        SERVICE="$svc"
        break
    fi
done

# Fallback — check stopped services too
if [ -z "$SERVICE" ]; then
    for svc in $(systemctl list-units --type=service --all --no-legend | awk '{print $1}'); do
        unit_dir=$(systemctl show "$svc" --property=WorkingDirectory 2>/dev/null | cut -d= -f2)
        if [ "$unit_dir" = "$BOT_DIR" ]; then
            SERVICE="$svc"
            break
        fi
    done
fi

# ── Discover instrument/name from service env or config ───────
get_instrument() {
    local name
    # Try service environment first
    name=$(sudo systemctl show "$SERVICE" --property=Environment 2>/dev/null \
        | grep -oP '(?<=OT_INSTRUMENT=)[^ ]+' | head -1)
    [ -n "$name" ] && { echo "$name"; return; }

    # Try INSTRUMENT in config.py
    name=$(grep -oP "(?<=INSTRUMENT\s=\s['\"])[^'\"]*" "$BOT_DIR/config.py" 2>/dev/null | head -1)
    [ -n "$name" ] && { echo "$name"; return; }

    # Fall back to directory name
    basename "$BOT_DIR"
}

# ── Discover version from main.py header ─────────────────────
get_version() {
    grep -oP '(?<=v)\d+\.\d+' "$BOT_DIR/main.py" 2>/dev/null | head -1 \
        || echo "1.0"
}

INSTRUMENT=$(get_instrument)
VERSION="v$(get_version)"
DATE=$(date +%Y-%m-%d)
TIME=$(date +%H%M)
FILENAME="${INSTRUMENT}_${DATE}_${VERSION}_${TIME}.tar.gz"
TARBALL="$SNAPSHOT_DIR/$FILENAME"

echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║     Vertigo Capital — Bot Snapshot                  ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  Instrument: ${BOLD}${INSTRUMENT}${RESET}"
echo -e "  Version:    ${BOLD}${VERSION}${RESET}"
echo -e "  Service:    ${BOLD}${SERVICE:-unknown}${RESET}"
echo -e "  Source:     ${BOT_DIR}"
echo -e "  Output:     ${TARBALL}"
echo ""

# ── Capture status.py output into snapshot ───────────────────
STATUS_FILE="/tmp/snapshot_status_$$.txt"
if [[ "${1:-}" != "--no-status" ]]; then
    echo "  Capturing bot status..."
    {
        echo "=========================================="
        echo "  Vertigo Capital — Snapshot"
        echo "  Instrument: $INSTRUMENT | $VERSION"
        echo "  Date: $(date)"
        echo "  Service: ${SERVICE:-unknown}"
        echo "=========================================="
        echo ""
        cd "$BOT_DIR"
        source "$BOT_DIR/venv/bin/activate" 2>/dev/null || true
        python status.py 2>/dev/null || echo "  (status.py unavailable)"
        echo ""
        echo "=========================================="
        echo "  Service Environment (secrets redacted)"
        echo "=========================================="
        sudo systemctl show "$SERVICE" --property=Environment 2>/dev/null \
            | tr ' ' '\n' \
            | grep -v "TOKEN\|SECRET\|PASSWORD\|KEY\|REFRESH" \
            | sort
    } > "$STATUS_FILE" 2>/dev/null
    echo "  Status captured."
fi

# ── Build tarball ─────────────────────────────────────────────
echo "  Building tarball..."

tar czf "$TARBALL" \
    --exclude="*/venv/*" \
    --exclude="*/__pycache__/*" \
    --exclude="*.pyc" \
    --exclude="*/#*" \
    --exclude="*/trades.db" \
    --exclude="*/bot.log" \
    --exclude="*/credentials.py" \
    --exclude="*.pem" \
    --exclude="*-deploy/*" \
    -C "$(dirname "$BOT_DIR")" \
    "$(basename "$BOT_DIR")" \
    2>/dev/null || true

# Append status file if captured
if [ -f "$STATUS_FILE" ]; then
    tar rzf "$TARBALL" -C /tmp "snapshot_status_$$.txt" 2>/dev/null || true
    rm -f "$STATUS_FILE"
fi

# ── Report ────────────────────────────────────────────────────
if [ -f "$TARBALL" ]; then
    SIZE=$(du -sh "$TARBALL" | cut -f1)
    FILES=$(tar tzf "$TARBALL" | wc -l)
    IP=$(curl -s --max-time 3 https://checkip.amazonaws.com 2>/dev/null | tr -d "\n" || hostname -I | awk "{print \$1}" || echo "<server-ip>")
    echo ""
    echo -e "${GREEN}  ✅ Snapshot complete!${RESET}"
    echo ""
    echo -e "  File:   ${BOLD}$FILENAME${RESET}"
    echo -e "  Size:   ${BOLD}$SIZE${RESET}"
    echo -e "  Files:  ${BOLD}$FILES${RESET}"
    echo ""
    echo "  To download:"
    echo "    scp -i tx-9.pem ubuntu@${IP}:~/snapshots/$FILENAME \"C:\\snapshots\\$FILENAME\""
    echo ""
    echo "  All snapshots:"
    ls -lh "$SNAPSHOT_DIR/" 2>/dev/null | grep -v "^total" | awk '{print "    " $NF " (" $5 ")"}'
    echo ""
else
    echo -e "${YELLOW}  ⚠  Tarball not created.${RESET}"
    exit 1
fi
