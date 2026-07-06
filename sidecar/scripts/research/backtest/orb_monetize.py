"""ORB $ MONETIZATION — does the Opening-Range breakout pay in real option DOLLARS?

Direction comes from the PRICE breakout on the independent IBKR ruler (orb_core); the trade is
a LONG option in the break direction (up-break -> CALL, down-break -> PUT), priced on REAL NBBO
with the standard friction ($1.30 RT commission + $0.10 slip). Per-contract $ BY YEAR, split by:

*** LOOKAHEAD FIX (methodology audit): the IBKR ruler bars are START-stamped, so an OR break
confirmed by a bar CLOSE occurs at wall-clock ~ts+60s. Options are now filled at the first quote
AT/AFTER ts+60s (both the 0DTE and 1-3DTE branches) instead of minute_at_or_before(ts), which had
bought the option ~1 min before the break was confirmable (~+$150/ct of free move; the convexity
family had the identical bug). triggers-on-bar-close=YES fills-same-minute=WAS-YES→FIXED.

  ENTRY  : OR15 / OR30 (first raw break)  and  OR15f / OR30f (filtered: >2pt clear, first 90min)
  STRIKE : ATM  and  +15 OTM (call: spot+15 ; put: spot-15)
  EXIT   : be_ladder (staged 1.5/2.5/5R scale-out + BE-ladder trail + runner-protect, no stop,
           hold-to-EOD fallback)   and   hold_eod (buy the break, sell all at the close)
  SIDE   : CALL vs PUT (0DTE PUT side has historically been the loser — reported explicitly)

0DTE (data.Session, 2022-2026) AND 1-3 DTE (option_quote_multidte, 2022..2025-08; hold-to-EOD &
hold+60min) since 0DTE theta is brutal on a slow-developing breakout.

Read-only. Run: .venv/Scripts/python.exe -m scripts.research.backtest.orb_monetize [START END]
"""
from __future__ import annotations

import bisect
import sys

import numpy as np

from .data import BAD, Session, connect
from .exits import COMMISSION_RT, SLIP, _entry, _result, _sell, simulate_be_ladder
from .holdout import HOLDOUT_START
from .ibkr_ruler import ibkr_open, ibkr_series
from .orb_core import FILT_BUFFER_PT, FILT_MAX_MIN, breaks, open_ms, or_band
from .thetadata_real_spot_validate import real_at

sys.stdout.reconfigure(encoding="utf-8")

QTY = 3
STRIKES = [("ATM", 0), ("OTM15", 15)]
ENTRY_DEFS = [(15, "OR15"), (30, "OR30")]
MDTE_MAX = "2025-08-12"        # option_quote_multidte coverage ends here
YEARS = ["2022", "2023", "2024", "2025", "2026", "ALL", "HOLD"]


# ------------------------------------------------------------------ entry generation
def session_entries(rt, rp, t_open):
    """[(label, ts, dir)] first-break entries for OR15/OR30 (raw) + OR15f/OR30f (filtered)."""
    out = []
    for or_min, tag in ENTRY_DEFS:
        band = or_band(rt, rp, t_open, or_min)
        if band is None:
            continue
        hi, lo, _rng, or_end = band
        raw = breaks(rt, rp, t_open, hi, lo, or_end)
        if raw:
            out.append((tag, raw[0][0], raw[0][1]))
        filt = breaks(rt, rp, t_open, hi, lo, or_end, buffer_pt=FILT_BUFFER_PT, max_min=FILT_MAX_MIN)
        if filt:
            out.append((tag + "f", filt[0][0], filt[0][1]))
    return out


# ------------------------------------------------------------------ 0DTE exits
def hold_eod_0dte(sess, ets, right, strike, qty=QTY):
    ep = _entry(sess, ets, right, strike, SLIP)
    if ep is None:
        return None
    for m in reversed(sess.minutes):
        if m <= ets:
            break
        q = sess.quote(m, strike, right)
        if q and q[2] not in BAD:
            sp = _sell(q[0], SLIP)
            return _result(right, strike, ets, ep, qty, sp * qty * 100, [(qty, round(sp, 2))], "eod", m)
    return None


def be_ladder_0dte(sess, ets, right, strike, qty=QTY):
    return simulate_be_ladder(sess, ets, right, strike, qty=qty, stop_pts=8, initial_stop=False,
                              dollar_cap=None, runner_protect=True, max_hold_min=None)


EXITS_0DTE = {"be_ladder": be_ladder_0dte, "hold_eod": hold_eod_0dte}


# ------------------------------------------------------------------ 1-3 DTE (both rights)
def load_mdte(con, d):
    rows = con.execute(
        "SELECT timestamp_ms,strike,right,dte,bid,ask,quote_quality FROM option_quote_multidte "
        "WHERE session_date=? AND dte BETWEEN 1 AND 3", (d,)).fetchall()
    if not rows:
        return None
    q, mins, strikes = {}, set(), set()
    for ts, k, r, dte, bid, ask, ql in rows:
        q[(ts, k, r, dte)] = (bid, ask, ql)
        mins.add(ts)
        strikes.add(k)
    return q, sorted(mins), sorted(strikes)


def hold_mdte(q, mins, strike, right, dte, ets, target):
    e = q.get((ets, strike, right, dte))
    if not e or e[2] in BAD:
        return None
    buy = e[1] + SLIP
    if buy <= 0.05:
        return None
    lo = bisect.bisect_right(mins, ets)
    hi = bisect.bisect_right(mins, target)
    for m in reversed(mins[lo:hi]):
        v = q.get((m, strike, right, dte))
        if v and v[2] not in BAD:
            sell = max(0.0, v[0] - SLIP)
            return (sell - buy) * 100 - COMMISSION_RT
    return None


def snap(strikes, target):
    return min(strikes, key=lambda k: abs(k - target))


def cl(vals):
    a = np.array([v for v in vals if v is not None and np.isfinite(v)], float)
    n = len(a)
    if n < 2 or a.std(ddof=1) == 0:
        return (float(a.mean()) if n else float("nan")), float("nan"), n
    return float(a.mean()), float(a.mean() / (a.std(ddof=1) / np.sqrt(n))), n


def cell(vals):
    m, t, n = cl(vals)
    return f"{m:+6.0f}(t{t:+4.1f})" if n >= 2 else f"{'·':>13s}"


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2022-01-03"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    ic = ibkr_open()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 112)
    print("ORB $ MONETIZATION — long option in the break direction; REAL NBBO, net friction ($/ct BY YEAR)")
    print(f"  {start}..{end} ({len(dates)} sess); entries OR15/OR30(+f); strikes ATM/+15OTM; qty={QTY}")
    print("=" * 112)

    d0 = []   # 0DTE recs: dict(entry,strike,exit,side,year,split,pc)
    dm = []   # multidte recs: dict(entry,strike,dte,hold,side,year,split,pc)
    n0 = nm = 0
    for k, d in enumerate(dates):
        rs = ibkr_series(ic, d)
        if rs is None:
            continue
        rt, rp = rs
        ents = session_entries(rt, rp, open_ms(d))
        if not ents:
            continue
        year, split = d[:4], ("HOLD" if d >= HOLDOUT_START else "DEV")
        # attach true-ruler spot at each break
        ents = [(lab, ts, dirn, real_at(rt, rp, ts)) for (lab, ts, dirn) in ents]
        ents = [(lab, ts, dirn, s0) for (lab, ts, dirn, s0) in ents if s0 is not None]
        if not ents:
            continue

        # ---- 0DTE ----
        sess = Session(con, d)
        if not sess.empty() and sess.strikes:
            for lab, ts, dirn, s0 in ents:
                # NO-LOOKAHEAD: OR break confirmed by the ruler bar CLOSE (~ts+60s, START-stamped
                # ruler) -> fill the option at the first quote AT/AFTER ts+60s.
                ets = sess.minute_at_or_after(ts + 60_000)
                if ets is None or ets - (ts + 60_000) > 3 * 60_000:
                    continue
                right = "C" if dirn > 0 else "P"
                side = "CALL" if dirn > 0 else "PUT"
                for sname, off in STRIKES:
                    strike = snap(sess.strikes, s0 + dirn * off)
                    for xname, fn in EXITS_0DTE.items():
                        t = fn(sess, ets, right, strike)
                        if t is None:
                            continue
                        d0.append(dict(entry=lab, strike=sname, exit=xname, side=side,
                                       year=year, split=split, pc=t["pnl_per_contract"]))
                n0 += 1

        # ---- 1-3 DTE ----
        if d <= MDTE_MAX:
            md = load_mdte(con, d)
            if md is not None:
                q, mins, mstr = md
                for lab, ts, dirn, s0 in ents:
                    j = bisect.bisect_left(mins, ts + 60_000)   # NO-LOOKAHEAD: first quote >= bar close
                    if j >= len(mins):
                        continue
                    ets = mins[j]
                    if ets - (ts + 60_000) > 3 * 60_000:
                        continue
                    right = "C" if dirn > 0 else "P"
                    side = "CALL" if dirn > 0 else "PUT"
                    for sname, off in STRIKES:
                        strike = snap(mstr, s0 + dirn * off)
                        for dte in (1, 2, 3):
                            for hname, tgt in (("EOD", mins[-1]), ("+60", ets + 60 * 60_000)):
                                pc = hold_mdte(q, mins, strike, right, dte, ets, tgt)
                                if pc is not None:
                                    dm.append(dict(entry=lab, strike=sname, dte=dte, hold=hname,
                                                   side=side, year=year, split=split, pc=pc))
                    nm += 1
        if (k + 1) % 150 == 0:
            print(f"  ...{k+1}/{len(dates)} ({d}) 0dte_ent={n0} mdte_ent={nm}", flush=True)

    print(f"\n  0DTE entries scored: {n0}    1-3DTE entries scored: {nm}\n")

    def sel0(recs, entry, strike, xname, yr, side=None):
        out = []
        for r in recs:
            if r["entry"] != entry or r["strike"] != strike or r["exit"] != xname:
                continue
            if side is not None and r["side"] != side:
                continue
            if yr == "HOLD" and r["split"] != "HOLD":
                continue
            if yr not in ("ALL", "HOLD") and r["year"] != yr:
                continue
            out.append(r["pc"])
        return out

    # =============================== 0DTE ===============================
    for xname in EXITS_0DTE:
        print("=" * 112)
        print(f"0DTE  EXIT {xname}   (per-contract $, net friction; +ve = the breakout PAYS)")
        print(f"  {'entry/strike':14s} " + " ".join(f"{y:>13s}" for y in YEARS) + "   win%")
        for entry, _ in [(e[1], e[0]) for e in ENTRY_DEFS] + [(e[1] + "f", e[0]) for e in ENTRY_DEFS]:
            for sname, _ in STRIKES:
                cells = [cell(sel0(d0, entry, sname, xname, yr)) for yr in YEARS]
                allv = sel0(d0, entry, sname, xname, "ALL")
                w = 100.0 * np.mean([p > 0 for p in allv]) if allv else float("nan")
                print(f"  {entry+'/'+sname:14s} " + " ".join(f"{c:>13s}" for c in cells) + f"   {w:4.0f}%")
        print()

    # 0DTE CALL vs PUT asymmetry (headline: OR30, ATM)
    print("=" * 112)
    print("0DTE  CALL vs PUT split (OR30 first break, ATM, ALL years)  — is one side the loser?")
    for xname in EXITS_0DTE:
        for side in ("CALL", "PUT"):
            m, t, n = cl(sel0(d0, "OR30", "ATM", xname, "ALL", side=side))
            w = 100.0 * np.mean([p > 0 for p in sel0(d0, "OR30", "ATM", xname, "ALL", side=side)]) if n else float("nan")
            print(f"  {xname:10s} {side:4s}  n={n:4d}  mean={m:+7.1f}$/ct (t{t:+4.1f})  win={w:4.0f}%")
    print()

    # =============================== 1-3 DTE ===============================
    def selm(entry, strike, dte, hold, yr, side=None):
        out = []
        for r in dm:
            if r["entry"] != entry or r["strike"] != strike or r["dte"] != dte or r["hold"] != hold:
                continue
            if side is not None and r["side"] != side:
                continue
            if yr == "HOLD" and r["split"] != "HOLD":
                continue
            if yr not in ("ALL", "HOLD") and r["year"] != yr:
                continue
            out.append(r["pc"])
        return out

    MYEARS = ["2022", "2023", "2024", "2025", "ALL"]
    for hold in ("EOD", "+60"):
        for entry in ("OR30", "OR30f"):
            print("=" * 112)
            print(f"1-3DTE  ENTRY {entry}  HOLD {hold}   (per-contract $, net friction; multidte 2022..2025-08)")
            print(f"  {'strike/dte':12s} " + " ".join(f"{y:>13s}" for y in MYEARS) + "   win%")
            for sname, _ in STRIKES:
                for dte in (1, 2, 3):
                    cells = [cell(selm(entry, sname, dte, hold, yr)) for yr in MYEARS]
                    allv = selm(entry, sname, dte, hold, "ALL")
                    w = 100.0 * np.mean([p > 0 for p in allv]) if allv else float("nan")
                    print(f"  {sname+'/'+str(dte)+'DTE':12s} " + " ".join(f"{c:>13s}" for c in cells) + f"   {w:4.0f}%")
            print()

    print("=" * 112)
    print("READ: a viable LONG needs +ve $/ct with |t|>~2 that REPEATS across years (not one regime).")
    print("  0DTE theta usually makes the breakout long negative; 1-3DTE flipping +ve = theta was the")
    print("  barrier. CALL>>PUT split = the edge is really just the bull-drift, not the breakout.")
    print("=" * 112)


if __name__ == "__main__":
    main()
