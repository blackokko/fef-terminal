"""MAGNITUDE FILTER — AMBIENT CONTROL + well-powered vol-state test  (READ-ONLY spike).

The event-conditional run (magnitude_filter.py) found P(|move30|>=5pt) ~= 0.80-0.84 AT a capitulation
event -> the BIG class is nearly saturated, so the event geometry itself looks like a magnitude
flag.  Two things that run could not settle, answered here:

  (1) AMBIENT CONTROL: is the event's forward magnitude actually ELEVATED vs entering at a RANDOM
      in-window minute?  If a random minute also moves >=5pt in 30min ~80% of the time, the event
      adds nothing.  We strided-sample every 5th in-window bar across all sessions as "any entry",
      measure the same forward |move|, and bootstrap the event-vs-ambient gap (clustered by session),
      DEV and HOLDOUT separately.  THIS is the real "does flagging the setup predict a big move" test.

  (2) WELL-POWERED VOL-STATE TEST: does VIX / realized-vol-so-far predict forward |move| at ALL?
      The event sample is thin and the holdout is a low-vol epoch with almost no VIX dispersion, so
      VIX could not be tested OOS on events.  On the FULL ambient sample (tens of thousands of bars,
      wide DEV VIX range) we can test it with real power, and still show the HOLDOUT (low-vol) result.

  (3) SELL-PREMIUM BRANCH: is there ANY subset (event or ambient/feature) that is reliably CALM
      (P(|move30| < 3pt) high) -> where selling premium would win?  Reported event vs ambient.

Target/ruler identical to magnitude_filter: forward |move| on the INDEPENDENT IBKR SPX 1-min ruler
at +30min from the confirmation close (no lookahead).  Glitch-filtered.

Run:  .venv/Scripts/python.exe -m scripts.research.backtest.magnitude_ambient
"""
from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict

import numpy as np

from . import confluence_expectancy as CE
from . import magnitude_filter as MF
from .holdout import HOLDOUT_START

sys.stdout.reconfigure(encoding="utf-8")

SCRATCH = CE.SCRATCH
DEV_START, HOLD_END = "2022-01-03", "2026-06-26"
STRIDE = 5                 # ambient sample: every 5th in-window bar
BIG_THRS = [5.0, 8.0, 12.0]
SMALL = 3.0                # "calm" threshold for the sell-premium branch
RNG = np.random.default_rng(20260701)
NBOOT = 5000
NBOOT_AMB = 1000


# ============================================================ ambient sample
def build_ambient():
    """Per-session strided in-window entries: date, split, arrays of (vix, rvol, rng, tod, abs30)."""
    cache = CE.build_full_cache()
    prior = CE.load_vix_prior()
    per = []
    for sess in cache:
        d = sess["date"]
        if d < DEV_START or d > HOLD_END:
            continue
        split = "DEV" if d < HOLDOUT_START else "HOLD"
        vp = prior(d) or {}
        vix = vp.get("vix")
        rt, rp, feats = CE.session_features(sess)
        rt = np.asarray(rt, np.int64); rp = np.asarray(rp, float)
        rec = dict(date=d, split=split, vix=(vix if vix is not None else np.nan),
                   rvol=[], rng=[], tod=[], abs30=[])
        for i in range(0, len(rt), STRIDE):
            h = CE._et_h(int(rt[i]))
            if h < CE.WIN_LO or h > CE.WIN_HI:
                continue
            mv = CE.fwd_fav(rt, rp, i, +1, 30)
            if mv is None:
                continue
            rs, rv = MF.sofar(rt, rp, i)
            rec["abs30"].append(abs(mv))
            rec["rvol"].append(rv)
            rec["rng"].append(rs)
            rec["tod"].append((h - 9.5) * 60.0)
        if rec["abs30"]:
            for k in ("rvol", "rng", "tod", "abs30"):
                rec[k] = np.array(rec[k], float)
            per.append(rec)
    return per


# ============================================================ stats
def pool(per, split, key):
    out = []
    for r in per:
        if r["split"] == split:
            out.append(np.asarray(r[key], float))
    return np.concatenate(out) if out else np.array([])


def summ(a):
    a = a[np.isfinite(a)]
    if len(a) == 0:
        return None
    d = dict(n=len(a), mean=float(a.mean()), med=float(np.median(a)),
             small=100.0 * float(np.mean(a < SMALL)))
    for t in BIG_THRS:
        d[f"p{int(t)}"] = 100.0 * float(np.mean(a >= t))
    return d


def boot_gap(ev_per, amb_per, split):
    """Session-block bootstrap of (event - ambient) for E|move30| and P>=5. Clusters by session."""
    ev = {r["date"]: np.asarray(r["abs30"], float) for r in ev_per if r["split"] == split}
    am = {r["date"]: np.asarray(r["abs30"], float) for r in amb_per if r["split"] == split}
    sess = sorted(set(ev) | set(am))
    d_mean = np.empty(NBOOT); d_p5 = np.empty(NBOOT)
    for b in range(NBOOT):
        pick = RNG.choice(sess, size=len(sess), replace=True)
        ea = np.concatenate([ev[s] for s in pick if s in ev]) if any(s in ev for s in pick) else np.array([])
        aa = np.concatenate([am[s] for s in pick if s in am]) if any(s in am for s in pick) else np.array([])
        if len(ea) == 0 or len(aa) == 0:
            d_mean[b] = np.nan; d_p5[b] = np.nan; continue
        d_mean[b] = ea.mean() - aa.mean()
        d_p5[b] = 100.0 * (np.mean(ea >= 5.0) - np.mean(aa >= 5.0))
    d_mean = d_mean[np.isfinite(d_mean)]; d_p5 = d_p5[np.isfinite(d_p5)]
    return (float(np.percentile(d_mean, 2.5)), float(np.percentile(d_mean, 97.5)),
            float(np.mean(d_mean <= 0)),
            float(np.percentile(d_p5, 2.5)), float(np.percentile(d_p5, 97.5)),
            float(np.mean(d_p5 <= 0)))


def amb_spearman(per, split, fkey):
    """Spearman(feature, abs30) on ambient, session-block bootstrap CI/p (cluster=session)."""
    xs, ys, ds = [], [], []
    for r in per:
        if r["split"] != split:
            continue
        if fkey == "vix":
            v = r["vix"]
            if not np.isfinite(v):
                continue
            n = len(r["abs30"])
            xs.append(np.full(n, v)); ys.append(np.asarray(r["abs30"], float))
            ds.append(np.full(n, r["date"], dtype=object))
        else:
            f = np.asarray(r[fkey], float); y = np.asarray(r["abs30"], float)
            m = np.isfinite(f) & np.isfinite(y)
            xs.append(f[m]); ys.append(y[m]); ds.append(np.full(m.sum(), r["date"], dtype=object))
    if not xs:
        return (np.nan,) * 4
    xs = np.concatenate(xs); ys = np.concatenate(ys); ds = np.concatenate(ds)
    rho = MF.spearman(xs, ys)
    sess = np.unique(ds); idx_by = {s: np.where(ds == s)[0] for s in sess}
    boots = np.empty(NBOOT_AMB)
    for b in range(NBOOT_AMB):
        pick = RNG.choice(sess, size=len(sess), replace=True)
        ii = np.concatenate([idx_by[s] for s in pick])
        boots[b] = MF.spearman(xs[ii], ys[ii])
    boots = boots[np.isfinite(boots)]
    lo, hi = np.percentile(boots, [2.5, 97.5])
    p = 2.0 * min(np.mean(boots <= 0), np.mean(boots >= 0))
    return rho, float(lo), float(hi), float(min(1.0, p))


# ============================================================ main
def main():
    print("=" * 118)
    print("MAGNITUDE — AMBIENT CONTROL: does the capitulation EVENT predict elevated forward |move| vs a")
    print("random in-window entry? + well-powered vol-state test on the full ambient sample. Ruler=IBKR SPX.")
    print(f"  target = |move| at +30min.  ambient = every {STRIDE}th in-window bar (09:45-15:00 ET).")
    print("=" * 118)

    events = MF.build_events()
    # reshape events into the same per-session structure for the gap bootstrap
    ev_per = []
    by = defaultdict(lambda: {"abs30": []})
    for e in events:
        if e["abs30"] is not None and np.isfinite(e["abs30"]):
            by[(e["date"], e["split"])]["abs30"].append(e["abs30"])
    for (d, sp), v in by.items():
        ev_per.append(dict(date=d, split=sp, abs30=np.array(v["abs30"], float)))
    amb_per = build_ambient()

    # ---- event vs ambient magnitude ----
    print("\nEVENT vs AMBIENT forward-|move30| distribution (the magnitude 'flag' test):")
    print(f"  {'set':16s} {'split':5s} {'N/sess':>11s} {'E|move|':>8s} {'med':>6s} "
          f"{'P>=5':>6s} {'P>=8':>6s} {'P>=12':>6s} {'P<3(calm)':>9s}")
    ev_n = {}
    for split in ("DEV", "HOLD"):
        for lbl, per in (("EVENT(capfade)", ev_per), ("AMBIENT(random)", amb_per)):
            a = pool(per, split, "abs30")
            s = summ(a)
            ns = len({r["date"] for r in per if r["split"] == split and len(np.atleast_1d(r["abs30"]))})
            if s is None:
                continue
            print(f"  {lbl:16s} {split:5s} {s['n']:6d}/{ns:<4d} {s['mean']:8.2f} {s['med']:6.2f} "
                  f"{s['p5']:5.0f}% {s['p8']:5.0f}% {s['p12']:5.0f}% {s['small']:8.0f}%")
        print()

    print("  EVENT - AMBIENT gap (session-block bootstrap 95% CI; p = one-sided P(gap<=0)):")
    for split in ("DEV", "HOLD"):
        mlo, mhi, mp, plo, phi, pp = boot_gap(ev_per, amb_per, split)
        sig_m = "SIG" if mlo > 0 else "ns"
        sig_p = "SIG" if plo > 0 else "ns"
        print(f"    {split:5s}  dE|move| = [{mlo:+.2f},{mhi:+.2f}]pt p={mp:.3f} {sig_m}   "
              f"dP>=5 = [{plo:+.1f},{phi:+.1f}]pp p={pp:.3f} {sig_p}")

    # ---- well-powered vol-state test on ambient ----
    print("\n" + "=" * 118)
    print("WELL-POWERED VOL-STATE TEST — does VIX / realized-vol-so-far predict forward |move30| at a")
    print("RANDOM entry? (full ambient sample; big N + wide DEV VIX range. HOLD is a low-vol epoch.)")
    print("  Spearman rho(feature, |move30|), session-block bootstrap 95% CI / p:")
    for fkey, prior in (("vix", "+"), ("rvol", "+"), ("rng", "+"), ("tod", "?")):
        for split in ("DEV", "HOLD"):
            rho, lo, hi, p = amb_spearman(amb_per, split, fkey)
            tag = "  *sig*" if (np.isfinite(rho) and (lo > 0 or hi < 0)) else ""
            print(f"    {fkey:5s}[{prior}] {split:5s} rho={rho:+.3f} 95%CI[{lo:+.3f},{hi:+.3f}] p={p:.3f}{tag}")
    # VIX ladder on ambient (E|move30| + P>=5 by prior-VIX bucket, huge N)
    print("\n  AMBIENT E|move30| / P>=5 by PRIOR-VIX bucket (does VIX regime set the magnitude base rate?):")
    for split in ("DEV", "HOLD"):
        cells = []
        for lbl, lo, hi in (("<15", 0, 15), ("15-20", 15, 20), ("20-25", 20, 25),
                            ("25-30", 25, 30), (">=30", 30, 999)):
            vals = []
            for r in amb_per:
                if r["split"] == split and np.isfinite(r["vix"]) and lo <= r["vix"] < hi:
                    vals.append(np.asarray(r["abs30"], float))
            a = np.concatenate(vals) if vals else np.array([])
            a = a[np.isfinite(a)]
            cells.append(f"{lbl}:{(a.mean() if len(a) else float('nan')):4.1f}/"
                         f"{(100*np.mean(a>=5) if len(a) else 0):3.0f}%(n{len(a)})")
        print(f"    {split:5s} " + "  ".join(cells))

    # ---- CSV ----
    path = os.path.join(SCRATCH, "magnitude_ambient_summary.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["set", "split", "n", "mean_absmove30", "median", "p5", "p8", "p12", "p_small3"])
        for split in ("DEV", "HOLD"):
            for lbl, per in (("EVENT", ev_per), ("AMBIENT", amb_per)):
                a = pool(per, split, "abs30"); s = summ(a)
                if s:
                    w.writerow([lbl, split, s["n"], f"{s['mean']:.3f}", f"{s['med']:.3f}",
                                f"{s['p5']:.1f}", f"{s['p8']:.1f}", f"{s['p12']:.1f}", f"{s['small']:.1f}"])
    print(f"\n  summary CSV -> {path}")
    print("\nDONE.")


if __name__ == "__main__":
    main()
