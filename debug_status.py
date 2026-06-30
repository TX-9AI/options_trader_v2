"""
debug_status.py — Verbose debug for status.py instrument issue
"""
import os, sys, subprocess, re

sys.path.insert(0, os.path.expanduser("~/options-trader"))

SERVICE_NAME = "optionsbot"

print("=== STEP 1: systemd raw output ===")
result = subprocess.run(
    ["sudo", "systemctl", "show", SERVICE_NAME, "--property=Environment"],
    capture_output=True, text=True
)
print(repr(result.stdout[:300]))
print()

print("=== STEP 2: regex match ===")
m = re.search(r'OT_INSTRUMENT=([^ ]+)', result.stdout)
print("Match:", m.group(1) if m else "NO MATCH")
print()

print("=== STEP 3: get_runtime_env function ===")
def get_runtime_env(key, default=""):
    try:
        r = subprocess.run(
            ["sudo", "systemctl", "show", SERVICE_NAME, "--property=Environment"],
            capture_output=True, text=True
        )
        match = re.search(rf'(?:^| ){re.escape(key)}=([^ ]+)', r.stdout)
        if match:
            return match.group(1)
    except Exception as e:
        print(f"Exception: {e}")
    return os.environ.get(key, default)

val = get_runtime_env("OT_INSTRUMENT", "QQQ")
print("get_runtime_env result:", val)
print()

print("=== STEP 4: config import ===")
try:
    from config import DB_PATH, SESSION_LOSS_LIMIT, BOT_NAME
    print("DB_PATH:", DB_PATH)
    print("BOT_NAME:", BOT_NAME)
except Exception as e:
    print("Config import error:", e)
print()

print("=== STEP 5: INSTRUMENT after config import ===")
INSTRUMENT = get_runtime_env("OT_INSTRUMENT", "QQQ")
print("INSTRUMENT:", INSTRUMENT)
