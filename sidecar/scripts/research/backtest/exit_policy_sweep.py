"""FADE-entry EXIT-POLICY sweep (READ-ONLY research; engine code untouched).

Question under test: on FADE entries (PhaseDetector emits FADE, direction = LONG_CALLS/
LONG_PUTS), is the missed profit on red->green trend days an EXIT/hold problem rather than a
missing entry signal? We hold the ENTRY set fixed and vary only the EXIT, measuring captured
SIGNED SPX points forward on the INDEPENDENT IBKR ruler (never a parity-reconstructed spot;
see MEMORY [[validate-against-independent-price]]). Direction-aware: for LONG_PUTS "favorable"
means price DOWN.

Exit policies compared, forward up to +120 min (capped at RTH close):
  * fixed        : exit at +30 (baseline == report metric), +60, and hold-to-close(+120cap).
  * target/stop  : first close >= +K favorable or <= -J adverse   (K in 8/15/25, J in 8/12).
  * peak-trail   : arm after +A favorable, exit on W-pt giveback from running peak (A 0/5, W 4/8/12/18).
  * trend-break  : ride until price closes back through a causal EMA of the ruler (span 20/30 min),
                   after the trade side of the EMA is first confirmed.

Entries = FADE EPISODE STARTS (same state+dir within 2 min collapsed) in 09:35..15:30 ET, exactly
the set the validated FADE gate would act on. Points are SPX points on the ruler; option-dollar
translation is a LATER step and is NOT attempted here.

Metrics per policy, pooled AND by year AND by regime (HIGHVOL 2022+2025 vs CHOP 2023+2024):
  mean & MEDIAN captured, hit%, P75/P90, %>20pt, %>40pt (the TAIL), session-clustered t, n.

Run:
  .venv/Scripts/python.exe -m scripts.research.backtest.exit_policy_sweep            # big_move (primary)
  .venv/Scripts/python.exe -m scripts.research.backtest.exit_policy_sweep --preset balanced
  .venv/Scripts/python.exe -m scripts.research.backtest.exit_policy_sweep --both     # both presets
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import pickle
import sys
from zoneinfo import ZoneInfo

import numpy as np

from fef_sidecar.engine.phase_detector import PhaseDetector

sys.stdout.reconfigure(encoding="utf-8")
ET = ZoneInfo("America/New_York")

CACHE_PKL = os.path.join(os.environ.get("TEMP", "."), "phase_cache.pkl")
OUT_DIR = (r"C:\Users\diant\AppData\Local\Temp\claude\C--Users-diant"
           r"\13885fae-da20-4300-a83a-94697ac91ce4\scratchpad")

WIN_LO, WIN_HI = 9.5833, 15.5      # entries 09:35..15:30 ET (same as validated harness)
EP_GAP_MS = 120_000                # same state+dir within 2 min = same episode
HORIZON_MIN = 120                  # max hold (min); RTH-close cap is automatic (ruler ends 16:00)
TOL_MS = 120_000                   # nearest-bar tolerance for fixed-time exits
GLITCH_FRAC = 0.05                 # ignore an entry whose entry px is a >5% ruler glitch (defensive)

FIXED = dict(window_sec=300, vel_sec=75, vel_stall=3.0, iv_delta=0.006, mp_overshoot=8.0)
PRESETS = {
    "big_move": dict(FIXED, vel_ignite=16.0, min_move=18.0, bounce_pt=3.0, fade_min=5, ride_min=7),
    "balanced": dict(FIXED, vel_ignite=16.0, min_move=12.0, bounce_pt=3.0, fade_min=5, ride_min=7),
}
HIGHVOL_YEARS = {"2022", "2025"}
CHOP_YEARS = {"2023", "2024"}


def _et_h(ts_ms: int) -> float:
    x = dt.datetime.fromtimestamp(ts_ms / 1000, ET)
    return x.hour + x.minute / 60.0


def _ema(px: np.ndarray, span: int) -> np.ndarray:
    """Causal EMA seeded at the session open (well warmed by any mid-day entry)."""
    a = 2.0 / (span + 1.0)
    out = np.empty_like(px)
    out[0] = px[0]
    for i in range(1, len(px)):
        out[i] = a * px[i] + (1.0 - a) * out[i - 1]
    return out


# ----------------------------------------------------------------------------- entries
def collect_entries(cache, cfg):
    """FADE episode starts. Returns list of dicts with the forward ruler path pre-sliced."""
    ents = []
    for s in cache:
        rt, rp = s["rt"], s["rp"]
        ema20 = _ema(rp, 20)
        ema30 = _ema(rp, 30)
        det = PhaseDetector(**cfg)
        last_key = last_ts = None
        for i, smp in enumerate(s["samples"]):
            v = det.update(smp)
            if not v.actionable or v.state != "FADE":
                continue
            h = _et_h(smp.ts_ms)
            if h < WIN_LO or h > WIN_HI:
                continue
            key = (v.state, v.direction)
            ep_start = not (key == last_key and last_ts is not None and smp.ts_ms - last_ts <= EP_GAP_MS)
            last_key, last_ts = key, smp.ts_ms
            if not ep_start:
                continue
            ts0 = int(rt[i]); s0 = float(rp[i])
            ds = 1.0 if v.direction == "LONG_CALLS" else -1.0
            hi = ts0 + HORIZON_MIN * 60_000
            m = (rt > ts0) & (rt <= hi)
            if not m.any():
                continue
            fts = rt[m]; fpx = rp[m]
            if abs(fpx[0] - s0) > GLITCH_FRAC * s0:      # defensive glitch guard
                continue
            fav = ds * (fpx - s0)
            ents.append(dict(
                date=s["date"], year=s["year"], dir_sign=ds,
                direction=v.direction, score=v.score, ts0=ts0, s0=s0,
                fts=fts, fpx=fpx, fav=fav,
                ema20=ema20[m], ema30=ema30[m]))
    return ents


# ----------------------------------------------------------------------------- exit policies
def ex_fixed(e, minutes):
    """Captured at the bar nearest ts0+minutes (fallback: last bar within horizon)."""
    tgt = e["ts0"] + minutes * 60_000
    fts, fav = e["fts"], e["fav"]
    j = int(np.searchsorted(fts, tgt))
    best, bd = None, TOL_MS + 1
    for c in (j - 1, j):
        if 0 <= c < len(fts):
            dd = abs(int(fts[c]) - tgt)
            if dd <= TOL_MS and dd < bd:
                bd, best = dd, c
    if best is None:
        best = len(fts) - 1        # session ended before target -> hold to close
    return float(fav[best])


def ex_target_stop(e, K, J):
    fav = e["fav"]
    for x in fav:
        if x >= K:
            return float(x)        # target (observed close that first crossed +K)
        if x <= -J:
            return float(x)        # stop  (observed close that first crossed -J)
    return float(fav[-1])          # neither -> time stop at +120/close


def ex_trail(e, A, W):
    fav = e["fav"]
    armed = A <= 0
    peak = 0.0 if armed else None
    for x in fav:
        if not armed and x >= A:
            armed = True
            peak = max(0.0, float(x))
        if armed:
            peak = max(peak, float(x))
            if peak - x >= W:
                return float(x)
    return float(fav[-1])


def ex_trend_break(e, span):
    ema = e["ema20"] if span == 20 else e["ema30"]
    fpx, fav, ds = e["fpx"], e["fav"], e["dir_sign"]
    confirmed = False
    for k in range(len(fpx)):
        fav_side = (fpx[k] > ema[k]) if ds > 0 else (fpx[k] < ema[k])
        if fav_side:
            confirmed = True
            continue
        if confirmed:                # crossed back through the EMA against the trade
            return float(fav[k])
    return float(fav[-1])            # trend never broke (or never confirmed) -> hold to close


def build_policies():
    pols = []
    pols.append(("fixed +30min (BASELINE)", lambda e: ex_fixed(e, 30)))
    pols.append(("fixed +60min", lambda e: ex_fixed(e, 60)))
    pols.append(("hold to close (+120cap)", lambda e: ex_fixed(e, 10_000)))
    for K in (8, 15, 25):
        for J in (8, 12):
            pols.append((f"tgt+{K}/stop-{J}", lambda e, K=K, J=J: ex_target_stop(e, K, J)))
    for A in (0, 5):
        for W in (4, 8, 12, 18):
            pols.append((f"trail A{A}/give{W}", lambda e, A=A, W=W: ex_trail(e, A, W)))
    for span in (20, 30):
        pols.append((f"trend-break EMA{span}", lambda e, span=span: ex_trend_break(e, span)))
    return pols


# ----------------------------------------------------------------------------- metrics
def _clustered_t(cap, dates):
    by = {}
    for c, d in zip(cap, dates):
        by.setdefault(d, []).append(c)
    a = np.array([np.mean(v) for v in by.values()], float)
    if len(a) < 2:
        return float("nan")
    se = a.std(ddof=1) / np.sqrt(len(a))
    return float(a.mean() / se) if se > 0 else float("nan")


def summarize(cap, dates):
    cap = np.asarray(cap, float)
    n = len(cap)
    if n == 0:
        return None
    return dict(
        n=n, nsess=len(set(dates)),
        mean=float(cap.mean()), median=float(np.median(cap)),
        p75=float(np.percentile(cap, 75)), p90=float(np.percentile(cap, 90)),
        hit=100.0 * float((cap > 0).mean()),
        g20=100.0 * float((cap > 20).mean()), g40=100.0 * float((cap > 40).mean()),
        tcl=_clustered_t(cap, dates))


# ----------------------------------------------------------------------------- run one preset
def run_preset(cache, preset):
    cfg = PRESETS[preset]
    ents = collect_entries(cache, cfg)
    years = [e["year"] for e in ents]
    ncall = sum(1 for e in ents if e["dir_sign"] > 0)
    nput = len(ents) - ncall
    nsess = len(set(e["date"] for e in ents))
    pols = build_policies()

    print("=" * 118)
    print(f"EXIT-POLICY SWEEP on FADE entries  —  preset='{preset}'  cfg={cfg}")
    print(f"  entries (FADE episode starts, 09:35-15:30 ET): {len(ents)}  "
          f"(calls={ncall} puts={nput})  over {nsess} sessions  |  horizon <= +{HORIZON_MIN}min, RTH-close cap")
    yr_counts = {y: years.count(y) for y in ("2022", "2023", "2024", "2025")}
    print(f"  entries by year: {yr_counts}")
    print("=" * 118)

    # ---- MAIN TABLE (pooled) ----
    rows = []
    print("\n  POOLED (all years) — captured SIGNED SPX points per trade")
    hdr = (f"    {'policy':26s} {'n':>4s} {'mean':>7s} {'med':>7s} {'P75':>6s} {'P90':>6s} "
           f"{'hit%':>6s} {'>20pt':>6s} {'>40pt':>6s} {'t_clu':>6s}")
    print(hdr)
    base_mean = None
    for name, fn in pols:
        cap = [fn(e) for e in ents]
        dates = [e["date"] for e in ents]
        a = summarize(cap, dates)
        if name.startswith("fixed +30"):
            base_mean = a["mean"]
        rows.append((name, cap, dates))
        star = ""
        if base_mean is not None and not name.startswith("fixed +30"):
            star = "  <=" if a["mean"] < base_mean else "  ^^"
        print(f"    {name:26s} {a['n']:4d} {a['mean']:+7.2f} {a['median']:+7.2f} {a['p75']:+6.1f} "
              f"{a['p90']:+6.1f} {a['hit']:5.1f}% {a['g20']:5.1f}% {a['g40']:5.1f}% {a['tcl']:+6.2f}{star}")

    # ---- BY YEAR (mean captured) ----
    print("\n  BY YEAR — mean captured (pt) [n]")
    yrs = ["2022", "2023", "2024", "2025"]
    print(f"    {'policy':26s} " + " ".join(f"{y:>13s}" for y in yrs))
    for name, cap, dates in rows:
        cells = []
        for y in yrs:
            idx = [k for k, e in enumerate(ents) if e["year"] == y]
            if idx:
                cv = np.array([cap[k] for k in idx], float)
                cells.append(f"{cv.mean():+7.2f}[{len(idx):3d}]")
            else:
                cells.append(f"{'—':>13s}")
        print(f"    {name:26s} " + " ".join(cells))

    # ---- REGIME SPLIT ----
    print("\n  REGIME SPLIT — HIGHVOL(2022+2025) vs CHOP(2023+2024)")
    print(f"    {'policy':26s} | {'HV mean':>8s} {'HV med':>7s} {'HV P90':>7s} {'HV>20':>6s} {'HVn':>4s}"
          f" | {'CH mean':>8s} {'CH med':>7s} {'CH P90':>7s} {'CH>20':>6s} {'CHn':>4s}")
    for name, cap, dates in rows:
        hv = [(cap[k], dates[k]) for k, e in enumerate(ents) if e["year"] in HIGHVOL_YEARS]
        ch = [(cap[k], dates[k]) for k, e in enumerate(ents) if e["year"] in CHOP_YEARS]
        ah = summarize([c for c, _ in hv], [d for _, d in hv])
        ac = summarize([c for c, _ in ch], [d for _, d in ch])
        print(f"    {name:26s} | {ah['mean']:+8.2f} {ah['median']:+7.2f} {ah['p90']:+7.1f} "
              f"{ah['g20']:5.1f}% {ah['n']:4d} | {ac['mean']:+8.2f} {ac['median']:+7.2f} {ac['p90']:+7.1f} "
              f"{ac['g20']:5.1f}% {ac['n']:4d}")

    # ---- CSV dump ----
    csv = os.path.join(OUT_DIR, f"exit_policy_{preset}.csv")
    with open(csv, "w") as f:
        f.write("policy,n,mean,median,p75,p90,hit,g20,g40,tclust,"
                "hv_mean,hv_median,hv_p90,hv_g20,hv_n,ch_mean,ch_median,ch_p90,ch_g20,ch_n,"
                "y2022_mean,y2023_mean,y2024_mean,y2025_mean\n")
        for name, cap, dates in rows:
            a = summarize(cap, dates)
            hv = [(cap[k], dates[k]) for k, e in enumerate(ents) if e["year"] in HIGHVOL_YEARS]
            ch = [(cap[k], dates[k]) for k, e in enumerate(ents) if e["year"] in CHOP_YEARS]
            ah = summarize([c for c, _ in hv], [d for _, d in hv])
            ac = summarize([c for c, _ in ch], [d for _, d in ch])
            ym = {}
            for y in yrs:
                idx = [k for k, e in enumerate(ents) if e["year"] == y]
                ym[y] = np.mean([cap[k] for k in idx]) if idx else float("nan")
            f.write(f'"{name}",{a["n"]},{a["mean"]:.3f},{a["median"]:.3f},{a["p75"]:.3f},{a["p90"]:.3f},'
                    f'{a["hit"]:.2f},{a["g20"]:.2f},{a["g40"]:.2f},{a["tcl"]:.3f},'
                    f'{ah["mean"]:.3f},{ah["median"]:.3f},{ah["p90"]:.3f},{ah["g20"]:.2f},{ah["n"]},'
                    f'{ac["mean"]:.3f},{ac["median"]:.3f},{ac["p90"]:.3f},{ac["g20"]:.2f},{ac["n"]},'
                    f'{ym["2022"]:.3f},{ym["2023"]:.3f},{ym["2024"]:.3f},{ym["2025"]:.3f}\n')
    print(f"\n  -> {csv}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", choices=list(PRESETS), default="big_move")
    ap.add_argument("--both", action="store_true")
    a = ap.parse_args()
    cache = pickle.load(open(CACHE_PKL, "rb"))
    print(f"loaded cache: {len(cache)} sessions from {CACHE_PKL}")
    presets = ["big_move", "balanced"] if a.both else [a.preset]
    for p in presets:
        run_preset(cache, p)
        print("\n")


if __name__ == "__main__":
    main()
