"""PART 2 - REAL-DATA VRP TEST AT THE LONGEST DTE WE ACTUALLY HAVE (1-3 DTE).

Does simply moving OFF 0DTE improve the premium-selling mean/tail?  Identical mechanics
at every tenor so DTE 0/1/2/3 are directly comparable:

  structures : V1a short-put spread, V1b short-call spread, V2 iron condor  (10-wide, defined risk)
  short leg  : ~0.16 delta (BSM delta from the ATM-straddle-implied total vol v=sigma*sqrt(T);
               T-convention-free strike selection, same as premium_selling.py)
  fills      : SELL shorts @ real BID, BUY longs @ real ASK (full NBBO spread paid)
  entry      : first snapshot >= 10:00 ET
  settle     : REAL SPX close on the expiry date (PM cash-settle assumption; SPX 1-min ruler)
  costs      : $0.65/leg commission
  DTE 0      : option_quote_history (0DTE) ; DTE 1/2/3 : option_quote_multidte  (both real NBBO)

Two exit policies:
  HOLD     - hold to expiry, settle at SPX close on E                       (full defined-risk tail)
  MANAGED  - +50%-of-credit profit target OR -2x-credit loss stop, tracked forward across days
             D..E-1 on the real multidte marks (cost-to-close = buy shorts @ ask / sell longs @ bid),
             else settle at SPX close on E.

COVERAGE NOTE: multidte spans 2022-01-03..2025-08-12 = the DEV window only (no holdout).  So part 2
is reported BY YEAR with the 2022 bear called out; there is no independent low-vol holdout here.

Run:  .venv/Scripts/python.exe -m scripts.research.backtest.vrp_realdte [START END]
CSV -> scratchpad/vrp_realdte_trades.csv
"""
from __future__ import annotations

import bisect
import csv
import datetime as dt
import math
import os
import sys
from zoneinfo import ZoneInfo

import numpy as np

from . import vrp_data as V
from .data import BAD

sys.stdout.reconfigure(encoding="utf-8")
ET = ZoneInfo("America/New_York")
SCRATCH = (r"C:\Users\diant\AppData\Local\Temp\claude\C--Users-diant"
           r"\13885fae-da20-4300-a83a-94697ac91ce4\scratchpad")

ENTRY_ET = 10.0
WING = 10.0
SHORT_DELTA = 0.16
STRADDLE_K = 0.7978845608
COMM_LEG = 0.65
CONTRACT = 100.0
PT_FRAC = 0.50          # managed profit target = close for +50% of credit
STOP_MULT = 3.0         # managed loss stop = cost-to-close >= 3x credit (loss = 2x credit)
DTES = [0, 1, 2, 3]
STRUCTS = ["V1a_putspread", "V1b_callspread", "V2_condor"]


def et_h(ms):
    x = dt.datetime.fromtimestamp(ms / 1000, ET)
    return x.hour + x.minute / 60.0


def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def call_delta(S, K, v):
    if v <= 0 or S <= 0 or K <= 0:
        return float("nan")
    d1 = (math.log(S / K) + 0.5 * v * v) / v
    return ncdf(d1)


def pick_by_delta(strikes, S, v, target, side):
    best, bd = None, 1e9
    for K in strikes:
        if side == "C":
            if K <= S:
                continue
            dl = call_delta(S, K, v)
        else:
            if K >= S:
                continue
            dl = 1.0 - call_delta(S, K, v)
        if not np.isfinite(dl):
            continue
        if abs(dl - target) < bd:
            bd, best = abs(dl - target), K
    return best


MARK_BAD = BAD | {"wide"}   # forward-mark path also rejects 'wide' (a single stale/wide
                            # tick must not false-trigger the managed profit/stop)


def bidask(qd, K, r, bad=BAD):
    v = qd.get((K, r))
    if v is None:
        return None
    bid, ask, qual = v
    if qual in bad or bid is None or ask is None:
        return None
    return (float(bid), float(ask))


def parity_spot(qd, strikes):
    fwds = []
    for K in strikes:
        c = qd.get((K, "C")); p = qd.get((K, "P"))
        if not c or not p or c[2] in BAD or p[2] in BAD:
            continue
        cm = (c[0] + c[1]) / 2.0; pm = (p[0] + p[1]) / 2.0
        fwds.append((abs(cm - pm), K + cm - pm))
    if not fwds:
        return None
    fwds.sort()
    return float(np.median([f for _, f in fwds[:5]]))


# ---------------------------------------------------------------- chain loaders
def entry_chain_multidte(con, date, dte):
    """(entry_ts, {(strike,right):(bid,ask,q)}) at first snapshot >=10:00 ET for a DTE."""
    ts_rows = con.execute(
        "SELECT DISTINCT timestamp_ms FROM option_quote_multidte WHERE session_date=? AND dte=? "
        "ORDER BY timestamp_ms", (date, dte)).fetchall()
    ets = next((t[0] for t in ts_rows if et_h(t[0]) >= ENTRY_ET), None)
    if ets is None or et_h(ets) > ENTRY_ET + 0.5:
        return None
    qd = {}
    for k, r, bid, ask, q in con.execute(
        "SELECT strike,right,bid,ask,quote_quality FROM option_quote_multidte "
        "WHERE session_date=? AND dte=? AND timestamp_ms=?", (date, dte, ets)):
        qd[(float(k), r)] = (bid, ask, q)
    return (ets, qd) if qd else None


def entry_chain_0dte(con, date):
    ts_rows = con.execute(
        "SELECT DISTINCT timestamp_ms FROM option_quote_history WHERE session_date=? "
        "ORDER BY timestamp_ms", (date,)).fetchall()
    ets = next((t[0] for t in ts_rows if et_h(t[0]) >= ENTRY_ET), None)
    if ets is None or et_h(ets) > ENTRY_ET + 0.5:
        return None
    qd = {}
    for k, r, bid, ask, q in con.execute(
        "SELECT strike,right,bid,ask,quote_quality FROM option_quote_history "
        "WHERE session_date=? AND timestamp_ms=?", (date, ets)):
        qd[(float(k), r)] = (bid, ask, q)
    return (ets, qd) if qd else None


def forward_legs(con, date, dte, legs):
    """{ts: cost_to_close_pts} for a set of legs [(K,right,sign)] on a forward session at `dte`.
    cost-to-close = buy back shorts @ ask (sign<0) + sell longs @ bid (sign>0), in points.
    Only timestamps where ALL legs quote good are returned."""
    ks = tuple(sorted({K for K, _r, _s in legs}))
    ph = ",".join("?" * len(ks))
    rows = con.execute(
        f"SELECT timestamp_ms,strike,right,bid,ask,quote_quality FROM option_quote_multidte "
        f"WHERE session_date=? AND dte=? AND strike IN ({ph})", (date, dte, *ks)).fetchall()
    byts = {}
    for ts, k, r, bid, ask, q in rows:
        byts.setdefault(ts, {})[(float(k), r)] = (bid, ask, q)
    out = {}
    for ts, qd in byts.items():
        cc = 0.0; ok = True
        for K, r, sgn in legs:
            ba = bidask(qd, K, r, MARK_BAD)
            if ba is None:
                ok = False; break
            cc += ba[1] if sgn < 0 else -ba[0]   # short: buy@ask ; long: sell@bid
        if ok:
            out[ts] = cc
    return out


# ---------------------------------------------------------------- structure pricer
def build_trade(qd, S, v, entry_ts, date, dte):
    """Return dict of structures -> leg strikes/credit, or None if unusable at entry."""
    strikes = sorted({k for (k, _r) in qd})
    if len(strikes) < 6:
        return None
    atmK = min(strikes, key=lambda k: abs(k - S))
    c = qd.get((atmK, "C")); p = qd.get((atmK, "P"))
    if not c or not p or c[2] in BAD or p[2] in BAD:
        return None
    straddle = (c[0] + c[1]) / 2.0 + (p[0] + p[1]) / 2.0
    if straddle <= 0:
        return None
    v_used = v if v is not None else straddle / (STRADDLE_K * S)
    scs = pick_by_delta(strikes, S, v_used, SHORT_DELTA, "C")
    sps = pick_by_delta(strikes, S, v_used, SHORT_DELTA, "P")
    if scs is None or sps is None:
        return None
    scl = min(strikes, key=lambda k: abs(k - (scs + WING)))
    spl = min(strikes, key=lambda k: abs(k - (sps - WING)))
    if len({scs, scl}) < 2 or len({sps, spl}) < 2:
        return None
    wc = abs(scl - scs); wp = abs(sps - spl)
    q_scs = bidask(qd, scs, "C"); q_scl = bidask(qd, scl, "C")
    q_sps = bidask(qd, sps, "P"); q_spl = bidask(qd, spl, "P")
    if None in (q_scs, q_scl, q_sps, q_spl):
        return None
    call_credit = q_scs[0] - q_scl[1]
    put_credit = q_sps[0] - q_spl[1]
    if call_credit <= 0 or put_credit <= 0:
        return None
    out = dict(S=S, v=v_used, atmK=atmK, entry_ts=entry_ts,
               scs=scs, scl=scl, sps=sps, spl=spl, wc=wc, wp=wp,
               call_credit=call_credit, put_credit=put_credit,
               scd=call_delta(S, scs, v_used), spd=1.0 - call_delta(S, sps, v_used))
    out["V1a_putspread"] = dict(credit=put_credit, wing=wp, legs=2,
                                legset=[(sps, "P", -1), (spl, "P", +1)])
    out["V1b_callspread"] = dict(credit=call_credit, wing=wc, legs=2,
                                 legset=[(scs, "C", -1), (scl, "C", +1)])
    out["V2_condor"] = dict(credit=call_credit + put_credit, wing=max(wc, wp), legs=4,
                            legset=[(sps, "P", -1), (spl, "P", +1), (scs, "C", -1), (scl, "C", +1)])
    return out


def settle_pnl(struct, tr, S_close):
    """hold-to-expiry pnl in POINTS for a structure (before commission)."""
    call_pay = min(max(0.0, S_close - tr["scs"]), tr["wc"])
    put_pay = min(max(0.0, tr["sps"] - S_close), tr["wp"])
    if struct == "V1a_putspread":
        return tr["put_credit"] - put_pay
    if struct == "V1b_callspread":
        return tr["call_credit"] - call_pay
    return (tr["call_credit"] + tr["put_credit"]) - call_pay - put_pay


def managed_pnl(con, struct, tr, credit, S_close, fwd_marks):
    """Managed pnl in POINTS.  fwd_marks = ordered [(ts, cost_to_close)] across D..E-1.
    +50% credit PT (cc<=0.5*credit) or 2x-credit stop (cc>=3*credit); else settle at expiry.
    Returns (pnl_pts, exit_legs_multiplier) where the exit multiplier=2 if a managed close
    occurred (extra exit commission), 1 if held to settle."""
    for ts, cc in fwd_marks:
        if cc <= PT_FRAC * credit:
            return credit - cc, 2
        if cc >= STOP_MULT * credit:
            return credit - cc, 2
    return settle_pnl(struct, tr, S_close), 1


# ---------------------------------------------------------------- distribution
def dist(pnls):
    a = np.array([p for p in pnls if p is not None and np.isfinite(p)], float)
    if len(a) == 0:
        return None
    wins = a[a > 0]
    eq = np.cumsum(a); peak = np.maximum.accumulate(eq)
    return dict(n=len(a), mean=float(a.mean()), median=float(np.median(a)),
                win=100.0 * len(wins) / len(a), total=float(a.sum()),
                worst=float(a.min()), best=float(a.max()),
                p5=float(np.percentile(a, 5)), p1=float(np.percentile(a, 1)),
                maxdd=float((eq - peak).min()),
                avg_win=float(wins.mean()) if len(wins) else 0.0,
                std=float(a.std(ddof=1)) if len(a) > 1 else 0.0,
                sharpe=(float(a.mean() / a.std(ddof=1)) if len(a) > 1 and a.std(ddof=1) > 0 else float("nan")),
                t=(float(a.mean() / (a.std(ddof=1) / math.sqrt(len(a)))) if len(a) > 1 and a.std(ddof=1) > 0 else float("nan")))


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2022-01-03"
    end = sys.argv[2] if len(sys.argv) > 2 else V.DEV_END
    con = V.connect()

    print("=" * 118)
    print("PART 2 — REAL 1-3 DTE vs 0DTE defined-risk premium selling (10-wide, short ~0.16d, real NBBO)")
    print(f"  {start}..{end}  entry ~{ENTRY_ET:.0f}:00 ET, SPX-close settle. multidte=DEV-only (no holdout).")
    print("=" * 118)

    daily, close_of = V.load_spx_daily(con)
    spx_dates = [d for d, _ in daily]
    dpos = {d: i for i, d in enumerate(spx_dates)}

    # session universe = multidte sessions (0DTE baseline shares the same dates)
    md_dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_quote_multidte WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]
    print(f"  multidte sessions in range: {len(md_dates)}")

    trades = []   # dicts: date, year, dte, struct, credit_$, hold_$, mgd_$, ...
    n_done = 0
    for di, date in enumerate(md_dates):
        year = date[:4]
        idx = dpos.get(date)
        if idx is None:
            continue
        # entry spot from independent SPX 1-min
        sxr = V.spx_intraday(con, date)
        for dte in DTES:
            # expiry session + its SPX close
            eidx = idx + dte
            if eidx >= len(spx_dates):
                continue
            E = spx_dates[eidx]
            S_close = close_of.get(E)
            if S_close is None:
                continue
            ec = entry_chain_0dte(con, date) if dte == 0 else entry_chain_multidte(con, date, dte)
            if ec is None:
                continue
            entry_ts, qd = ec
            strikes = sorted({k for (k, _r) in qd})
            # entry spot: SPX 1-min nearest entry_ts, fallback parity
            S = None
            if sxr is not None:
                ts, px = sxr
                j = int(np.searchsorted(ts, entry_ts))
                for cnd in (j - 1, j):
                    if 0 <= cnd < len(ts) and abs(int(ts[cnd]) - entry_ts) <= 180_000:
                        S = float(px[cnd]); break
            if S is None:
                S = parity_spot(qd, strikes)
            if S is None:
                continue
            tr = build_trade(qd, S, None, entry_ts, date, dte)
            if tr is None:
                continue
            # forward marks for managed (dte>=1 only; 0DTE managed handled intraday same session)
            fwd_cache = {}   # struct legset key -> ordered marks

            def get_marks(legset):
                key = tuple(legset)
                if key in fwd_cache:
                    return fwd_cache[key]
                marks = []
                if dte == 0:
                    # intraday same-session marks from option_quote_history
                    ks = tuple(sorted({K for K, _r, _s in legset}))
                    ph = ",".join("?" * len(ks))
                    rows = con.execute(
                        f"SELECT timestamp_ms,strike,right,bid,ask,quote_quality FROM option_quote_history "
                        f"WHERE session_date=? AND strike IN ({ph})", (date, *ks)).fetchall()
                    byts = {}
                    for ts_, k_, r_, b_, a_, q_ in rows:
                        byts.setdefault(ts_, {})[(float(k_), r_)] = (b_, a_, q_)
                    for ts_ in sorted(byts):
                        if ts_ <= entry_ts:
                            continue
                        cc = 0.0; ok = True
                        for K, r, sgn in legset:
                            ba = bidask(byts[ts_], K, r, MARK_BAD)
                            if ba is None:
                                ok = False; break
                            cc += ba[1] if sgn < 0 else -ba[0]
                        if ok:
                            marks.append((ts_, cc))
                else:
                    for o in range(0, dte):          # days D..E-1
                        s_o = spx_dates[idx + o]
                        dte_o = dte - o
                        fm = forward_legs(con, s_o, dte_o, legset)
                        for ts_ in sorted(fm):
                            if o == 0 and ts_ <= entry_ts:
                                continue
                            marks.append((ts_, fm[ts_]))
                fwd_cache[key] = marks
                return marks

            for struct in STRUCTS:
                sd = tr[struct]
                credit = sd["credit"]
                hold_pts = settle_pnl(struct, tr, S_close)
                hold_usd = hold_pts * CONTRACT - COMM_LEG * sd["legs"]
                marks = get_marks(sd["legset"])
                mgd_pts, exmul = managed_pnl(con, struct, tr, credit, S_close, marks)
                mgd_usd = mgd_pts * CONTRACT - COMM_LEG * sd["legs"] * exmul
                risk = (sd["wing"] - credit) * CONTRACT
                trades.append(dict(date=date, year=year, dte=dte, struct=struct,
                                   credit_usd=credit * CONTRACT, risk_usd=risk,
                                   hold_usd=hold_usd, mgd_usd=mgd_usd,
                                   S=S, S_close=S_close, scd=tr["scd"], spd=tr["spd"]))
        n_done += 1
        if (di + 1) % 100 == 0:
            print(f"    ...{di+1}/{len(md_dates)} ({date})  trades={len(trades)}", flush=True)

    print(f"\n  total trades: {len(trades)}  over {n_done} sessions")

    # ---- headline table: per-contract $ by DTE x structure (HOLD) ----
    def sub(dte, struct, exit_key, year=None):
        return [t[exit_key] for t in trades if t["dte"] == dte and t["struct"] == struct
                and (year is None or t["year"] == year)]

    for exit_key, exit_name in (("hold_usd", "HOLD-TO-EXPIRY"), ("mgd_usd", "MANAGED (+50%PT / -2x stop)")):
        print("\n" + "=" * 118)
        print(f"[{exit_name}]  per-contract $ (1 spread / 1 condor), real NBBO, SPX-close settle. "
              f"mean/med/win%/worst/p5/maxDD/Sharpe/t")
        print(f"  {'struct':16s} {'dte':>3s} {'n':>4s} {'mean$':>7s} {'med$':>6s} {'win%':>5s} "
              f"{'total$':>9s} {'worst$':>8s} {'p5$':>7s} {'maxDD$':>9s} {'wErase':>6s} {'Shp':>5s} {'t':>6s}")
        for struct in STRUCTS:
            for dte in DTES:
                s = dist(sub(dte, struct, exit_key))
                if not s:
                    continue
                we = abs(s["worst"]) / s["avg_win"] if s["avg_win"] > 0 else float("inf")
                print(f"  {struct:16s} {dte:>3d} {s['n']:4d} {s['mean']:7.1f} {s['median']:6.1f} "
                      f"{s['win']:4.0f}% {s['total']:9.0f} {s['worst']:8.0f} {s['p5']:7.1f} "
                      f"{s['maxdd']:9.0f} {we:6.1f} {s['sharpe']:5.2f} {s['t']:6.2f}")
            print()

    # ---- by year (condor + put spread, HOLD) ----
    years = ["2022", "2023", "2024", "2025"]
    for struct in ("V2_condor", "V1a_putspread"):
        print("=" * 118)
        print(f"[{struct}] HOLD per-contract mean$ / win% BY YEAR (2022 bear = tail stress)")
        print(f"  {'dte':>3s} " + " ".join(f"{y:>16s}" for y in years))
        for dte in DTES:
            cells = []
            for y in years:
                s = dist(sub(dte, struct, "hold_usd", y))
                cells.append(f"{s['mean']:+6.0f}/{s['win']:2.0f}%/{s['n']:>3d}" if s else f"{'·':>14s}")
            print(f"  {dte:>3d} " + " ".join(f"{c:>16s}" for c in cells))
        print()

    # ---- 0DTE baseline vs off-0DTE summary (condor, managed) ----
    print("=" * 118)
    print("DOES MOVING OFF 0DTE HELP?  V2 condor, mean$/worst$/maxDD, HOLD vs MANAGED, per DTE")
    print(f"  {'dte':>3s} | {'HOLD mean':>9s} {'HOLD worst':>10s} {'HOLD maxDD':>10s} | "
          f"{'MGD mean':>9s} {'MGD worst':>9s} {'MGD maxDD':>9s}")
    for dte in DTES:
        h = dist(sub(dte, "V2_condor", "hold_usd")); m = dist(sub(dte, "V2_condor", "mgd_usd"))
        if h and m:
            print(f"  {dte:>3d} | {h['mean']:9.1f} {h['worst']:10.0f} {h['maxdd']:10.0f} | "
                  f"{m['mean']:9.1f} {m['worst']:9.0f} {m['maxdd']:9.0f}")

    # ---- CSV ----
    path = os.path.join(SCRATCH, "vrp_realdte_trades.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "year", "dte", "struct", "credit_usd", "risk_usd",
                                          "hold_usd", "mgd_usd", "S", "S_close", "scd", "spd"])
        w.writeheader()
        for t in trades:
            w.writerow({k: (f"{t[k]:.3f}" if isinstance(t[k], float) else t[k]) for k in w.fieldnames})
    print(f"\n  CSV -> {path}  ({len(trades)} rows)")
    print("\nREAD: higher mean$ AND smaller |worst|/maxDD as DTE rises = moving off 0DTE improves the sell.")
    print("DONE.")


if __name__ == "__main__":
    main()
