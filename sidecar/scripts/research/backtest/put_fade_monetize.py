"""DECISIVE $ test: does the validated PUT-FADE pay in real option DOLLARS — and which EXIT holds it?

Entry = aggressive 0DTE put surge (net_signed_prem PUT-dominant, the validated fade trigger) ->
go LONG a CALL. Sweep strike (ATM/15/25/35 OTM). REAL ThetaData option NBBO, friction = $1.30 RT +
$0.10 slip (exits.py). Split ALL vs PUT x EXPANDING-vol. Per-contract $ + t + win% BY YEAR. qty=3.

KEY: the direction is validated (fade wins ~65% at +30min) but a TIGHT spot stop gets noise-hit at
the capitulation low ([[naked-long-direction-null]]). So we test exits that can HOLD the bounce:
  hold20 / hold30 - exit ALL at +20/+30min (pure directional capture; the clean diagnostic)
  bcd_trail       - option-% ladder + trailing peak (no spot stop)
  ladder_noStop   - be_ladder, NO initial stop, BE-trail after T1, 45m cap
  R1_tightstop    - the validated-geometry tight -1pt stop (CONTRAST: expected to lose)

Read-only DB. Run:  .venv/Scripts/python.exe -m scripts.research.backtest.put_fade_monetize [START END]
"""
from __future__ import annotations

import bisect
import sys

import numpy as np

from .data import BAD, Session, connect
from .direction_confluence_realspot import surges, trail_range
from .exits import SLIP, _entry, _result, _sell, simulate_bcd, simulate_be_ladder
from .holdout import HOLDOUT_START
from .ibkr_ruler import ibkr_open, ibkr_series

sys.stdout.reconfigure(encoding="utf-8")

QTY = 3
STRIKES = [0, 15, 25, 35]
TRAIL_MIN = 15


def hold_to(sess, ets, strike, hmin, qty=QTY):
    """Buy CALL at entry, exit ALL at ~+hmin min (last valid quote at/before target)."""
    ep = _entry(sess, ets, "C", strike, SLIP)
    if ep is None:
        return None
    target = ets + hmin * 60_000
    lo = bisect.bisect_right(sess.minutes, ets)
    hi = bisect.bisect_right(sess.minutes, target)
    for m in reversed(sess.minutes[lo:hi]):
        q = sess.quote(m, strike, "C")
        if q and q[2] not in BAD:
            sp = _sell(q[0], SLIP)
            return _result("C", strike, ets, ep, qty, sp * qty * 100, [(qty, round(sp, 2))], f"hold{hmin}", m)
    return None


EXITS = {
    "hold20": lambda s, e, k: hold_to(s, e, k, 20),
    "hold30": lambda s, e, k: hold_to(s, e, k, 30),
    "bcd_trail": lambda s, e, k: simulate_bcd(s, e, "C", k, qty=QTY, max_hold_min=45),
    "ladder_noStop": lambda s, e, k: simulate_be_ladder(
        s, e, "C", k, qty=QTY, stop_pts=8, initial_stop=False, dollar_cap=None,
        runner_protect=True, max_hold_min=45),
    "R1_tightstop": lambda s, e, k: simulate_be_ladder(
        s, e, "C", k, qty=QTY, stop_pts=1.0, initial_stop=True, dollar_cap=None,
        runner_protect=True, cut_if_flat_min=20, max_hold_min=90),
}


def cl(vals):
    a = np.array([v for v in vals if v is not None and np.isfinite(v)], float)
    n = len(a)
    if n < 2 or a.std(ddof=1) == 0:
        return (float(a.mean()) if n else float("nan")), float("nan"), n
    return float(a.mean()), float(a.mean() / (a.std(ddof=1) / np.sqrt(n))), n


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2022-01-03"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    ic = ibkr_open()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 108)
    print("PUT-FADE MONETIZATION — long CALL on aggressive put surge; which EXIT holds it? ($/ct BY YEAR)")
    print(f"  {start}..{end} ({len(dates)} sess); strikes OTM {STRIKES}; exits {list(EXITS)}; qty={QTY}")
    print("=" * 108)

    recs = []
    n_ent = 0
    for k, d in enumerate(dates):
        ents = [ts for ts, dirn in surges(con, d) if dirn < 0]
        if not ents:
            continue
        sess = Session(con, d)
        if sess.empty() or not sess.strikes:
            continue
        rs = ibkr_series(ic, d)
        year, split = d[:4], ("DEV" if d < HOLDOUT_START else "HOLD")
        meta, trs = [], []
        for ts in ents:
            ets = sess.minute_at_or_before(ts)
            if ets is None:
                continue
            spot0 = sess.spot(ets)
            if spot0 is None:
                continue
            tr = trail_range(rs[0], rs[1], ts - TRAIL_MIN * 60_000, ts) if rs is not None else None
            if tr is not None:
                trs.append(tr)
            meta.append((ets, spot0, tr))
        tr_med = float(np.median(trs)) if trs else None
        for ets, spot0, tr in meta:
            expand = (tr is not None and tr_med is not None and tr > tr_med)
            for N in STRIKES:
                strike = min(sess.strikes, key=lambda kk: abs(kk - (spot0 + N)))
                for xname, fn in EXITS.items():
                    t = fn(sess, ets, strike)
                    if t is None:
                        continue
                    recs.append(dict(year=year, split=split, N=N, exit=xname,
                                     expand=expand, pc=t["pnl_per_contract"]))
            n_ent += 1
        if (k + 1) % 100 == 0:
            print(f"  ...{k+1}/{len(dates)} ({d}) entries={n_ent}", flush=True)

    print(f"\n  entries: {n_ent}\n")
    YEARS = ["2022", "2023", "2024", "2025", "2026", "ALL", "HOLD"]

    def sel(xname, sig, N, yr):
        out = []
        for r in recs:
            if r["exit"] != xname or r["N"] != N:
                continue
            if sig == "EXPAND" and not r["expand"]:
                continue
            if yr == "HOLD" and r["split"] != "HOLD":
                continue
            if yr not in ("ALL", "HOLD") and r["year"] != yr:
                continue
            out.append(r["pc"])
        return out

    for xname in EXITS:
        for sig in ("ALL", "EXPAND"):
            print("=" * 108)
            print(f"EXIT {xname}   SIGNAL {sig}   (per-contract $, net friction; +ve = the fade PAYS)")
            print(f"  {'strike':7s} " + " ".join(f"{y:>13s}" for y in YEARS) + "   win%(ALL)")
            for N in STRIKES:
                cells = []
                for yr in YEARS:
                    m, t, n = cl(sel(xname, sig, N, yr))
                    cells.append(f"{m:+6.1f}(t{t:+4.1f})" if n >= 2 else f"{'·':>13s}")
                pcs = sel(xname, sig, N, 'ALL')
                w = 100.0 * np.mean([p > 0 for p in pcs]) if pcs else float('nan')
                print(f"  +{N:<5d} " + " ".join(f"{c:>13s}" for c in cells) + f"   {w:4.0f}%")
        print()
    print("=" * 108)
    print("READ: if hold20/hold30/bcd/ladder_noStop show +ve $/ct (esp. EXPAND) while R1_tightstop loses,")
    print("  the edge is REAL money gated by EXIT GEOMETRY (hold the bounce, don't tight-stop the dip).")
    print("=" * 108)


if __name__ == "__main__":
    main()
