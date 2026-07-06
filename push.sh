#!/bin/bash
# =============================================================================
# push.sh — Vertigo Capital Git Push / Deploy Tool
# v1.0 — 2026-06-27 — initial release
# v1.1 — 2026-06-27 — add rebase pull before push, fix success check, exclude WAL files
# v1.2 — 2026-06-30 — auto-detect and repair doubled/malformed remote URLs
# v1.3 — 2026-06-30 — handle diverged/unrelated history cleanly (force-push prompt)
# v1.4 — 2026-07-02 — normalize the executable bit on all tracked .sh files
# v1.5 — 2026-07-02 — set git author to the repo owner (TX-9AI)
# v1.6 — 2026-07-06 — add DOWNLOAD direction so fleet can wake a box and deploy:
#         `push.sh --deploy` (alias --pull) fetches origin and hard-resets THIS
#         bot to the remote branch, repairs .sh +x, restarts the service, and
#         verifies it. `--no-restart` skips the restart. Default (no flag) is the
#         original upload: commit local changes and push to GitHub.
#         Unattended-safe: per-bot config is in the systemd env; runtime state
#         (trades.db, bot.log, orb_state.json, orb_range.json) is untracked and
#         is NOT touched by `git reset --hard`.
#
# Usage:
#   bash push.sh                        — commit local changes & push to GitHub
#   bash push.sh "your commit message"  — push with a custom message
#   bash push.sh --deploy               — fetch + reset --hard + restart (pull side)
#   bash push.sh --deploy --no-restart  — deploy without restarting the service
# =============================================================================

BOLD='\033[1m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'
YELLOW='\033[1;33m'; RED='\033[0;31m'; RESET='\033[0m'

# ── Parse flags (leave any commit message as the remaining positional arg) ────
MODE="push"; RESTART=true; ARGS=()
for a in "$@"; do
    case "$a" in
        --deploy|--pull) MODE="deploy" ;;
        --no-restart)    RESTART=false ;;
        *)               ARGS+=("$a") ;;
    esac
done
set -- "${ARGS[@]}"

echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════╗${RESET}"
if [ "$MODE" = "deploy" ]; then
    echo -e "${BOLD}${CYAN}║     Vertigo Capital — Git Deploy (pull)             ║${RESET}"
else
    echo -e "${BOLD}${CYAN}║     Vertigo Capital — Git Push                      ║${RESET}"
fi
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""

# ── Detect which bot and repo ─────────────────────────────────────────────────
BOT_DIR=""
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
cd "$BOT_DIR" || exit 1

# ── If a previous run left a rebase in progress, clear it before continuing ──
if [ -d ".git/rebase-merge" ] || [ -d ".git/rebase-apply" ]; then
    echo -e "  ${YELLOW}⚠  Found an in-progress rebase from a previous run — aborting it.${RESET}"
    git rebase --abort 2>/dev/null || true
    echo ""
fi

# ── Repair a malformed remote URL if present ──────────────────────────────────
CURRENT_REMOTE_RAW=$(git remote get-url origin 2>/dev/null || echo "")
if echo "$CURRENT_REMOTE_RAW" | grep -qE 'github\.com/.*github\.com/'; then
    echo -e "  ${YELLOW}⚠  Detected malformed remote URL — repairing...${RESET}"
    FIXED_PATH=$(echo "$CURRENT_REMOTE_RAW" | sed -E 's#.*github\.com/##')
    FIXED_PATH="${FIXED_PATH%.git}"
    FIXED_PATH="${FIXED_PATH%/}"
    git remote set-url origin "https://github.com/${FIXED_PATH}.git"
    echo -e "  ${GREEN}✓  Remote repaired: https://github.com/${FIXED_PATH}.git${RESET}"
    echo ""
fi

# Read current remote URL to determine repo (after any repair above)
CURRENT_REMOTE=$(git remote get-url origin 2>/dev/null || echo "")
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

BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "main")
CLEAN_URL="https://github.com/TX-9AI/${REPO}.git"

echo -e "  Bot dir: ${BOLD}${BOT_DIR}${RESET}"
echo -e "  Repo:    ${BOLD}https://github.com/TX-9AI/${REPO}${RESET} (${BRANCH})"
echo -e "  Service: ${BOLD}${SERVICE}${RESET}"
echo ""

# ══════════════════════════════════════════════════════════════════════════════
# DEPLOY (pull) — fetch + hard-reset this bot to origin, repair perms, restart
# ══════════════════════════════════════════════════════════════════════════════
if [ "$MODE" = "deploy" ]; then
    # Token only needed for a private repo — inject for the fetch, then restore.
    TOKEN=$(sudo systemctl show "$SERVICE" --property=Environment 2>/dev/null \
        | grep -o 'GITHUB_TOKEN=[^ ]*' | cut -d= -f2)
    [ -n "$TOKEN" ] && git remote set-url origin \
        "https://TX-9AI:${TOKEN}@github.com/TX-9AI/${REPO}.git"

    echo "  Fetching origin/${BRANCH}…"
    if ! git fetch origin "$BRANCH" --quiet; then
        git remote set-url origin "$CLEAN_URL"
        echo -e "  ${RED}⚠  Fetch failed — check network/token.${RESET}"
        exit 1
    fi

    OLD_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "?")
    git reset --hard "origin/${BRANCH}" >/dev/null
    git remote set-url origin "$CLEAN_URL"          # always restore token-free URL
    git ls-files '*.sh' | xargs -r chmod +x 2>/dev/null || true
    NEW_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "?")

    if [ "$OLD_SHA" = "$NEW_SHA" ]; then
        echo -e "  ${GREEN}Already up to date${RESET} @ ${NEW_SHA}"
    else
        echo -e "  ${GREEN}Updated${RESET} ${OLD_SHA} → ${BOLD}${NEW_SHA}${RESET}"
    fi

    if [ "$RESTART" = true ]; then
        echo "  Restarting ${SERVICE}…"
        sudo systemctl restart "$SERVICE"
        sleep 3
        STATE=$(systemctl is-active "$SERVICE" 2>/dev/null)
        if [ "$STATE" = "active" ]; then
            echo -e "  ${GREEN}✅ ${SERVICE} active${RESET} @ ${NEW_SHA}"
        else
            echo -e "  ${RED}🚨 ${SERVICE} ${STATE}${RESET} — journalctl -u ${SERVICE} -n 20"
            exit 1
        fi
    else
        echo -e "  ${YELLOW}(service not restarted — --no-restart)${RESET}"
    fi
    echo ""
    exit 0
fi

# ══════════════════════════════════════════════════════════════════════════════
# PUSH (upload) — original behavior: commit local changes and push to GitHub
# ══════════════════════════════════════════════════════════════════════════════

# ── Author commits as the repo owner, not the ubuntu system user ──────────────
GH_OWNER=$(echo "$CURRENT_REMOTE" | sed -E 's#.*github\.com[:/]+([^/]+)/.*#\1#')
if [ -n "$GH_OWNER" ] && [ "$GH_OWNER" != "$CURRENT_REMOTE" ]; then
    git config user.name  "$GH_OWNER"
    git config user.email "${GH_OWNER}@users.noreply.github.com"
fi

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

# ── Keep shell scripts executable ─────────────────────────────────────────────
git ls-files '*.sh' | xargs -r chmod +x 2>/dev/null || true

# Check for changes
HAS_CHANGES=true
if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
    HAS_CHANGES=false
fi

if [ "$HAS_CHANGES" = true ]; then
    echo "  Staged changes:"
    git status --short
    echo ""

    COMMIT_MSG="${1:-$(date '+%Y-%m-%d') — patch update}"
    git add .
    git ls-files '*.sh' | xargs -r git update-index --chmod=+x 2>/dev/null || true
    git commit -m "$COMMIT_MSG"
else
    echo -e "  ${GREEN}Nothing new to commit — checking if push is still needed.${RESET}"
fi

# ── Push with token ────────────────────────────────────────────────────────────
git remote set-url origin "https://TX-9AI:${TOKEN}@github.com/TX-9AI/${REPO}.git"

PULL_OUTPUT=$(git pull --rebase origin "$BRANCH" 2>&1)
PULL_STATUS=$?

if [ $PULL_STATUS -ne 0 ] || [ -d ".git/rebase-merge" ] || [ -d ".git/rebase-apply" ]; then
    git rebase --abort 2>/dev/null || true
    echo ""
    echo -e "  ${YELLOW}⚠  Remote history has diverged from this server's local history.${RESET}"
    echo ""
    echo "  Options:"
    echo "    1) Force-push THIS SERVER's files as the new GitHub state (overwrites GitHub)"
    echo "    2) Cancel — resolve manually"
    echo ""
    read -rp "  Choice [1/2]: " CHOICE
    if [ "$CHOICE" = "1" ]; then
        if git push origin "$BRANCH" --force; then
            git remote set-url origin "$CLEAN_URL"
            echo ""
            echo -e "  ${GREEN}✅ Force-pushed local state to ${REPO} (${BRANCH}).${RESET}"
            echo -e "  ${YELLOW}     Other servers: fleet.py update  (runs push.sh --deploy)${RESET}"
        else
            git remote set-url origin "$CLEAN_URL"
            echo -e "  ${RED}⚠  Force push failed — check errors above.${RESET}"
            exit 1
        fi
    else
        git remote set-url origin "$CLEAN_URL"
        echo -e "  ${YELLOW}Cancelled. No changes pushed.${RESET}"
        exit 1
    fi
else
    if git push origin "$BRANCH"; then
        git remote set-url origin "$CLEAN_URL"
        echo ""
        echo -e "  ${GREEN}✅ Pushed to ${REPO} (${BRANCH}) successfully.${RESET}"
    else
        git remote set-url origin "$CLEAN_URL"
        echo -e "  ${YELLOW}⚠  Push failed — check errors above.${RESET}"
        exit 1
    fi
fi
echo ""
