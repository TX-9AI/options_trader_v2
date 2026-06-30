#!/bin/bash
# =============================================================================
# push.sh — Vertigo Capital Git Push Tool
# v1.0 — 2026-06-27 — initial release
# v1.1 — 2026-06-27 — add rebase pull before push, fix success check, exclude WAL files
# v1.2 — 2026-06-30 — auto-detect and repair doubled/malformed remote URLs
#         (e.g. https://github.com/https://github.com/owner/repo.git) before
#         pushing, so a bad GITHUB_REPO value entered at install time can't
#         silently break every future push
#
# Pushes local bot changes to GitHub without exposing token in scripts.
# Token read from systemd service environment or prompted interactively.
#
# Usage:
#   bash push.sh                        — push with auto commit message
#   bash push.sh "your commit message"  — push with custom message
# =============================================================================

BOLD='\033[1m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'
YELLOW='\033[1;33m'; RESET='\033[0m'

echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║     Vertigo Capital — Git Push                      ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""

# ── Detect which bot and repo ─────────────────────────────────────────────────
BOT_DIR=""
REPO_URL=""
SERVICE=""

for dir in "$HOME"/*/; do
    [[ "$dir" == *"-deploy"* ]] && continue
    if [ -f "${dir}main.py" ] && [ -f "${dir}config.py" ]; then
        BOT_DIR="${dir%/}"
        break
    fi
done

if [ -z "$BOT_DIR" ]; then
    echo -e "${YELLOW}  ⚠  Could not detect bot directory. Run from bot home.${RESET}"
    exit 1
fi

# ── Repair a malformed remote URL if present ──────────────────────────────────
# Catches doubled URLs like https://github.com/https://github.com/owner/repo.git
# that can result from pasting a full URL into a "owner/repo" prompt.
CURRENT_REMOTE_RAW=$(cd "$BOT_DIR" && git remote get-url origin 2>/dev/null || echo "")
if echo "$CURRENT_REMOTE_RAW" | grep -qE 'github\.com/.*github\.com/'; then
    echo -e "  ${YELLOW}⚠  Detected malformed remote URL — repairing...${RESET}"
    # Extract owner/repo from the LAST github.com/ occurrence in the string
    FIXED_PATH=$(echo "$CURRENT_REMOTE_RAW" | sed -E 's#.*github\.com/##')
    FIXED_PATH="${FIXED_PATH%.git}"
    FIXED_PATH="${FIXED_PATH%/}"
    cd "$BOT_DIR" && git remote set-url origin "https://github.com/${FIXED_PATH}.git"
    echo -e "  ${GREEN}✓  Remote repaired: https://github.com/${FIXED_PATH}.git${RESET}"
    echo ""
fi

# Read current remote URL to determine repo (after any repair above)
CURRENT_REMOTE=$(cd "$BOT_DIR" && git remote get-url origin 2>/dev/null || echo "")
if echo "$CURRENT_REMOTE" | grep -q "crypto_trader"; then
    SERVICE="cryptobot"
    REPO="crypto_trader_v6"
elif echo "$CURRENT_REMOTE" | grep -q "options_trader"; then
    SERVICE="optionsbot"
    REPO="options_trader_v2"
else
    echo -e "${YELLOW}  ⚠  Could not detect repo from git remote. Is git initialized?${RESET}"
    echo "  Current remote: $CURRENT_REMOTE"
    exit 1
fi

echo -e "  Bot dir: ${BOLD}${BOT_DIR}${RESET}"
echo -e "  Repo:    ${BOLD}https://github.com/TX-9AI/${REPO}${RESET}"
echo -e "  Service: ${BOLD}${SERVICE}${RESET}"
echo ""

# ── Get GitHub token ──────────────────────────────────────────────────────────
TOKEN=$(sudo systemctl show "$SERVICE" --property=Environment 2>/dev/null \
    | grep -o 'GITHUB_TOKEN=[^ ]*' | cut -d= -f2)

if [ -z "$TOKEN" ]; then
    echo -e "  ${YELLOW}GITHUB_TOKEN not in systemd environment.${RESET}"
    read -rsp "  GitHub personal access token: " TOKEN
    echo ""
fi

if [ -z "$TOKEN" ]; then
    echo -e "  ${YELLOW}⚠  No token provided. Aborting.${RESET}"
    exit 1
fi

# ── Ensure WAL files are ignored ─────────────────────────────────────────────
GITIGNORE="$BOT_DIR/.gitignore"
for pattern in "trades.db-shm" "trades.db-wal" "*.db-shm" "*.db-wal"; do
    grep -qF "$pattern" "$GITIGNORE" 2>/dev/null || echo "$pattern" >> "$GITIGNORE"
done

# ── Stage and commit ──────────────────────────────────────────────────────────
cd "$BOT_DIR"

# Determine branch name dynamically (main or master) instead of hardcoding
BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "main")

# Check for changes
if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
    echo -e "  ${GREEN}Nothing to commit — working tree clean.${RESET}"
    exit 0
fi

echo "  Staged changes:"
git status --short
echo ""

COMMIT_MSG="${1:-$(date '+%Y-%m-%d') — patch update}"
git add .
git commit -m "$COMMIT_MSG"

# ── Push with token, then reset URL ──────────────────────────────────────────
git remote set-url origin "https://TX-9AI:${TOKEN}@github.com/TX-9AI/${REPO}.git"

# Pull remote changes first to avoid rejection
git pull --rebase origin "$BRANCH" 2>/dev/null || true

if git push origin "$BRANCH"; then
    git remote set-url origin "https://github.com/TX-9AI/${REPO}.git"
    echo ""
    echo -e "  ${GREEN}✅ Pushed to ${REPO} (${BRANCH}) successfully.${RESET}"
else
    git remote set-url origin "https://github.com/TX-9AI/${REPO}.git"
    echo ""
    echo -e "  ${YELLOW}⚠  Push failed — check errors above.${RESET}"
    exit 1
fi
echo ""
