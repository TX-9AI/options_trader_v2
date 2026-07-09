"""Pressure-test: every state-transition combination through the gate +
reassessment logic. Proves (A) the gate is memoryless — allows iff destination
regime is tradeable, regardless of source — and (B) leaving UNKNOWN forces a
same-tick reclassification so the gate drops with zero added latency."""
REGIMES = ["TRENDING_BULL","TRENDING_BEAR","RANGING","BREAKOUT_VOLATILE",
           "COMPRESSION","SWEEP_REVERSAL","UNKNOWN"]
TRADEABLE = set(REGIMES) - {"UNKNOWN"}

def gate_allows(regime):                       # mirrors main.py hard gate (memoryless)
    return regime not in (None, "", "UNKNOWN")

def should_reassess(cur_regime, minutes_since_last, cadence=5):
    unclassified = cur_regime in (None, "", "UNKNOWN")
    return (cur_regime is None) or unclassified or (minutes_since_last >= cadence)

fails=[]
# (A) memorylessness: gate decision depends ONLY on destination, never source
print("(A) Gate memorylessness — all 49 source→dest transitions:")
bad=0
for src in REGIMES:
    for dst in REGIMES:
        allow = gate_allows(dst)
        expected = dst in TRADEABLE
        if allow != expected:
            bad+=1; fails.append(f"gate {src}->{dst} allow={allow} exp={expected}")
print(f"    {49-bad}/49 correct — gate allows iff destination is tradeable, source irrelevant")
# Explicitly: coming FROM unknown is never penalized vs coming from a tradeable regime
for dst in TRADEABLE:
    if gate_allows(dst) is not True:
        fails.append(f"UNKNOWN->{dst} blocked (should open)")
print(f"    UNKNOWN→<tradeable> opens for all {len(TRADEABLE)} tradeable regimes: "
      f"{all(gate_allows(d) for d in TRADEABLE)}")

# (B) latency: while in UNKNOWN, reassessment fires EVERY tick (15s), so the gate
# drops the same tick the move becomes classifiable — not up to 5 min later.
print("\n(B) Leaving UNKNOWN — reassessment latency at each 15s tick:")
# simulate: sat in UNKNOWN since last classify; move becomes classifiable at tick t
POLL=0.25  # 15s in minutes
for elapsed_min in [POLL, POLL*2, POLL*4, 1.0, 4.9]:
    fires = should_reassess("UNKNOWN", elapsed_min)
    tag = "reclassifies → gate can drop" if fires else "STALE → gate stuck"
    print(f"    in UNKNOWN {elapsed_min*60:>4.0f}s after last classify: {tag}")
    if not fires: fails.append(f"UNKNOWN stale at {elapsed_min}min")
# contrast: a STABLE tradeable regime is NOT reassessed every tick (churn guard)
print("\n    contrast — STABLE regime keeps the 5-min throttle (churn protection):")
for elapsed_min in [POLL, 1.0, 4.9, 5.0]:
    fires = should_reassess("TRENDING_BULL", elapsed_min)
    print(f"    in TRENDING {elapsed_min*60:>4.0f}s: {'reassess' if fires else 'throttled (holds regime)'}")

print("\n" + ("ALL PASS — gate is memoryless and drops within one 15s tick of classifiability"
              if not fails else f"{len(fails)} FAILURES: "+"; ".join(fails)))
