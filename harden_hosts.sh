#!/usr/bin/env bash
# options_trader_v2/harden_hosts.sh — v1.0
# =============================================================================
# Host hardening for a trading box. Two independent guards against the
# mid-session restarts we traced to unattended-upgrades -> needrestart on
# 2026-07-07 (optionsbot was auto-restarted 09:36–10:06 ET when a package
# upgrade ran during RTH):
#
#   1) needrestart drop-in: never auto-restart optionsbot after a package
#      upgrade. needrestart may LIST it, but it will not act on it. The daily
#      EC2 stop/start already cold-boots every box, so upgraded libraries are
#      picked up next morning without an in-session restart.
#
#   2) apt timer overrides: move apt-daily / apt-daily-upgrade to run overnight
#      ET (well outside 09:30–16:00) with Persistent=false, so a run missed
#      while the box was stopped overnight does NOT fire on the ~09:17 ET
#      morning boot (which would land right at the open).
#
# Idempotent: safe to run repeatedly. Requires passwordless sudo (present on
# the fleet). Deploy to the live fleet from the control server WITHOUT a bot
# restart:
#
#   fleet.py run "cd ~/options-trader && git fetch origin && \
#       git reset --hard origin/main && bash harden_hosts.sh"
#
# (Do NOT use push.sh --deploy / fleet.py update to apply this mid-session —
#  that restarts optionsbot. This script never touches the bot service.)
# =============================================================================
set -euo pipefail

SERVICE_NAME="optionsbot"

echo "== [1/3] needrestart: shield ${SERVICE_NAME} from auto-restart =="
sudo install -d -m 0755 /etc/needrestart/conf.d
sudo tee /etc/needrestart/conf.d/90-optionsbot.conf >/dev/null <<'EOF'
# Vertigo Capital — do NOT let package upgrades (unattended-upgrades) auto-
# restart the trading bot mid-session. needrestart may list optionsbot but
# must never act on it. The operator restarts on their own schedule; the daily
# EC2 stop/start gives every box a fresh boot that picks up upgraded libraries.
$nrconf{override_rc} = {
    qr(^optionsbot\.service$) => 0,
};
EOF

echo "== [2/3] apt timers: overnight ET, never at boot =="
sudo install -d -m 0755 /etc/systemd/system/apt-daily.timer.d
sudo tee /etc/systemd/system/apt-daily.timer.d/override.conf >/dev/null <<'EOF'
# Move the apt metadata download out of RTH. Empty OnCalendar= first CLEARS the
# vendor default (6,18:00 with a 12h random spread) before setting ours.
[Timer]
OnCalendar=
OnCalendar=*-*-* 02:30:00 America/New_York
RandomizedDelaySec=1800
Persistent=false
EOF

sudo install -d -m 0755 /etc/systemd/system/apt-daily-upgrade.timer.d
sudo tee /etc/systemd/system/apt-daily-upgrade.timer.d/override.conf >/dev/null <<'EOF'
# Move the actual upgrade (the step that invoked needrestart) fully out of RTH.
# Persistent=false: a run missed while the box was stopped overnight will NOT
# fire on the ~09:17 ET cold boot — it simply waits for the next 03:00 ET slot.
[Timer]
OnCalendar=
OnCalendar=*-*-* 03:00:00 America/New_York
RandomizedDelaySec=1800
Persistent=false
EOF

# Apply. daemon-reload picks up the drop-ins; restarting the .timer units
# re-arms them with the new next-elapse. Restarting a .timer does NOT run its
# .service, so this triggers no upgrade now.
sudo systemctl daemon-reload
sudo systemctl restart apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true

echo "== [3/3] verify =="
echo "-- needrestart override --"
sudo grep -H 'optionsbot' /etc/needrestart/conf.d/90-optionsbot.conf || true
echo "-- apt timers (next elapse should be overnight ET, not today RTH) --"
systemctl list-timers 'apt-daily*' --all --no-pager | head -4 || true
echo "harden_hosts.sh: done on $(hostname)"
