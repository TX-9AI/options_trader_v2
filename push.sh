#!/bin/bash
# =============================================================================
# push.sh — Vertigo Capital Git Push Tool
# v1.0 — 2026-06-27 — initial release
# v1.1 — 2026-06-27 — add rebase pull before push, fix success check, exclude WAL files
# v1.2 — 2026-06-30 — auto-detect and repair doubled/malformed remote URLs
#         (e.g. https://github.com/https://github.com/owner/repo.git) before
#         pushing, so a bad GITHUB_REPO value entered at install time can't
#         silently break every future push
# v1.3 — 2026-06-30 — handle diverged/unrelated history cleanly. If a fresh
#         local repo (root-commit) conflicts with existing GitHub history,
#         abort any in-progress rebase and prompt to force-push local as the
#         authoritative version instead of leaving the working tree mid-conflict.
# v1.4 — 2026-07-02 — normalize the executable bit on all tracked .sh files on
#         every push (uploading/SCP strips +x, which flips the repo mode to
#         100644 and makes ./configure.sh "Permission denied" on fresh clones).
#         Also chmods the local scripts, so running push.sh repairs this server.
#
# Pushes local bot changes to GitHub without exposing token in scripts.
# Token read from systemd service environment or prompted interactively.
#
# Usage:
#   bash push.sh                        — push with auto commit message
#   bash push.sh "your commit message"  — push with custom message
# =============================================================================

BOLD='\033[1m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'
YELLOW='\033[1;33m'; RED='\033[0;31m'; RESET='\033[0m'

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

cd "$BOT_DIR"

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

# ── Keep shell scripts executable ─────────────────────────────────────────────
# Uploading/SCP'ing a file drops the +x bit; committing it then flips the repo
# mode to 100644 and a fresh clone lands ./configure.sh non-executable. Repair
# every tracked .sh locally so it's runnable on THIS server and so the +x mode
# gets committed below.
git ls-files '*.sh' | xargs -r chmod +x 2>/dev/null || true

# Determine branch name dynamically (main or master) instead of hardcoding
BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "main")

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
    # Force the executable bit into the index for every tracked .sh, regardless
    # of core.fileMode — this is what keeps the repo at 100755 permanently.
    git ls-files '*.sh' | xargs -r git update-index --chmod=+x 2>/dev/null || true
    git commit -m "$COMMIT_MSG"
else
    echo -e "  ${GREEN}Nothing new to commit — checking if push is still needed.${RESET}"
fi

# ── Push with token ────────────────────────────────────────────────────────────
git remote set-url origin "https://TX-9AI:${TOKEN}@github.com/TX-9AI/${REPO}.git"

# Try a normal rebase-pull first
PULL_OUTPUT=$(git pull --rebase origin "$BRANCH" 2>&1)
PULL_STATUS=$?

if [ $PULL_STATUS -ne 0 ] || [ -d ".git/rebase-merge" ] || [ -d ".git/rebase-apply" ]; then
    # Rebase hit conflicts — almost always unrelated/diverged history
    # (e.g. a fresh root-commit on this server vs. existing GitHub history)
    git rebase --abort 2>/dev/null || true
    echo ""
    echo -e "  ${YELLOW}⚠  Remote history has diverged from this server's local history.${RESET}"
    echo -e "  ${YELLOW}     This usually means GitHub has commits this server never pulled.${RESET}"
    echo ""
    echo "  Options:"
    echo "    1) Force-push THIS SERVER's files as the new GitHub state (overwrites GitHub)"
    echo "    2) Cancel — resolve manually"
    echo ""
    read -rp "  Choice [1/2]: " CHOICE
    if [ "$CHOICE" = "1" ]; then
        if git push origin "$BRANCH" --force; then
            git remote set-url origin "https://github.com/TX-9AI/${REPO}.git"
            echo ""
            echo -e "  ${GREEN}✅ Force-pushed local state to ${REPO} (${BRANCH}).${RESET}"
            echo -e "  ${YELLOW}     Other servers should run: git fetch origin && git reset --hard origin/${BRANCH}${RESET}"
        else
            git remote set-url origin "https://github.com/TX-9AI/${REPO}.git"
            echo ""
            echo -e "  ${RED}⚠  Force push failed — check errors above.${RESET}"
            exit 1
        fi
    else
        git remote set-url origin "https://github.com/TX-9AI/${REPO}.git"
        echo -e "  ${YELLOW}Cancelled. No changes pushed.${RESET}"
        exit 1
    fi
else
    # Normal pull succeeded — proceed with a regular push
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
fi
echo ""
