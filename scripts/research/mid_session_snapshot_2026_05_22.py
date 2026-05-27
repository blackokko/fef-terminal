"""Mid-session full-picture snapshot for 2026-05-22.

Pulls every persisted layer that has data captured today plus a give-back analysis
comparing MFE (Maximum Favorable Excursion) to final-R per live trade.

R-unit migration (Chat audit Priority 3 — 2026-05-22): the R-unit in this
script is now per-signal |entry - spx_target_stop| in SPX points, falling
back to 10pt only when stop is missing. Matches daily_review.
"""
import sys
import sqlite3
import json
import datetime as dt
import re
import os
from collections import defaultdict, Counter

# Canonical R-unit helper
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _r_unit import compute_r_unit  # noqa: E402

# Force UTF-8 stdout on Windows so we can print delta/mu/etc safely
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

DB = r"C:\Users\diant\.fef-terminal\signal_log.db"
TODAY = "2026-05-22"


def hr(title):
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def sub(title):
    print()
    print("-" * 60)
    print(title)
    print("-" * 60)


def parse_ts(s: str) -> dt.datetime:
    # "2026-05-22 13:38:28.290291" -> naive datetime
    # signal_iter / live_order / audit_log strings are UTC (verified)
    if not s:
        return None
    # tolerate fractional seconds
    return dt.datetime.fromisoformat(s)


def ts_to_epoch_ms(s: str) -> int:
    d = parse_ts(s)
    if not d:
        return None
    # The string IS UTC (verified by matching flow_snapshot.timestamp_ms which is epoch UTC ms).
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row
c = db.cursor()


# =============================================================================
# Section 1: Session overview
# =============================================================================
hr("SECTION 1 - SESSION OVERVIEW (2026-05-22)")
total_iter = c.execute("SELECT COUNT(*) FROM signal_iter WHERE session_date=?", (TODAY,)).fetchone()[0]
print(f"Total signal_iter rows today: {total_iter}")

print("\nengine_version distribution:")
rows = c.execute("SELECT engine_version, COUNT(*) n FROM signal_iter WHERE session_date=? GROUP BY engine_version ORDER BY engine_version", (TODAY,)).fetchall()
for r in rows:
    print(f"  engine_version={r['engine_version']}: {r['n']}")

print("\naccount_class distribution (via extras.account_class):")
ac = Counter()
rows = c.execute("SELECT extras FROM signal_iter WHERE session_date=?", (TODAY,)).fetchall()
for r in rows:
    if r['extras']:
        try:
            e = json.loads(r['extras'])
            ac[e.get('account_class') or 'NULL'] += 1
        except Exception:
            ac['PARSE_ERROR'] += 1
    else:
        ac['NO_EXTRAS'] += 1
for k, v in ac.most_common():
    print(f"  {k}: {v}")


# =============================================================================
# Section 2: drift_bias distribution
# =============================================================================
hr("SECTION 2 - DRIFT_BIAS DISTRIBUTION TODAY")
rows = c.execute("SELECT drift_bias, COUNT(*) n FROM signal_iter WHERE session_date=? GROUP BY drift_bias ORDER BY n DESC", (TODAY,)).fetchall()
total = sum(r['n'] for r in rows)
print(f"  {'bias':25s}  count   pct")
for r in rows:
    bias = r['drift_bias'] or 'NULL'
    pct = 100.0 * r['n'] / total if total else 0
    print(f"  {bias:25s}  {r['n']:5d}  {pct:5.1f}%")


# =============================================================================
# Section 3: direction + confidence breakdown
# =============================================================================
hr("SECTION 3 - DIRECTION x CONFIDENCE BREAKDOWN")
rows = c.execute("SELECT direction, confidence, COUNT(*) n FROM signal_iter WHERE session_date=? GROUP BY direction, confidence ORDER BY direction, confidence", (TODAY,)).fetchall()
print(f"  {'direction':14s}  {'confidence':10s}  count")
for r in rows:
    d = r['direction'] or 'NULL'
    cf = r['confidence'] or 'NULL'
    print(f"  {d:14s}  {cf:10s}  {r['n']:5d}")


# =============================================================================
# Section 4: ACTIONABLE signals today (direction != WAIT)
# =============================================================================
hr("SECTION 4 - ACTIONABLE SIGNALS TODAY (direction != WAIT)")
actionable_rows = c.execute("""
SELECT id, iter_num, timestamp, spx_price, direction, confidence, drift_bias,
       top_cex_walls, top_vex_walls, top_gex_by_expiry, top_dex_by_expiry, recent_orders,
       reasoning, extras
FROM signal_iter
WHERE session_date=? AND direction != 'WAIT'
ORDER BY iter_num
""", (TODAY,)).fetchall()
print(f"Count: {len(actionable_rows)}\n")

for r in actionable_rows:
    extras = json.loads(r['extras']) if r['extras'] else {}
    alt30 = extras.get('alt_direction_30m', {}) or {}
    alt4h = extras.get('alt_direction_4h', {}) or {}
    cex_walls = json.loads(r['top_cex_walls']) if r['top_cex_walls'] else []
    vex_walls = json.loads(r['top_vex_walls']) if r['top_vex_walls'] else []
    gex_by_exp = json.loads(r['top_gex_by_expiry']) if r['top_gex_by_expiry'] else []
    dex_by_exp = json.loads(r['top_dex_by_expiry']) if r['top_dex_by_expiry'] else []
    rec_orders = json.loads(r['recent_orders']) if r['recent_orders'] else []
    print(f"iter#{r['iter_num']:>3} | id={r['id']} | {r['timestamp']}")
    print(f"  spx={r['spx_price']:.2f}  dir={r['direction']}  conf={r['confidence']}  drift_bias={r['drift_bias']}")
    print(f"  regime_at_fire={extras.get('regime_at_fire')}  fe_ratio={extras.get('fe_ratio'):.4f}" if extras.get('fe_ratio') is not None else f"  regime_at_fire={extras.get('regime_at_fire')}  fe_ratio=NULL")
    dp = extras.get('delta_price')
    dd = extras.get('delta_drift_m')
    print(f"  delta_price={dp:.2f}pt  delta_drift_m={dd:+.3f}M" if dp is not None and dd is not None else f"  delta_price={dp}  delta_drift_m={dd}")
    if alt30:
        print(f"  alt_dir_30m: signal={str(alt30.get('signal')):8s} slope_5b={alt30.get('ema_8_slope_5bar')}  spx_vs_ema8={alt30.get('spx_vs_ema8_pts')}pt  n_bars={alt30.get('n_bars')}")
    if alt4h:
        print(f"  alt_dir_4h:  signal={str(alt4h.get('signal')):8s} slope_25b={alt4h.get('ema_50_slope_25bar')}  spx_vs_ema50={alt4h.get('spx_vs_ema50_pts')}pt  n_bars={alt4h.get('n_bars')}")
    print(f"  v2 arrays: cex={len(cex_walls)} vex={len(vex_walls)} gex_by_exp={len(gex_by_exp)} dex_by_exp={len(dex_by_exp)} rec_orders={len(rec_orders)}")
    rsn = r['reasoning'] or ''
    if len(rsn) > 200:
        rsn = rsn[:200] + '...'
    print(f"  reasoning: {rsn}")

    # Lookahead outcomes
    los = c.execute("""
SELECT lookahead_minutes, would_have_hit_t1, would_have_hit_stop, spx_at_signal, spx_mfe_pts, spx_mae_pts, spx_close_at_window_end
FROM signal_lookahead_outcome
WHERE signal_iter_id=?
ORDER BY lookahead_minutes
""", (r['id'],)).fetchall()
    if los:
        print(f"  lookahead outcomes:")
        for lo in los:
            mfe = lo['spx_mfe_pts']
            mae = lo['spx_mae_pts']
            cls = lo['spx_close_at_window_end']
            at_sig = lo['spx_at_signal']
            close_chg = (cls - at_sig) if (cls is not None and at_sig is not None) else None
            mfe_s = f'{mfe:+.2f}' if mfe is not None else 'None'
            mae_s = f'{mae:+.2f}' if mae is not None else 'None'
            close_s = f'{close_chg:+.2f}' if close_chg is not None else 'None'
            print(f"    {lo['lookahead_minutes']:>3}min: t1={'Y' if lo['would_have_hit_t1'] else 'N'} stop={'Y' if lo['would_have_hit_stop'] else 'N'} mfe={mfe_s} mae={mae_s} close_chg={close_s}")
    else:
        print(f"  lookahead outcomes: NONE")
    print()


# =============================================================================
# Section 5: LIVE ORDER outcomes today
# =============================================================================
hr("SECTION 5 - LIVE ORDER OUTCOMES TODAY")
parent_rows = c.execute("""
SELECT * FROM live_order
WHERE submitted_at LIKE ? AND parent_id IS NULL
ORDER BY id
""", (f"{TODAY}%",)).fetchall()
print(f"Total parents today: {len(parent_rows)}\n")

# Header
print(f"{'pid':>4}  {'time(UTC)':19s}  {'sym':3s}  {'strike':>7s}  {'R':1s}  {'qty':>3s}  {'status':18s}  {'fq':>2s}  {'avg_px':>7s}  {'entry_spx':>9s}  {'dyn_stop':>8s}  {'be_max':>6s}  {'t1/t2/t3':>10s}  {'tier_qty':>10s}")
for p in parent_rows:
    fq = p['filled_qty'] or 0
    avg = f"{p['avg_fill_price']:.2f}" if p['avg_fill_price'] is not None else "-"
    en = f"{p['entry_spx_price']:.2f}" if p['entry_spx_price'] is not None else "-"
    ds = f"{p['dynamic_stop_level']:.2f}" if p['dynamic_stop_level'] is not None else "-"
    bm = f"{p['be_ladder_max_step_R']:.1f}" if p['be_ladder_max_step_R'] is not None else "-"
    fired = f"{p['spx_target_t1_fired'] or 0}/{p['spx_target_t2_fired'] or 0}/{p['spx_target_t3_fired'] or 0}"
    tq = f"{p['spx_target_t1_qty'] or 0}/{p['spx_target_t2_qty'] or 0}/{p['spx_target_t3_qty'] or 0}"
    print(f"{p['id']:>4}  {p['submitted_at'][:19]}  {p['symbol']:3s}  {p['strike']:>7.1f}  {p['right']:1s}  {p['qty']:>3d}  {p['status']:18s}  {fq:>2d}  {avg:>7s}  {en:>9s}  {ds:>8s}  {bm:>6s}  {fired:>10s}  {tq:>10s}")

# Now children grouped under each parent
sub("CHILDREN PER PARENT")
parent_ids = [p['id'] for p in parent_rows]
if parent_ids:
    qmarks = ','.join('?' * len(parent_ids))
    children = c.execute(f"SELECT * FROM live_order WHERE parent_id IN ({qmarks}) ORDER BY parent_id, id", parent_ids).fetchall()
    by_parent = defaultdict(list)
    for ch in children:
        by_parent[ch['parent_id']].append(ch)
    for pid in parent_ids:
        kids = by_parent.get(pid, [])
        if not kids:
            continue
        print(f"\nParent {pid}: {len(kids)} children")
        for k in kids:
            avg = f"{k['avg_fill_price']:.2f}" if k['avg_fill_price'] is not None else "-"
            print(f"  child {k['id']:>4}  role={k['bracket_role']:8s}  action={k['action']:6s}  qty={k['qty']:>3d}  fq={k['filled_qty'] or 0:>3d}  status={k['status']:18s}  avg={avg}")


# =============================================================================
# Section 6: IBKR execution fills today
# =============================================================================
hr("SECTION 6 - IBKR EXECUTION FILLS TODAY")
rows = c.execute("SELECT * FROM ibkr_execution WHERE session_date=? ORDER BY id", (TODAY,)).fetchall()
print(f"Count: {len(rows)}")
if rows:
    print(f"{'time(UTC)':19s}  {'sym':5s}  {'side':5s}  {'qty':>5s}  {'price':>8s}  {'comm':>6s}  {'rpnl':>8s}")
    for r in rows:
        ts = r['timestamp'][:19] if r['timestamp'] else '-'
        comm = f"{r['commission']:.2f}" if r['commission'] is not None else "-"
        rp = f"{r['realized_pnl']:.2f}" if r['realized_pnl'] is not None else "-"
        print(f"{ts}  {r['symbol']:5s}  {r['side']:5s}  {r['shares']:>5.0f}  {r['price']:>8.2f}  {comm:>6s}  {rp:>8s}")
else:
    print("** STILL EMPTY ** despite STOP/TICK closes in audit log -> capture bug persists pre-restart")


# =============================================================================
# Section 7: GIVE-BACK ANALYSIS (priority)
# =============================================================================
hr("SECTION 7 - GIVE-BACK ANALYSIS (MFE vs final-R)")

# Get flow_snapshot SPX price series as the spot history (1-2s cadence, ~308 rows today)
flow_rows = c.execute("SELECT timestamp_ms, spx_price FROM flow_snapshot WHERE session_date=? ORDER BY timestamp_ms", (TODAY,)).fetchall()
spot_series = [(r['timestamp_ms'], r['spx_price']) for r in flow_rows]
print(f"Spot price series: {len(spot_series)} flow_snapshot rows used as 1-2s SPX cadence.")
if spot_series:
    print(f"  first: {dt.datetime.utcfromtimestamp(spot_series[0][0]/1000)} UTC  spx={spot_series[0][1]:.2f}")
    print(f"  last:  {dt.datetime.utcfromtimestamp(spot_series[-1][0]/1000)} UTC  spx={spot_series[-1][1]:.2f}")

# Also use audit_log NEAR_STOP/STOP/TICK/BE LADDER spot prints (richer 5s-cadence during active orders).
# Parse spot=XXXX.XX from audit summary text.
spot_re = re.compile(r"spot=([0-9]+\.?[0-9]*)")
tick_re = re.compile(r"at SPX ([0-9]+\.?[0-9]*)")
audit_spot_rows = c.execute("""
SELECT timestamp, summary FROM audit_log
WHERE category='spx_watcher' AND timestamp LIKE ?
ORDER BY id
""", (f"{TODAY}%",)).fetchall()
audit_spots = []
for a in audit_spot_rows:
    m = spot_re.search(a['summary'] or '')
    if m:
        try:
            ts_ms = ts_to_epoch_ms(a['timestamp'])
            audit_spots.append((ts_ms, float(m.group(1))))
            continue
        except Exception:
            pass
    m2 = tick_re.search(a['summary'] or '')
    if m2:
        try:
            ts_ms = ts_to_epoch_ms(a['timestamp'])
            audit_spots.append((ts_ms, float(m2.group(1))))
        except Exception:
            pass
audit_spots.sort()
print(f"audit_log spot prints (NEAR_STOP/STOP/TICK): {len(audit_spots)}")
if audit_spots:
    print(f"  first: {dt.datetime.utcfromtimestamp(audit_spots[0][0]/1000)} UTC spx={audit_spots[0][1]}")
    print(f"  last:  {dt.datetime.utcfromtimestamp(audit_spots[-1][0]/1000)} UTC spx={audit_spots[-1][1]}")

# Merge spot series
merged = sorted(spot_series + audit_spots)


def mfe_mae(entry_ts_ms, exit_ts_ms, direction, parent_row=None):
    """Return (best_spx, worst_spx, n_pts) within window for given direction.
    For LONG_CALLS: best = max, worst = min
    For LONG_PUTS:  best = min, worst = max

    Refinement: if the parent has any tier fired, we KNOW spot reached that target
    (the watcher fires at-or-better). Use the deepest-fired tier as a hard floor/ceiling
    on best_spx so we don't under-report MFE.
    """
    pts = [p for p in merged if entry_ts_ms <= p[0] <= exit_ts_ms]
    if not pts:
        return None, None, 0
    if direction == 'LONG_CALLS':
        best_spx = max(p[1] for p in pts)
        worst_spx = min(p[1] for p in pts)
    else:
        best_spx = min(p[1] for p in pts)
        worst_spx = max(p[1] for p in pts)

    # Use fired tier targets as a hard floor/ceiling on best_spx (we KNOW spot got at least that good)
    if parent_row is not None:
        fired_targets = []
        if (parent_row['spx_target_t1_fired'] or 0) and parent_row['spx_target_t1'] is not None:
            fired_targets.append(parent_row['spx_target_t1'])
        if (parent_row['spx_target_t2_fired'] or 0) and parent_row['spx_target_t2'] is not None:
            fired_targets.append(parent_row['spx_target_t2'])
        if (parent_row['spx_target_t3_fired'] or 0) and parent_row['spx_target_t3'] is not None:
            fired_targets.append(parent_row['spx_target_t3'])
        # at_spx where reported is the actual SPX at fire — use that to get an even tighter MFE
        if parent_row['spx_target_t1_at_spx'] is not None:
            fired_targets.append(parent_row['spx_target_t1_at_spx'])
        if parent_row['spx_target_t2_at_spx'] is not None:
            fired_targets.append(parent_row['spx_target_t2_at_spx'])
        if parent_row['spx_target_t3_at_spx'] is not None:
            fired_targets.append(parent_row['spx_target_t3_at_spx'])
        if fired_targets:
            if direction == 'LONG_CALLS':
                best_spx = max(best_spx, max(fired_targets))
            else:
                best_spx = min(best_spx, min(fired_targets))

    return best_spx, worst_spx, len(pts)


# For each parent today, match its trigger signal_iter and compute MFE
sub("PER-TRADE GIVE-BACK BREAKDOWN")
print(f"{'pid':>4}  {'time':19s}  {'dir':10s}  {'qty':>3s}  {'status':18s}  "
      f"{'entry_spx':>9s}  {'best':>7s}  {'best_R':>6s}  "
      f"{'final_R_est':>11s}  {'gap_R':>6s}  {'gap_$':>10s}  "
      f"{'PCD_R':>6s}")
print(f"{'':4}  {'':19s}  {'':10s}  {'':3s}  {'':18s}  "
      f"{'':9s}  {'spx':>7s}  {'':6s}  "
      f"{'':11s}  {'':6s}  {'($500/R)':>10s}  "
      f"{'(peak-trail)':>6s}")

total_gap_dollars = 0.0
total_actual_R_est = 0.0
total_pcd_R = 0.0
n_with_status = 0
gap_rows = []

for p in parent_rows:
    if p['status'] in (None, 'SUBMITTED'):
        # still alive — note but skip the math for now
        continue
    n_with_status += 1
    direction = 'LONG_PUTS' if p['right'] == 'P' else 'LONG_CALLS'
    entry_spx = p['entry_spx_price']
    entry_ts = ts_to_epoch_ms(p['submitted_at'])
    exit_ts_str = p['last_status_at'] or p['submitted_at']
    exit_ts = ts_to_epoch_ms(exit_ts_str)
    if not entry_spx or not entry_ts or not exit_ts:
        continue
    best_spx, worst_spx, n_pts = mfe_mae(entry_ts, exit_ts, direction, parent_row=p)
    if best_spx is None:
        continue
    # Per-signal R-unit (Chat audit Priority 3 — 2026-05-22). |entry - stop|
    # in SPX points; falls back to 10pt only if spx_target_stop is missing.
    sp_st = p['spx_target_stop']
    risk_pts = compute_r_unit(entry_spx, sp_st, default_pts=10.0)

    if direction == 'LONG_CALLS':
        best_R = (best_spx - entry_spx) / risk_pts
        worst_R = (worst_spx - entry_spx) / risk_pts
    else:
        best_R = (entry_spx - best_spx) / risk_pts
        worst_R = (entry_spx - worst_spx) / risk_pts

    # final-R estimate from order state
    t1f = p['spx_target_t1_fired'] or 0
    t2f = p['spx_target_t2_fired'] or 0
    t3f = p['spx_target_t3_fired'] or 0
    t1q = p['spx_target_t1_qty'] or 0
    t2q = p['spx_target_t2_qty'] or 0
    t3q = p['spx_target_t3_qty'] or 0
    qty = p['qty']
    be_max = p['be_ladder_max_step_R'] or 0.0
    # R per tier: divide each tier's SPX distance from entry by per-signal
    # risk_pts. With a typical -10pt stop and +10/+20/+30 targets these
    # come out to ~+1/+2/+3R as before; with a tighter stop (e.g. -7pt)
    # the same targets are wider in R-units, matching the live engine.
    sp_t1 = p['spx_target_t1']
    sp_t2 = p['spx_target_t2']
    sp_t3 = p['spx_target_t3']
    if direction == 'LONG_CALLS':
        r_t1 = (sp_t1 - entry_spx) / risk_pts if sp_t1 else 1.0
        r_t2 = (sp_t2 - entry_spx) / risk_pts if sp_t2 else 2.0
        r_t3 = (sp_t3 - entry_spx) / risk_pts if sp_t3 else 3.0
    else:
        r_t1 = (entry_spx - sp_t1) / risk_pts if sp_t1 else 1.0
        r_t2 = (entry_spx - sp_t2) / risk_pts if sp_t2 else 2.0
        r_t3 = (entry_spx - sp_t3) / risk_pts if sp_t3 else 3.0

    banked_R = 0.0
    if qty > 0:
        if t1f:
            banked_R += (t1q / qty) * r_t1
        if t2f:
            banked_R += (t2q / qty) * r_t2
        if t3f:
            banked_R += (t3q / qty) * r_t3
        # remainder qty: closed at stop
        remaining_qty = qty - (t1q if t1f else 0) - (t2q if t2f else 0) - (t3q if t3f else 0)
        if remaining_qty < 0:
            remaining_qty = 0
        # apply stop result on remainder
        if p['status'] == 'STOPPED':
            # all tier fires sum + remainder closed at stop
            # stop_R = be_max if be locked, else -1
            stop_R = be_max if be_max >= 0.5 else -1.0
            banked_R += (remaining_qty / qty) * stop_R
        elif p['status'] == 'CLOSED':
            # If all tiers fired, no remainder; else remainder closed at stop or manually
            if remaining_qty > 0:
                # assume closed at BE (be_max) if be_max > 0 else 0
                stop_R = be_max if be_max >= 0.5 else 0.0
                banked_R += (remaining_qty / qty) * stop_R

    final_R = banked_R
    gap_R = best_R - final_R
    gap_dollars = gap_R * 500 * qty  # $500 per 10pt-R per contract == 500/contract/R
    # Actually the user trades ~3 contracts here so this isn't $500/R sum — it's $500/R per contract.
    # Adjusted: dollar gap = gap_R * 500 * qty? No — let's clarify. User said "$500/R"
    # 1R = 10pts of SPX. $500/R is the standard payoff in their convention (1 SPX contract = $100/pt? No, SPX option ~ $100/pt of delta * delta. Their convention).
    # User said "$500/R" so we'll use that as the absolute conversion regardless of contracts.
    # But if qty is varying (5 then 3 then 20), we need to scale. Let's just use total_R per contract * 500 as one number, and qty as separate. Reframe: report gap_R per trade + gap_$ = gap_R * 500 (per contract convention).
    gap_dollars_one_contract = gap_R * 500.0
    gap_dollars_total = gap_R * 500.0 * qty
    total_gap_dollars += gap_dollars_total
    total_actual_R_est += final_R * qty
    gap_rows.append({
        'pid': p['id'], 'dir': direction, 'qty': qty, 'status': p['status'],
        'entry_spx': entry_spx, 'best_spx': best_spx, 'worst_spx': worst_spx,
        'best_R': best_R, 'worst_R': worst_R, 'final_R': final_R, 'gap_R': gap_R,
        'gap_dollars_total': gap_dollars_total, 'n_spot_pts': n_pts,
        'r_t1': r_t1, 'r_t2': r_t2, 'r_t3': r_t3,
    })

    # B+C+D peak-trail policy:
    # 50% banked at +0.5R, 30% at +1R, 20% trail with -0.5R from peak.
    # If MFE < +0.5R, position fully stopped at worst_R (capped at -1R).
    if best_R < 0.5:
        pcd_R = max(worst_R if worst_R is not None else -1.0, -1.0)
    else:
        pcd_R = 0.5 * 0.5  # 50% chunk banks +0.5R
        if best_R >= 1.0:
            pcd_R += 0.3 * 1.0  # 30% chunk banks +1R
        else:
            # 30% chunk gets BE lock at +0.5R after T1 hit
            pcd_R += 0.3 * 0.5
        # 20% runner trails -0.5R from peak
        runner_R = max(best_R - 0.5, 0.0)
        pcd_R += 0.2 * runner_R

    total_pcd_R += pcd_R * qty
    gap_rows[-1]['pcd_R'] = pcd_R
    print(f"{p['id']:>4}  {p['submitted_at'][:19]}  {direction:10s}  {qty:>3d}  {p['status']:18s}  "
          f"{entry_spx:>9.2f}  {best_spx:>7.2f}  {best_R:>+6.2f}  "
          f"{final_R:>+11.2f}  {gap_R:>+6.2f}  {gap_dollars_total:>+10.2f}  "
          f"{pcd_R:>+6.2f}")

print(f"\nTOTALS (over {n_with_status} parent orders with non-SUBMITTED status):")
print(f"  Sum estimated final R (R*qty): {total_actual_R_est:+.2f}")
print(f"  Sum dollars left on table (sum gap_R * 500 * qty): {total_gap_dollars:+.2f}")
print(f"  What B+C+D peak-trail would have banked (sum pcd_R * qty): {total_pcd_R:+.2f}")
print(f"  Estimated B+C+D dollars: {total_pcd_R*500:+.2f}")


# =============================================================================
# Section 8: signal_lookahead_outcome aggregate
# =============================================================================
hr("SECTION 8 - SIGNAL_LOOKAHEAD_OUTCOME AGGREGATE FOR ACTIONABLES")
# Group by direction + lookahead_minutes
rows = c.execute("""
SELECT direction_at_signal AS dir, lookahead_minutes AS m,
       COUNT(*) AS n,
       AVG(spx_mfe_pts) AS avg_mfe,
       AVG(spx_mae_pts) AS avg_mae,
       SUM(CASE WHEN would_have_hit_t1 THEN 1 ELSE 0 END) AS t1_hits,
       SUM(CASE WHEN would_have_hit_stop THEN 1 ELSE 0 END) AS stop_hits
FROM signal_lookahead_outcome
WHERE session_date=? AND direction_at_signal != 'WAIT'
GROUP BY direction_at_signal, lookahead_minutes
ORDER BY direction_at_signal, lookahead_minutes
""", (TODAY,)).fetchall()
print(f"{'dir':10s}  {'lookahead':>9s}  {'n':>3s}  {'avg_mfe':>9s}  {'avg_mae':>9s}  {'t1_hits':>7s}  {'t1_pct':>7s}  {'stop_hits':>9s}  {'stop_pct':>8s}")
for r in rows:
    t1p = 100.0 * r['t1_hits'] / r['n'] if r['n'] else 0
    sp = 100.0 * r['stop_hits'] / r['n'] if r['n'] else 0
    amfe = f"{r['avg_mfe']:>+9.2f}" if r['avg_mfe'] is not None else f"{'NULL':>9s}"
    amae = f"{r['avg_mae']:>+9.2f}" if r['avg_mae'] is not None else f"{'NULL':>9s}"
    print(f"{r['dir']:10s}  {r['m']:>9d}  {r['n']:>3d}  {amfe}  {amae}  {r['t1_hits']:>7d}  {t1p:>6.1f}%  {r['stop_hits']:>9d}  {sp:>7.1f}%")
print("\n[ Note: T2/T3 columns require sidecar restart to populate (commit 737386a not active). ]")


# =============================================================================
# Section 9: CEX/VEX context per actionable signal
# =============================================================================
hr("SECTION 9 - CEX/VEX BULLISH-SCORE PER ACTIONABLE SIGNAL")
print("Convention: bullish_score = +net_m if strike < spx, -net_m if strike >= spx")
print()
print(f"{'iter#':>5}  {'spx':>7s}  {'dir':10s}  {'cex_n':>5s}  {'vex_n':>5s}  {'cex_bull':>11s}  {'vex_bull':>11s}")
for r in actionable_rows:
    cex = json.loads(r['top_cex_walls']) if r['top_cex_walls'] else []
    vex = json.loads(r['top_vex_walls']) if r['top_vex_walls'] else []
    spx = r['spx_price']
    def bull(walls):
        s = 0.0
        for w in walls:
            nm = w.get('net_m', 0) or 0
            k = w.get('strike', 0) or 0
            if k < spx:
                s += nm
            else:
                s -= nm
        return s
    cb = bull(cex)
    vb = bull(vex)
    print(f"{r['iter_num']:>5}  {spx:>7.2f}  {r['direction']:10s}  {len(cex):>5d}  {len(vex):>5d}  {cb:>+11.2f}  {vb:>+11.2f}")


# =============================================================================
# Section 10: Audit log breakdown today
# =============================================================================
hr("SECTION 10 - AUDIT LOG BREAKDOWN TODAY")
print("Count by category:")
rows = c.execute("SELECT category, COUNT(*) n FROM audit_log WHERE timestamp LIKE ? GROUP BY category ORDER BY n DESC", (f"{TODAY}%",)).fetchall()
for r in rows:
    print(f"  {r['category']:30s} {r['n']}")

# spx_watcher breakdown by event type
sub("spx_watcher event-type breakdown")
rows = c.execute("SELECT summary FROM audit_log WHERE category='spx_watcher' AND timestamp LIKE ?", (f"{TODAY}%",)).fetchall()
buckets = Counter()
for r in rows:
    s = r['summary'] or ''
    if 'NEAR_STOP' in s: buckets['NEAR_STOP'] += 1
    elif s.startswith('BE LADDER'): buckets['BE_LADDER'] += 1
    elif s.startswith('STOP/TICK'): buckets['STOP_TICK'] += 1
    elif s.startswith('STOP '): buckets['STOP'] += 1
    elif s.startswith('T1 '): buckets['T1_HIT'] += 1
    elif s.startswith('T2 '): buckets['T2_HIT'] += 1
    elif s.startswith('T3 '): buckets['T3_HIT'] += 1
    elif s.startswith('RUNNER'): buckets['RUNNER_PROTECT'] += 1
    elif s.startswith('STARTED'): buckets['STARTED'] += 1
    elif s.startswith('STOPPED'): buckets['STOPPED'] += 1
    else: buckets['other'] += 1
for k, v in buckets.most_common():
    print(f"  {k:20s} {v}")

# BE_LADDER detail
sub("BE_LADDER events detail")
rows = c.execute("SELECT timestamp, summary FROM audit_log WHERE category='spx_watcher' AND summary LIKE 'BE LADDER%' AND timestamp LIKE ? ORDER BY id", (f"{TODAY}%",)).fetchall()
for r in rows:
    print(f"  {r['timestamp']}  {r['summary']}")

# STOP/TICK detail
sub("STOP/TICK events detail (actual closes)")
rows = c.execute("SELECT timestamp, summary FROM audit_log WHERE category='spx_watcher' AND (summary LIKE 'STOP/TICK%' OR summary LIKE 'STOP %') AND timestamp LIKE ? ORDER BY id", (f"{TODAY}%",)).fetchall()
for r in rows:
    print(f"  {r['timestamp']}  {r['summary']}")

# T1 fire detail
sub("T1 fire events detail")
rows = c.execute("SELECT timestamp, summary FROM audit_log WHERE category='spx_watcher' AND summary LIKE 'T1%' AND timestamp LIKE ? ORDER BY id", (f"{TODAY}%",)).fetchall()
for r in rows:
    print(f"  {r['timestamp']}  {r['summary']}")

# RUNNER_PROTECT detail
sub("RUNNER_PROTECT events")
rows = c.execute("SELECT timestamp, summary FROM audit_log WHERE category='spx_watcher' AND summary LIKE 'RUNNER%' AND timestamp LIKE ? ORDER BY id", (f"{TODAY}%",)).fetchall()
for r in rows:
    print(f"  {r['timestamp']}  {r['summary']}")

# auto_trade_block detail
sub("auto_trade_block events")
rows = c.execute("SELECT timestamp, summary, details FROM audit_log WHERE category='auto_trade_block' AND timestamp LIKE ? ORDER BY id", (f"{TODAY}%",)).fetchall()
for r in rows:
    d = json.loads(r['details']) if r['details'] else {}
    print(f"  {r['timestamp']}  {r['summary']}")
    print(f"    reason={d.get('reason')} code={d.get('reason_code')}")

# kill_switch / order_refused
sub("kill_switch / order_refused")
rows = c.execute("SELECT timestamp, category, summary FROM audit_log WHERE category IN ('kill_switch','order_refused') AND timestamp LIKE ? ORDER BY id", (f"{TODAY}%",)).fetchall()
if rows:
    for r in rows:
        print(f"  [{r['category']}] {r['timestamp']}  {r['summary']}")
else:
    print("  (none)")

# Most recent 30 audit rows
sub("Most recent 30 audit_log rows today")
rows = c.execute("SELECT timestamp, category, summary FROM audit_log WHERE timestamp LIKE ? ORDER BY id DESC LIMIT 30", (f"{TODAY}%",)).fetchall()
for r in reversed(rows):
    s = (r['summary'] or '')[:120]
    print(f"  {r['timestamp']}  [{r['category']:18s}] {s}")


# =============================================================================
# Section 11: VIX coverage today
# =============================================================================
hr("SECTION 11 - VIX COVERAGE TODAY")
start_ms = ts_to_epoch_ms(f"{TODAY} 00:00:00")
end_ms = ts_to_epoch_ms(f"{TODAY} 23:59:59")
r = c.execute("SELECT COUNT(*) n, MIN(value) lo, MAX(value) hi, AVG(value) avg FROM vix_sample WHERE timestamp_ms BETWEEN ? AND ?", (start_ms, end_ms)).fetchone()
print(f"  n={r['n']}, min={r['lo']}, max={r['hi']}, avg={r['avg']}")
if r['n'] == 0:
    print("  ** EMPTY ** VIX fallback from QuantData iv_rank (commit 416a849) requires sidecar restart to activate")


# =============================================================================
# Section 12: Pattern matches today
# =============================================================================
hr("SECTION 12 - PATTERN MATCHES TODAY")
rows = c.execute("""
SELECT p.name, COUNT(*) n
FROM pattern_match pm
JOIN pattern p ON p.id = pm.pattern_id
WHERE pm.matched_at LIKE ?
GROUP BY p.name
ORDER BY n DESC
""", (f"{TODAY}%",)).fetchall()
for r in rows:
    print(f"  {r['name']:40s} {r['n']}")
if not rows:
    print("  (none)")


# =============================================================================
# Section 13: v2 new feature capture rate
# =============================================================================
hr("SECTION 13 - V2 FEATURE CAPTURE RATE TODAY")
total = c.execute("SELECT COUNT(*) FROM signal_iter WHERE session_date=?", (TODAY,)).fetchone()[0]
ev2 = c.execute("SELECT COUNT(*) FROM signal_iter WHERE session_date=? AND engine_version=2", (TODAY,)).fetchone()[0]
def cap(field):
    n = c.execute(f"SELECT COUNT(*) FROM signal_iter WHERE session_date=? AND {field} IS NOT NULL AND {field} != '[]' AND {field} != ''", (TODAY,)).fetchone()[0]
    return n
fields = ['top_cex_walls', 'top_vex_walls', 'top_gex_by_expiry', 'top_dex_by_expiry', 'recent_orders', 'top_gex_walls', 'top_dex_walls']
print(f"Total signal_iter rows today: {total}")
print(f"engine_version=2: {ev2}")
for f in fields:
    n = cap(f)
    pct = 100.0 * n / total if total else 0
    print(f"  {f:25s} {n:>3d}  ({pct:5.1f}%)")
print("\nNote: 3-tool wiring fix (commit 3d7dec7) for gex/dex_by_expiry + recent_orders requires sidecar restart")


# =============================================================================
# Section 14: Tournament + walk-forward
# =============================================================================
hr("SECTION 14 - TOURNAMENT + WALK-FORWARD TODAY")
print("Walk-forward runs today:")
rows = c.execute("SELECT * FROM walk_forward_run WHERE run_at LIKE ? ORDER BY id DESC", (f"{TODAY}%",)).fetchall()
for r in rows:
    print(f"  id={r['id']} run_at={r['run_at']}")
    print(f"    window_days={r['window_days']} train_test_split={r['train_test_split']}")
    print(f"    current: wall_prox={r['current_wall_proximity_pts']} wall_collapse_pct={r['current_wall_collapse_pct']} wall_collapse_floor={r['current_wall_collapse_floor_m']} abs_fe={r['current_absorption_fe_ratio_min']}")
    print(f"    optimal: wall_prox={r['optimal_wall_proximity_pts']} wall_collapse_pct={r['optimal_wall_collapse_pct']} wall_collapse_floor={r['optimal_wall_collapse_floor_m']} abs_fe={r['optimal_absorption_fe_ratio_min']}")
    print(f"    optimal_sharpe={r['optimal_sharpe']} total_pnl={r['optimal_total_pnl']} win_rate={r['optimal_win_rate']} drift_score={r['drift_score']}")
    print(f"    notes: {r['notes']}")

print("\nTournament runs today:")
rows = c.execute("SELECT * FROM tournament_run WHERE run_at LIKE ? ORDER BY id DESC", (f"{TODAY}%",)).fetchall()
for r in rows:
    print(f"  id={r['id']} run_at={r['run_at']} preset_count={r['preset_count']} dominant_regime={r['dominant_regime']}")
    if r['regime_distribution']:
        print(f"    regime_distribution: {r['regime_distribution']}")
    # show top result
    top = c.execute("SELECT preset_name, rank, trade_count, win_rate_pct, expectancy_r, sharpe_ratio, cumulative_r FROM tournament_run_result WHERE tournament_run_id=? ORDER BY rank LIMIT 5", (r['id'],)).fetchall()
    for t in top:
        print(f"    rank{t['rank']} {t['preset_name']:25s} n={t['trade_count']:>3d} wr={t['win_rate_pct']:>5.1f}% exp={t['expectancy_r']:+.3f} sharpe={t['sharpe_ratio']:+.3f} cum_R={t['cumulative_r']:+.2f}")


# =============================================================================
# Section 15: Key data gaps detected
# =============================================================================
hr("SECTION 15 - KEY DATA GAPS DETECTED")
gaps = []
if c.execute("SELECT COUNT(*) FROM ibkr_execution WHERE session_date=?", (TODAY,)).fetchone()[0] == 0:
    gaps.append("ibkr_execution: 0 rows despite STOP/TICK closes -> commission/realized_pnl capture broken (pre-restart)")
if c.execute("SELECT COUNT(*) FROM vix_sample WHERE timestamp_ms BETWEEN ? AND ?", (start_ms, end_ms)).fetchone()[0] == 0:
    gaps.append("vix_sample: 0 rows today -> VIX fallback (commit 416a849) not active until sidecar restart")
filled_orders = c.execute("SELECT COUNT(*) FROM live_order WHERE submitted_at LIKE ? AND (filled_qty IS NULL OR filled_qty = 0)", (f"{TODAY}%",)).fetchone()[0]
total_orders = c.execute("SELECT COUNT(*) FROM live_order WHERE submitted_at LIKE ?", (f"{TODAY}%",)).fetchone()[0]
gaps.append(f"live_order: filled_qty=0 on {filled_orders}/{total_orders} rows today -> avg_fill_price never populated (post-restart fix in commit ?)")
gex_e_n = c.execute("SELECT COUNT(*) FROM signal_iter WHERE session_date=? AND top_gex_by_expiry IS NOT NULL AND top_gex_by_expiry != '[]' AND top_gex_by_expiry != ''", (TODAY,)).fetchone()[0]
dex_e_n = c.execute("SELECT COUNT(*) FROM signal_iter WHERE session_date=? AND top_dex_by_expiry IS NOT NULL AND top_dex_by_expiry != '[]' AND top_dex_by_expiry != ''", (TODAY,)).fetchone()[0]
ro_n = c.execute("SELECT COUNT(*) FROM signal_iter WHERE session_date=? AND recent_orders IS NOT NULL AND recent_orders != '[]' AND recent_orders != ''", (TODAY,)).fetchone()[0]
gaps.append(f"signal_iter.top_gex_by_expiry: {gex_e_n}/{total} populated (still 0% -> 3-tool wiring fix in commit 3d7dec7 needs restart)")
gaps.append(f"signal_iter.top_dex_by_expiry: {dex_e_n}/{total} populated (still 0%)")
gaps.append(f"signal_iter.recent_orders: {ro_n}/{total} populated (still 0%)")
# signal_lookahead_outcome t2/t3 columns absent
cols = [row['name'] for row in db.execute("PRAGMA table_info(signal_lookahead_outcome)").fetchall()]
if 'would_have_hit_t2' not in cols:
    gaps.append("signal_lookahead_outcome: no would_have_hit_t2/t3 columns (commit 737386a not applied yet -> sidecar restart needed)")
ds = c.execute("SELECT trades_taken FROM daily_session WHERE session_date=?", (TODAY,)).fetchone()
if ds and ds['trades_taken'] == 0:
    gaps.append(f"daily_session.trades_taken = 0 despite {n_with_status} closed parent orders (counter not updating live)")
# historical bars stop early
last_bar = c.execute("SELECT MAX(timestamp_ms) ts FROM historical_bar WHERE symbol='SPX' AND bar_size='1 min'").fetchone()
if last_bar and last_bar['ts']:
    last_bar_dt = dt.datetime.utcfromtimestamp(last_bar['ts']/1000)
    gaps.append(f"historical_bar(SPX 1 min) latest ts = {last_bar_dt} UTC -> bars not arriving during active session (only ~42 today, all pre-market)")

for g in gaps:
    print(f"  - {g}")

db.close()
print("\nSnapshot complete.")
