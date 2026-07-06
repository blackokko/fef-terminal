"""H7 complete dev diagnostic (consolidated plan, step 3a). Near-ATM (short ATM+/-5) vs
OTM Dale-style (short ATM+/-20) iron fly/condor, GEX-gated to LONG_GAMMA, 14:00 entry,
both exits (quick PT50%/stop2x vs hold-to-settle), real fills, 3ct. DECIDING METRIC =
expectancy after friction + LOSS DISTRIBUTION (not win rate). Headline = near-ATM beats
OTM once the tail is priced? Reported BEFORE the full stack. STOP after this.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np

from .credit import simulate_iron_condor_managed as mgd
from .data import Session, connect, sessions
from .holdout import assert_dev, dev_range

ET = ZoneInfo("America/New_York")
CELLS = {  # name -> (short_off, profit_target, stop_mult)
    "near-ATM quick-exit": (5, 0.5, 2.0),
    "near-ATM hold-settle": (5, 999, 999),
    "OTM Dale quick-exit": (20, 0.5, 2.0),
    "OTM Dale hold-settle": (20, 999, 999),
}


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


def ldist(recs):
    a = np.array([p for p, _ in recs], float)
    n = len(a)
    if n == 0:
        return None
    wins, losses = a[a > 0], a[a <= 0]
    awl = (wins.mean() / abs(losses.mean())) if (len(losses) and losses.mean()) else float("inf")
    # max drawdown of the equity curve (trades already in date order)
    cum = peak = dd = 0.0
    for p, _ in recs:
        cum += p
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return {"n": n, "win": 100 * (a > 0).mean(), "mean": a.mean(), "total": a.sum(),
            "awl": awl, "avgL": losses.mean() if len(losses) else 0.0,
            "worst": a.min(), "p5": np.percentile(a, 5), "maxdd": dd}


def line(tag, s):
    if not s:
        print(f"  {tag:22s} (none)")
        return
    print(f"  {tag:22s} n={s['n']:3d} win={s['win']:3.0f}% mean=${s['mean']:+5.0f} "
          f"tot=${s['total']:+8.0f} W/L={s['awl']:.2f} avgL=${s['avgL']:+6.0f} "
          f"worst=${s['worst']:+6.0f} maxDD=${s['maxdd']:+8.0f}")


def main():
    con = connect()
    start, end = dev_range()
    assert_dev(end)
    gated = {k: [] for k in CELLS}
    blind = {k: [] for k in CELLS}
    reasons = {k: {} for k in CELLS}
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
        regime = regime_at(con, d, ets)
        if regime is None:
            continue
        for name, (off, pt, sm) in CELLS.items():
            t = mgd(s, ets, atm, short_off=off, wing=10, qty=3, profit_target=pt, stop_mult=sm)
            if not t:
                continue
            blind[name].append((t["pnl"], d))
            if regime == "LONG_GAMMA":
                gated[name].append((t["pnl"], d))
                reasons[name][t["reason"]] = reasons[name].get(t["reason"], 0) + 1

    print("H7 DEV DIAGNOSTIC — GEX-gated (LONG_GAMMA), 14:00, 3ct, real fills\n")
    print("=== HEADLINE: near-ATM vs OTM, both exits, GATED (loss distribution) ===")
    for k in CELLS:
        line(k, ldist([(p, d) for p, d in gated[k]]))
    print("\n=== GATE VALUE: does positive-GEX gate improve the TAIL? (hold-settle) ===")
    line("near-ATM GATED", ldist(gated["near-ATM hold-settle"]))
    line("near-ATM regime-BLIND", ldist(blind["near-ATM hold-settle"]))
    print("\n=== THETA-vs-GAMMA RACE: exit-reason mix (near-ATM quick-exit, gated) ===")
    print(f"  {reasons['near-ATM quick-exit']}")


if __name__ == "__main__":
    main()
