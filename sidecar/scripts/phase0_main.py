"""
Phase 0 GEX-regime premise validation. READ-ONLY.
Builds a backward-looking gamma-regime classifier from top_gex_walls,
sanity-checks the GEX sign against realized vol, then tests whether
ACCELERATION-regime forward SPX moves are larger/more sustained than
SUPPRESSION-regime moves.

Registered thresholds (NO sweeping):
  FLIP_BAND_PT = 5.0       (TRANSITION zone half-width around flip)
  RV_WINDOW_MIN = 30       (realized-vol lookahead for sign sanity check)
  FWD_WINDOWS = [15,30,60] (forward-move horizons in minutes)
  MR_END_BAND = 0.5        (mean-reversion: |end move| < 0.5*max excursion => reverted)
  ET_TO_UTC_H = 5          (signal_iter local ET -> UTC bar offset, EDT)
"""
import sqlite3, json, datetime, statistics, bisect, math
from collections import defaultdict

DB = r'C:\Users\diant\.fef-terminal\signal_log.db'
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row

FLIP_BAND_PT = 5.0
RV_WINDOW_MIN = 30
FWD_WINDOWS = [15, 30, 60]
MR_END_BAND = 0.5
ET_TO_UTC_H = 5
SESSIONS = ['2026-05-18','2026-05-19','2026-05-20','2026-05-21','2026-05-22','2026-05-26','2026-05-27']

def walls_of(s):
    if not s or s in ('[]','null'): return []
    try: return json.loads(s)
    except: return []

# --- load SPX 1-min bars indexed by UTC ms ---
bars = c.execute("SELECT timestamp_ms, close FROM historical_bar WHERE symbol='SPX' AND bar_size='1 min' ORDER BY timestamp_ms").fetchall()
bms = [b['timestamp_ms'] for b in bars]
bcl = [b['close'] for b in bars]

def parse_ts(ts):
    return datetime.datetime.strptime(ts.split('.')[0], '%Y-%m-%d %H:%M:%S')

def iter_to_utc_ms(ts):
    dt = parse_ts(ts) + datetime.timedelta(hours=ET_TO_UTC_H)
    return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp()*1000)

def bar_close_at(utc_ms, tol_min=2):
    """nearest bar close within tol; returns (close, bar_ms) or None"""
    i = bisect.bisect_left(bms, utc_ms)
    best=None
    for j in (i-1, i, i+1):
        if 0<=j<len(bms):
            d=abs(bms[j]-utc_ms)
            if best is None or d<best[0]:
                best=(d, bcl[j], bms[j])
    if best and best[0] <= tol_min*60000:
        return best[1], best[2]
    return None

def bars_between(start_ms, end_ms):
    i=bisect.bisect_left(bms, start_ms); j=bisect.bisect_right(bms, end_ms)
    return [(bms[k], bcl[k]) for k in range(i,j)]

# --- regime classifier ---
def classify(walls, spot):
    """Returns (regime, net_gex_m, flip_strike, spot_vs_flip, flip_found)."""
    if not walls or spot is None:
        return None
    net = sum(w['net_m'] for w in walls)          # signed: CALL>0, PUT<0
    items = sorted([(w['strike'], w['net_m']) for w in walls])
    # cumulative dealer gamma from low strike up; locate sign change (flip)
    flip = None
    cum = 0.0; prev_cum = None; prev_strike=None
    for st, nm in items:
        prev_cum = cum
        cum += nm
        if prev_strike is not None and prev_cum is not None and (prev_cum<=0)!=(cum<=0) and (prev_cum!=cum):
            # interpolate strike of zero-crossing of cumulative
            frac = (0-prev_cum)/(cum-prev_cum) if (cum-prev_cum)!=0 else 0.5
            flip = prev_strike + frac*(st-prev_strike)
        prev_strike = st
    flip_found = flip is not None
    if flip is None:
        # fallback proxy: boundary between highest-PUT-strike and lowest-CALL-strike
        puts=[s for s,n in items if n<0]; calls=[s for s,n in items if n>0]
        if puts and calls and max(puts) < min(calls):
            flip = (max(puts)+min(calls))/2.0
        elif net>0:
            flip = items[0][0]-1   # all-positive: flip below everything -> spot above flip
        elif net<0:
            flip = items[-1][0]+1  # all-negative: flip above everything -> spot below flip
        else:
            flip = spot
    svf = spot - flip
    # classify
    if abs(svf) <= FLIP_BAND_PT:
        reg='TRANSITION'
    elif svf > 0 and net > 0:
        reg='SUPPRESSION'      # spot above flip AND positive net GEX -> dealers dampen
    elif svf < 0 or net < 0:
        reg='ACCELERATION'     # spot below flip OR negative net GEX -> dealers amplify
    else:
        reg='TRANSITION'
    return reg, net, flip, svf, flip_found

# --- realized vol forward (sign sanity check) ---
def realized_vol_fwd(utc_ms, win_min):
    end = utc_ms + win_min*60000
    seg = bars_between(utc_ms, end)
    if len(seg) < max(5, win_min//3):
        return None
    rets=[]
    for k in range(1,len(seg)):
        p0=seg[k-1][1]; p1=seg[k][1]
        if p0: rets.append((p1-p0)/p0)
    if len(rets)<4: return None
    return statistics.pstdev(rets)*10000  # bps per minute

# --- forward move metrics ---
def fwd_metrics(utc_ms, win_min, spot_now):
    end = utc_ms + win_min*60000
    seg = bars_between(utc_ms, end)
    if len(seg) < max(4, win_min//4):
        return None
    closes=[p for _,p in seg]
    end_move = closes[-1]-spot_now
    excursions=[p-spot_now for p in closes]
    max_up=max(excursions); max_dn=min(excursions)
    max_abs_exc = max(abs(max_up), abs(max_dn))
    abs_end = abs(end_move)
    # continuation: did it keep going in the direction of the initial 5-min impulse?
    return {
        'end_move': end_move,
        'abs_end': abs_end,
        'max_abs_exc': max_abs_exc,
        'max_up': max_up, 'max_dn': max_dn,
        'reverted': (abs_end < MR_END_BAND*max_abs_exc) if max_abs_exc>0 else True,
        'efficiency': (abs_end/max_abs_exc) if max_abs_exc>0 else 0.0,  # 1=pure trend,0=full reversion
    }

# ============ RUN ============
records=[]  # one per iter with regime + fwd metrics
for sess in SESSIONS:
    rows=c.execute("SELECT id, timestamp, spx_price, top_gex_walls FROM signal_iter WHERE session_date=? ORDER BY iter_num",(sess,)).fetchall()
    for r in rows:
        cl=classify(walls_of(r['top_gex_walls']), r['spx_price'])
        if cl is None: continue
        reg, net, flip, svf, flip_found = cl
        utc_ms=iter_to_utc_ms(r['timestamp'])
        ba=bar_close_at(utc_ms)
        if ba is None:   # iter outside RTH bar coverage
            continue
        spot_bar, bar_ms = ba
        rec={'sess':sess,'id':r['id'],'reg':reg,'net':net,'svf':svf,'flip_found':flip_found,
             'spot':r['spx_price'],'bar_ms':bar_ms,'spot_bar':spot_bar}
        rec['rv30']=realized_vol_fwd(bar_ms, RV_WINDOW_MIN)
        for w in FWD_WINDOWS:
            rec[f'fwd{w}']=fwd_metrics(bar_ms, w, spot_bar)
        records.append(rec)

print(f"TOTAL iters classified & bar-aligned: {len(records)}")
print(f"flip located within captured walls (cum sign-change): {sum(1 for r in records if r['flip_found'])}/{len(records)} = {100*sum(1 for r in records if r['flip_found'])/len(records):.0f}%")

# regime counts
rc=defaultdict(int)
for r in records: rc[r['reg']]+=1
print("Regime counts:", dict(rc))

# ---- SIGN SANITY CHECK: realized vol by regime ----
print("\n===== SIGN-CONVENTION SANITY CHECK (fwd 30-min realized vol, bps/min) =====")
print(f"{'regime':14s} {'n':>5s} {'median_rv':>10s} {'mean_rv':>9s}")
rv_by={}
for reg in ['SUPPRESSION','TRANSITION','ACCELERATION']:
    vals=[r['rv30'] for r in records if r['reg']==reg and r['rv30'] is not None]
    rv_by[reg]=vals
    if vals:
        print(f"{reg:14s} {len(vals):5d} {statistics.median(vals):10.2f} {statistics.mean(vals):9.2f}")
    else:
        print(f"{reg:14s} {0:5d}        n/a       n/a")
sup_med = statistics.median(rv_by['SUPPRESSION']) if rv_by['SUPPRESSION'] else None
acc_med = statistics.median(rv_by['ACCELERATION']) if rv_by['ACCELERATION'] else None
if sup_med is not None and acc_med is not None:
    verdict = "CORRECT (SUPPRESSION rv < ACCELERATION rv)" if sup_med < acc_med else "*** BACKWARDS *** (SUPPRESSION rv >= ACCELERATION rv) -> GEX SIGN LIKELY FLIPPED"
    print(f"\nSIGN CHECK: SUPPRESSION_med={sup_med:.2f}  ACCELERATION_med={acc_med:.2f}  -> {verdict}")

# ---- FORWARD-MOVE TABLE ----
print("\n===== REGIME x FORWARD-MOVE (the kill gate) =====")
for w in FWD_WINDOWS:
    print(f"\n-- Forward {w}-min --")
    print(f"{'regime':14s} {'n':>5s} {'med|end|':>9s} {'med_maxexc':>11s} {'mean_eff':>9s} {'revert%':>8s}")
    for reg in ['SUPPRESSION','TRANSITION','ACCELERATION']:
        ms=[r[f'fwd{w}'] for r in records if r['reg']==reg and r[f'fwd{w}'] is not None]
        if not ms:
            print(f"{reg:14s} {0:5d}"); continue
        med_end=statistics.median(m['abs_end'] for m in ms)
        med_exc=statistics.median(m['max_abs_exc'] for m in ms)
        mean_eff=statistics.mean(m['efficiency'] for m in ms)
        revert=100*sum(1 for m in ms if m['reverted'])/len(ms)
        print(f"{reg:14s} {len(ms):5d} {med_end:9.2f} {med_exc:11.2f} {mean_eff:9.2f} {revert:7.0f}%")

# ---- Mann-Whitney-ish: is ACCEL |end move| distribution > SUPP? simple bootstrap of median diff ----
print("\n===== ACCEL vs SUPP forward |end move| (30-min) — distribution test =====")
def mwu_p(a, b):
    # rank-sum U statistic, normal approx
    n1,n2=len(a),len(b)
    if n1==0 or n2==0: return None,None
    combined=sorted([(v,0) for v in a]+[(v,1) for v in b])
    ranks={}
    i=0
    while i<len(combined):
        j=i
        while j<len(combined) and combined[j][0]==combined[i][0]: j+=1
        avg=(i+1+j)/2.0
        for k in range(i,j): ranks[k]=avg
        i=j
    R1=sum(ranks[idx] for idx,(v,g) in enumerate(combined) if g==0)
    U1=R1-n1*(n1+1)/2.0
    mu=n1*n2/2.0; sig=math.sqrt(n1*n2*(n1+n2+1)/12.0)
    if sig==0: return None,None
    z=(U1-mu)/sig
    from math import erf, sqrt
    p=2*(1-0.5*(1+erf(abs(z)/sqrt(2))))
    return z,p
acc=[r['fwd30']['abs_end'] for r in records if r['reg']=='ACCELERATION' and r['fwd30']]
sup=[r['fwd30']['abs_end'] for r in records if r['reg']=='SUPPRESSION' and r['fwd30']]
z,p=mwu_p(acc,sup)
print(f"ACCEL n={len(acc)} med={statistics.median(acc):.2f}  SUPP n={len(sup)} med={statistics.median(sup):.2f}")
if z is not None:
    print(f"Mann-Whitney U z={z:.2f} two-sided p={p:.4f}  ({'SIGNIFICANT' if p<0.05 else 'NOT significant'} at 0.05)")

# ---- per-session regime breakdown ----
print("\n===== PER-SESSION REGIME BREAKDOWN =====")
print(f"{'session':12s} {'n':>4s} {'SUPP':>6s} {'TRANS':>6s} {'ACCEL':>6s} {'flip%':>6s} {'rv30med':>8s} {'fwd30 med|end|':>14s}")
for sess in SESSIONS:
    rs=[r for r in records if r['sess']==sess]
    if not rs:
        print(f"{sess:12s} {0:4d}"); continue
    cnt=defaultdict(int)
    for r in rs: cnt[r['reg']]+=1
    flipp=100*sum(1 for r in rs if r['flip_found'])/len(rs)
    rv=[r['rv30'] for r in rs if r['rv30'] is not None]
    fe=[r['fwd30']['abs_end'] for r in rs if r['fwd30']]
    print(f"{sess:12s} {len(rs):4d} {cnt['SUPPRESSION']:6d} {cnt['TRANSITION']:6d} {cnt['ACCELERATION']:6d} {flipp:5.0f}% "
          f"{statistics.median(rv) if rv else float('nan'):8.2f} {statistics.median(fe) if fe else float('nan'):14.2f}")

# ---- session-level net GEX bias (suppression-heavy day cross-check) ----
print("\n===== SESSION GEX CHARACTER (for credit-spread cross-check) =====")
print(f"{'session':12s} {'%SUPP':>6s} {'%ACCEL':>7s} {'session_range':>13s} {'open->close':>11s} {'character':>22s}")
# session range from SPX bars (RTH 13:30-20:00 UTC)
for sess in SESSIONS:
    rs=[r for r in records if r['sess']==sess]
    if not rs: continue
    cnt=defaultdict(int)
    for r in rs: cnt[r['reg']]+=1
    pct_sup=100*cnt['SUPPRESSION']/len(rs); pct_acc=100*cnt['ACCELERATION']/len(rs)
    y,m,d=[int(x) for x in sess.split('-')]
    day_start=int(datetime.datetime(y,m,d,13,30,tzinfo=datetime.timezone.utc).timestamp()*1000)
    day_end=int(datetime.datetime(y,m,d,20,0,tzinfo=datetime.timezone.utc).timestamp()*1000)
    seg=bars_between(day_start, day_end)
    if seg:
        hi=max(p for _,p in seg); lo=min(p for _,p in seg)
        rng=hi-lo; oc=seg[-1][1]-seg[0][1]
    else:
        rng=float('nan'); oc=float('nan')
    char = 'SUPP-heavy(range?)' if pct_sup>pct_acc else 'ACCEL-heavy(trend?)'
    print(f"{sess:12s} {pct_sup:5.0f}% {pct_acc:6.0f}% {rng:13.1f} {oc:+11.1f} {char:>22s}")
