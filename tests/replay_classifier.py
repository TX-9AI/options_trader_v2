"""Replay the CORRECTED sweep logic over tonight's 6 candle files.
Confirms: acceptance-through-a-level (AVGO 380+) is no longer a sweep, and
interior pokes with no named level (QQQ) are no longer sweeps. Uses the same
reclaim/closes_beyond rule now in liquidity_mapper v1.3."""
import pandas as pd, numpy as np
from datetime import datetime
U="/mnt/user-data/uploads/"
F={"AVGO":U+"AVGO_2026-07-08.csv","PLTR":U+"PLTR.csv","GLD":U+"GLD.csv","TSLA":U+"TSLA.csv","QQQ":U+"QQQ.csv","AMZN":U+"AMZN.csv"}
REJ=3  # SWEEP_REJECTION_CANDLES window
ACCEPT=2
def classify_pokes(c, named_levels):
    """For every poke of a rolling level, apply OLD vs NEW sweep test."""
    highs,lows,closes=c.high.tolist(),c.low.tolist(),c.close.tolist(); n=len(c); LB=15
    old_sweeps=new_sweeps=breakouts=interior=0
    for i in range(LB,n-1):
        ph=max(highs[i-LB:i]); pl=min(lows[i-LB:i])
        for kind,lvl,pen in [("high",ph,highs[i]>ph),("low",pl,lows[i]<pl)]:
            if not pen: continue
            win=range(i,min(i+REJ+1,n))
            # OLD test: distance from wick to LAST close in window (the bug)
            rc_old=closes[i]
            for k in range(1,min(REJ+1,n-i)): rc_old=closes[i+k]
            old_pct=(highs[i]-rc_old)/highs[i] if kind=="high" else (rc_old-lows[i])/lows[i]
            old_fire = old_pct>=0.002
            # NEW test: real reclaim + not accepted-through
            if kind=="high":
                closes_beyond=sum(1 for k in win if closes[k]>lvl)
                reclaimed = any(closes[k]<=lvl for k in win)
            else:
                closes_beyond=sum(1 for k in win if closes[k]<lvl)
                reclaimed = any(closes[k]>=lvl for k in win)
            # is this rolling level a NAMED/mapped zone? (proxy: matches a known level)
            is_named = any(abs(lvl-L)/lvl<0.0015 for L in named_levels)
            new_fire = reclaimed and closes_beyond<ACCEPT and is_named
            if old_fire: old_sweeps+=1
            if new_fire: new_sweeps+=1
            elif old_fire and closes_beyond>=ACCEPT: breakouts+=1     # old said sweep, really breakout
            elif old_fire and not is_named: interior+=1               # old said sweep, interior noise
    return old_sweeps,new_sweeps,breakouts,interior

# named levels per name: session H/L + ORB (proxy for mapped zones we CAN see; PDH/ON are Jason's)
print(f"{'name':<6}{'OLD sweeps':>11}{'NEW sweeps':>11}{'→breakout':>11}{'→interior':>11}")
for nm,p in F.items():
    c=pd.read_csv(p); c["ts"]=c.timestamp.map(datetime.fromisoformat); c=c.sort_values("ts").reset_index(drop=True)
    orb=c.iloc[:5]; named=[orb.high.max(),orb.low.min(),c.high.cummax().iloc[-1],c.low.cummin().iloc[-1]]
    o,nw,bk,intr=classify_pokes(c,named)
    print(f"{nm:<6}{o:>11}{nw:>11}{bk:>11}{intr:>11}")
print("\nOLD = fired on any wick (the bug). NEW = requires reclaim + named zone + not accepted.")
print("→breakout = old-fired pokes that were ACCEPTED through (now correctly excluded).")
print("→interior = old-fired pokes NOT at a mapped zone (now correctly excluded).")
