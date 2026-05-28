"""Phase 0 credit-spread cross-check + flip-found session robustness. READ-ONLY."""
import sqlite3, json, datetime, statistics, bisect, math
from collections import defaultdict
DB=r'C:\Users\diant\.fef-terminal\signal_log.db'
c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
SESSIONS=['2026-05-18','2026-05-19','2026-05-20','2026-05-21','2026-05-22','2026-05-26','2026-05-27']

bars=c.execute("SELECT timestamp_ms, close FROM historical_bar WHERE symbol='SPX' AND bar_size='1 min' ORDER BY timestamp_ms").fetchall()
bms=[b['timestamp_ms'] for b in bars]; bcl=[b['close'] for b in bars]
def between(a,b):
    i=bisect.bisect_left(bms,a); j=bisect.bisect_right(bms,b); return [(bms[k],bcl[k]) for k in range(i,j)]

print("=== RANGE-DAY / TREND-DAY CHARACTER (RTH SPX) ===")
print(f"{'session':12s} {'open':>8s} {'close':>8s} {'O->C':>7s} {'range':>7s} {'|O-C|/range':>11s} {'character':>14s}")
char_by={}
for sess in SESSIONS:
    y,m,d=[int(x) for x in sess.split('-')]
    seg=between(int(datetime.datetime(y,m,d,13,30,tzinfo=datetime.timezone.utc).timestamp()*1000),
                int(datetime.datetime(y,m,d,20,0,tzinfo=datetime.timezone.utc).timestamp()*1000))
    if not seg: continue
    o=seg[0][1]; cl=seg[-1][1]; hi=max(p for _,p in seg); lo=min(p for _,p in seg)
    rng=hi-lo; oc=cl-o
    ratio=abs(oc)/rng if rng else 0
    # range-day if net move is small fraction of intraday range (mean-reverting/chop)
    char='RANGE/CHOP' if ratio<0.35 else 'TREND'
    char_by[sess]=char
    print(f"{sess:12s} {o:8.1f} {cl:8.1f} {oc:+7.1f} {rng:7.1f} {ratio:11.2f} {char:>14s}")

print("\n  (RANGE/CHOP days = where credit-spreads / premium-selling would win)")

print("\n=== daily_review actuals (P&L proxy via expectancy/R) ===")
cols=[r['name'] for r in c.execute("PRAGMA table_info(daily_review)")]
for r in c.execute("SELECT * FROM daily_review WHERE session_date IN ('2026-05-18','2026-05-19','2026-05-20','2026-05-21','2026-05-22','2026-05-26','2026-05-27') ORDER BY session_date"):
    d=dict(r)
    print(f"  {d['session_date']}: actionable={d.get('actionable_signals')} win_rate={d.get('win_rate_pct')} expectancy_R={d.get('expectancy_r')} actual_total_R={d.get('actual_total_R')} dedup_exp_R={d.get('dedup_expectancy_r')} profit_factor={d.get('profit_factor')}")

# flip-found session-level robustness (reuse classifier inline, flip-found only)
FLIP_BAND_PT=5.0; ET_TO_UTC_H=5; FWD=30
def walls_of(s):
    if not s or s in ('[]','null'): return []
    try: return json.loads(s)
    except: return []
def parse_ts(ts): return datetime.datetime.strptime(ts.split('.')[0],'%Y-%m-%d %H:%M:%S')
def iter_utc(ts): return int((parse_ts(ts)+datetime.timedelta(hours=ET_TO_UTC_H)).replace(tzinfo=datetime.timezone.utc).timestamp()*1000)
def bar_at(ms,tol=2):
    i=bisect.bisect_left(bms,ms); best=None
    for j in (i-1,i,i+1):
        if 0<=j<len(bms):
            dd=abs(bms[j]-ms)
            if best is None or dd<best[0]: best=(dd,bcl[j],bms[j])
    return (best[1],best[2]) if best and best[0]<=tol*60000 else None
def fwd_abs(ms,spot,win=FWD):
    seg=between(ms,ms+win*60000)
    return abs(seg[-1][1]-spot) if len(seg)>=win//4 else None
def classify(walls,spot):
    if not walls or spot is None: return None
    net=sum(w['net_m'] for w in walls); items=sorted([(w['strike'],w['net_m']) for w in walls])
    flip=None; cum=0.0; pc=None; ps=None
    for st,nm in items:
        pc=cum; cum+=nm
        if ps is not None and (pc<=0)!=(cum<=0) and pc!=cum:
            flip=ps+((0-pc)/(cum-pc))*(st-ps)
        ps=st
    found=flip is not None
    if not found: return None  # flip-found only
    svf=spot-flip
    if abs(svf)<=FLIP_BAND_PT: reg='TRANSITION'
    elif svf>0 and net>0: reg='SUPPRESSION'
    elif svf<0 or net<0: reg='ACCELERATION'
    else: reg='TRANSITION'
    return reg,svf

print("\n=== FLIP-FOUND-ONLY: per-session ACCEL vs SUPP (cluster-aware) ===")
print(f"  {'session':12s} {'nA':>3s} {'nS':>3s} {'ACCELmed':>9s} {'SUPPmed':>9s} {'diff':>8s}")
recs=defaultdict(lambda: {'A':[],'S':[]})
for sess in SESSIONS:
    for r in c.execute("SELECT timestamp,spx_price,top_gex_walls FROM signal_iter WHERE session_date=? ORDER BY iter_num",(sess,)):
        cl=classify(walls_of(r['top_gex_walls']),r['spx_price'])
        if cl is None: continue
        reg,svf=cl
        ba=bar_at(iter_utc(r['timestamp']))
        if not ba: continue
        fa=fwd_abs(ba[1],ba[0])
        if fa is None: continue
        if reg=='ACCELERATION': recs[sess]['A'].append(fa)
        elif reg=='SUPPRESSION': recs[sess]['S'].append(fa)
pos=0; tot=0
for sess in SESSIONS:
    A=recs[sess]['A']; S=recs[sess]['S']
    if len(A)>=3 and len(S)>=3:
        ma=statistics.median(A); ms_=statistics.median(S); tot+=1
        if ma>ms_: pos+=1
        print(f"  {sess:12s} {len(A):3d} {len(S):3d} {ma:9.2f} {ms_:9.2f} {ma-ms_:+8.2f}")
    else:
        print(f"  {sess:12s} {len(A):3d} {len(S):3d}   (insufficient one side)")
print(f"  ACCEL>SUPP in {pos}/{tot} sessions with both-regime coverage")
