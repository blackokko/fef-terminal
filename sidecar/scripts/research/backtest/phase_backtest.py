"""PhaseDetector backtest harness (READ-ONLY research; engine code untouched).

Per session: build aligned 1-min PhaseSamples (phase_pillars), feed a fresh
`PhaseDetector`, collect every actionable RIDE/FADE verdict, and score its forward
SIGNED move IN THE TRADE'S DIRECTION on the INDEPENDENT IBKR ruler at +10/+30 min
(fav = +1 when calls-and-up or puts-and-down). Reports hit%, mean fav (pt), %big
(fav>+8pt) and signal frequency, split RIDE vs FADE, BY YEAR, plus a random-direction
control and session-clustered t on fav.

Run (default engine thresholds):
  .venv/Scripts/python.exe -m scripts.research.backtest.phase_backtest
Run a tuned config, e.g. the recommended default:
  .venv/Scripts/python.exe -m scripts.research.backtest.phase_backtest --vel-ignite 16 \
      --min-move 18 --bounce-pt 8 --fade-min 5 --ride-min 4
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from zoneinfo import ZoneInfo

import numpy as np

from fef_sidecar.engine import phase_detector as PD
from fef_sidecar.engine.phase_detector import PhaseDetector

from .data import connect
from .ibkr_ruler import ibkr_open
from .phase_pillars import build_session, dev_sessions, ruler_at

sys.stdout.reconfigure(encoding="utf-8")
ET = ZoneInfo("America/New_York")

# holdout-safe DEV window == the iv_atm-covered set (2022..2025-08). See holdout.py.
DEF_START, DEF_END = "2022-01-03", "2025-08-12"
WIN_LO, WIN_HI = 9.5833, 15.5      # entries only 09:35..15:30 ET (warm-up done, +30min exists)
BIG_PT = 8.0                       # a "big" favorable move (pt)
GLITCH_FRAC = 0.05                 # drop a forward reading with |move|/spot0 > 5%
EP_GAP_MS = 120_000               # same state+dir within 2min = same episode
RNG = np.random.default_rng(0)
HORIZONS = [(10, "+10min"), (30, "+30min")]


def _et_h(ts_ms: int) -> float:
    x = dt.datetime.fromtimestamp(ts_ms / 1000, ET)
    return x.hour + x.minute / 60.0


def build_all(con, ic, dates, label="build"):
    """Reconstruct every session once (expensive DB reads); cache for reuse across configs."""
    cache = []
    for k, d in enumerate(dates):
        s = build_session(con, ic, d)
        if s is not None:
            cache.append(s)
        if (k + 1) % 150 == 0:
            print(f"  [{label}] {k+1}/{len(dates)} built (usable={len(cache)})", flush=True)
    return cache


def eval_config(cache, cfg: dict):
    """Run the detector over cached sessions; return (records, per-session-counts).

    record: date, year, state, direction, dir_sign, score, ep_start, fav{10,30}, ... hit/big.
    """
    records = []
    counts = []   # per-session dict(date, year, n_sig, n_ride, n_fade, n_ep)
    for s in cache:
        det = PhaseDetector(**cfg)
        rt, rp = s["rt"], s["rp"]
        n_sig = n_ride = n_fade = n_ep = 0
        last_key, last_ts = None, None
        for smp in s["samples"]:
            v = det.update(smp)
            if not v.actionable:
                continue
            h = _et_h(smp.ts_ms)
            if h < WIN_LO or h > WIN_HI:
                continue
            n_sig += 1
            if v.state == "RIDE":
                n_ride += 1
            else:
                n_fade += 1
            key = (v.state, v.direction)
            ep_start = not (key == last_key and last_ts is not None and smp.ts_ms - last_ts <= EP_GAP_MS)
            if ep_start:
                n_ep += 1
            last_key, last_ts = key, smp.ts_ms
            dir_sign = 1.0 if v.direction == "LONG_CALLS" else -1.0
            s0 = ruler_at(rt, rp, smp.ts_ms)
            rec = dict(date=s["date"], year=s["year"], state=v.state, direction=v.direction,
                       dir_sign=dir_sign, score=v.score, ep_start=ep_start)
            coin = 1.0 if RNG.random() < 0.5 else -1.0
            for hmin, hname in HORIZONS:
                fav = hit = big = rndv = None
                if s0 is not None:
                    sH = ruler_at(rt, rp, smp.ts_ms + hmin * 60_000)
                    if sH is not None and abs(sH - s0) <= GLITCH_FRAC * s0:
                        mv = sH - s0
                        fav = dir_sign * mv
                        hit = 1.0 if fav > 0 else 0.0
                        big = 1.0 if fav > BIG_PT else 0.0
                        rndv = coin * mv
                rec[f"fav{hmin}"] = fav
                rec[f"hit{hmin}"] = hit
                rec[f"big{hmin}"] = big
                rec[f"rnd{hmin}"] = rndv
            records.append(rec)
        counts.append(dict(date=s["date"], year=s["year"], n_sig=n_sig,
                           n_ride=n_ride, n_fade=n_fade, n_ep=n_ep))
    return records, counts


def _clustered_t(per_sess_means):
    a = np.array([m for m in per_sess_means if m is not None and np.isfinite(m)], float)
    if len(a) < 2:
        return float("nan")
    se = a.std(ddof=1) / np.sqrt(len(a))
    return float(a.mean() / se) if se > 0 else float("nan")


def aggregate(records, hmin, pred):
    """Aggregate the subset matching pred for horizon hmin. None if empty."""
    rr = [r for r in records if pred(r) and r.get(f"fav{hmin}") is not None]
    if not rr:
        return None
    fav = np.array([r[f"fav{hmin}"] for r in rr], float)
    hit = np.array([r[f"hit{hmin}"] for r in rr], float)
    big = np.array([r[f"big{hmin}"] for r in rr], float)
    rnd = np.array([r[f"rnd{hmin}"] for r in rr], float)
    # session-clustered t on fav
    by_sess = {}
    for r in rr:
        by_sess.setdefault(r["date"], []).append(r[f"fav{hmin}"])
    tcl = _clustered_t([float(np.mean(v)) for v in by_sess.values()])
    return dict(n=len(rr), nsess=len(by_sess), hit=100.0 * hit.mean(), fav=float(fav.mean()),
                big=100.0 * big.mean(), tcl=tcl, rnd=float(rnd.mean()))


def _freq_report(counts):
    ns = np.array([c["n_sig"] for c in counts], float)
    ne = np.array([c["n_ep"] for c in counts], float)
    tot = int(ns.sum())
    fired = int((ns > 0).sum())
    print(f"  sessions            : {len(counts)}  (fired >=1 signal: {fired}, "
          f"{100.0*fired/max(1,len(counts)):.0f}%)")
    print(f"  total actionable sig: {tot}   ({int(np.array([c['n_ride'] for c in counts]).sum())} RIDE / "
          f"{int(np.array([c['n_fade'] for c in counts]).sum())} FADE)")
    print(f"  signals / session   : mean {ns.mean():.2f}  median {np.median(ns):.0f}  "
          f"p90 {np.percentile(ns,90):.0f}  max {ns.max():.0f}")
    print(f"  episodes / session  : mean {ne.mean():.2f}  (>=2min-gap-collapsed)   "
          f"total episodes {int(ne.sum())}")


def _quality_report(records, title):
    print(f"\n  {title}")
    hdr = (f"    {'group':16s} {'n':>5s} {'sess':>4s} {'hit%':>6s} {'favPt':>7s} "
           f"{'%big':>5s} {'tclust':>6s} {'rndPt':>7s}")
    years = ["2022", "2023", "2024", "2025"]
    for hmin, hname in HORIZONS:
        print(f"\n  HORIZON {hname}")
        print(hdr)
        rows = [("ALL", lambda r: True), ("ALL·RIDE", lambda r: r["state"] == "RIDE"),
                ("ALL·FADE", lambda r: r["state"] == "FADE")]
        for y in years:
            rows.append((y, lambda r, y=y: r["year"] == y))
            rows.append((f"{y}·RIDE", lambda r, y=y: r["year"] == y and r["state"] == "RIDE"))
            rows.append((f"{y}·FADE", lambda r, y=y: r["year"] == y and r["state"] == "FADE"))
        for label, pred in rows:
            a = aggregate(records, hmin, pred)
            if a is None:
                print(f"    {label:16s} {'—':>5s}")
                continue
            print(f"    {label:16s} {a['n']:5d} {a['nsess']:4d} {a['hit']:5.1f}% {a['fav']:+7.2f} "
                  f"{a['big']:4.0f}% {a['tcl']:+6.2f} {a['rnd']:+7.2f}")


def make_cfg(a) -> dict:
    return dict(window_sec=a.window_sec, vel_sec=a.vel_sec, vel_ignite=a.vel_ignite,
                vel_stall=a.vel_stall, min_move=a.min_move, bounce_pt=a.bounce_pt,
                iv_delta=a.iv_delta, mp_overshoot=a.mp_overshoot,
                fade_min=a.fade_min, ride_min=a.ride_min)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEF_START)
    ap.add_argument("--end", default=DEF_END)
    ap.add_argument("--window-sec", type=int, default=PD.WINDOW_SEC)
    ap.add_argument("--vel-sec", type=int, default=PD.VEL_SEC)
    ap.add_argument("--vel-ignite", type=float, default=PD.VEL_IGNITE)
    ap.add_argument("--vel-stall", type=float, default=PD.VEL_STALL)
    ap.add_argument("--min-move", type=float, default=PD.MIN_MOVE)
    ap.add_argument("--bounce-pt", type=float, default=PD.BOUNCE_PT)
    ap.add_argument("--iv-delta", type=float, default=PD.IV_DELTA)
    ap.add_argument("--mp-overshoot", type=float, default=PD.MP_OVERSHOOT)
    ap.add_argument("--fade-min", type=int, default=PD.FADE_MIN)
    ap.add_argument("--ride-min", type=int, default=PD.RIDE_MIN)
    a = ap.parse_args()
    cfg = make_cfg(a)

    con, ic = connect(), ibkr_open()
    dates = dev_sessions(con, a.start, a.end)
    print("=" * 96)
    print("PHASE DETECTOR BACKTEST — independent IBKR SPX ruler, 1-min reconstructed pillars")
    print(f"  window {a.start}..{a.end}  ({len(dates)} candidate DEV sessions)")
    print(f"  CONFIG: {cfg}")
    print("=" * 96)
    cache = build_all(con, ic, dates)
    print(f"  usable sessions built: {len(cache)}")
    records, counts = eval_config(cache, cfg)

    print("\n" + "-" * 96)
    print("FREQUENCY (over-firing check)")
    _freq_report(counts)
    _quality_report(records, "SIGNAL QUALITY — forward move in trade direction on IBKR ruler")
    print("\n" + "=" * 96)
    print("Legend: fav>0 = move went the signaled way; %big = fav>+8pt; tclust = session-clustered")
    print("        t on fav (|t|>~2 = mean fav distinguishable from 0); rndPt = random-dir control (~0).")
    print("=" * 96)


if __name__ == "__main__":
    main()
