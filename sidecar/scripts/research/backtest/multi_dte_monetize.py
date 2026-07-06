"""1-3 DTE long-call monetization of the put-fade BIG-BOUNCE SUBSET — does a slightly longer-dated
call survive theta where 0DTE didn't? The direction/move is identical; only theta changes.

Entry = put-fade subset (EXPAND + surge r>=2) -> buy a CALL at dte 1/2/3, strike ATM or +15, hold to
+30min, REAL option_quote_multidte NBBO (friction $1.30 RT + $0.10 slip). $/ct BY YEAR. Compare to the
0DTE baseline (big_bounce_monetize: ALL -$16/ct, 2022 -$107/ct). multidte covers 2022..2025-08.
Read-only. Run:  .venv/Scripts/python.exe -m scripts.research.backtest.multi_dte_monetize [START END]

*** LOOKAHEAD AUDIT (FLAGGED, minor): the ENTRY DECISION is the FLOW surge (option_flow_minute,
option/flow clock) and the option is filled at the option minute <= surge ts, so the trade side/timing
is CONTEMPORANEOUS (no material lookahead — unlike orb_monetize/convexity which decide on a
START-stamped price bar). The one residual is STRIKE CENTERING: s0 = real_at(rt, rp, ts) reads the
IBKR ruler bar labeled ts, whose close is ~60s ahead of the surge instant. On a 5pt strike grid this
almost never changes the chosen strike, so it is left as-is; big_bounce_monetize centers on the
contemporaneous parity spot sess.spot(ets) and is the cleaner reference. triggers-on-bar-close=NO
(flow-triggered) fills-same-minute=CONTEMPORANEOUS had-lookahead=NO(material) → FLAGGED(minor strike).
"""
from __future__ import annotations

import bisect
import sys

import numpy as np

from .big_bounce_subset import fade_entries
from .data import BAD, connect
from .direction_confluence_realspot import trail_range
from .exits import COMMISSION_RT, SLIP
from .holdout import HOLDOUT_START
from .ibkr_ruler import ibkr_open, ibkr_series
from .thetadata_real_spot_validate import real_at

sys.stdout.reconfigure(encoding="utf-8")
STRIKES = [0, 15]
DTES = [1, 2, 3]
TRAIL_MIN = 15
R_MIN = 2.0
HOLD = 30


def load_calls(con, d):
    rows = con.execute(
        "SELECT timestamp_ms, strike, dte, bid, ask, quote_quality FROM option_quote_multidte "
        "WHERE session_date=? AND right='C' AND dte BETWEEN 1 AND 3", (d,)).fetchall()
    if not rows:
        return None
    q, mins, strikes = {}, set(), set()
    for ts, k, dte, bid, ask, ql in rows:
        q[(ts, k, dte)] = (bid, ask, ql)
        mins.add(ts); strikes.add(k)
    return q, sorted(mins), sorted(strikes)


def hold_pc(q, mins, strike, dte, ets, target):
    """per-contract $ of a long call: buy ask+slip at ets, sell bid-slip at last minute<=target."""
    e = q.get((ets, strike, dte))
    if not e or e[2] in BAD:
        return None
    buy = e[1] + SLIP
    if buy <= 0.05:
        return None
    lo = bisect.bisect_right(mins, ets)
    hi = bisect.bisect_right(mins, target)
    for m in reversed(mins[lo:hi]):
        v = q.get((m, strike, dte))
        if v and v[2] not in BAD:
            sell = max(0.0, v[0] - SLIP)
            return (sell - buy) * 100 - COMMISSION_RT
    return None


def cl(vals):
    a = np.array([v for v in vals if v is not None and np.isfinite(v)], float)
    n = len(a)
    if n < 2 or a.std(ddof=1) == 0:
        return (float(a.mean()) if n else float("nan")), float("nan"), n
    return float(a.mean()), float(a.mean() / (a.std(ddof=1) / np.sqrt(n))), n


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2022-01-03"
    end = sys.argv[2] if len(sys.argv) > 2 else "2025-08-12"
    con = connect()
    ic = ibkr_open()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 100)
    print("1-3 DTE long-call monetization — put-fade big-bounce subset, hold +30min ($/ct BY YEAR)")
    print(f"  {start}..{end} ({len(dates)} sess); strikes {STRIKES}; dtes {DTES}; vs 0DTE ALL -$16 / 2022 -$107")
    print("=" * 100)

    recs = []
    n_ent = 0
    for k, d in enumerate(dates):
        ents = fade_entries(con, d)
        if not ents:
            continue
        rs = ibkr_series(ic, d)
        if rs is None:
            continue
        rt, rp = rs
        calls = load_calls(con, d)
        if calls is None:
            continue
        q, mins, strikes = calls
        year = d[:4]
        trs = [trail_range(rt, rp, ts - TRAIL_MIN * 60_000, ts) for ts, _ in ents]
        tr_med = float(np.median([t for t in trs if t is not None])) if any(t is not None for t in trs) else None
        for (ts, r), tr in zip(ents, trs):
            expand = (tr is not None and tr_med is not None and tr > tr_med)
            if not (expand and r >= R_MIN):
                continue
            s0 = real_at(rt, rp, ts)
            if s0 is None:
                continue
            j = bisect.bisect_right(mins, ts) - 1
            if j < 0:
                continue
            ets = mins[j]
            if ts - ets > 3 * 60_000:      # entry quote must be within 3 min of the surge
                continue
            for N in STRIKES:
                strike = min(strikes, key=lambda kk: abs(kk - (s0 + N)))
                for dte in DTES:
                    pc = hold_pc(q, mins, strike, dte, ets, ets + HOLD * 60_000)
                    if pc is not None:
                        recs.append(dict(year=year, N=N, dte=dte, pc=pc))
            n_ent += 1
        if (k + 1) % 150 == 0:
            print(f"  ...{k+1}/{len(dates)} ({d}) subset_entries={n_ent}", flush=True)

    print(f"\n  subset entries: {n_ent}\n")
    YEARS = ["2022", "2023", "2024", "2025", "ALL"]
    for N in STRIKES:
        print("=" * 100)
        print(f"STRIKE +{N} OTM   (per-contract $, hold +30min; +ve = 1-3 DTE call PAYS)")
        print(f"  {'dte':4s} " + " ".join(f"{y:>14s}" for y in YEARS) + "   win%(ALL)")
        for dte in DTES:
            cells = []
            for yr in YEARS:
                vals = [r["pc"] for r in recs if r["N"] == N and r["dte"] == dte
                        and (yr == "ALL" or r["year"] == yr)]
                m, t, n = cl(vals)
                cells.append(f"{m:+7.1f}(t{t:+4.1f})" if n >= 2 else f"{'·':>14s}")
            allv = [r["pc"] for r in recs if r["N"] == N and r["dte"] == dte]
            w = 100.0 * np.mean([p > 0 for p in allv]) if allv else float("nan")
            print(f"  {dte}DTE " + " ".join(f"{c:>14s}" for c in cells) + f"   {w:4.0f}%")
        print()
    print("=" * 100)
    print("READ: +ve $/ct across years (esp. 2022 flipping from -$107) with |t|>~2 = theta was the barrier")
    print("  and a 1-3 DTE call is the tradeable LONG expression. Still negative = the edge isn't long-monetizable.")
    print("=" * 100)


if __name__ == "__main__":
    main()
