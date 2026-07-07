"""Offline stress test for _theta_bleed v1.4 gates. Patches minutes_since to
control hold time; no network. Verifies the four gates and the corrected
denominator. Run from repo root: python3 stress_theta_bleed.py"""
import sys
import execution.exit_engine as ee
from execution.exit_engine import (
    ExitEngine, THETA_MIN_HOLD_MIN, THETA_MIN_GAIN_PCT,
    MINUTES_PER_CALENDAR_DAY,
)
from config import FVG_TRAIL_ARM_PCT, THETA_LOOKAHEAD_MIN

eng = ExitEngine(paper_trading=True)
fails = []

def rec(entry=2.00, theta=0.20, entry_time="2026-07-07T14:00:00+00:00"):
    return {"trade_id": "t", "entry_premium": entry, "current_theta": theta,
            "entry_time": entry_time}

def call(held_min, entry, current, theta):
    ee.minutes_since = lambda dt, _h=held_min: _h          # patch hold time
    pnl = (current - entry) / entry
    return eng._theta_bleed(rec(entry=entry, theta=theta), current, pnl)

def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond: fails.append(label)

print(f"gates: hold>={THETA_MIN_HOLD_MIN}m  floor={THETA_MIN_GAIN_PCT:.0%}  "
      f"ceiling(trail-arm)={FVG_TRAIL_ARM_PCT:.0%}  lookahead={THETA_LOOKAHEAD_MIN}m/{MINUTES_PER_CALENDAR_DAY}min-day\n")

# (3) MIN-HOLD: fresh trade, healthy 15% gain, huge theta — must NOT fire (blackout)
check("fresh (<20m) never theta-exits, even with big theta",
      call(held_min=5, entry=2.00, current=2.30, theta=40.0) is False)

# (1) GAIN FLOOR: past hold, but only +3% — must NOT fire
check("held 30m but gain 3% (< floor) does not fire",
      call(held_min=30, entry=2.00, current=2.06, theta=40.0) is False)

# (2) TRAIL CEILING: past hold, +25% — trail owns it, theta silent
check("held 30m, gain 25% (>= trail arm) does not fire (trail owns it)",
      call(held_min=30, entry=2.00, current=2.50, theta=40.0) is False)

# (4) WARRANTED: in-window (30m, +12%), decay genuinely dwarfs the gain -> FIRE
#     gain/share=0.24; proj=theta*20/1440; theta=40 -> 0.556 >= 0.24 -> True
check("held 30m, gain 12%, decay > gain -> FIRES (legit theta exit)",
      call(held_min=30, entry=2.00, current=2.24, theta=40.0) is True)

# (4) NOT warranted: same window but realistic small theta -> decay < gain -> no fire
#     theta=0.20 -> proj=0.0028 << 0.24 -> False
check("held 30m, gain 12%, normal theta -> does NOT fire (decay < gain)",
      call(held_min=30, entry=2.00, current=2.24, theta=0.20) is False)

# DENOMINATOR: a case that WOULD have fired under the old /390 but not under /1440.
#   gain/share=0.24; theta=5.0
#   old proj = 5*(20/390)  = 0.256 >= 0.24 -> would have fired (too eager)
#   new proj = 5*(20/1440) = 0.069 <  0.24 -> correctly does NOT fire
old_proj = 5.0 * (THETA_LOOKAHEAD_MIN / 390.0)
new_fires = call(held_min=30, entry=2.00, current=2.24, theta=5.0)
check(f"denominator corrected: old/390 proj={old_proj:.3f}>=0.24 would fire; new/1440 does not",
      old_proj >= 0.24 and new_fires is False)

# entry_time missing -> cannot verify hold -> must NOT cut
ee.minutes_since = lambda dt: 999
check("missing entry_time -> fails safe (no exit)",
      eng._theta_bleed({"trade_id":"t","entry_premium":2.0,"current_theta":40.0,"entry_time":None}, 2.24, 0.12) is False)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURE(S): " + "; ".join(fails)))
sys.exit(0 if not fails else 1)
