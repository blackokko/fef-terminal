"""Phase 0 robustness checks. READ-ONLY.
1. Restrict to iters where a real cumulative-gamma flip was located.
2. Account for within-session autocorrelation (effective N ~ sessions, not iters).
3. Compare flip-based vs net-sign-only classification.
4. Session-level paired view of regime mix vs realized forward move.
"""
import sqlite3, json, datetime, statistics, bisect, math
from collections import defaultdict

DB = r'C:\Users\diant\.fef-terminal\signal_log.db'
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row

FLIP_BAND_PT=5.0; ET_TO_UTC_H=5; FWD=30
SESSIONS=['2026-05-18','2026-05-19','2026-05-20','2026-05-21','2026-05-22','2026-05-26','2026-05-27']

def walls_of(s):
    if not s or s in ('[]','null'): return []
    try: return json.loads(s)
    except: return []

bars=c.execute("SELECT timestamp_ms, close FROM historical_bar WHERE symbol='SPX' AND bar_size='1 min' ORDER BY timestamp_ms").fetchall()
bms=[b['timestamp_ms'] for b in bars]; bcl=[b['close'] for b in bars]
def parse_ts(ts): return datetime.datetime.strptime(ts.split('.')[0],'%Y-%m-%d %H:%M:%S')
def iter_utc(ts): return int((parse_ts(ts)+datetime.timedelta(hours=ET_TO_UTC_H)).replace(tzinfo=datetime.timezone.utc).timestamp()*1000)
def bar_at(ms,tol=2):
    i=bisect.bisect_left(bms,ms); best=None
    for j in (i-1,i,i+1):
        if 0<=j<len(bms):
            d=abs(bms[j]-ms)
            if best is None or d<best[0]: best=(d,bcl[j],bms[j])
    return (best[1],best[2]) if best and best[0]<=tol*60000 else None
def between(a,b):
    i=bisect.bisect_left(bms,a); j=bisect.bisect_right(bms,b); return [(bms[k],bcl[k]) for k in range(i,j)]

def classify(walls,spot):
    if not walls or spot is None: return None
    net=sum(w['net_m'] for w in walls)
    items=sorted([(w['strike'],w['net_m']) for w in walls])
    flip=None; cum=0.0; pc=None; ps=None
    for st,nm in items:
        pc=cum; cum+=nm
        if ps is not None and (pc<=0)!=(cum<=0) and pc!=cum:
            frac=(0-pc)/(cum-pc); flip=ps+frac*(st-ps)
        ps=st
    found=flip is not None
    if flip is None:
        puts=[s for s,n in items if n<0]; calls=[s for s,n in items if n>0]
        if puts and calls and max(puts)<min(calls): flip=(max(puts)+min(calls))/2
        elif net>0: flip=items[0][0]-1
        elif net<0: flip=items[-1][0]+1
        else: flip=spot
    svf=spot-flip
    if abs(svf)<=FLIP_BAND_PT: reg='TRANSITION'
    elif svf>0 and net>0: reg='SUPPRESSION'
    elif svf<0 or net<0: reg='ACCELERATION'
    else: reg='TRANSITION'
    # net-sign-only variant
    reg_net = 'SUPPRESSION' if net>0 else ('ACCELERATION' if net<0 else 'TRANSITION')
    return reg,reg_net,net,svf,found

def fwd_abs_end(ms,spot,win=FWD):
    seg=between(ms,ms+win*60000)
    if len(seg)<win//4: return None
    return abs(seg[-1][1]-spot)

recs=[]
for sess in SESSIONS:
    for r in c.execute("SELECT timestamp,spx_price,top_gex_walls FROM signal_iter WHERE session_date=? ORDER BY iter_num",(sess,)):
        cl=classify(walls_of(r['top_gex_walls']),r['spx_price'])
        if cl is None: continue
        reg,reg_net,net,svf,found=cl
        ms=iter_utc(r['timestamp']); ba=bar_at(ms)
        if not ba: continue
        sb,bms2=ba
        recs.append({'sess':sess,'reg':reg,'reg_net':reg_net,'found':found,'net':net,'svf':svf,
                     'fae':fwd_abs_end(bms2,sb)})

def med(L): return statistics.median(L) if L else float('nan')

print("=== (1) FLIP-FOUND-ONLY subset (real cumulative-gamma flip located) ===")
fr=[r for r in recs if r['found'] and r['fae'] is not None]
print(f"  n={len(fr)}")
for reg in ['SUPPRESSION','TRANSITION','ACCELERATION']:
    v=[r['fae'] for r in fr if r['reg']==reg]
    print(f"  {reg:14s} n={len(v):3d} med|end30|={med(v):.2f}")

print("\n=== (2) NET-SIGN-ONLY classifier (no flip needed) ===")
nr=[r for r in recs if r['fae'] is not None]
for reg in ['SUPPRESSION','ACCELERATION']:
    v=[r['fae'] for r in nr if r['reg_net']==reg]
    print(f"  {reg:14s} n={len(v):3d} med|end30|={med(v):.2f}  (net>0=SUPP, net<0=ACCEL)")

print("\n=== (3) SESSION-LEVEL test (1 datapoint/session -> honest N) ===")
# For each session: dominant regime + that session's median forward move + actual range
print(f"{'session':12s} {'%ACCEL':>6s} {'domreg':>12s} {'med|end30|':>10s} {'realized_range':>14s}")
sess_rows=[]
for sess in SESSIONS:
    rs=[r for r in recs if r['sess']==sess and r['fae'] is not None]
    if not rs: continue
    cnt=defaultdict(int)
    for r in rs: cnt[r['reg']]+=1
    pacc=100*cnt['ACCELERATION']/len(rs)
    dom=max(cnt,key=cnt.get)
    mfe=med([r['fae'] for r in rs])
    y,m,d=[int(x) for x in sess.split('-')]
    seg=between(int(datetime.datetime(y,m,d,13,30,tzinfo=datetime.timezone.utc).timestamp()*1000),
                int(datetime.datetime(y,m,d,20,0,tzinfo=datetime.timezone.utc).timestamp()*1000))
    rng=(max(p for _,p in seg)-min(p for _,p in seg)) if seg else float('nan')
    sess_rows.append((sess,pacc,dom,mfe,rng))
    print(f"{sess:12s} {pacc:5.0f}% {dom:>12s} {mfe:10.2f} {rng:14.1f}")

# correlation: does session %ACCEL predict session median forward move / range?
import statistics as st
xs=[r[1] for r in sess_rows]; ys_fe=[r[3] for r in sess_rows]; ys_rng=[r[4] for r in sess_rows]
def pearson(a,b):
    n=len(a); ma=sum(a)/n; mb=sum(b)/n
    num=sum((a[i]-ma)*(b[i]-mb) for i in range(n))
    da=math.sqrt(sum((x-ma)**2 for x in a)); db=math.sqrt(sum((x-mb)**2 for x in b))
    return num/(da*db) if da and db else float('nan')
print(f"\n  Pearson r(%ACCEL, session med|end30|) = {pearson(xs,ys_fe):+.3f}  (n=7 sessions)")
print(f"  Pearson r(%ACCEL, session realized_range) = {pearson(xs,ys_rng):+.3f}  (n=7 sessions)")

print("\n=== (4) Cluster-aware ACCEL-vs-SUPP test: per-session median, then sign test ===")
# pair each session's ACCEL-median vs SUPP-median (where both exist)
within=[]
for sess in SESSIONS:
    rs=[r for r in recs if r['sess']==sess and r['fae'] is not None]
    a=[r['fae'] for r in rs if r['reg']=='ACCELERATION']
    s=[r['fae'] for r in rs if r['reg']=='SUPPRESSION']
    if len(a)>=5 and len(s)>=5:
        within.append((sess, med(a), med(s), med(a)-med(s)))
print(f"  sessions with >=5 in both regimes: {len(within)}")
print(f"  {'session':12s} {'ACCELmed':>9s} {'SUPPmed':>9s} {'diff(A-S)':>10s}")
pos=0
for sess,am,sm,df in within:
    print(f"  {sess:12s} {am:9.2f} {sm:9.2f} {df:+10.2f}")
    if df>0: pos+=1
print(f"  ACCEL>SUPP in {pos}/{len(within)} sessions")
# binomial p for sign test
from math import comb
n=len(within)
if n>0:
    p=sum(comb(n,k) for k in range(pos,n+1))*0.5**n
    print(f"  one-sided sign-test p = {p:.3f}")
