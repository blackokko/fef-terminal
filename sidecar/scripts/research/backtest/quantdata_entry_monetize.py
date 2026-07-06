"""TASK A (confirmatory, AFTER Task B): monetize the QuantData 0DTE surge ENTRIES on
REAL ThetaData option NBBO, and DIAGNOSE whether the take-profits actually fire.

CONTEXT: the DIRECTION of this signal already FAILED on real SPX
(directional_validate_0dte_netdrift: +10min 48.7% hit / +0.087pt favorable). So this
test only QUANTIFIES the loss — any positive P&L is a likely artifact, NOT a green light.

ENTRIES: QuantData 0DTE net-drift surge points, generated EXACTLY as
  directional_validate_0dte_netdrift.py does (build_series 2-min resample; 3-iter ΔDrift;
  surge = |Δ| >= per-session p90; dir = sign(Δ); full RTH 09:30-16:00). Entry minute =
  nearest option-quote minute at/before the surge bucket; strike = ATM (parity spot).
  Overlapping intraday surges are NOT de-duplicated (faithful to the generator) -> total $
  counts EVERY surge as a separate qty=3 trade; read per-CONTRACT as the clean metric.

EXITS (REAL option_quote_history NBBO via exits.py, qty=3 — friction: $1.30 RT + $0.10 slip):
  - be_ladder   R=1.0pt  (stop_pts=1.0, initial_stop=True, dollar_cap=None,
                 runner_protect=True, cut_if_flat_min=20, max_hold_min=90)
                 -> T1=+1.5pt, T2=+2.5pt, T3=+5pt; hard stop -1pt.
  - fixed_dollar d_targets=(300,500,1000), d_stop=-500
                 -> at qty=3 these are WIDE: T1 $300=+$100/ct(~+2pt), T2 $500≈+$200/ct,
                    T3 $1000≈+$333/ct(~+6.7pt); stop -$500=-$167/ct(~-3.3pt).

DIAGNOSTICS (per entry, walking the REAL NBBO premium path entry->EOD — the opportunity):
  - exit-reason breakdown per variant (T-targets / stop / no_move / time / runner / eod).
  - TP reachability: % of entries whose premium MFE ever reaches +$100/$200/$333 per ct
    (the fixed_dollar trigger levels), and fixed_dollar's ACTUAL T1/T2/T3 fire-rates (fills).
  - premium MFE/MAE distribution (median / p90|p10) in $/ct before EOD.
  - favorable SPX excursion vs be_ladder's +1.5pt T1 and -1pt stop.
  - realized entry delta: empirical $ premium change per +1 SPX pt (through-origin regression
    of Δmid on Δspot over the first 15 min) — shows whether the ~+0.09pt SPX lean maps to any
    premium move near the TPs (ATM 0DTE ~ $40-55/ct/pt -> +0.09pt ~ +$4-5/ct, far below T1).

Read-only DB + network-read API. No engine/live code touched.
Run:  .venv/Scripts/python.exe -m scripts.research.backtest.quantdata_entry_monetize [START END]
"""
from __future__ import annotations

import sys

import numpy as np

from .data import BAD, Session, connect
from .directional_validate_0dte_netdrift import (HISTORY_WINDOW, SURGE_Q, _api,
                                                 _post, build_series)
from .exits import SLIP, simulate_be_ladder, simulate_fixed_dollar
from .holdout import HOLDOUT_START

sys.stdout.reconfigure(encoding="utf-8")

QTY = 3
BL_KW = dict(qty=QTY, stop_pts=1.0, initial_stop=True, dollar_cap=None,
             runner_protect=True, cut_if_flat_min=20, max_hold_min=90)
FD_KW = dict(qty=QTY, d_targets=(300.0, 500.0, 1000.0), d_stop=-500.0)
# fixed_dollar per-contract trigger levels at qty=3 (first-target $ / 3 ct, etc.)
REACH = [("$100/ct(~T1,+2pt)", 100.0), ("$200/ct(~T2)", 200.0), ("$333/ct(~T3,+6.7pt)", 1000.0 / 3.0)]


def gen_entries(data):
    """QuantData 0DTE surge entries (bucket_ts_ms, 'C'/'P') — directional_validate parity."""
    series = build_series(data)
    if series is None:
        return []
    bt, bnet, _ = series
    cum = np.cumsum(bnet)
    if len(cum) <= HISTORY_WINDOW:
        return []
    delta = np.full(len(cum), np.nan)
    delta[HISTORY_WINDOW:] = cum[HISTORY_WINDOW:] - cum[:-HISTORY_WINDOW]
    valid = delta[~np.isnan(delta)]
    if len(valid) < 10:
        return []
    p90 = np.percentile(np.abs(valid), SURGE_Q)
    out = []
    for i in range(len(delta)):
        if np.isnan(delta[i]) or abs(delta[i]) < p90:
            continue
        out.append((int(bt[i]), "C" if delta[i] > 0 else "P"))
    return out


def premium_path(sess, ets, right, atm):
    """Walk REAL NBBO entry->EOD. Returns the opportunity diagnostics for one entry."""
    q = sess.quote(ets, atm, right)
    if not q or q[2] in BAD:
        return None
    entry_px = q[1] + SLIP                 # ask+slip cost basis (matches exits._entry)
    if entry_px <= 0.05:
        return None
    mid0 = (q[0] + q[1]) / 2.0
    spot0 = sess.spot(ets)
    sgn = 1.0 if right == "C" else -1.0
    prem_mfe = prem_mae = 0.0              # $/ct excursion vs cost basis (×100 later)
    spot_fav_mfe = spot_fav_mae = 0.0      # favorable SPX-pt excursion (dir-signed)
    sxx = sxy = 0.0                        # through-origin regression accumulators (15 min)
    for ts in sess.minutes:
        if ts <= ets:
            continue
        qq = sess.quote(ts, atm, right)
        if qq is None or qq[2] in BAD:
            continue
        mid = (qq[0] + qq[1]) / 2.0
        dpx = mid - entry_px
        prem_mfe = max(prem_mfe, dpx)
        prem_mae = min(prem_mae, dpx)
        s = sess.spot(ts)
        if s is not None and spot0 is not None:
            fav = sgn * (s - spot0)
            spot_fav_mfe = max(spot_fav_mfe, fav)
            spot_fav_mae = min(spot_fav_mae, fav)
            if ts - ets <= 15 * 60_000:
                x = s - spot0
                sxx += x * x
                sxy += x * (mid - mid0)
    delta_per_pt = (sxy / sxx * 100.0) if sxx > 1.0 else None
    return dict(prem_mfe=prem_mfe * 100.0, prem_mae=prem_mae * 100.0,
                reach=[prem_mfe * 100.0 >= lv for _, lv in REACH],
                spot_fav_mfe=spot_fav_mfe, spot_fav_mae=spot_fav_mae,
                delta=delta_per_pt)


def _fd_tfires(t):
    tags = {f[0] for f in t.get("fills", [])}
    return ("T1" in tags, "T2" in tags, "T3" in tags)


# -------------------------------------------------------------------------- stats
def agg(rr, pnl_key, pc_key):
    if not rr:
        return None
    pc = np.array([r[pc_key] for r in rr], float)
    tot = float(np.sum([r[pnl_key] for r in rr]))
    se = pc.std(ddof=1) / np.sqrt(len(pc)) if len(pc) > 1 else 0.0
    return dict(n=len(rr), total=tot, perct=float(pc.mean()), se=float(se),
                t=(float(pc.mean() / se) if se > 0 else float("nan")),
                win=100.0 * float((pc > 0).mean()))


def line(tag, s):
    if not s:
        print(f"    {tag:18s}   (no trades)")
        return
    print(f"    {tag:18s} n={s['n']:5d}  per-ct ${s['perct']:+7.2f} ±{s['se']:4.2f} "
          f"t={s['t']:+5.2f}  win={s['win']:4.1f}%   TOTAL ${s['total']:+12,.0f}")


GROUPS = [("2025", lambda r: r["year"] == "2025"),
          ("2026", lambda r: r["year"] == "2026"),
          ("ALL", lambda r: True),
          ("DEV<8/13", lambda r: r["split"] == "DEV"),
          ("HOLDOUT", lambda r: r["split"] == "HOLD")]


def reason_mix(rr, key):
    if not rr:
        return {}
    n = len(rr)
    cnt = {}
    for r in rr:
        cnt[r[key]] = cnt.get(r[key], 0) + 1
    return {k: (v, 100.0 * v / n) for k, v in sorted(cnt.items(), key=lambda x: -x[1])}


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute "
        "WHERE session_date BETWEEN ? AND ? ORDER BY session_date", (start, end))]

    print("=" * 100)
    print("TASK A — monetize QuantData 0DTE surge entries on REAL option NBBO  (qty=3)")
    print(f"  overlap {start}..{end}  ({len(dates)} candidate sessions)")
    print(f"  CONTEXT: direction already FAILED on real SPX -> this quantifies the loss only.")
    print(f"  be_ladder {BL_KW}")
    print(f"  fixed_dollar {FD_KW}")
    print("=" * 100)

    recs = []
    n_entries = n_skipped = n_api_fail = 0
    with _api() as c:
        for k, d in enumerate(dates):
            data = _post(c, {"sessionDate": d, "filter": {"ticker": "SPX", "expirationDate": d}})
            if not data:
                n_api_fail += 1
                continue
            entries = gen_entries(data)
            if not entries:
                continue
            sess = Session(con, d)
            if sess.empty():
                continue
            year, split = d[:4], ("DEV" if d < HOLDOUT_START else "HOLD")
            for bt_i, right in entries:
                ets = sess.minute_at_or_before(bt_i)
                if ets is None:
                    n_skipped += 1
                    continue
                atm = sess.atm(ets)
                if atm is None:
                    n_skipped += 1
                    continue
                tb = simulate_be_ladder(sess, ets, right, atm, **BL_KW)
                tf = simulate_fixed_dollar(sess, ets, right, atm, **FD_KW)
                if tb is None or tf is None:
                    n_skipped += 1
                    continue
                pp = premium_path(sess, ets, right, atm)
                t1, t2, t3 = _fd_tfires(tf)
                recs.append(dict(
                    date=d, year=year, split=split,
                    bl_pnl=tb["pnl"], bl_pc=tb["pnl_per_contract"], bl_reason=tb["reason"],
                    fd_pnl=tf["pnl"], fd_pc=tf["pnl_per_contract"], fd_reason=tf["reason"],
                    fd_t1=t1, fd_t2=t2, fd_t3=t3,
                    prem_mfe=(pp["prem_mfe"] if pp else None),
                    prem_mae=(pp["prem_mae"] if pp else None),
                    reach=(pp["reach"] if pp else None),
                    spot_fav_mfe=(pp["spot_fav_mfe"] if pp else None),
                    spot_fav_mae=(pp["spot_fav_mae"] if pp else None),
                    delta=(pp["delta"] if pp else None)))
                n_entries += 1
            if (k + 1) % 25 == 0:
                print(f"  ...{k+1}/{len(dates)} ({d})  entries={n_entries}", flush=True)

    print(f"\n  entries simulated: {n_entries}   skipped: {n_skipped}   api_fail: {n_api_fail}")

    # ---- P&L by group, both variants ----------------------------------------
    for vname, pnl_key, pc_key in (("be_ladder R=1pt", "bl_pnl", "bl_pc"),
                                   ("fixed_dollar 300/500/1000", "fd_pnl", "fd_pc")):
        print("\n" + "=" * 100)
        print(f"P&L — {vname}   (qty=3; per-ct = clean metric, TOTAL = sum over ALL entries)")
        for glabel, gpred in GROUPS:
            line(glabel, agg([r for r in recs if gpred(r)], pnl_key, pc_key))

    # ---- exit-reason breakdown ----------------------------------------------
    print("\n" + "=" * 100)
    print("EXIT-REASON BREAKDOWN  (share of trades resolved by each reason, ALL years)")
    for vname, key in (("be_ladder", "bl_reason"), ("fixed_dollar", "fd_reason")):
        mix = reason_mix(recs, key)
        print(f"  {vname:13s}: " + "  ".join(f"{r}={c}({p:.0f}%)" for r, (c, p) in mix.items()))

    # ---- TP reachability + fixed_dollar actual fire-rates -------------------
    print("\n" + "=" * 100)
    print("TAKE-PROFIT REACHABILITY  (does the REAL premium path ever reach the targets?)")
    have = [r for r in recs if r["reach"] is not None]
    n = len(have)
    if n:
        for j, (lab, _) in enumerate(REACH):
            pct = 100.0 * np.mean([r["reach"][j] for r in have])
            print(f"  premium MFE reaches {lab:22s}: {pct:5.1f}% of entries")
        print(f"  fixed_dollar ACTUAL fires:  "
              f"T1={100.0*np.mean([r['fd_t1'] for r in recs]):.1f}%   "
              f"T2={100.0*np.mean([r['fd_t2'] for r in recs]):.1f}%   "
              f"T3={100.0*np.mean([r['fd_t3'] for r in recs]):.1f}%")
        sfm = np.array([r["spot_fav_mfe"] for r in recs if r["spot_fav_mfe"] is not None], float)
        sfa = np.array([r["spot_fav_mae"] for r in recs if r["spot_fav_mae"] is not None], float)
        print(f"  be_ladder context: favorable SPX move reaches +1.5pt (T1) in "
              f"{100.0*np.mean(sfm >= 1.5):.1f}% ; adverse hits -1.0pt (stop) in "
              f"{100.0*np.mean(sfa <= -1.0):.1f}% of entries")

    # ---- premium MFE/MAE distribution + realized delta ----------------------
    print("\n" + "=" * 100)
    print("PREMIUM EXCURSION (entry->EOD, $/contract) + REALIZED ENTRY DELTA")
    mfe = np.array([r["prem_mfe"] for r in have], float)
    mae = np.array([r["prem_mae"] for r in have], float)
    if len(mfe):
        print(f"  premium MFE $/ct : median {np.median(mfe):+7.1f}   p90 {np.percentile(mfe,90):+7.1f}   "
              f"max {mfe.max():+7.1f}")
        print(f"  premium MAE $/ct : median {np.median(mae):+7.1f}   p10 {np.percentile(mae,10):+7.1f}   "
              f"min {mae.min():+7.1f}")
    deltas = np.array([r["delta"] for r in recs if r["delta"] is not None], float)
    if len(deltas):
        md = float(np.median(deltas))
        print(f"  realized entry delta: median ${md:+.1f}/ct per +1 SPX pt  (n={len(deltas)})")
        print(f"     -> the validated edge (~+0.09pt real SPX) maps to ~${md*0.09:+.1f}/ct of premium;")
        print(f"        fixed_dollar T1 needs +$100/ct (~+{100.0/md:.1f}pt) -> the edge is ~"
              f"{100.0/(md*0.09):.0f}x too small to reach T1 from signal alone.")

    print("\n" + "=" * 100)
    print("READ: TPs that DO fire are paid by premium VARIANCE (noise), not the ~+0.09pt lean;")
    print("net per-ct P&L is the balance of those noise-wins against stops + theta + friction.")
    print("=" * 100)


if __name__ == "__main__":
    main()
