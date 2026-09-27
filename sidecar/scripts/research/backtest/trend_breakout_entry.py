"""TREND-CONTINUATION / BREAKOUT ENTRY — is there a FLOW-INDEPENDENT, price-only
directional ENTRY edge for intraday SPX 0DTE, and does it BEAT flow direction?  (READ-ONLY)

Motivation (labeled example 2026-07-01): market ripped ~+20pt into an 11:00 ET breakout
while every FLOW signal was WRONG (drift_bias BEARISH, aggression ~39%C/53%P) the whole
up-move.  The move lived only in PRICE: higher-lows off the 10:44 low, price holding above
the short EMAs, then a range-break above ~7497 with sustained positive velocity.  Claim:
trend/continuation DIRECTION must come from price-structure + momentum, not flow.

RULER / DATA (holdout-safe by construction — the phase_cache is built ONLY on the DEV set
2022-01-03..2025-08-12; the reserved holdout 2025-08-13+ is never read here):
  reuse $TEMP/phase_cache.pkl from the phase harness.  Each session = the INDEPENDENT IBKR
  SPX 1-min close ruler (rt,rp) PLUS aligned 1-min flow proxies (drift_bias, call_pct,
  put_pct) on every bar.  1-min granularity, price-only history.  No historical tick
  microstructure (OFI/microprice/book are live-only from 2026-07-01) -> OFI-confirmation is
  a live-only future enhancement, NOT backtestable here.  Bars verified contiguous 1-min
  (index == minute), so entry-side rolling windows use bar indices; forward moves use a
  time-based nearest lookup (real_at, 120s tol, 5% glitch filter).

SIGNAL (LONG; SHORT is the mirror).  Entry at bar i (fresh rising-edge + per-dir cooldown):
  TRIGGER   break above trailing M-min high  AND  velocity_3min >= V pt  AND  2 consecutive
            up bars (persistence, not a 1-bar spike).
  FILTER    one of {none | ema_hl | orh | union}:
              ema_hl = price above a RISING short EMA (span S) AND higher-lows (N-min low >
                       prior N-min low)
              orh    = price above the opening-range high (first OR_MIN min)
              union  = ema_hl OR orh                 (as literally specified in the brief)
              none   = trigger only (control: does the trend filter add anything?)

MEASURE (pooled + by-year, session-clustered t): forward favorable pt at +10/+30min; a
  TRAILING-exit variant (give back W pt from peak, EOD cap) for W in {5,10}; MFE-to-EOD (the
  best the leg offered); hit%, mean, median; TAIL %>15pt / %>30pt (does it catch big legs?).

HEAD-TO-HEAD vs FLOW (the money comparison): at each breakout entry, what did drift_bias /
  aggression say, and what is the forward edge of FOLLOW-PRICE-BREAKOUT vs FOLLOW-FLOW at the
  SAME moments; how often was flow contrarian during WINNING breakouts.

REGIME: trend vs chop split (session efficiency-ratio & realized-range terciles) + a CAUSAL
  trailing-choppiness filter test (can a live chop filter rescue it?).

Run:  .venv/Scripts/python.exe -m scripts.research.backtest.trend_breakout_entry
Outputs CSVs to $TEMP scratchpad.  Engine/live code untouched.
"""
from __future__ import annotations

import datetime as dt
import itertools
import os
import pickle
import sys
from zoneinfo import ZoneInfo

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

sys.stdout.reconfigure(encoding="utf-8")
ET = ZoneInfo("America/New_York")
SCRATCH = (r"D:\fef-data")
CACHE_PKL = os.path.join(os.environ.get("TEMP", "."), "phase_cache.pkl")

# ---- fidelity constants (match the rest of the harness) --------------------------
SANE_LO, SANE_HI = 3000.0, 9000.0
MOVE_GLITCH_FRAC = 0.05
TOL_MS = 120_000
WIN_LO, WIN_HI = 9.8333, 15.5      # entries 09:50..15:30 ET (2N history done; +30 exists)
COOLDOWN_MS = 10 * 60_000          # <=1 entry per direction per 10 min (fresh-edge gated too)
VEL_WIN = 3                        # velocity lookback (min)
HORIZONS = [10, 30]
YEARS = ["2022", "2023", "2024", "2025"]

# ---- signal grid -----------------------------------------------------------------
MODES = ["none", "ema_hl", "orh", "union"]
GRID_M = [10, 15, 20]              # trailing-high breakout lookback (min)
GRID_V = [3.0, 5.0, 8.0]          # 3-min velocity threshold (pt)
DEF_SPAN = 20                      # EMA span (main grid); swept in sensitivity
DEF_N = 10                         # higher-low structure window (min)
DEF_OR = 15                        # opening-range window (min)
TRAIL_W = [5.0, 10.0]             # trailing give-back variants (pt)


def _et_h(ts_ms: int) -> float:
    x = dt.datetime.fromtimestamp(ts_ms / 1000, ET)
    return x.hour + x.minute / 60.0


# ============================================================ numpy signal primitives
def _ema(x: np.ndarray, span: int) -> np.ndarray:
    a = 2.0 / (span + 1.0)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1.0 - a) * out[i - 1]
    return out


def _roll_prior_max(x: np.ndarray, m: int) -> np.ndarray:
    """max over the m bars PRIOR to (excluding) each bar i; NaN before enough history."""
    n = len(x)
    out = np.full(n, np.nan)
    if n > m:
        sw = sliding_window_view(x, m).max(1)        # sw[j] = max(x[j:j+m])
        out[m:] = sw[:n - m]                          # out[i] = max(x[i-m:i])
    return out


def _roll_prior_min(x: np.ndarray, m: int) -> np.ndarray:
    n = len(x)
    out = np.full(n, np.nan)
    if n > m:
        sw = sliding_window_view(x, m).min(1)
        out[m:] = sw[:n - m]
    return out


def _roll_win_min(x: np.ndarray, w: int) -> np.ndarray:
    """min over the w bars ENDING at (including) each bar i."""
    n = len(x)
    out = np.full(n, np.nan)
    if n >= w:
        sw = sliding_window_view(x, w).min(1)        # sw[j] = min(x[j:j+w]) ends at j+w-1
        out[w - 1:] = sw
    return out


def _roll_win_max(x: np.ndarray, w: int) -> np.ndarray:
    n = len(x)
    out = np.full(n, np.nan)
    if n >= w:
        sw = sliding_window_view(x, w).max(1)
        out[w - 1:] = sw
    return out


def _nearest(ts: np.ndarray, target: int):
    if len(ts) == 0:
        return None
    j = int(np.searchsorted(ts, target))
    best, bd = None, TOL_MS + 1
    for c in (j - 1, j):
        if 0 <= c < len(ts):
            dd = abs(int(ts[c]) - target)
            if dd <= TOL_MS and dd < bd:
                bd, best = dd, c
    return best


def real_at(rt, rp, target):
    idx = _nearest(rt, target)
    if idx is None:
        return None
    s = float(rp[idx])
    return s if SANE_LO <= s <= SANE_HI else None


# ============================================================ per-session precompute
def precompute(s: dict) -> dict:
    rt = np.asarray(s["rt"], np.int64)
    rp = np.asarray(s["rp"], float)
    n = len(rp)
    smp = s["samples"]
    drift = np.array([{"BULLISH": 1, "BEARISH": -1}.get(x.drift_bias, 0) for x in smp], int)
    call_pct = np.array([x.call_pct if x.call_pct is not None else np.nan for x in smp], float)
    # aggression dir: call% > put% -> +1 (put% = 100-call%)
    aggr = np.where(np.isnan(call_pct), 0, np.where(call_pct > 50.0, 1, np.where(call_pct < 50.0, -1, 0)))

    vel = np.full(n, np.nan)
    vel[VEL_WIN:] = rp[VEL_WIN:] - rp[:-VEL_WIN]
    up2 = np.zeros(n, bool)
    dn2 = np.zeros(n, bool)
    up2[2:] = (rp[2:] > rp[1:-1]) & (rp[1:-1] > rp[:-2])
    dn2[2:] = (rp[2:] < rp[1:-1]) & (rp[1:-1] < rp[:-2])

    feat = dict(date=s["date"], year=s["year"], rt=rt, rp=rp, n=n,
                drift=drift, aggr=aggr, vel=vel, up2=up2, dn2=dn2, fwd={})
    for m in GRID_M:
        feat[f"pmax{m}"] = _roll_prior_max(rp, m)
        feat[f"pmin{m}"] = _roll_prior_min(rp, m)
    for span in (15, 20, 30):
        e = _ema(rp, span)
        er = np.zeros(n, bool); ef = np.zeros(n, bool)
        er[3:] = (e[3:] > e[:-3]) & (rp[3:] > e[3:])          # rising EMA & price above
        ef[3:] = (e[3:] < e[:-3]) & (rp[3:] < e[3:])          # falling EMA & price below
        feat[f"emaR{span}"] = er
        feat[f"emaF{span}"] = ef
    for nn in (8, 10, 15):
        cmin = _roll_win_min(rp, nn)                          # curr N-min low (ends at i)
        cmax = _roll_win_max(rp, nn)
        hl = np.zeros(n, bool); lh = np.zeros(n, bool)
        hl[2 * nn - 1:] = cmin[2 * nn - 1:] > cmin[nn - 1:n - nn]     # higher-low
        lh[2 * nn - 1:] = cmax[2 * nn - 1:] < cmax[nn - 1:n - nn]     # lower-high
        feat[f"hl{nn}"] = hl
        feat[f"lh{nn}"] = lh
    # opening ranges (first OR_MIN bars from the 09:30 open == index 0 for full days)
    for orm in (15, 30):
        seg = rp[:orm]
        feat[f"orh{orm}"] = float(seg.max()) if len(seg) >= orm // 2 else np.inf
        feat[f"orl{orm}"] = float(seg.min()) if len(seg) >= orm // 2 else -np.inf

    # session regime tags (DIAGNOSTIC, non-causal — uses whole session)
    rng = float(rp.max() - rp.min())
    net = float(rp[-1] - rp[0])
    feat["rng"] = rng
    feat["er"] = abs(net) / (rng + 1e-9)                      # efficiency ratio (trend vs chop)
    return feat


def _entry_window_mask(rt: np.ndarray) -> np.ndarray:
    h = np.array([_et_h(int(t)) for t in rt])
    return (h >= WIN_LO) & (h <= WIN_HI)


def entries_for_config(feat: dict, cfg: dict):
    """List of (i, dir) breakout entries for one config (fresh rising-edge + cooldown)."""
    rp = feat["rp"]; n = feat["n"]
    m, v, mode = cfg["M"], cfg["V"], cfg["mode"]
    span, nn, orm = cfg["span"], cfg["N"], cfg["or"]
    pmax, pmin = feat[f"pmax{m}"], feat[f"pmin{m}"]
    vel, up2, dn2 = feat["vel"], feat["up2"], feat["dn2"]

    brkL = rp > pmax
    brkS = rp < pmin
    trigL = brkL & (vel >= v) & up2
    trigS = brkS & (vel <= -v) & dn2

    if mode == "none":
        fL = np.ones(n, bool); fS = np.ones(n, bool)
    elif mode == "ema_hl":
        fL = feat[f"emaR{span}"] & feat[f"hl{nn}"]
        fS = feat[f"emaF{span}"] & feat[f"lh{nn}"]
    elif mode == "orh":
        fL = rp > feat[f"orh{orm}"]
        fS = rp < feat[f"orl{orm}"]
    else:  # union
        fL = (feat[f"emaR{span}"] & feat[f"hl{nn}"]) | (rp > feat[f"orh{orm}"])
        fS = (feat[f"emaF{span}"] & feat[f"lh{nn}"]) | (rp < feat[f"orl{orm}"])

    sigL = trigL & fL
    sigS = trigS & fS
    win = feat["_win"]
    rt = feat["rt"]

    out = []
    last_long = last_short = -10 ** 15
    for i in range(2, n):
        if not win[i]:
            continue
        t = int(rt[i])
        if sigL[i] and not sigL[i - 1] and t - last_long >= COOLDOWN_MS:
            out.append((i, 1)); last_long = t
        if sigS[i] and not sigS[i - 1] and t - last_short >= COOLDOWN_MS:
            out.append((i, -1)); last_short = t
    return out


def fwd_metrics(feat: dict, i: int, d: int):
    """Memoized forward metrics for an entry at bar i, direction d (+1 long / -1 short)."""
    key = (i, d)
    if key in feat["fwd"]:
        return feat["fwd"][key]
    rt, rp, n = feat["rt"], feat["rp"], feat["n"]
    t0 = int(rt[i]); s0 = float(rp[i])
    res = dict(valid=False)
    if SANE_LO <= s0 <= SANE_HI:
        # fixed horizons
        for h in HORIZONS:
            sH = real_at(rt, rp, t0 + h * 60_000)
            fav = None
            if sH is not None and abs(sH - s0) <= MOVE_GLITCH_FRAC * s0:
                fav = d * (sH - s0)
            res[f"fav{h}"] = fav
        # forward path (i+1..EOD) for trailing exits + MFE
        path = rp[i + 1:]
        mask = (path >= SANE_LO) & (path <= SANE_HI)
        path = path[mask]
        if len(path):
            exc = d * (path - s0)                        # signed favorable excursion
            res["mfe"] = float(exc.max())
            res["mae"] = float(exc.min())
            for w in TRAIL_W:
                peak = 0.0                                # peak favorable excursion so far
                cap = float(exc[-1])                       # default EOD
                for e in exc:
                    if e > peak:
                        peak = e
                    if e <= peak - w:                     # gave back w from peak
                        cap = e
                        break
                res[f"cap{int(w)}"] = cap
        else:
            res["mfe"] = res["mae"] = 0.0
            for w in TRAIL_W:
                res[f"cap{int(w)}"] = 0.0
        res["valid"] = True
    # flow snapshot at entry
    res["drift"] = int(feat["drift"][i])
    res["aggr"] = int(feat["aggr"][i])
    res["s0"] = s0
    feat["fwd"][key] = res
    return res


def build_records(feats, cfg):
    """All entry records for one config across every session."""
    recs = []
    for feat in feats:
        for i, d in entries_for_config(feat, cfg):
            fm = fwd_metrics(feat, i, d)
            if not fm["valid"]:
                continue
            r = dict(date=feat["date"], year=feat["year"], dir=d,
                     er=feat["er"], rng=feat["rng"])
            r.update(fm)
            recs.append(r)
    return recs


# ============================================================ stats helpers
def _clustered_t(per_sess_means):
    a = np.array([m for m in per_sess_means if m is not None and np.isfinite(m)], float)
    if len(a) < 2:
        return float("nan")
    se = a.std(ddof=1) / np.sqrt(len(a))
    return float(a.mean() / se) if se > 0 else float("nan")


def agg(recs, field, pred=lambda r: True):
    """Aggregate `field` (fav30, cap10, mfe, ...) over recs matching pred."""
    rr = [r for r in recs if pred(r) and r.get(field) is not None]
    if not rr:
        return None
    a = np.array([r[field] for r in rr], float)
    by = {}
    for r in rr:
        by.setdefault(r["date"], []).append(r[field])
    tcl = _clustered_t([float(np.mean(v)) for v in by.values()])
    return dict(n=len(rr), nsess=len(by), hit=100.0 * np.mean(a > 0), mean=float(a.mean()),
                med=float(np.median(a)), p15=100.0 * np.mean(a > 15), p30=100.0 * np.mean(a > 30),
                tcl=tcl, adv=100.0 * np.mean(a < -15))


def year_signs(recs, field):
    out = {}
    for y in YEARS:
        a = agg(recs, field, lambda r, y=y: r["year"] == y)
        out[y] = a["mean"] if a else None
    return out


# ============================================================ MAIN GRID (Table 1)
def run_grid(feats):
    print("=" * 132)
    print("TABLE 1 — SIGNAL GRID  (long+short pooled; forward on IBKR 1-min ruler; session-clustered t)")
    print(f"  fixed: EMA span={DEF_SPAN}  N(higher-low)={DEF_N}min  OR={DEF_OR}min  "
          f"vel_win={VEL_WIN}min  cooldown=10min  entries 09:50-15:30 ET")
    print("=" * 132)
    hdr = (f"  {'mode':7s} {'M':>3s} {'V':>3s} | {'ent/s':>5s} {'N':>5s} | "
           f"{'+30 hit':>7s} {'mean':>6s} {'med':>6s} {'>15%':>5s} {'>30%':>5s} {'tcl':>5s} "
           f"{'adv%':>5s} | {'+10mn':>6s} | {'trailW10':>8s} {'W10hit':>6s} | "
           f"{'MFE':>6s} | {'22 23 24 25 sign':>17s}")
    print(hdr)
    rows = []
    for mode in MODES:
        for m in GRID_M:
            for v in GRID_V:
                cfg = dict(mode=mode, M=m, V=v, span=DEF_SPAN, N=DEF_N, **{"or": DEF_OR})
                recs = build_records(feats, cfg)
                ent_s = len(recs) / max(1, len(feats))
                a30 = agg(recs, "fav30")
                a10 = agg(recs, "fav10")
                aT = agg(recs, "cap10")
                aT5 = agg(recs, "cap5")
                aM = agg(recs, "mfe")
                ys = year_signs(recs, "fav30")
                if a30 is None:
                    continue
                sgn = " ".join("+" if (ys[y] is not None and ys[y] > 0) else
                               ("-" if ys[y] is not None else ".") for y in YEARS)
                print(f"  {mode:7s} {m:>3d} {v:>3.0f} | {ent_s:5.2f} {a30['n']:5d} | "
                      f"{a30['hit']:6.1f}% {a30['mean']:+6.2f} {a30['med']:+6.2f} "
                      f"{a30['p15']:4.0f}% {a30['p30']:4.0f}% {a30['tcl']:+5.2f} {a30['adv']:4.0f}% | "
                      f"{a10['mean']:+6.2f} | {aT['mean']:+8.2f} {aT['hit']:5.1f}% | "
                      f"{aM['mean']:+6.1f} | {sgn:>17s}")
                rows.append(dict(mode=mode, M=m, V=v, ent_s=ent_s, a30=a30, a10=a10,
                                 aT=aT, aT5=aT5, aM=aM, ys=ys, cfg=cfg))
    # CSV
    csv = os.path.join(SCRATCH, "trend_breakout_grid.csv")
    with open(csv, "w") as f:
        f.write("mode,M,V,ent_per_sess,n,nsess,hit30,mean30,med30,p15_30,p30_30,tcl30,adv30,"
                "mean10,hit10,capW10_mean,capW10_hit,capW5_mean,mfe_mean,"
                "sign2022,sign2023,sign2024,sign2025\n")
        for r in rows:
            a30, a10, aT, aT5, aM, ys = r["a30"], r["a10"], r["aT"], r["aT5"], r["aM"], r["ys"]
            f.write(f"{r['mode']},{r['M']},{r['V']:.0f},{r['ent_s']:.3f},{a30['n']},{a30['nsess']},"
                    f"{a30['hit']:.2f},{a30['mean']:.3f},{a30['med']:.3f},{a30['p15']:.1f},"
                    f"{a30['p30']:.1f},{a30['tcl']:.2f},{a30['adv']:.1f},"
                    f"{a10['mean']:.3f},{a10['hit']:.2f},{aT['mean']:.3f},{aT['hit']:.2f},"
                    f"{aT5['mean']:.3f},{aM['mean']:.3f},"
                    + ",".join(f"{ys[y]:.3f}" if ys[y] is not None else "nan" for y in YEARS) + "\n")
    print(f"\n  full grid CSV -> {csv}")
    return rows


# ============================================================ HEAD-TO-HEAD vs FLOW (Table 2)
def run_head_to_head(feats, cfg, label):
    recs = build_records(feats, cfg)
    print("\n" + "=" * 132)
    print(f"TABLE 2 — PRICE-BREAKOUT vs FLOW-DIRECTION at the SAME entry moments  [{label}]")
    print(f"  config: mode={cfg['mode']} M={cfg['M']} V={cfg['V']:.0f} span={cfg['span']} "
          f"N={cfg['N']} OR={cfg['or']}   n_entries={len(recs)}")
    print("=" * 132)

    # forward move (unsigned by price dir) at +30 for each entry, plus each arm's signed fav
    for h in (30,):
        rows = []
        base = [r for r in recs if r.get(f"fav{h}") is not None]
        # price arm: fav already = dir*(move); recover raw move = price_dir*fav
        for r in base:
            mv = r["dir"] * r[f"fav{h}"]                 # raw signed SPX move
            r["_mv"] = mv
            r["_price_fav"] = r[f"fav{h}"]               # follow the breakout
            r["_drift_fav"] = r["drift"] * mv if r["drift"] != 0 else None
            r["_aggr_fav"] = r["aggr"] * mv if r["aggr"] != 0 else None
        def arm(fav_key, pred=lambda r: True):
            rr = [r for r in base if pred(r) and r.get(fav_key) is not None]
            if not rr:
                return None
            a = np.array([r[fav_key] for r in rr], float)
            by = {}
            for r in rr:
                by.setdefault(r["date"], []).append(r[fav_key])
            return dict(n=len(rr), hit=100.0 * np.mean(a > 0), mean=float(a.mean()),
                        tcl=_clustered_t([float(np.mean(v)) for v in by.values()]))
        pa = arm("_price_fav")
        da = arm("_drift_fav")
        aa = arm("_aggr_fav")
        # random control
        rng = np.random.default_rng(0)
        rnd = np.array([(1 if rng.random() < 0.5 else -1) * r["_mv"] for r in base], float)
        print(f"\n  +{h}min forward edge, following each signal's direction (same moments):")
        print(f"    {'arm':22s} {'n':>5s} {'hit%':>6s} {'meanFav':>8s} {'clustT':>7s}")
        print(f"    {'FOLLOW price-breakout':22s} {pa['n']:5d} {pa['hit']:5.1f}% {pa['mean']:+8.3f} {pa['tcl']:+7.2f}")
        if da:
            print(f"    {'FOLLOW drift_bias':22s} {da['n']:5d} {da['hit']:5.1f}% {da['mean']:+8.3f} {da['tcl']:+7.2f}  (non-neutral only)")
        if aa:
            print(f"    {'FOLLOW aggression':22s} {aa['n']:5d} {aa['hit']:5.1f}% {aa['mean']:+8.3f} {aa['tcl']:+7.2f}  (non-neutral only)")
        print(f"    {'random direction':22s} {len(rnd):5d} {'':6s} {rnd.mean():+8.3f}")

        # agreement / contrarian diagnostics
        nd = [r for r in base if r["drift"] != 0]
        na = [r for r in base if r["aggr"] != 0]
        agr_d = np.mean([r["drift"] == r["dir"] for r in nd]) if nd else float("nan")
        agr_a = np.mean([r["aggr"] == r["dir"] for r in na]) if na else float("nan")
        pct_neu_d = 100.0 * np.mean([r["drift"] == 0 for r in base])
        wins = [r for r in base if r["_price_fav"] > 0]
        wins_d = [r for r in wins if r["drift"] != 0]
        wins_a = [r for r in wins if r["aggr"] != 0]
        flow_wrong_d = 100.0 * np.mean([r["drift"] != r["dir"] for r in wins_d]) if wins_d else float("nan")
        flow_wrong_a = 100.0 * np.mean([r["aggr"] != r["dir"] for r in wins_a]) if wins_a else float("nan")
        print(f"\n  agreement (breakout dir == flow dir):  drift {100*agr_d:.1f}%   aggression {100*agr_a:.1f}%"
              f"   (drift NEUTRAL at entry: {pct_neu_d:.0f}% of entries)")
        print(f"  during WINNING breakouts (fav30>0, n={len(wins)}): flow was CONTRARIAN "
              f"drift {flow_wrong_d:.1f}%   aggression {flow_wrong_a:.1f}%")

        # split: price-breakout edge when flow AGREES vs DISAGREES
        print(f"\n  price-breakout +{h} edge, split by whether flow agreed at entry:")
        print(f"    {'subset':26s} {'n':>5s} {'hit%':>6s} {'meanFav':>8s} {'clustT':>7s}")
        for lbl, pred in [
            ("drift AGREES", lambda r: r["drift"] == r["dir"] and r["drift"] != 0),
            ("drift DISAGREES", lambda r: r["drift"] != r["dir"] and r["drift"] != 0),
            ("drift NEUTRAL", lambda r: r["drift"] == 0),
            ("aggression AGREES", lambda r: r["aggr"] == r["dir"] and r["aggr"] != 0),
            ("aggression DISAGREES", lambda r: r["aggr"] != r["dir"] and r["aggr"] != 0)]:
            a = arm("_price_fav", pred)
            if a:
                print(f"    {lbl:26s} {a['n']:5d} {a['hit']:5.1f}% {a['mean']:+8.3f} {a['tcl']:+7.2f}")
    return recs


# ============================================================ REGIME SPLIT (Table 3)
def run_regime(feats, cfg, label):
    recs = build_records(feats, cfg)
    print("\n" + "=" * 132)
    print(f"TABLE 3 — REGIME SPLIT (does the breakout entry bleed in chop?)  [{label}]")
    print("=" * 132)

    # session terciles by efficiency ratio (trend vs chop) and by realized range (vol)
    er_lo, er_hi = np.percentile([f["er"] for f in feats], [33, 67])
    rg_lo, rg_hi = np.percentile([f["rng"] for f in feats], [33, 67])

    def show(splitname, pred_map):
        print(f"\n  by {splitname}:")
        print(f"    {'bucket':16s} {'entries':>7s} {'ent/s(bkt)':>10s} {'+30 hit':>7s} "
              f"{'mean':>6s} {'med':>6s} {'>15%':>5s} {'tcl':>5s} {'trailW10':>8s}")
        for bname, pred in pred_map:
            rr = [r for r in recs if pred(r)]
            nsess = len(set(f["date"] for f in feats if pred({"er": f["er"], "rng": f["rng"]})))
            a = agg(recs, "fav30", pred)
            aT = agg(recs, "cap10", pred)
            if a is None:
                print(f"    {bname:16s} {'—':>7s}")
                continue
            print(f"    {bname:16s} {a['n']:7d} {a['n']/max(1,nsess):10.2f} {a['hit']:6.1f}% "
                  f"{a['mean']:+6.2f} {a['med']:+6.2f} {a['p15']:4.0f}% {a['tcl']:+5.2f} "
                  f"{aT['mean']:+8.2f}")

    show("EFFICIENCY RATIO tercile (non-causal diagnostic; hi=trend day, lo=chop day)", [
        ("CHOP (ER low)", lambda r: r["er"] <= er_lo),
        ("MID", lambda r: er_lo < r["er"] <= er_hi),
        ("TREND (ER high)", lambda r: r["er"] > er_hi)])
    show("REALIZED-RANGE tercile (vol; hi ~ 2022/2025 high-vol)", [
        ("LOW vol", lambda r: r["rng"] <= rg_lo),
        ("MID vol", lambda r: rg_lo < r["rng"] <= rg_hi),
        ("HIGH vol", lambda r: r["rng"] > rg_hi)])

    # CAUSAL trailing-choppiness filter: ER over trailing 30 bars at entry.
    print("\n  CAUSAL trailing-choppiness filter (ER over last 30 min at entry — a LIVE-usable gate):")
    for feat in feats:
        rp = feat["rp"]; n = feat["n"]
        er30 = np.full(n, np.nan)
        for i in range(30, n):
            seg = rp[i - 30:i + 1]
            er30[i] = abs(seg[-1] - seg[0]) / (seg.max() - seg.min() + 1e-9)
        feat["_er30"] = er30
    # recompute entries with the causal er30 gate attached at each entry bar
    recs2 = []
    for feat in feats:
        er30 = feat["_er30"]
        for i, d in entries_for_config(feat, cfg):
            fm = fwd_metrics(feat, i, d)
            if not fm["valid"]:
                continue
            rr = dict(date=feat["date"], year=feat["year"], dir=d, er30=float(er30[i]))
            rr.update(fm)
            recs2.append(rr)
    print(f"    {'gate':22s} {'entries':>7s} {'+30 hit':>7s} {'mean':>6s} {'tcl':>5s} {'trailW10':>8s}")
    for lbl, pred in [
        ("no gate (all)", lambda r: True),
        ("ER30 >= 0.30", lambda r: np.isfinite(r["er30"]) and r["er30"] >= 0.30),
        ("ER30 >= 0.50", lambda r: np.isfinite(r["er30"]) and r["er30"] >= 0.50),
        ("ER30 >= 0.70", lambda r: np.isfinite(r["er30"]) and r["er30"] >= 0.70)]:
        a = agg(recs2, "fav30", pred)
        aT = agg(recs2, "cap10", pred)
        if a:
            print(f"    {lbl:22s} {a['n']:7d} {a['hit']:6.1f}% {a['mean']:+6.2f} {a['tcl']:+5.2f} {aT['mean']:+8.2f}")


# ============================================================ SENSITIVITY (Table 4)
def run_sensitivity(feats, base_cfg):
    print("\n" + "=" * 132)
    print("TABLE 4 — SENSITIVITY around the chosen config (one knob at a time)")
    print("=" * 132)
    print(f"    {'variant':22s} {'ent/s':>5s} {'+30 hit':>7s} {'mean':>6s} {'>15%':>5s} "
          f"{'tcl':>5s} {'trailW10':>8s}")

    def one(lbl, **over):
        cfg = dict(base_cfg); cfg.update(over)
        recs = build_records(feats, cfg)
        a = agg(recs, "fav30")
        aT = agg(recs, "cap10")
        ent = len(recs) / max(1, len(feats))
        if a:
            print(f"    {lbl:22s} {ent:5.2f} {a['hit']:6.1f}% {a['mean']:+6.2f} {a['p15']:4.0f}% "
                  f"{a['tcl']:+5.2f} {aT['mean']:+8.2f}")

    one(f"BASE", )
    for span in (15, 20, 30):
        one(f"EMA span={span}", span=span)
    for nn in (8, 10, 15):
        one(f"N(higher-low)={nn}", N=nn)
    for orm in (15, 30):
        one(f"OR window={orm}", **{"or": orm})
    for m in GRID_M:
        one(f"M(breakout)={m}", M=m)
    for v in GRID_V:
        one(f"V(velocity)={v:.0f}", V=v)


# ============================================================ main
def main():
    if not os.path.exists(CACHE_PKL):
        print(f"cache missing: {CACHE_PKL}"); return
    cache = pickle.load(open(CACHE_PKL, "rb"))
    print(f"loaded {len(cache)} DEV sessions from {CACHE_PKL}")
    feats = [precompute(s) for s in cache]
    for f in feats:
        f["_win"] = _entry_window_mask(f["rt"])
    print(f"precomputed {len(feats)} sessions  (years: "
          + ", ".join(f"{y}:{sum(1 for f in feats if f['year']==y)}" for y in YEARS) + ")\n")

    rows = run_grid(feats)

    # choose the best config: highest quality at sane frequency.
    # score = mean fav30 + 0.03*(hit30-50), among modes with >=1.0 & <=8 entries/session and enough n.
    elig = [r for r in rows if 1.0 <= r["ent_s"] <= 8.0 and r["a30"]["n"] >= 200]
    elig.sort(key=lambda r: r["a30"]["mean"] + 0.03 * (r["a30"]["hit"] - 50.0), reverse=True)
    print("\n" + "-" * 132)
    print("TOP 8 CONFIGS BY QUALITY AT SANE FREQUENCY (1<=ent/s<=8, n>=200) — score=mean30+0.03*(hit30-50)")
    print(f"  {'mode':7s} {'M':>3s} {'V':>3s} {'ent/s':>5s} {'hit30':>6s} {'mean30':>7s} "
          f"{'>15%':>5s} {'tcl':>5s} {'trailW10':>8s}")
    for r in elig[:8]:
        print(f"  {r['mode']:7s} {r['M']:>3d} {r['V']:>3.0f} {r['ent_s']:5.2f} {r['a30']['hit']:5.1f}% "
              f"{r['a30']['mean']:+7.2f} {r['a30']['p15']:4.0f}% {r['a30']['tcl']:+5.2f} {r['aT']['mean']:+8.2f}")

    best = elig[0]["cfg"] if elig else dict(mode="union", M=15, V=5.0, span=DEF_SPAN, N=DEF_N, **{"or": DEF_OR})
    # also always show the literal-brief 'union' mid config for reference
    ref = dict(mode="union", M=15, V=5.0, span=DEF_SPAN, N=DEF_N, **{"or": DEF_OR})

    run_head_to_head(feats, best, "BEST-QUALITY config")
    if (best["mode"], best["M"], best["V"]) != (ref["mode"], ref["M"], ref["V"]):
        run_head_to_head(feats, ref, "REFERENCE union M15 V5")
    run_regime(feats, best, "BEST-QUALITY config")
    run_sensitivity(feats, best)

    print("\n" + "=" * 132)
    print("READ: fav>0 = move went the breakout's way; %>15/%>30 = big-leg capture; adv% = fav<-15 (reversed);")
    print("  clustT = session-clustered t on fav (|t|>~2 distinguishable from 0). Head-to-head = the money test.")
    print("=" * 132)


if __name__ == "__main__":
    main()
