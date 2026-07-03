#!/bin/bash
# =============================================================================
# bootstrap.sh — one-shot unattended deploy for a fresh EC2 instance.
#
# This is a TEMPLATE (placeholders only) — safe to commit. Do NOT put real
# secrets in this file. Put them only in a copy named bootstrap.sh, which is
# gitignored (the .gitignore ignores every bootstrap*.sh except this .example).
#
# HOW TO USE:
#   1. cp bootstrap.example.sh bootstrap.sh   # your copy — gitignored
#   2. Fill in the REPLACE_ME values in bootstrap.sh.
#   3. scp bootstrap.sh ubuntu@IP:~
#   4. On the instance:  bash bootstrap.sh
#      (no chmod needed — `bash <file>` ignores the execute bit that SCP strips.
#       It re-launches itself in tmux; if SSH drops, reconnect and run
#       `tmux attach -t deploy` to watch it finish.)
#
#   Forked this repo? Point GITHUB_REPO and the install.sh URL below at YOUR fork.
#
# It exports every value setup_ec2.sh would otherwise prompt for, then runs the
# standard web installer hands-free. setup_ec2.sh securely SHREDS your
# bootstrap.sh during cleanup, once the credentials are in the systemd unit.
# On a failed install it remains so you can re-run — delete it by hand if you
# abandon the deploy.
# =============================================================================

# ── Run inside tmux ───────────────────────────────────────────────────────────
# Re-launch this script inside a tmux session so a dropped SSH connection can't
# kill a multi-minute install (and can't leave secrets un-shredded). If you get
# disconnected, reconnect and run:  tmux attach -t deploy
if [ -z "$TMUX" ]; then
    command -v tmux >/dev/null 2>&1 || { sudo apt-get update -qq; sudo apt-get install -y -qq tmux; }
    if command -v tmux >/dev/null 2>&1; then
        exec tmux new-session -A -s deploy "bash '$(readlink -f "$0")'"
    else
        echo "  (tmux unavailable — running directly; keep this session connected)"
    fi
fi

# ── Instrument (optional; defaults to QQQ if omitted) ─────────────────────────
export OT_INSTRUMENT="QQQ"        # QQQ | SPY | SPX | any supported single name
# NOTE: installs are ALWAYS paper at $200/trade. There is deliberately no paper/
# live or risk knob here — set risk and switch to live later via configure.sh.

# ── TastyTrade OAuth ──────────────────────────────────────────────────────────
export TT_CLIENT_SECRET="REPLACE_ME"
export TT_REFRESH_TOKEN="REPLACE_ME"
export TT_ACCOUNT_NUMBER="REPLACE_ME"   # e.g. 5WT12345

# ── Telegram alerts ───────────────────────────────────────────────────────────
export TELEGRAM_TOKEN="REPLACE_ME"
export TELEGRAM_CHAT_ID="REPLACE_ME"

# ── GitHub (for push.sh; also sets commit author to the repo owner) ───────────
export GITHUB_REPO="TX-9AI/options_trader_v2"   # ENTER-to-skip equivalent: leave as ""
export GITHUB_TOKEN="REPLACE_ME"

# ── Run the standard installer (inherits every export above) ──────────────────
curl -fsSL https://raw.githubusercontent.com/TX-9AI/options_trader_v2/main/install.sh -o install.sh \
    && bash install.sh
