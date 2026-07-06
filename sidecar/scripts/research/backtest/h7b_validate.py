"""H7b development-set full-stack validation (locked config, zero tuning). Reports
expectancy/R/Sharpe, DSR @ N_trials (full H1-H7 load), by-year, and the DECIDING metric:
loss distribution + max-drawdown ($ and % of account) + vol-event content. STOPS before
the holdout -- user decides whether the dev results + drawdown justify the one holdout look.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np

from scripts.research.methodology import dsr_both_ways

from .credit import simulate_iron_condor_managed as mgd
from .data import Session, connect, sessions
from .holdout import assert_dev, dev_range

ET = ZoneInfo("America/New_York")
N_TRIALS = 200          # honest count: all H1-H7 configs + variants + controls across both tracks
ACCOUNTS = [25000, 50000]
WING = 10


def emin(s, hh):
    for ts in s.minutes:
        h = dt.datetime.fromtimestamp(ts / 1000, ET)
        if h.hour + h.minute / 60 >= hh:
            return ts
    return None


def regime_at(con, d, ts):
    r = con.execute("SELECT regime FROM option_dealer_exposure WHERE session_date=? AND ts_ms<=? "
                    "ORDER BY ts_ms DESC LIMIT 1", (d, ts)).fetchone()
    return r[0] if r else None


def block_boot(x, block=5, iters=4000):
    n = len(x)
    rng = np.random.default_rng(0)
    nb = int(np.ceil(n / block))
    means = np.empty(iters)
    for k in range(iters):
        idx = []
        for _ in range(nb):
            st = int(rng.integers(0, n))
            idx.extend((st + j) % n for j in range(block))
        means[k] = np.mean([x[i] for i in idx[:n]])
    return np.percentile(means, 2.5), np.percentile(means, 97.5)


def main():
    con = connect()
    start, end = dev_range()
    assert_dev(end)
    rows = []
    for d in sessions(con, start, end):
        s = Session(con, d)
        if s.empty():
            continue
        ets = emin(s, 14.0)
        if ets is None:
            continue
        atm = s.atm(ets)
        if atm is None:
            continue
        if regime_at(con, d, ets) != "LONG_GAMMA":
            continue
        t = mgd(s, ets, atm, short_off=5, wing=WING, qty=3, profit_target=999, stop_mult=999)
        if not t:
            continue
        allsp = [x for x in (s.spot(m) for m in s.minutes) if x is not None]
        rng_day = (max(allsp) - min(allsp)) if allsp else 0.0
        rows.append((d, t["pnl"], t["credit"], rng_day))
    rows.sort()
    pnls = [r[1] for r in rows]
    a = np.array(pnls, float)
    n = len(a)
    mean, sd = a.mean(), a.std(ddof=1)
    sharpe = mean / sd if sd else 0
    Rs = [r[1] / (max(0.05, WING - r[2]) * 300) for r in rows]

    print("H7b DEV FULL STACK — near-ATM iron fly, LONG_GAMMA gate, 14:00, hold-to-settle, 3ct\n")
    print(f"n={n}  mean=${mean:+.0f}/trade  total=${a.sum():+,.0f}  win={100*(a>0).mean():.0f}%  meanR={np.mean(Rs):+.2f}")
    print(f"per-trade Sharpe={sharpe:.3f}  t-stat={sharpe*np.sqrt(n):.2f}")
    lo, hi = block_boot(pnls)
    print(f"moving-block bootstrap 95% CI on mean: [${lo:+.0f}, ${hi:+.0f}]  "
          f"({'excludes 0 -> positive' if lo > 0 else 'straddles 0'})")
    try:
        d = dsr_both_ways(pnls, N_TRIALS)
        print(f"DSR @ N_trials={N_TRIALS}: dsr_global={d['dsr_global']:.3f}  (KILL if < 0.30)")
    except Exception as e:
        print(f"DSR: error ({e})")

    print("\nby year (must survive >1):")
    for y in sorted({r[0][:4] for r in rows}):
        yp = [r[1] for r in rows if r[0][:4] == y]
        print(f"  {y}: n={len(yp):3d}  mean=${np.mean(yp):+5.0f}  total=${np.sum(yp):+8,.0f}  win={100*np.mean([p>0 for p in yp]):3.0f}%")

    sp = sorted(pnls)
    wins, losses = a[a > 0], a[a <= 0]
    print("\nLOSS DISTRIBUTION (the deciding metric):")
    print(f"  avg win=${wins.mean():+.0f}  avg loss=${losses.mean():+.0f}  W/L={wins.mean()/abs(losses.mean()):.2f}")
    print(f"  5 worst trades: {['$%+.0f' % x for x in sp[:5]]}")
    cum = peak = dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    accts = "  ".join(f"{100*abs(dd)/ac:.0f}% of ${ac//1000}k" for ac in ACCOUNTS)
    print(f"  MAX DRAWDOWN: ${dd:+,.0f}   ({accts})")
    ve = [r for r in rows if r[3] >= 60]
    print(f"\nVOL-EVENT content (gated day range>=60pt): {len(ve)}/{n} ({100*len(ve)/max(n,1):.0f}%)")
    if ve:
        vp = [r[1] for r in ve]
        print(f"  P&L on vol-event days: mean=${np.mean(vp):+.0f}  worst=${min(vp):+.0f}  "
              f"(if light, the tail is UNDER-tested -> holdout vol content critical)")


if __name__ == "__main__":
    main()
