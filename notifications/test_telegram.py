"""
test_telegram.py — Telegram connectivity test for options_trader.
v1.0 — 2026-06-27 — initial release

Reads TELEGRAM_TOKEN and TELEGRAM_CHAT_ID from:
  1. Systemd service environment (auto-detected)
  2. Shell environment (fallback)

Usage:
  python test_telegram.py
"""

import os
import sys
import subprocess
import requests


def load_systemd_env(service: str = "optionsbot") -> dict:
    """Read environment variables from the systemd service unit."""
    env = {}
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "show", service, "--property=Environment"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return env
        line = result.stdout.strip()
        # Format: Environment=VAR1=val1 VAR2=val2 ...
        if line.startswith("Environment="):
            line = line[len("Environment="):]
        for part in line.split():
            if "=" in part:
                key, _, val = part.partition("=")
                env[key] = val
    except Exception as e:
        print(f"  Could not read systemd env: {e}")
    return env


def main():
    print("")
    print("=" * 50)
    print("  options_trader — Telegram Test")
    print("=" * 50)
    print("")

    # Try systemd first, fall back to shell env
    svc_env = load_systemd_env("optionsbot")

    token   = svc_env.get("TELEGRAM_TOKEN")   or os.environ.get("TELEGRAM_TOKEN",   "")
    chat_id = svc_env.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")

    if svc_env:
        print("  ✓  Loaded credentials from systemd service environment")
    else:
        print("  →  Using shell environment variables")

    if not token:
        print("  ❌  TELEGRAM_TOKEN not found")
        print("      Export it or ensure optionsbot service is configured")
        sys.exit(1)

    if not chat_id:
        print("  ❌  TELEGRAM_CHAT_ID not found")
        sys.exit(1)

    print(f"  Token:   {token[:20]}...")
    print(f"  Chat ID: {chat_id}")
    print("")
    print("  Sending test message...")

    try:
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text":    "✅ options_trader Telegram test — connection confirmed",
        }, timeout=10)

        if resp.status_code == 200:
            print("  ✅  Message sent successfully!")
        else:
            print(f"  ❌  Failed: {resp.status_code} — {resp.text}")
            sys.exit(1)

    except Exception as e:
        print(f"  ❌  Error: {e}")
        sys.exit(1)

    print("")


if __name__ == "__main__":
    main()
