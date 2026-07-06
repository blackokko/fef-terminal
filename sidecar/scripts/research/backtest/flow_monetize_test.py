"""MONETIZATION test for the VALIDATED flow-surge directional edge.

THE EDGE (validated elsewhere; reproduced here as the trigger):
  * per session: option_flow_minute ordered by ts_ms; 5-row rolling sum of net_signed_prem.
  * surge = |rolling sum| >= per-session p90 of |rolling sum|.
  * decision points: ET 09:45-15:00, every 3rd minute. dir = sign(rolling sum) -> C/P.
  * entry ATM-at-entry. DIRECTION is robust EVERY year (~54-55% hit, +~1pt favorable move).

THE PROBLEM (confluence_test.py finding):
  Monetized with the FIXED-$ exit (targets 300/500/1000, stop -500 on qty=3) the by-year
  P&L is regime-contingent and NET-NEGATIVE in 2022-2024. Diagnosis: the $ targets need
  ~+2-3pt of SPX to trigger, but the edge is only ~+1pt -> targets too big on cheap (2022)
  options; the move never reaches them, theta wins.

HYPOTHESIS TESTED HERE:
  An exit SCALED to the ~+1pt edge (small SPX-point R-multiple, or small premium-%),
  sized small + fast (cut-if-flat 20m, max-hold 90m vs 0DTE theta), possibly conditioned
  on mid-IV regime, will let the robust signal monetize consistently across ALL years.

WHAT THIS SCRIPT DOES (ONE P&L pass per dataset, identical surge points for all exits):
  1. Reproduces the fixed-$ baseline by-year (apples-to-apples vs confluence_test.py).
  2. Tests simulate_be_ladder R-multiple exits, R in {1.0,1.5,2.0,3.0} (T1=1.5R/T2=2.5R/
     T3=5R, stop R via initial_stop=True, pure-R risk via dollar_cap=None), +cut/maxhold.
  3. Tests a premium-% exit sized to the edge (simulate_prempct +8/15/25%, -30% stop, trail).
  4. Conditions on mid-IV regime. IV source: DEV iv_atm via option_dealer_exposure (2022..
     2025-08); BSM ATM-IV derived from option_quote_history at entry is ALSO computed on
     BOTH dev and holdout (uniform, comparable) and the mid-IV tercile uses the BSM IV.

HOLDOUT DISCIPLINE: dev_range()/holdout_range(). All variant selection on DEV. The holdout
(2025-08-13..2026-06-26) is touched ONCE at the end, for the single chosen config only.

DB opened READ-ONLY. option_dealer_exposure (gex/iv_atm) exists for DEV only.
"""
from __future__ import annotations

import datetime as dt
import sys
from zoneinfo import ZoneInfo

import numpy as np

from .data import Session, connect, sessions
from .exits import simulate_be_ladder, simulate_fixed_dollar, simulate_prempct
from .greeks import implied_vol
from .holdout import assert_dev, dev_range, holdout_range

sys.stdout.reconfigure(encoding="utf-8")
ET = ZoneInfo("America/New_York")

# --- trigger (identical to confluence_test.py) ------------------------------
WIN_LO, WIN_HI = 9.75, 15.0
STEP = 3
ROLL = 5
SURGE_Q = 90

# --- baseline fixed-$ exit (identical to confluence_test.py FD) -------------
FD = dict(qty=3, d_targets=(300.0, 500.0, 1000.0), d_stop=-500.0)

# --- edge-sized R-multiple sweep (be_ladder) --------------------------------
#   initial_stop=True  -> a real stop at R (the func only stops at R when this is set)
#   dollar_cap=None    -> pure-R risk (the -$600 default would muddy "scaled to move")
#   T1=1.5R, T2=2.5R, T3=5R (function constants); fast time mgmt vs 0DTE theta.
R_SWEEP = (1.0, 1.5, 2.0, 3.0)
BL_KW = dict(qty=3, initial_stop=True, dollar_cap=None, runner_protect=True,
             cut_if_flat_min=20, max_hold_min=90)

# --- edge-sized premium-% exit (prempct) ------------------------------------
PP_KW = dict(qty=3, tp_pct=(0.08, 0.15, 0.25), stop_pct=0.30, peak_drawdown=0.20,
             cut_if_flat_min=20, max_hold_min=90)

_YEAR_MIN = 365.0 * 24 * 60


def _et_h(ts_ms):
    x = dt.datetime.fromtimestamp(ts_ms / 1000, ET)
    return x.hour + x.minute / 60.0


def _roll(x, w=ROLL):
    out = np.full(len(x), np.nan)
    c = np.cumsum(x)
    out[w - 1:] = c[w - 1:] - np.concatenate(([0.0], c[:-w]))
    return out


def _atm_iv(sess, ets, atm, right):
    """BSM ATM implied vol at entry from option_quote_history (works dev AND holdout)."""
    mid = sess.mid(ets, atm, right)
    s = sess.spot(ets)
    if mid is None or s is None:
        return None
    now = dt.datetime.fromtimestamp(ets / 1000, ET)
    expiry = now.replace(hour=16, minute=0, second=0, microsecond=0)
    mins = (expiry - now).total_seconds() / 60.0
    if mins <= 0:
        return None
    return implied_vol(mid, s, atm, mins / _YEAR_MIN, right)


# --------------------------------------------------------------------------- build
def build(con, start, end, want_exposure):
    """ONE pass over [start,end]. At each surge point, base-direction only:
       fixed-$ baseline + be_ladder(R-sweep) + prempct, plus BSM IV (+ dev table IV)."""
    recs = []
    dates = sessions(con, start, end)
    for di, d in enumerate(dates):
        rows = con.execute(
            "SELECT ts_ms, net_signed_prem FROM option_flow_minute "
            "WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
        n = len(rows)
        if n < 20:
            continue
        ts = np.array([r[0] for r in rows], dtype=np.int64)
        net = np.array([r[1] for r in rows], float)
        roll_net = _roll(net)
        absnet = np.abs(roll_net)
        if np.isnan(absnet[ROLL - 1:]).all() or len(absnet) - (ROLL - 1) < 10:
            continue
        p90 = np.percentile(absnet[ROLL - 1:], SURGE_Q)

        iv_tbl = {}
        if want_exposure:
            for r in con.execute("SELECT ts_ms, iv_atm FROM option_dealer_exposure "
                                 "WHERE session_date=? ORDER BY ts_ms", (d,)):
                iv_tbl[r[0]] = r[1]

        sess = None
        wcount = -1
        for i in range(ROLL - 1, n):
            h = _et_h(int(ts[i]))
            if h < WIN_LO or h > WIN_HI:
                continue
            wcount += 1
            if wcount % STEP != 0:
                continue
            if absnet[i] < p90:
                continue

            if sess is None:
                sess = Session(con, d)
                if sess.empty():
                    sess = False
            if not sess:
                break
            ets = sess.minute_at_or_before(int(ts[i]))
            if ets is None:
                continue
            atm = sess.atm(ets)
            if atm is None:
                continue
            base_dir = "C" if roll_net[i] > 0 else "P"

            # baseline inclusion gate identical to confluence (require BOTH C & P fillable)
            tc = simulate_fixed_dollar(sess, ets, "C", atm, **FD)
            tp = simulate_fixed_dollar(sess, ets, "P", atm, **FD)
            if tc is None or tp is None:
                continue
            base_pnl = (tc["pnl"] if base_dir == "C" else tp["pnl"]) / 3.0

            bl = {}
            for Rv in R_SWEEP:
                t = simulate_be_ladder(sess, ets, base_dir, atm, stop_pts=Rv, **BL_KW)
                bl[Rv] = (None if t is None else t["pnl_per_contract"],
                          None if t is None else t["reason"])
            tpp = simulate_prempct(sess, ets, base_dir, atm, **PP_KW)
            pp = (None if tpp is None else tpp["pnl_per_contract"],
                  None if tpp is None else tpp["reason"])

            bsm_iv = _atm_iv(sess, ets, atm, base_dir)
            recs.append({
                "date": d, "year": d[:4], "h": h, "dir": base_dir,
                "base_pnl": base_pnl, "bl": bl, "pp": pp,
                "bsm_iv": bsm_iv, "tbl_iv": iv_tbl.get(int(ts[i])),
            })
        if (di + 1) % 200 == 0:
            print(f"    ...{di+1}/{len(dates)} sessions ({d}), {len(recs)} surge pts",
                  flush=True)
    return recs


# -------------------------------------------------------------------------- stats
def st(pnls):
    a = np.array([p for p in pnls if p is not None], float)
    n = len(a)
    if n == 0:
        return None
    se = a.std(ddof=1) / np.sqrt(n) if n > 1 else 0.0
    t = a.mean() / se if se > 0 else float("nan")
    return {"n": n, "mean": float(a.mean()), "se": float(se),
            "t": float(t), "win": 100.0 * (a > 0).mean()}


def line(tag, s, base=None):
    if not s:
        print(f"    {tag:24s}     (no trades)")
        return
    d = f"   Δ={s['mean'] - base['mean']:+6.2f}" if base else ""
    print(f"    {tag:24s} n={s['n']:5d}   ${s['mean']:+7.2f}/ct ±{s['se']:4.2f}  "
          f"t={s['t']:+5.2f}  win={s['win']:4.1f}%{d}")


def by_year(recs, getter):
    """{year -> st(...)} for a per-record pnl getter; plus 'ALL'."""
    out = {}
    for y in sorted({r["year"] for r in recs}):
        out[y] = st([getter(r) for r in recs if r["year"] == y])
    out["ALL"] = st([getter(r) for r in recs])
    return out


def show_by_year(label, recs, getter):
    res = by_year(recs, getter)
    for y in [k for k in res if k != "ALL"]:
        line(f"  {label} {y}", res[y])
    line(f"  {label} ALL", res["ALL"])
    return res


def all_dev_years_positive(res):
    return all(res[y] and res[y]["mean"] > 0 for y in res if y != "ALL")


# --------------------------------------------------------------------------- main
def main():
    con = connect()
    ds, de = dev_range()
    assert_dev(de)
    hs, he = holdout_range()
    print("FLOW-SURGE MONETIZATION TEST — edge-sized exits vs the fixed-$ baseline")
    print(f"  DEV {ds}..{de}   |   HOLDOUT {hs}..{he}")
    print(f"  trigger: surge=p{SURGE_Q} |5min Σnet_signed_prem|, ET {WIN_LO:.2f}-{WIN_HI:.2f} "
          f"every {STEP}rd min, dir=sign(roll), ATM-at-entry")
    print(f"  baseline exit: fixed_dollar {FD}")
    print(f"  be_ladder sweep R={R_SWEEP}  kw={BL_KW}")
    print(f"  prempct: {PP_KW}\n")

    print("  building DEV records (fixed-$ C&P gate + be_ladder sweep + prempct + IV)...",
          flush=True)
    dev = build(con, ds, de, want_exposure=True)
    print(f"  DEV surge points: {len(dev)}\n", flush=True)

    # =====================================================================
    # 1) BASELINE reproduction (must match confluence_test.py)
    # =====================================================================
    print("=" * 84)
    print("1) FIXED-$ BASELINE  (reproduce confluence_test.py by-year)")
    base_res = show_by_year("dev", dev, lambda r: r["base_pnl"])
    print("    (confluence reported: DEV net-NEGATIVE, only positive from 2025; holdout +$25)")

    # =====================================================================
    # 2) edge-sized R-multiple (be_ladder) sweep — DEV ONLY for selection
    # =====================================================================
    print("\n" + "=" * 84)
    print("2) be_ladder R-MULTIPLE SWEEP  (T1=1.5R T2=2.5R T3=5R, stop R, pure-R, fast mgmt) — DEV")
    bl_res = {}
    for Rv in R_SWEEP:
        print(f"\n  --- R = {Rv} pt  (T1=+{1.5*Rv:.1f}pt, stop=-{Rv:.1f}pt) ---")
        res = show_by_year(f"R{Rv}", dev, lambda r, Rv=Rv: r["bl"][Rv][0])
        bl_res[Rv] = res
        print(f"        -> all dev years positive? {all_dev_years_positive(res)}")
        # exit-reason mix for context
        reasons = [r["bl"][Rv][1] for r in dev if r["bl"][Rv][1] is not None]
        if reasons:
            mix = {k: round(100 * reasons.count(k) / len(reasons)) for k in sorted(set(reasons))}
            print(f"        exit-reason mix: {mix}")

    # =====================================================================
    # 3) edge-sized premium-% (prempct) — DEV ONLY
    # =====================================================================
    print("\n" + "=" * 84)
    print(f"3) prempct PREMIUM-% LADDER  (tp={PP_KW['tp_pct']}, stop -{PP_KW['stop_pct']:.0%}, "
          f"trail {PP_KW['peak_drawdown']:.0%}) — DEV")
    pp_res = show_by_year("pp", dev, lambda r: r["pp"][0])
    print(f"        -> all dev years positive? {all_dev_years_positive(pp_res)}")
    reasons = [r["pp"][1] for r in dev if r["pp"][1] is not None]
    if reasons:
        mix = {k: round(100 * reasons.count(k) / len(reasons)) for k in sorted(set(reasons))}
        print(f"        exit-reason mix: {mix}")

    # =====================================================================
    # 4) mid-IV conditioning (DEV). BSM IV terciles (uniform); cross-check table IV.
    # =====================================================================
    print("\n" + "=" * 84)
    print("4) MID-IV CONDITIONING (DEV) — BSM ATM-IV terciles at entry")
    ivs = np.array([r["bsm_iv"] for r in dev if r["bsm_iv"] is not None], float)
    iv_lo = iv_hi = None
    mid_filter = lambda r: False
    if len(ivs) > 50:
        iv_lo, iv_hi = np.percentile(ivs, [33.3, 66.7])
        mid_filter = lambda r: r["bsm_iv"] is not None and iv_lo <= r["bsm_iv"] < iv_hi
        cov = 100.0 * np.mean([r["bsm_iv"] is not None for r in dev])
        print(f"    BSM IV terciles: low<{iv_lo:.3f}  mid[{iv_lo:.3f},{iv_hi:.3f})  high>={iv_hi:.3f}"
              f"   (IV available on {cov:.0f}% of surge pts)")
        # cross-check vs dealer-exposure table IV where both present
        both = [(r["bsm_iv"], r["tbl_iv"]) for r in dev if r["bsm_iv"] and r["tbl_iv"]]
        if len(both) > 30:
            a = np.array(both, float)
            cc = np.corrcoef(a[:, 0], a[:, 1])[0, 1]
            print(f"    sanity: corr(BSM IV, dealer-exposure iv_atm) = {cc:+.2f}  (n={len(both)})")
    else:
        print("    insufficient BSM IV -> skipping IV conditioning")

    # apply mid-IV to baseline + each be_ladder R + prempct, DEV by-year
    def show_mid(label, getter):
        sub = [r for r in dev if mid_filter(r)]
        print(f"\n  --- mid-IV subset, {label} ---")
        res = show_by_year(f"{label}|midIV", sub, getter)
        print(f"        -> all dev years positive (mid-IV)? {all_dev_years_positive(res)}")
        return res

    mid_results = {}
    if iv_lo is not None:
        mid_results["baseline"] = show_mid("baseline", lambda r: r["base_pnl"])
        for Rv in R_SWEEP:
            mid_results[f"R{Rv}"] = show_mid(f"be_ladder R{Rv}", lambda r, Rv=Rv: r["bl"][Rv][0])
        mid_results["prempct"] = show_mid("prempct", lambda r: r["pp"][0])

    # =====================================================================
    # 5) PICK the single best DEV config, then touch HOLDOUT ONCE.
    # =====================================================================
    print("\n" + "=" * 84)
    print("5) DEV SELECTION SUMMARY")
    print("   Objective = monetize CONSISTENTLY across ALL years -> primary key is the")
    print("   WORST dev-year mean (robustness), tie-broken by pooled t. (Pooled mean/t")
    print("   alone would over-reward a config that just concentrates on good years.)")

    def worst_year(res):
        ys = [res[y]["mean"] for y in res if y != "ALL" and res[y]]
        return min(ys) if ys else -1e9

    candidates = []  # (name, res_dict, getter, mid_only)
    candidates.append(("fixed_dollar (baseline)", base_res, lambda r: r["base_pnl"], False))
    for Rv in R_SWEEP:
        candidates.append((f"be_ladder R{Rv}", bl_res[Rv], (lambda r, Rv=Rv: r["bl"][Rv][0]), False))
    candidates.append(("prempct", pp_res, lambda r: r["pp"][0], False))
    if iv_lo is not None:
        for nm, res in mid_results.items():
            getter = (lambda r: r["base_pnl"]) if nm == "baseline" else (
                (lambda r: r["pp"][0]) if nm == "prempct" else
                (lambda r, Rv=float(nm[1:]): r["bl"][Rv][0]))
            candidates.append((nm + " |midIV", res, getter, True))

    print(f"\n  {'config':30s} {'allYrs+':>8} {'worstYr':>9} {'ALL $/ct':>10} {'t':>6} {'n':>6}")
    for nm, res, _, _ in candidates:
        s = res["ALL"]
        if s:
            print(f"  {nm:30s} {str(all_dev_years_positive(res)):>8} {worst_year(res):>+9.2f} "
                  f"{s['mean']:>+10.2f} {s['t']:>+6.2f} {s['n']:>6d}")

    # choose by worst-year robustness (primary), pooled t (tie-break).
    chosen = max(candidates, key=lambda c: (worst_year(c[1]), c[1]["ALL"]["t"]))
    cname, cres, cgetter, cmid = chosen
    cstat, cpos = cres["ALL"], all_dev_years_positive(cres)
    print(f"\n  CHOSEN (DEV, most consistent): {cname}   worstYr ${worst_year(cres):+.2f}  "
          f"ALL ${cstat['mean']:+.2f}/ct  t={cstat['t']:+.2f}  n={cstat['n']}  all-yrs+={cpos}")

    # =====================================================================
    # HOLDOUT — touched ONCE, chosen config only.
    # =====================================================================
    print("\n" + "=" * 84)
    print("6) HOLDOUT CONFIRMATION  (touched ONCE; chosen config only)")
    print("  building HOLDOUT records (flow only; BSM IV)...", flush=True)
    hold = build(con, hs, he, want_exposure=False)
    print(f"  HOLDOUT surge points: {len(hold)}\n", flush=True)

    # holdout mid-IV terciles from the HOLDOUT's own BSM IV (do not reuse dev thresholds)
    h_filter = (lambda r: True)
    if cmid:
        hivs = np.array([r["bsm_iv"] for r in hold if r["bsm_iv"] is not None], float)
        if len(hivs) > 30:
            h_lo, h_hi = np.percentile(hivs, [33.3, 66.7])
            h_filter = lambda r: r["bsm_iv"] is not None and h_lo <= r["bsm_iv"] < h_hi
            print(f"  holdout BSM IV mid tercile: [{h_lo:.3f}, {h_hi:.3f})")

    hsub = [r for r in hold if h_filter(r)]
    print(f"\n  --- HOLDOUT, chosen config: {cname} ---")
    h_res = show_by_year("hold", hsub, cgetter)

    # also show the chosen config under a mid-IV filter (informative; the holdout has no
    # 2022-style bear regime so it cannot confirm the dev 2022 weakness either way).
    hmid_lo, hmid_hi = np.percentile(
        np.array([r["bsm_iv"] for r in hold if r["bsm_iv"] is not None], float), [33.3, 66.7])
    hmid = [r for r in hold if r["bsm_iv"] is not None and hmid_lo <= r["bsm_iv"] < hmid_hi]
    print(f"\n  --- HOLDOUT, chosen config + mid-IV [{hmid_lo:.3f},{hmid_hi:.3f}) (secondary) ---")
    h_res_mid = show_by_year("hold|midIV", hmid, cgetter)

    # baseline holdout for reference (always show, fixed-$ all-IV)
    print("\n  --- HOLDOUT reference: fixed-$ baseline (all-IV) ---")
    show_by_year("hold-base", hold, lambda r: r["base_pnl"])

    print("\n" + "=" * 84)
    print("HEADLINE")
    print(f"  baseline DEV all-years-positive? {all_dev_years_positive(base_res)}  "
          f"(ALL ${base_res['ALL']['mean']:+.2f}/ct)")
    print(f"  chosen   DEV all-years-positive? {cpos}  (ALL ${cstat['mean']:+.2f}/ct, t={cstat['t']:+.2f})")
    if h_res.get("ALL"):
        print(f"  chosen   HOLDOUT (all-IV) ALL ${h_res['ALL']['mean']:+.2f}/ct  t={h_res['ALL']['t']:+.2f}  "
              f"n={h_res['ALL']['n']}  win={h_res['ALL']['win']:.1f}%")
    if h_res_mid.get("ALL"):
        print(f"  chosen   HOLDOUT (mid-IV) ALL ${h_res_mid['ALL']['mean']:+.2f}/ct  t={h_res_mid['ALL']['t']:+.2f}  "
              f"n={h_res_mid['ALL']['n']}  win={h_res_mid['ALL']['win']:.1f}%")
    print("=" * 84)


if __name__ == "__main__":
    main()
