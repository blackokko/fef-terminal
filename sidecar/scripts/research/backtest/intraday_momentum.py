"""MARKET INTRADAY MOMENTUM (Gao, Han, Li & Zhou, JFE 2018) — PART 1: the PREDICTIVE test.

The ONE peer-reviewed intraday DIRECTIONAL effect: the first-half-hour market return predicts
the last-half-hour return, and the predictability is STRONGER on high-volatility / high-volume days.
Published in-sample R^2 was ~1.6% on SPY 1993-2013 — MODEST, and it may have decayed. This script
re-tests it on OUR independent 2022-2026 SPX 1-min data and reports whether it survives OOS + is
stronger on high-vol days, WITHOUT over-claiming a 1.6%-R^2 effect.

RULER (no self-reference): independent IBKR SPX 1-min CLOSE ruler (ibkr_ruler; ibkr_spx_1m.db),
which is independent of the ThetaData option chain -> NO parity-spot reconstruction artifact
(MEMORY [[validate-against-independent-price]]). Bars are 1-min, START-stamped (close ~= stamp+60s);
the last bar (15:59) close == the 16:00 cash close, which is the 0DTE PM settlement reference.

RETURNS (per session, prices sampled at the ruler bar nearest each ET mark, <=120s):
  prior_close = last ruler bar of the PRIOR session (== its 16:00 close)
  p_open      = first ruler bar (~09:31, a 1-min-late open proxy; close-only ruler)
  p1000,p1500,p1530,p1600 = ruler close nearest 10:00 / 15:00 / 15:30 / 16:00 (16:00 -> 15:59 bar)
  r1_on  = p1000/prior_close - 1   (first-half-hour incl. overnight gap -- the task's headline r1)
  r1_id  = p1000/p_open      - 1   (first-half-hour PURE intraday 09:30->10:00)
  r12    = p1530/p1500       - 1   (penultimate half-hour 15:00->15:30 -- the paper's 2nd predictor,
                                    and it is KNOWN at the 15:30 entry, so it is directly tradeable)
  rL     = p1600/p1530       - 1   (LAST half-hour 15:30->16:00 -- the predictand)
Glitch filter: any |return| > 5% is dropped (ruler spike).

CONDITIONING (paper: predictability rises with vol/volume): VIX prior-day close buckets;
realized-vol-so-far (std of 1-min log-returns 09:30..15:30, causal at entry) DEV quartiles;
|r1| DEV quartiles (big first-half-hour move). NOTE: the cash index has no meaningful VOLUME, so
we proxy "high activity" with VIX + realized-vol + |r1| (stated honestly; volume not testable).

STATS: OLS rL~r1 (slope, R^2, t) AND rL~sign(r1); sign hit-rate P(sign rL == sign r1) and mean
favorable last-half-hour move sign(r1)*rL. ONE observation per session => the ordinary t IS the
session-clustered t (N_eff = #sessions); a session case-bootstrap 95% CI is added for robustness.
DEV (2022-01-03..2025-08-12) builds; HOLDOUT (2025-08-13..2026-06-26) confirms — side by side.

Read-only. Run: .venv/Scripts/python.exe -m scripts.research.backtest.intraday_momentum
CSVs -> scratchpad.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import sqlite3
import sys

import numpy as np

from .holdout import DEV_END, DEV_START, HOLDOUT_END, HOLDOUT_START
from .ibkr_ruler import ET, IBKR_DB, ibkr_open, ibkr_series

sys.stdout.reconfigure(encoding="utf-8")

SIG_DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
SCRATCH = (r"C:\Users\diant\AppData\Local\Temp\claude\C--Users-diant"
           r"\13885fae-da20-4300-a83a-94697ac91ce4\scratchpad")
GLITCH = 0.05          # drop any |return| > 5% (ruler spike)
TOL_MS = 120_000       # nearest-bar tolerance for an ET mark
RNG = np.random.default_rng(20260701)
NBOOT = 5000


# ------------------------------------------------------------------ ruler sampling
def et_ms(d, h, m):
    y, mo, dd = map(int, d.split("-"))
    return int(dt.datetime(y, mo, dd, h, m, tzinfo=ET).timestamp() * 1000)


def nearest(rt, rp, target, tol=TOL_MS):
    j = int(np.searchsorted(rt, target))
    best, bd = None, tol + 1
    for c in (j - 1, j):
        if 0 <= c < len(rt):
            dd = abs(int(rt[c]) - target)
            if dd <= tol and dd < bd:
                bd, best = dd, c
    return float(rp[best]) if best is not None else None


def realized_vol_sofar(rt, rp, d):
    """std of 1-min log-returns from 09:30 to 15:30 (causal at the 15:30 entry), in bps/min."""
    lo, hi = et_ms(d, 9, 30), et_ms(d, 15, 30)
    m = (rt >= lo) & (rt <= hi)
    seg = rp[m]
    if len(seg) < 30:
        return np.nan
    lr = np.diff(np.log(seg))
    lr = lr[np.abs(lr) < GLITCH]
    if len(lr) < 20:
        return np.nan
    return float(np.std(lr, ddof=1) * 1e4)   # bps per minute


# ------------------------------------------------------------------ VIX prior-day close
def load_vix_prev():
    c = sqlite3.connect(f"file:{SIG_DB}?mode=ro", uri=True)
    rows = c.execute("SELECT timestamp_ms, close FROM historical_bar "
                     "WHERE symbol='VIX' AND bar_size='1 day' ORDER BY timestamp_ms").fetchall()
    c.close()
    by_date = {}
    for ts, cl in rows:
        dd = dt.datetime.fromtimestamp(ts / 1000, ET).strftime("%Y-%m-%d")
        by_date[dd] = float(cl)
    dates = sorted(by_date)

    def prev(d):
        # most-recent VIX close STRICTLY before session d
        import bisect
        i = bisect.bisect_left(dates, d)
        return by_date[dates[i - 1]] if i > 0 else None
    return prev


# ------------------------------------------------------------------ build per-session table
def build_rows():
    prev_vix = load_vix_prev()
    ic = ibkr_open()
    # all ruler session dates in range
    con = sqlite3.connect(f"file:{IBKR_DB}?mode=ro", uri=True)
    days = sorted({dt.datetime.fromtimestamp(ts / 1000, ET).strftime("%Y-%m-%d")
                   for (ts,) in con.execute("SELECT ts_ms FROM bar")})
    con.close()
    days = [d for d in days if DEV_START <= d <= HOLDOUT_END]

    prior_close = None
    rows = []
    for d in days:
        rs = ibkr_series(ic, d)
        if rs is None:
            prior_close = None
            continue
        rt, rp = rs
        p_open = float(rp[0])
        p1000 = nearest(rt, rp, et_ms(d, 10, 0))
        p1500 = nearest(rt, rp, et_ms(d, 15, 0))
        p1530 = nearest(rt, rp, et_ms(d, 15, 30))
        p1600 = nearest(rt, rp, et_ms(d, 16, 0))   # -> last (15:59) bar == cash close
        pc = prior_close
        prior_close = p1600 if p1600 is not None else float(rp[-1])
        if None in (p1000, p1500, p1530, p1600):
            continue

        def ret(a, b):
            if a is None or b is None or b == 0:
                return None
            r = a / b - 1.0
            return r if abs(r) < GLITCH else None

        r1_on = ret(p1000, pc) if pc else None
        r1_id = ret(p1000, p_open)
        r12 = ret(p1530, p1500)
        rL = ret(p1600, p1530)
        if rL is None or r1_id is None or r12 is None:
            continue
        split = "DEV" if d <= DEV_END else "HOLD"
        rows.append(dict(
            date=d, split=split, year=d[:4],
            prior_close=pc, p_open=p_open, p1000=p1000, p1500=p1500, p1530=p1530, p1600=p1600,
            r1_on=r1_on, r1_id=r1_id, r12=r12, rL=rL,
            vix_prev=prev_vix(d), rvol_sofar=realized_vol_sofar(rt, rp, d),
        ))
    ic.close()
    return rows


# ------------------------------------------------------------------ stats
def ols(x, y):
    """slope, intercept, R^2, t(slope), n  (t = session-clustered since 1 obs/session)."""
    x = np.asarray(x, float); y = np.asarray(y, float)
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    n = len(x)
    if n < 5 or np.std(x) == 0:
        return (np.nan,) * 4 + (n,)
    xm, ym = x.mean(), y.mean()
    sxx = np.sum((x - xm) ** 2)
    b = np.sum((x - xm) * (y - ym)) / sxx
    a = ym - b * xm
    resid = y - (a + b * x)
    sse = np.sum(resid ** 2)
    sst = np.sum((y - ym) ** 2)
    r2 = 1 - sse / sst if sst > 0 else np.nan
    se = np.sqrt(sse / (n - 2) / sxx) if n > 2 else np.nan
    t = b / se if se and se > 0 else np.nan
    return b, a, (r2 * 100 if np.isfinite(r2) else np.nan), t, n


def boot_slope_ci(x, y):
    x = np.asarray(x, float); y = np.asarray(y, float)
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    n = len(x)
    if n < 8:
        return (np.nan, np.nan)
    bs = np.empty(NBOOT)
    idx = np.arange(n)
    for b in range(NBOOT):
        pick = RNG.choice(idx, size=n, replace=True)
        xb, yb = x[pick], y[pick]
        if np.std(xb) == 0:
            bs[b] = np.nan; continue
        xm = xb.mean()
        bs[b] = np.sum((xb - xm) * (yb - yb.mean())) / np.sum((xb - xm) ** 2)
    bs = bs[np.isfinite(bs)]
    return tuple(np.percentile(bs, [2.5, 97.5]))


def sign_stats(r1, rL):
    """hit-rate P(sign rL==sign r1) [%], mean favorable move sign(r1)*rL [bps], its t, n."""
    r1 = np.asarray(r1, float); rL = np.asarray(rL, float)
    m = np.isfinite(r1) & np.isfinite(rL) & (r1 != 0)
    r1, rL = r1[m], rL[m]
    n = len(r1)
    if n < 5:
        return dict(n=n, hit=np.nan, fav=np.nan, t=np.nan)
    fav = np.sign(r1) * rL
    hit = 100.0 * np.mean(fav > 0)
    favb = fav * 1e4
    se = favb.std(ddof=1) / np.sqrt(n)
    t = favb.mean() / se if se > 0 else np.nan
    return dict(n=n, hit=hit, fav=float(favb.mean()), t=float(t))


# ------------------------------------------------------------------ reporting
def block(title):
    print("\n" + "=" * 108)
    print(title)
    print("=" * 108)


def report_predictor(rows, pkey, plabel):
    block(f"PREDICTOR {plabel}  ->  last-half-hour return rL   (continuous OLS + sign)")
    print(f"  {'split':6s} {'N':>5s} | {'OLS slope':>9s} {'R2%':>6s} {'t':>6s} {'boot95%CI(slope)':>22s}"
          f" | {'sign hit%':>9s} {'meanFav(bps)':>12s} {'t_fav':>6s}")
    for split in ("DEV", "HOLD", "ALL"):
        rr = [r for r in rows if split == "ALL" or r["split"] == split]
        x = [r[pkey] for r in rr]; y = [r["rL"] for r in rr]
        b, a, r2, t, n = ols(x, y)
        lo, hi = boot_slope_ci(x, y)
        ss = sign_stats(x, y)
        if not np.isfinite(b):
            print(f"  {split:6s} {n:5d} | (insufficient)")
            continue
        print(f"  {split:6s} {n:5d} | {b:+9.3f} {r2:6.2f} {t:+6.2f} [{lo:+.3f},{hi:+.3f}]"
              f" | {ss['hit']:8.1f}% {ss['fav']:+12.3f} {ss['t']:+6.2f}")


def report_by_year(rows, pkey, plabel):
    print(f"\n  BY YEAR — {plabel}: N, sign hit%, meanFav(bps), t_fav, OLS R2%")
    print(f"    {'year':5s} {'N':>5s} {'hit%':>6s} {'meanFav':>8s} {'t_fav':>6s} {'R2%':>6s}")
    for yk in ["2022", "2023", "2024", "2025", "2026"]:
        rr = [r for r in rows if r["year"] == yk]
        x = [r[pkey] for r in rr]; y = [r["rL"] for r in rr]
        ss = sign_stats(x, y)
        _, _, r2, _, n = ols(x, y)
        if ss["n"] < 10:
            continue
        print(f"    {yk:5s} {ss['n']:5d} {ss['hit']:5.1f}% {ss['fav']:+8.2f} {ss['t']:+6.2f} "
              f"{r2 if np.isfinite(r2) else float('nan'):6.2f}")


def dev_quartile_edges(rows, key, absval=False):
    a = np.array([abs(r[key]) if absval else r[key] for r in rows
                  if r["split"] == "DEV" and r.get(key) is not None and np.isfinite(r[key])], float)
    if len(a) < 8:
        return None
    return np.percentile(a, [25, 50, 75])


def bucket(v, edges):
    if v is None or not np.isfinite(v) or edges is None:
        return None
    if v <= edges[0]:
        return "Q1"
    if v <= edges[1]:
        return "Q2"
    if v <= edges[2]:
        return "Q3"
    return "Q4"


def report_conditioning(rows, pkey, plabel):
    block(f"CONDITIONING — is {plabel}->rL predictability STRONGER on high-vol / big-move days?")

    # VIX prior-close ladder
    print(f"  VIX prior-close ladder (sign hit% / meanFav bps / t_fav / N):")
    print(f"    {'bucket':8s} | {'DEV':>28s} | {'HOLD':>28s}")
    for lbl, lo, hi in (("<15", 0, 15), ("15-20", 15, 20), ("20-25", 20, 25),
                        ("25-30", 25, 30), (">=30", 30, 999)):
        cells = []
        for split in ("DEV", "HOLD"):
            rr = [r for r in rows if r["split"] == split and r["vix_prev"] is not None
                  and lo <= r["vix_prev"] < hi]
            ss = sign_stats([r[pkey] for r in rr], [r["rL"] for r in rr])
            cells.append(f"{ss['hit']:5.1f}% {ss['fav']:+6.2f} t{ss['t']:+4.1f} n{ss['n']}"
                         if ss["n"] >= 5 else f"{'(n<5)':>28s}")
        print(f"    {lbl:8s} | {cells[0]:>28s} | {cells[1]:>28s}")

    # realized-vol-so-far quartiles (DEV edges)
    rv_edges = dev_quartile_edges(rows, "rvol_sofar")
    print(f"\n  Realized-vol-so-far (09:30-15:30) DEV-quartile buckets  (edges={np.round(rv_edges,3) if rv_edges is not None else None} bps/min):")
    print(f"    {'bucket':8s} | {'DEV':>28s} | {'HOLD':>28s}")
    for q in ("Q1", "Q2", "Q3", "Q4"):
        cells = []
        for split in ("DEV", "HOLD"):
            rr = [r for r in rows if r["split"] == split and bucket(r["rvol_sofar"], rv_edges) == q]
            ss = sign_stats([r[pkey] for r in rr], [r["rL"] for r in rr])
            cells.append(f"{ss['hit']:5.1f}% {ss['fav']:+6.2f} t{ss['t']:+4.1f} n{ss['n']}"
                         if ss["n"] >= 5 else f"{'(n<5)':>28s}")
        print(f"    {q:8s} | {cells[0]:>28s} | {cells[1]:>28s}")

    # |r1| quartiles (big first-half-hour move; DEV edges)
    a1_edges = dev_quartile_edges(rows, pkey, absval=True)
    print(f"\n  |{plabel}| DEV-quartile buckets (big first-half-hour move; edges={np.round(np.array(a1_edges)*1e4,1) if a1_edges is not None else None} bps):")
    print(f"    {'bucket':8s} | {'DEV':>28s} | {'HOLD':>28s}")
    for q in ("Q1", "Q2", "Q3", "Q4"):
        cells = []
        for split in ("DEV", "HOLD"):
            rr = [r for r in rows if r["split"] == split and bucket(abs(r[pkey]) if r[pkey] is not None else None, a1_edges) == q]
            ss = sign_stats([r[pkey] for r in rr], [r["rL"] for r in rr])
            cells.append(f"{ss['hit']:5.1f}% {ss['fav']:+6.2f} t{ss['t']:+4.1f} n{ss['n']}"
                         if ss["n"] >= 5 else f"{'(n<5)':>28s}")
        print(f"    {q:8s} | {cells[0]:>28s} | {cells[1]:>28s}")


def main():
    print("=" * 108)
    print("MARKET INTRADAY MOMENTUM (Gao-Han-Li-Zhou JFE 2018) — PART 1 PREDICTIVE TEST on SPX 2022-2026")
    print("  first-half-hour return -> last-half-hour return; independent IBKR SPX ruler (no parity spot)")
    print(f"  DEV {DEV_START}..{DEV_END}  |  HOLDOUT {HOLDOUT_START}..{HOLDOUT_END}")
    print("  Published in-sample R^2 ~1.6% (SPY 1993-2013) — MODEST; test honestly for decay + OOS.")
    print("=" * 108)

    rows = build_rows()
    nd = sum(r["split"] == "DEV" for r in rows); nh = sum(r["split"] == "HOLD" for r in rows)
    print(f"\n  sessions with a complete return set: {len(rows)}  (DEV {nd} / HOLD {nh})")
    onn = sum(1 for r in rows if r["r1_on"] is not None)
    print(f"  of which have r1_on (overnight variant, needs prior close): {onn}")

    # unconditional last-half-hour return (drift baseline the effect sits on top of)
    rLall = np.array([r["rL"] for r in rows], float) * 1e4
    print(f"  unconditional rL: mean={rLall.mean():+.3f} bps  std={rLall.std():.2f} bps  "
          f"(the last-30-min move is TINY -- this is the monetization hurdle)")

    for pkey, plabel in (("r1_on", "r1_on (10:00/prevclose-1, headline)"),
                         ("r1_id", "r1_id (10:00/open-1, pure intraday)"),
                         ("r12", "r12 (15:30/15:00-1, penultimate, causal@entry)")):
        report_predictor(rows, pkey, plabel)
        report_by_year(rows, pkey, plabel)

    # combined sign(r1_on)+sign(r12): trade only when they AGREE (paper's combined signal)
    block("COMBINED SIGNAL — trade only when sign(r1_on)==sign(r12) (agreement gate)")
    print(f"  {'split':6s} {'N_agree':>7s} {'covg%':>6s} {'hit%':>6s} {'meanFav(bps)':>12s} {'t_fav':>6s}")
    for split in ("DEV", "HOLD", "ALL"):
        rr = [r for r in rows if (split == "ALL" or r["split"] == split) and r["r1_on"] is not None]
        agree = [r for r in rr if np.sign(r["r1_on"]) == np.sign(r["r12"]) and r["r1_on"] != 0]
        ss = sign_stats([r["r1_on"] for r in agree], [r["rL"] for r in agree])
        cov = 100.0 * len(agree) / len(rr) if rr else float("nan")
        print(f"  {split:6s} {ss['n']:7d} {cov:5.1f}% {ss['hit']:5.1f}% {ss['fav']:+12.3f} {ss['t']:+6.2f}")

    # conditioning on the headline predictor
    report_conditioning(rows, "r1_on", "r1_on")

    # ---- CSV ----
    csvp = os.path.join(SCRATCH, "intraday_momentum_sessions.csv")
    cols = ["date", "split", "year", "prior_close", "p_open", "p1000", "p1500", "p1530", "p1600",
            "r1_on", "r1_id", "r12", "rL", "vix_prev", "rvol_sofar"]
    with open(csvp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\n  per-session CSV -> {csvp}  ({len(rows)} rows)")

    block("PART 1 READ")
    print("  A REAL effect: OLS slope>0 with |t|>~2 AND boot CI excludes 0 AND sign-hit>50 on BOTH DEV")
    print("  and HOLD, ideally STRONGER in the high-VIX / high-rvol / big-|r1| buckets (paper's claim).")
    print("  A DECAYED effect: slope~0 / R2<<1% / hit~50 / t~0, or DEV-only that dies OOS. Given the")
    print("  published R2 was only ~1.6%, expect at most a THIN tilt; judge monetization in Part 2.")


if __name__ == "__main__":
    main()
