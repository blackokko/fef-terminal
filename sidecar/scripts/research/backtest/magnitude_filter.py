"""MAGNITUDE FILTER — can we predict move SIZE (not direction) after a capitulation event?
(READ-ONLY research spike; engine/live code untouched.)

QUESTION (from the operator): every DIRECTION signal in this project is a coin-flip. But
MAGNITUDE (will the NEXT move be BIG or SMALL) may have structure (vol clusters). If we can flag
at entry "this will be a big move", we route:  big -> buy convexity (long calls) ; small/chop ->
sell premium (credit spreads) ; neither -> sit out.  Build + OOS-test that filter.

EVENT SET  (reused capitulation geometry from confluence_expectancy.cap / blow, but NO VIX pre-gate
  -- VIX is a PREDICTOR under test, and we want more events for power):
    CAPFADE (down): sharp >=15pt down-leg over 5min + velocity-stall + >=3pt bounce off the low,
                    within 6pt of the 30-min extension low.  (dir +1 = fade UP)
    BLOWOFF (up)  : the mirror sharp up-leg exhaustion.                       (dir -1 = fade DOWN)
  Magnitude is direction-agnostic, so we pool CAPFADE+BLOWOFF for the magnitude sample (and also
  report CAPFADE-only as a robustness cut).  Episodes = first bar of each consecutive run
  (CE.episodes), 09:45-15:00 ET.  Cache: conf_cache_full.pkl.  Ruler: INDEPENDENT IBKR SPX 1-min
  (ibkr_ruler) -- events AND forward move both on real SPX, NOT a parity-reconstructed spot
  (see MEMORY [[validate-against-independent-price]]).  No spot-reconstruction artifact.

TARGET  (no lookahead): at each event's CONFIRMATION close rp[i] (== price at bar start +60s),
  the forward move on the ruler at +10 and +30 min:
    abs10/abs30 = |rp[+H] - rp[i]|                 (ABSOLUTE magnitude; ignore sign)
    fav10/fav30 = dir*(rp[+H] - rp[i])             (favorable-direction snapback; reported too)
    rng10/rng30 = max_{i<k<=+H} |rp[k]-rp[i]|      (realized intra-horizon range; convexity-relevant
                                                    SECONDARY -- what a straddle could capture)
  "BIG" = |move| >= 5pt (also report >=8, >=12).  Glitch-filtered (|move|/entry > 5% dropped).

PREDICTORS (entry-moment, causal).  FULL 2022-2026 coverage (OOS-validatable -- prioritized):
    dl_size    the preceding leg SIZE (pt, short-window range)         [prior: +]
    dl_sharp   the leg SHARPNESS (pt/min, extreme->extreme)            [prior: +]
    vix        VIX prior-day close level                               [prior: +]
    backwrd    VIX9D>VIX backwardation (prior close, bool)             [prior: +]
    vix_v3m    VIX - VIX3M prior close (term slope; +=stress)          [prior: +]
    tod_min    minutes from 09:30 ET (open/midday/last-hour)           [prior: open bigger]
    rng_sofar  session hi-lo range so far (pt, ruler)                  [prior: +]
    rvol_sofar session realized 1-min vol so far (pt/min, ruler)       [prior: +]
  DEV-ONLY (coverage ends 2025-08-12 == holdout wall -> CANNOT validate OOS; flagged):
    gex        dealer gamma exposure (option_dealer_exposure)          [prior: -  (long gamma damps)]
    iv_atm     ATM implied vol                                         [prior: +]
    flip_dist  distance to gamma flip                                  [prior: ? ]

METHOD: per feature, E[|move|] and P(BIG) by DEV-quartile bucket (edges from DEV, applied to HOLD),
  + Spearman rho(feature, |move30|) with a SESSION-BLOCK bootstrap 95% CI/p (clusters by session).
  A small, mechanism-motivated set of pre-registered FILTER RULES -> LIFT = P(BIG|rule) vs base rate
  and E[|move||rule] vs unconditional, with coverage (events/sessions) and a session-bootstrap p.
  DEV (2022-01-03..2025-08-12) builds; HOLDOUT (2025-08-13..2026-06-26) confirms — side by side.
  A feature/rule only counts if the lift HOLDS OOS.  Multiple-testing: few features, priors fixed.

Run:  .venv/Scripts/python.exe -m scripts.research.backtest.magnitude_filter
CSVs -> scratchpad.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import sqlite3
import sys
from collections import defaultdict

import numpy as np

from . import confluence_expectancy as CE
from .holdout import HOLDOUT_START

sys.stdout.reconfigure(encoding="utf-8")

SCRATCH = CE.SCRATCH
SIG_DB = CE.SIG_DB
DEV_START, DEV_END = "2022-01-03", "2025-08-12"
HOLD_START, HOLD_END = "2025-08-13", "2026-06-26"
BIG_THRS = [5.0, 8.0, 12.0]          # "BIG |move|" ladders (pt)
BIG_MAIN = 5.0
RNG = np.random.default_rng(20260701)
NBOOT = 5000

# full-coverage (OOS-validatable) numeric features + pre-registered sign prior (+1/-1)
NUM_FEATS = [
    ("dl_size",   +1), ("dl_sharp", +1), ("vix", +1), ("vix_v3m", +1),
    ("tod_min",   -1),   # earlier (smaller minutes-from-open) -> bigger  => prior sign -1
    ("rng_sofar", +1), ("rvol_sofar", +1),
]
DEVONLY_NUM = [("gex", -1), ("iv_atm", +1), ("flip_dist", 0)]   # gex: long gamma damps => -1


# ============================================================ dealer exposure (DEV-only)
def load_dealer():
    """date -> (ts[], gex[], flip_dist[], iv_atm[]) from option_dealer_exposure (DEV-only)."""
    c = sqlite3.connect(f"file:{SIG_DB}?mode=ro", uri=True)
    out = {}
    cur = c.execute("SELECT session_date, ts_ms, gex, flip_dist, iv_atm FROM option_dealer_exposure "
                    "ORDER BY session_date, ts_ms")
    tmp = defaultdict(list)
    for d, ts, gex, fd, iv in cur:
        tmp[d].append((ts, gex, fd, iv))
    for d, rows in tmp.items():
        ts = np.array([r[0] for r in rows], np.int64)
        out[d] = (ts,
                  np.array([r[1] if r[1] is not None else np.nan for r in rows], float),
                  np.array([r[2] if r[2] is not None else np.nan for r in rows], float),
                  np.array([r[3] if r[3] is not None else np.nan for r in rows], float))
    c.close()
    return out


def dealer_at(dz, d, ts, tol=90_000):
    a = dz.get(d)
    if a is None:
        return (np.nan, np.nan, np.nan)
    tsa, gex, fd, iv = a
    j = int(np.searchsorted(tsa, ts))
    best, bd = None, tol + 1
    for c in (j - 1, j):
        if 0 <= c < len(tsa):
            dd = abs(int(tsa[c]) - ts)
            if dd <= tol and dd < bd:
                bd, best = dd, c
    if best is None:
        return (np.nan, np.nan, np.nan)
    return (float(gex[best]), float(fd[best]), float(iv[best]))


# ============================================================ event geometry (reused verbatim)
def cap_geo(f):
    return (f["down_leg"] and f["rng_s"] >= CE.MIN_MOVE and f["vel"] is not None
            and f["vel"] >= -CE.VEL_STALL and f["off_low"] >= CE.BOUNCE_PT
            and f["ext_from_low"] <= CE.EXT_NEAR)


def blow_geo(f):
    return (f["up_leg"] and f["rng_s"] >= CE.MIN_MOVE and f["vel"] is not None
            and f["vel"] <= CE.VEL_STALL and f["off_high"] >= CE.BOUNCE_PT
            and f["ext_from_high"] <= CE.EXT_NEAR)


def leg_sharpness(rt, rp, i):
    """pt/min of the preceding leg inside the 5-min short window (extreme->extreme)."""
    ti = int(rt[i])
    s_lo = int(np.searchsorted(rt, ti - CE.SHORT_MIN * 60_000, "left"))
    seg = rp[s_lo:i + 1]
    seg_ts = rt[s_lo:i + 1]
    if len(seg) < 3:
        return None
    ih = int(np.argmax(seg)); il = int(np.argmin(seg))
    rng = float(seg.max() - seg.min())
    dur_min = abs(int(seg_ts[il]) - int(seg_ts[ih])) / 60000.0
    if dur_min <= 0:
        return None
    return rng / dur_min


def max_abs_exc(rt, rp, i, hmin):
    """max |rp[k]-rp[i]| for i<k<= rt[i]+hmin, glitch-filtered. None if no bars."""
    e = float(rp[i]); hi = int(rt[i]) + hmin * 60_000
    best = None
    k = i + 1
    while k < len(rt) and int(rt[k]) <= hi:
        mv = float(rp[k]) - e
        if abs(mv) <= CE.GLITCH * e:
            best = abs(mv) if best is None else max(best, abs(mv))
        k += 1
    return best


# ============================================================ session-so-far ruler stats
def sofar(rt, rp, i):
    """(range_so_far pt, realized 1-min vol pt/min) using ruler bars from session open .. i."""
    seg = rp[:i + 1]
    if len(seg) < 3:
        return (np.nan, np.nan)
    rng = float(seg.max() - seg.min())
    diffs = np.diff(seg)
    rvol = float(np.std(diffs, ddof=1)) if len(diffs) > 1 else np.nan
    return (rng, rvol)


# ============================================================ build the event table
def build_events():
    cache = CE.build_full_cache()
    prior = CE.load_vix_prior()
    dz = load_dealer()
    print(f"  dealer-exposure sessions (DEV-only): {len(dz)}", flush=True)

    events = []
    for sess in cache:
        d = sess["date"]
        if d < DEV_START or d > HOLD_END:
            continue
        split = "DEV" if d < HOLDOUT_START else "HOLD"
        vp = prior(d) or {}
        vix, v9, v3 = vp.get("vix"), vp.get("vix9d"), vp.get("vix3m")
        rt, rp, feats = CE.session_features(sess)
        rt = np.asarray(rt, np.int64); rp = np.asarray(rp, float)
        for etype, dirn, pred in (("CAP", +1, cap_geo), ("BLOW", -1, blow_geo)):
            for e in CE.episodes(rt, rp, feats, dirn, pred):
                i = e["i"]; ti = int(e["ts"])
                # targets
                abs10 = CE.fwd_fav(rt, rp, i, +1, 10); abs10 = abs(abs10) if abs10 is not None else None
                abs30 = CE.fwd_fav(rt, rp, i, +1, 30); abs30 = abs(abs30) if abs30 is not None else None
                fav10 = CE.fwd_fav(rt, rp, i, dirn, 10)
                fav30 = CE.fwd_fav(rt, rp, i, dirn, 30)
                rng10 = max_abs_exc(rt, rp, i, 10)
                rng30 = max_abs_exc(rt, rp, i, 30)
                # features
                sharp = leg_sharpness(rt, rp, i)
                rs, rv = sofar(rt, rp, i)
                h = CE._et_h(ti); tod = (h - 9.5) * 60.0
                gex, fd, iv = dealer_at(dz, d, ti) if split == "DEV" else (np.nan, np.nan, np.nan)
                events.append(dict(
                    date=d, split=split, ts=ti, etype=etype, dir=dirn,
                    dl_size=float(feats[i]["rng_s"]),
                    dl_sharp=sharp if sharp is not None else np.nan,
                    vix=vix if vix is not None else np.nan,
                    vix9d=v9 if v9 is not None else np.nan,
                    vix3m=v3 if v3 is not None else np.nan,
                    backwrd=(1 if (v9 is not None and vix is not None and v9 > vix) else 0),
                    vix_v3m=((vix - v3) if (vix is not None and v3 is not None) else np.nan),
                    tod_min=tod, rng_sofar=rs, rvol_sofar=rv,
                    gex=gex, flip_dist=fd, iv_atm=iv,
                    abs10=abs10, abs30=abs30, fav10=fav10, fav30=fav30,
                    rng10=rng10, rng30=rng30,
                ))
    return events


# ============================================================ stats helpers
def _vals(recs, key):
    return np.array([r[key] for r in recs if r.get(key) is not None and np.isfinite(r[key])], float)


def base_stats(recs, tkey):
    a = _vals(recs, tkey)
    ns = len({r["date"] for r in recs if r.get(tkey) is not None and np.isfinite(r[tkey])})
    if len(a) == 0:
        return None
    d = dict(n=len(a), nsess=ns, mean=float(a.mean()), median=float(np.median(a)))
    for thr in BIG_THRS:
        d[f"p{int(thr)}"] = 100.0 * float(np.mean(a >= thr))
    return d


def rankdata(a):
    order = np.argsort(a, kind="mergesort")
    r = np.empty(len(a), float)
    r[order] = np.arange(1, len(a) + 1)
    # average ties
    _, inv, cnt = np.unique(a, return_inverse=True, return_counts=True)
    sums = np.zeros(len(cnt)); counts = np.zeros(len(cnt))
    np.add.at(sums, inv, r); np.add.at(counts, inv, 1.0)
    return (sums / counts)[inv]


def spearman(x, y):
    if len(x) < 4:
        return np.nan
    rx, ry = rankdata(x), rankdata(y)
    rx -= rx.mean(); ry -= ry.mean()
    den = np.sqrt((rx * rx).sum() * (ry * ry).sum())
    return float((rx * ry).sum() / den) if den > 0 else np.nan


def paired(recs, fkey, tkey):
    """(feature, target, date) triples where both present/finite."""
    xs, ys, ds = [], [], []
    for r in recs:
        f, t = r.get(fkey), r.get(tkey)
        if f is not None and t is not None and np.isfinite(f) and np.isfinite(t):
            xs.append(float(f)); ys.append(float(t)); ds.append(r["date"])
    return np.array(xs), np.array(ys), np.array(ds)


def session_boot_spearman(xs, ys, ds):
    """Spearman rho + session-block bootstrap 95% CI and two-sided p (cluster=session)."""
    rho = spearman(xs, ys)
    if not np.isfinite(rho):
        return rho, np.nan, np.nan, np.nan
    sess = np.unique(ds)
    idx_by = {s: np.where(ds == s)[0] for s in sess}
    boots = np.empty(NBOOT)
    for b in range(NBOOT):
        pick = RNG.choice(sess, size=len(sess), replace=True)
        ii = np.concatenate([idx_by[s] for s in pick])
        boots[b] = spearman(xs[ii], ys[ii])
    boots = boots[np.isfinite(boots)]
    lo, hi = np.percentile(boots, [2.5, 97.5])
    p = 2.0 * min(np.mean(boots <= 0), np.mean(boots >= 0))
    return rho, float(lo), float(hi), float(min(1.0, p))


# ============================================================ bucketing
def dev_quartile_edges(events, fkey):
    a = _vals([e for e in events if e["split"] == "DEV"], fkey)
    if len(a) < 8:
        return None
    return np.percentile(a, [25, 50, 75])


def bucket_of(v, edges):
    if v is None or not np.isfinite(v) or edges is None:
        return None
    if v <= edges[0]:
        return "Q1"
    if v <= edges[1]:
        return "Q2"
    if v <= edges[2]:
        return "Q3"
    return "Q4"


# ============================================================ filter-rule lift
def rule_lift(events, split, predicate, tkey, big=BIG_MAIN):
    recs = [e for e in events if e["split"] == split]
    base = _vals(recs, tkey)
    if len(base) == 0:
        return None
    kept = [e for e in recs if predicate(e)]
    ka = _vals(kept, tkey)
    ks = len({e["date"] for e in kept if e.get(tkey) is not None and np.isfinite(e[tkey])})
    if len(ka) == 0:
        return dict(n=0, nsess=0, cov=0.0, pbig=np.nan, emove=np.nan,
                    base_pbig=100.0 * float(np.mean(base >= big)), base_emove=float(base.mean()),
                    lift_pbig=np.nan, lift_emove=np.nan, p=np.nan)
    base_pbig = 100.0 * float(np.mean(base >= big)); base_emove = float(base.mean())
    pbig = 100.0 * float(np.mean(ka >= big)); emove = float(ka.mean())
    # session-block bootstrap p on (P(big|rule) - base_pbig)
    ds = np.array([e["date"] for e in recs if e.get(tkey) is not None and np.isfinite(e[tkey])])
    tv = _vals(recs, tkey)
    kf = np.array([1.0 if predicate(e) else 0.0
                   for e in recs if e.get(tkey) is not None and np.isfinite(e[tkey])])
    sess = np.unique(ds); idx_by = {s: np.where(ds == s)[0] for s in sess}
    diffs = np.empty(NBOOT)
    for b in range(NBOOT):
        pick = RNG.choice(sess, size=len(sess), replace=True)
        ii = np.concatenate([idx_by[s] for s in pick])
        m = kf[ii] > 0
        if m.sum() == 0:
            diffs[b] = np.nan; continue
        diffs[b] = 100.0 * np.mean(tv[ii][m] >= big) - 100.0 * np.mean(tv[ii] >= big)
    diffs = diffs[np.isfinite(diffs)]
    p = float(np.mean(diffs <= 0)) if len(diffs) else np.nan   # one-sided: lift>0
    return dict(n=len(ka), nsess=ks, cov=100.0 * len(ka) / len(base),
                pbig=pbig, emove=emove, base_pbig=base_pbig, base_emove=base_emove,
                lift_pbig=pbig - base_pbig, lift_emove=emove - base_emove, p=p)


# ============================================================ main
def main():
    print("=" * 120)
    print("MAGNITUDE FILTER — does anything predict move SIZE (not direction) after a capitulation event?")
    print("  event = CAPFADE(down)/BLOWOFF(up) exhaustion geometry, NO VIX pre-gate; ruler = independent IBKR SPX")
    print(f"  DEV {DEV_START}..{DEV_END}  |  HOLDOUT {HOLD_START}..{HOLD_END}   BIG=|move|>={BIG_MAIN:.0f}pt")
    print("=" * 120)

    events = build_events()
    combined = events
    cap_only = [e for e in events if e["etype"] == "CAP"]

    # ---------- event counts ----------
    def cnt(evs, split):
        rr = [e for e in evs if e["split"] == split]
        return len(rr), len({e["date"] for e in rr})
    print("\nEVENT COUNTS (episodes / sessions):")
    for lbl, evs in (("CAPFADE", cap_only), ("BLOWOFF", [e for e in events if e['etype']=='BLOW']),
                     ("COMBINED", combined)):
        dn, ds = cnt(evs, "DEV"); hn, hs = cnt(evs, "HOLD")
        print(f"  {lbl:9s}  DEV {dn:4d}/{ds:3d}sess   HOLD {hn:4d}/{hs:3d}sess")

    # ---------- base rates ----------
    print("\n" + "=" * 120)
    print("BASE RATES — unconditional forward magnitude at the event (the base rate a filter must beat)")
    print(f"  {'sample':10s} {'split':5s} | {'horizon':7s} {'N/sess':>9s} {'E|move|':>8s} {'med':>6s} "
          f"{'P>=5':>6s} {'P>=8':>6s} {'P>=12':>6s} | {'E[fav]':>7s} {'E[range]':>8s}")
    for lbl, evs in (("CAPFADE", cap_only), ("COMBINED", combined)):
        for split in ("DEV", "HOLD"):
            rr = [e for e in evs if e["split"] == split]
            for hz, ak, fk, rk in (("+10min", "abs10", "fav10", "rng10"),
                                   ("+30min", "abs30", "fav30", "rng30")):
                s = base_stats(rr, ak)
                fmean = _vals(rr, fk).mean() if len(_vals(rr, fk)) else float("nan")
                rmean = _vals(rr, rk).mean() if len(_vals(rr, rk)) else float("nan")
                if s is None:
                    continue
                print(f"  {lbl:10s} {split:5s} | {hz:7s} {s['n']:4d}/{s['nsess']:<4d} "
                      f"{s['mean']:8.2f} {s['median']:6.2f} {s['p5']:5.0f}% {s['p8']:5.0f}% "
                      f"{s['p12']:5.0f}% | {fmean:+7.2f} {rmean:8.2f}")
        print()

    # ---------- feature bucket tables (COMBINED sample, target = abs30) ----------
    TKEY = "abs30"
    print("=" * 120)
    print(f"FEATURE -> MAGNITUDE  (COMBINED sample, target = |move| at +30min; buckets = DEV quartiles)")
    print(f"  per bucket: E|move30| / P(|move|>=5).  Spearman rho(feature,|move30|) w/ session-block boot 95%CI.")
    print("  FULL-COVERAGE (OOS-validatable) FEATURES:")
    feat_rows = []
    for fkey, prior_sign in NUM_FEATS:
        edges = dev_quartile_edges(combined, fkey)
        line = f"  {fkey:11s}[{'+' if prior_sign>0 else '-'}] "
        for split in ("DEV", "HOLD"):
            rr = [e for e in combined if e["split"] == split]
            cells = []
            for q in ("Q1", "Q2", "Q3", "Q4"):
                sub = [e for e in rr if bucket_of(e.get(fkey), edges) == q]
                a = _vals(sub, TKEY)
                if len(a):
                    cells.append(f"{q}:{a.mean():4.1f}/{100*np.mean(a>=BIG_MAIN):3.0f}%({len(a)})")
                else:
                    cells.append(f"{q}:--")
            line += f"{split}[ " + " ".join(cells) + " ]  "
        print(line)
        # spearman on DEV & HOLD
        for split in ("DEV", "HOLD"):
            rr = [e for e in combined if e["split"] == split]
            xs, ys, ds = paired(rr, fkey, TKEY)
            rho, lo, hi, p = session_boot_spearman(xs, ys, ds)
            tag = "" if not np.isfinite(rho) else ("  *OOS-sig*" if (lo > 0 or hi < 0) else "")
            print(f"       {split:4s} rho={rho:+.3f} 95%CI[{lo:+.3f},{hi:+.3f}] p={p:.3f} n={len(xs)}{tag}")
            feat_rows.append((fkey, split, rho, lo, hi, p, len(xs)))
    # boolean + categorical
    print("  BACKWARDATION (VIX9D>VIX prior close):")
    for split in ("DEV", "HOLD"):
        rr = [e for e in combined if e["split"] == split]
        c = []
        for bl in (0, 1):
            a = _vals([e for e in rr if e["backwrd"] == bl], TKEY)
            c.append(f"{'bw' if bl else 'contango'}:{(a.mean() if len(a) else float('nan')):4.1f}/"
                     f"{(100*np.mean(a>=BIG_MAIN) if len(a) else 0):3.0f}%({len(a)})")
        print(f"       {split:4s} " + "   ".join(c))
    print("  TIME-OF-DAY (open<11:00 / midday 11-14 / last 14-15 ET):")
    for split in ("DEV", "HOLD"):
        rr = [e for e in combined if e["split"] == split]
        c = []
        for lbl, lo, hi in (("open", 0, 90), ("mid", 90, 270), ("last", 270, 999)):
            a = _vals([e for e in rr if lo <= e["tod_min"] < hi], TKEY)
            c.append(f"{lbl}:{(a.mean() if len(a) else float('nan')):4.1f}/"
                     f"{(100*np.mean(a>=BIG_MAIN) if len(a) else 0):3.0f}%({len(a)})")
        print(f"       {split:4s} " + "   ".join(c))
    print("  VIX LADDER (prior close):")
    for split in ("DEV", "HOLD"):
        rr = [e for e in combined if e["split"] == split]
        c = []
        for lbl, lo, hi in (("<15", 0, 15), ("15-20", 15, 20), ("20-25", 20, 25),
                            ("25-30", 25, 30), (">=30", 30, 999)):
            a = _vals([e for e in rr if np.isfinite(e["vix"]) and lo <= e["vix"] < hi], TKEY)
            c.append(f"{lbl}:{(a.mean() if len(a) else float('nan')):4.1f}/"
                     f"{(100*np.mean(a>=BIG_MAIN) if len(a) else 0):3.0f}%({len(a)})")
        print(f"       {split:4s} " + "  ".join(c))

    print("\n  DEV-ONLY FEATURES (option_dealer_exposure; coverage ends 2025-08-12 -> CANNOT confirm OOS):")
    for fkey, prior_sign in DEVONLY_NUM:
        edges = dev_quartile_edges(combined, fkey)
        rr = [e for e in combined if e["split"] == "DEV"]
        cells = []
        for q in ("Q1", "Q2", "Q3", "Q4"):
            sub = [e for e in rr if bucket_of(e.get(fkey), edges) == q]
            a = _vals(sub, TKEY)
            cells.append(f"{q}:{(a.mean() if len(a) else float('nan')):4.1f}/"
                         f"{(100*np.mean(a>=BIG_MAIN) if len(a) else 0):3.0f}%({len(a)})" if len(a) else f"{q}:--")
        xs, ys, ds = paired(rr, fkey, TKEY)
        rho, lo, hi, p = session_boot_spearman(xs, ys, ds)
        sgn = "+" if prior_sign > 0 else ("-" if prior_sign < 0 else "?")
        print(f"  {fkey:11s}[{sgn}] DEV[ " + " ".join(cells) + f" ]  rho={rho:+.3f} "
              f"95%CI[{lo:+.3f},{hi:+.3f}] p={p:.3f} n={len(xs)}  (DEV-ONLY, un-validatable)")

    # ---------- filter rules ----------
    print("\n" + "=" * 120)
    print("FILTER RULES — pre-registered, mechanism-motivated (vol-clustering). LIFT vs unconditional base rate.")
    print("  target |move| at +30min; big=>=5pt. Rule keeps events where predicate holds; report P(big|rule),")
    print("  E|move|rule|, coverage (kept ev / kept sess), lift over base, session-bootstrap one-sided p(lift>0).")
    # DEV-derived thresholds for the data-driven cuts (medians/quartiles on DEV only)
    dev = [e for e in combined if e["split"] == "DEV"]
    q = lambda k, pct: float(np.percentile(_vals(dev, k), pct))
    dl_med, dl_q3 = q("dl_size", 50), q("dl_size", 75)
    rv_med = q("rvol_sofar", 50)
    rules = [
        ("dl_size>=median",       lambda e: np.isfinite(e["dl_size"]) and e["dl_size"] >= dl_med),
        ("dl_size>=Q3",           lambda e: np.isfinite(e["dl_size"]) and e["dl_size"] >= dl_q3),
        ("VIX>=20",               lambda e: np.isfinite(e["vix"]) and e["vix"] >= 20),
        ("VIX>=25",               lambda e: np.isfinite(e["vix"]) and e["vix"] >= 25),
        ("first-hour(<11ET)",     lambda e: e["tod_min"] < 90),
        ("rvol_sofar>=median",    lambda e: np.isfinite(e["rvol_sofar"]) and e["rvol_sofar"] >= rv_med),
        ("VIX>=20 & first-hour",  lambda e: np.isfinite(e["vix"]) and e["vix"] >= 20 and e["tod_min"] < 90),
        ("dl_size>=Q3 & VIX>=20", lambda e: np.isfinite(e["dl_size"]) and e["dl_size"] >= dl_q3
                                             and np.isfinite(e["vix"]) and e["vix"] >= 20),
        ("VIX>=25 & first-hour",  lambda e: np.isfinite(e["vix"]) and e["vix"] >= 25 and e["tod_min"] < 90),
    ]
    print(f"  {'rule':26s} | {'DEV: P>=5 (base) lift  E|mv|(base)  cov  p':52s} || "
          f"{'HOLD: P>=5 (base) lift  E|mv|  cov':40s}")
    rule_out = []
    for name, pred in rules:
        dv = rule_lift(combined, "DEV", pred, TKEY)
        hv = rule_lift(combined, "HOLD", pred, TKEY)
        def dcell(s):
            if s is None or s["n"] == 0:
                return f"{'(no events)':>50s}"
            return (f"{s['pbig']:4.0f}% ({s['base_pbig']:3.0f}%) {s['lift_pbig']:+4.0f}  "
                    f"{s['emove']:4.1f}({s['base_emove']:4.1f}) {s['cov']:3.0f}% p={s['p']:.3f}")
        def hcell(s):
            if s is None or s["n"] == 0:
                return f"{'(no events)':>38s}"
            return (f"{s['pbig']:4.0f}% ({s['base_pbig']:3.0f}%) {s['lift_pbig']:+4.0f}  "
                    f"{s['emove']:4.1f} {s['cov']:3.0f}% n={s['n']}")
        print(f"  {name:26s} | {dcell(dv)} || {hcell(hv)}")
        rule_out.append((name, dv, hv))

    # ---------- verdict ----------
    print("\n" + "=" * 120)
    print("VERDICT")
    print("=" * 120)
    # OOS-validatable Spearman survivors
    surv = []
    for fkey, _ in NUM_FEATS:
        rows = {sp: (rho, lo, hi, p) for (fk, sp, rho, lo, hi, p, n) in feat_rows if fk == fkey}
        if "DEV" in rows and "HOLD" in rows:
            rd, ld, hd, pd = rows["DEV"]; rh, lh, hh, ph = rows["HOLD"]
            dev_sig = np.isfinite(rd) and (ld > 0 or hd < 0)
            hold_same = np.isfinite(rh) and np.sign(rh) == np.sign(rd) and (lh > 0 or hh < 0)
            hold_dir = np.isfinite(rh) and np.sign(rh) == np.sign(rd)
            if dev_sig and hold_same:
                surv.append((fkey, "CONFIRMED", rd, rh))
            elif dev_sig and hold_dir:
                surv.append((fkey, "same-sign OOS (CI touches 0)", rd, rh))
            elif dev_sig:
                surv.append((fkey, "DEV-only (dies/flips OOS)", rd, rh))
    print("  Feature rank-correlation with |move30| (OOS-validatable features):")
    if surv:
        for fk, verd, rd, rh in surv:
            print(f"    {fk:12s} DEVrho={rd:+.3f} HOLDrho={rh:+.3f}  -> {verd}")
    else:
        print("    (no DEV feature reached OOS-validatable significance)")
    # best rule by DEV lift that also holds OOS
    print("\n  Filter rules that lift P(BIG) on BOTH DEV and HOLD (coverage>=10 HOLD events):")
    any_rule = False
    for name, dv, hv in rule_out:
        if dv and hv and dv["n"] and hv["n"] >= 10 and dv["lift_pbig"] > 0 and hv["lift_pbig"] > 0:
            any_rule = True
            print(f"    {name:26s} DEV lift {dv['lift_pbig']:+.0f}pt (p={dv['p']:.3f}) | "
                  f"HOLD lift {hv['lift_pbig']:+.0f}pt  cov DEV {dv['cov']:.0f}%/HOLD {hv['cov']:.0f}%")
    if not any_rule:
        print("    NONE — no pre-registered rule lifts P(BIG) on both splits with adequate HOLD coverage.")

    # ---------- CSVs ----------
    ev_csv = os.path.join(SCRATCH, "magnitude_events.csv")
    cols = ["date", "split", "ts", "etype", "dir", "dl_size", "dl_sharp", "vix", "vix9d", "vix3m",
            "backwrd", "vix_v3m", "tod_min", "rng_sofar", "rvol_sofar", "gex", "flip_dist", "iv_atm",
            "abs10", "abs30", "fav10", "fav30", "rng10", "rng30"]
    with open(ev_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for e in events:
            w.writerow(e)
    print(f"\n  events CSV -> {ev_csv}  ({len(events)} rows)")

    fb_csv = os.path.join(SCRATCH, "magnitude_feature_spearman.csv")
    with open(fb_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["feature", "split", "spearman_rho", "ci_lo", "ci_hi", "boot_p", "n"])
        for row in feat_rows:
            w.writerow([row[0], row[1], f"{row[2]:.4f}", f"{row[3]:.4f}", f"{row[4]:.4f}",
                        f"{row[5]:.4f}", row[6]])
    print(f"  spearman CSV -> {fb_csv}")

    rl_csv = os.path.join(SCRATCH, "magnitude_rules.csv")
    with open(rl_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rule", "split", "n", "nsess", "cov_pct", "pbig", "base_pbig", "lift_pbig",
                    "emove", "base_emove", "lift_emove", "boot_p"])
        for name, dv, hv in rule_out:
            for split, s in (("DEV", dv), ("HOLD", hv)):
                if s is None:
                    continue
                w.writerow([name, split, s["n"], s["nsess"], f"{s['cov']:.1f}", f"{s['pbig']:.1f}",
                            f"{s['base_pbig']:.1f}", f"{s['lift_pbig']:.1f}", f"{s['emove']:.2f}",
                            f"{s['base_emove']:.2f}", f"{s['lift_emove']:.2f}", f"{s['p']:.4f}"])
    print(f"  rules CSV -> {rl_csv}")
    print("\nDONE.")


if __name__ == "__main__":
    main()
