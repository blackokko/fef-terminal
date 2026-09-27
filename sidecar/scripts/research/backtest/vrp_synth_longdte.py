"""PART 3 - BSM-SYNTHESIZED 30-45 DTE DEFINED-RISK CREDIT-SPREAD / IRON-CONDOR SIM.

We lack real 30-45 DTE SPX NBBO, so this is a BSM APPROXIMATION (a directional check, NOT a
fill-accurate backtest).  It answers: IF the part-1 VRP is real at ~30d, is it EXTRACTABLE as a
defined-risk, early-managed sell with a survivable tail — after modeled costs?

MODEL / ASSUMPTIONS (stated):
  * underlying path : REAL SPX daily closes (gaps/overnight naturally included), 2022-2026.
  * ATM vol         : sigma_atm = VIX/100 (30d implied) at entry AND re-marked each day with THAT
                      day's VIX -> captures the vega / vol-spike P&L that hurts the seller.
  * skew (put bump) : iv(K) = sigma_atm * (1 + BETA_PUT*(-m))  for m=ln(K/S)<0 (puts richer),
                      iv(K) = sigma_atm * (1 - BETA_CALL*m)    for m>0 (calls cheaper); capped.
                      BETA_PUT/BETA_CALL are ASSUMED (reported skew-on vs skew-off).  Imperfect.
  * rates / carry   : r = 0, forward ~= spot (small for a 35d defined-risk spread).
  * day-count       : calendar/365 for T; path walked in trading days.
  * costs (MODELED) : per leg, half-spread HS_PT points + $0.65 commission, on entry AND exit.
                      Swept {0.05,0.15,0.30} pt/leg because with no real NBBO this is the #1 unknown.
  * entry           : 30-45 calendar DTE (target ~35).  short leg ~0.16 delta (skew-adj BSM, r=0).
  * management      : close at 21 DTE  OR  +50%-of-credit profit  OR  -2x-credit loss, whichever first
                      (always closed before expiry -> minimal assignment risk; gap risk during hold).
  * structures      : put credit spread / call credit spread / iron condor.  Wings swept {10,20,30}.

DEV 2022-01-03..2025-08-12  |  HOLDOUT 2025-08-13..2026-06-26 (low-vol, flatters sellers).

Run:  .venv/Scripts/python.exe -m scripts.research.backtest.vrp_synth_longdte
CSV -> scratchpad/vrp_synth_longdte_trades.csv
"""
from __future__ import annotations

import csv
import datetime as dt
import math
import os
import sys

import numpy as np

from . import vrp_data as V

sys.stdout.reconfigure(encoding="utf-8")
SCRATCH = (r"D:\fef-data")

DTE_TARGET = 35
DTE_LO, DTE_HI = 30, 45
MANAGE_DTE = 21          # close at/inside 21 DTE
PT_FRAC = 0.50
STOP_MULT = 3.0          # close if spread premium >= 3x credit (loss = 2x credit)
SHORT_DELTA = 0.16
BETA_PUT, BETA_CALL = 2.0, 1.0
COMM_LEG_USD = 0.65
CONTRACT = 100.0
GRID = 5.0               # SPX strike grid
BASE_HS = 0.15           # base modeled half-spread (pt/leg); swept elsewhere
BASE_WING = 20.0


def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bsm(S, K, sigma, T, right):
    """r=0 BSM price (points).  right 'C'/'P'."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if right == "C" else max(0.0, K - S)
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * v * v) / v
    d2 = d1 - v
    if right == "C":
        return S * ncdf(d1) - K * ncdf(d2)
    return K * ncdf(-d2) - S * ncdf(-d1)


def bsm_delta(S, K, sigma, T, right):
    if T <= 0 or sigma <= 0:
        return (1.0 if S > K else 0.0) if right == "C" else (-1.0 if S < K else 0.0)
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * v * v) / v
    return ncdf(d1) if right == "C" else ncdf(d1) - 1.0


def iv_skew(S, K, sigma_atm):
    """Assumed deterministic skew in log-moneyness (see header)."""
    m = math.log(K / S)
    if m < 0:
        iv = sigma_atm * (1.0 + BETA_PUT * (-m))
    else:
        iv = sigma_atm * (1.0 - BETA_CALL * m)
    return min(max(iv, 0.5 * sigma_atm), 3.0 * sigma_atm)


def grid_strikes(S, span=0.20):
    lo = math.floor(S * (1 - span) / GRID) * GRID
    hi = math.ceil(S * (1 + span) / GRID) * GRID
    return [lo + i * GRID for i in range(int((hi - lo) / GRID) + 1)]


def pick_short(S, sigma_atm, T, target, side):
    """Nearest grid strike whose skew-adj |delta| ~ target on the given side."""
    best, bd = None, 1e9
    for K in grid_strikes(S):
        if side == "C" and K <= S:
            continue
        if side == "P" and K >= S:
            continue
        iv = iv_skew(S, K, sigma_atm)
        dl = abs(bsm_delta(S, K, iv, T, side))
        if abs(dl - target) < bd:
            bd, best = abs(dl - target), K
    return best


def spread_premium(S, sigma_atm, T, short_K, long_K, right):
    """Current mid premium (points) of a credit spread = short leg value - long leg value,
    each leg priced with its own skew-adjusted iv."""
    sv = bsm(S, short_K, iv_skew(S, short_K, sigma_atm), T, right)
    lv = bsm(S, long_K, iv_skew(S, long_K, sigma_atm), T, right)
    return sv - lv


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


def cal_days(a, b):
    ya, ma, da = map(int, a.split("-")); yb, mb, db = map(int, b.split("-"))
    return (dt.date(yb, mb, db) - dt.date(ya, ma, da)).days


def simulate_trade(spx_dates, close_of, vix_of, i, structs, wing, hs):
    """Enter on spx_dates[i]; per structure return BOTH exit modes:
       exp_* = HOLD TO EXPIRY, settled at REAL SPX intrinsic on the expiry date (only the ENTRY
               credit is modeled; the settlement is real; no exit cost) -> cleanest VRP-in-$ test.
       mgd_* = MANAGED, closed at 21 DTE / +50% / -2x on the BSM-VIX mark (round-trip cost, reduced tail).
    Returns {struct: dict(...)} or None if no valid 30-45 DTE window / data."""
    t = spx_dates[i]
    S0 = close_of[t]; vix0 = vix_of.get(t)
    if vix0 is None or S0 <= 0:
        return None
    sig0 = vix0 / 100.0
    # expiry = trading date whose calendar-DTE is CLOSEST to the ~35d target, within [30,45]
    exp, bestd = None, 1e9
    for j in range(i + 1, len(spx_dates)):
        cd = cal_days(t, spx_dates[j])
        if cd > DTE_HI:
            break
        if DTE_LO <= cd <= DTE_HI and abs(cd - DTE_TARGET) < bestd:
            bestd, exp = abs(cd - DTE_TARGET), spx_dates[j]
    if exp is None:
        return None
    cdte0 = cal_days(t, exp)
    exp_idx = spx_dates.index(exp)
    S_exp = close_of[exp]
    T0 = cdte0 / 365.0
    scs = pick_short(S0, sig0, T0, SHORT_DELTA, "C")
    sps = pick_short(S0, sig0, T0, SHORT_DELTA, "P")
    if scs is None or sps is None:
        return None
    scl = scs + wing; spl = sps - wing
    leg_cost = hs + COMM_LEG_USD / CONTRACT     # per leg, points

    def expiry_pnl(right, short_K, credit0):
        """settle at REAL SPX intrinsic; entry cost only (cash-settle, no exit trade)."""
        pay = min(max(0.0, short_K - S_exp), wing) if right == "P" else min(max(0.0, S_exp - short_K), wing)
        return (credit0 - pay - 2 * leg_cost) * CONTRACT

    def managed_pnl(right, short_K, long_K, credit0):
        for j in range(i + 1, exp_idx + 1):
            d = spx_dates[j]
            cdte = cdte0 - cal_days(t, d)
            if d not in close_of or vix_of.get(d) is None:
                continue
            S = close_of[d]; sig = vix_of[d] / 100.0
            prem = min(max(spread_premium(S, sig, max(cdte, 0) / 365.0, short_K, long_K, right), 0.0), wing)
            reason = ("21dte" if cdte <= MANAGE_DTE else
                      "pt50" if prem <= PT_FRAC * credit0 else
                      "stop2x" if prem >= STOP_MULT * credit0 else None)
            if reason:
                pnl_pts = (credit0 - prem) - 4 * leg_cost      # entry(2) + exit(2) legs
                return pnl_pts * CONTRACT, j - i, cal_days(t, d), reason
        # fell through to expiry -> settle intrinsic, exit cost still applied (assume closed at expiry)
        pay = min(max(0.0, short_K - S_exp), wing) if right == "P" else min(max(0.0, S_exp - short_K), wing)
        return (credit0 - pay - 4 * leg_cost) * CONTRACT, exp_idx - i, cdte0, "expiry"

    pc0 = spread_premium(S0, sig0, T0, sps, spl, "P")
    cc0 = spread_premium(S0, sig0, T0, scs, scl, "C")
    legs = {"put": ("P", sps, spl, pc0), "call": ("C", scs, scl, cc0)}
    out = {}
    for name in ("put", "call"):
        if name not in structs:
            continue
        right, sk, lk, c0 = legs[name]
        if c0 <= 0:
            continue
        mp, mtd, mcal, mr = managed_pnl(right, sk, lk, c0)
        out[name] = dict(exp_usd=expiry_pnl(right, sk, c0), mgd_usd=mp,
                         hold_td=mtd, hold_cal=mcal, reason=mr,
                         exp_hold_td=exp_idx - i, exp_hold_cal=cdte0,
                         credit=c0 * CONTRACT, net_credit=(c0 - 2 * leg_cost) * CONTRACT)
    if "condor" in structs and pc0 > 0 and cc0 > 0:
        mp_p, mtd_p, mcal_p, mr_p = managed_pnl("P", sps, spl, pc0)
        mp_c, mtd_c, mcal_c, mr_c = managed_pnl("C", scs, scl, cc0)
        out["condor"] = dict(
            exp_usd=expiry_pnl("P", sps, pc0) + expiry_pnl("C", scs, cc0),
            mgd_usd=mp_p + mp_c, hold_td=max(mtd_p, mtd_c), hold_cal=max(mcal_p, mcal_c),
            reason=f"{mr_p}/{mr_c}", exp_hold_td=exp_idx - i, exp_hold_cal=cdte0,
            credit=(pc0 + cc0) * CONTRACT, net_credit=(pc0 + cc0 - 4 * leg_cost) * CONTRACT)
    return out or None


def main():
    con = V.connect()
    print("=" * 118)
    print("PART 3 — BSM-SYNTH 30-45 DTE defined-risk credit spreads / iron condor (VIX-as-IV, managed).")
    print(f"  APPROXIMATION (no real long-DTE NBBO). target {DTE_TARGET}d, manage@{MANAGE_DTE}dte/+50%/-2x, "
          f"wing {BASE_WING:.0f}, HS {BASE_HS:.2f}pt/leg.")
    print("=" * 118)

    daily, close_of = V.load_spx_daily(con)
    spx_dates = [d for d, _ in daily]
    vix_of = V.load_vix(con, "VIX")
    print(f"  SPX {len(spx_dates)} sessions {spx_dates[0]}..{spx_dates[-1]}; VIX days {len(vix_of)}")

    structs = ["put", "call", "condor"]

    # ---------- main pass: ALL-DAILY entries (per-trade expectancy + tail) ----------
    all_trades = {s: [] for s in structs}   # each: dict + date/year/split
    for i in range(len(spx_dates)):
        res = simulate_trade(spx_dates, close_of, vix_of, i, structs, BASE_WING, BASE_HS)
        if not res:
            continue
        d = spx_dates[i]
        for s, r in res.items():
            r2 = dict(r, date=d, year=d[:4], split=V.split_of(d))
            all_trades[s].append(r2)

    def sub(struct, key="exp_usd", split=None, year=None):
        return [r[key] for r in all_trades[struct]
                if (split is None or r["split"] == split) and (year is None or r["year"] == year)]

    for key, name in (("exp_usd", "HOLD-TO-EXPIRY (real SPX intrinsic settle; entry cost only)"),
                      ("mgd_usd", "MANAGED (close @21DTE/+50%/-2x on BSM-VIX mark; round-trip cost)")):
        print("\n" + "=" * 118)
        print(f"[ALL-DAILY ENTRIES / {name}]  per-contract $ (wing {BASE_WING:.0f}, HS {BASE_HS:.2f}/leg). "
              f"Overlapping -> expectancy+tail.")
        print(f"  {'struct':8s} {'split':5s} {'n':>4s} {'mean$':>7s} {'med$':>6s} {'win%':>5s} {'total$':>9s} "
              f"{'worst$':>8s} {'p5$':>7s} {'avgHoldTd':>9s} {'wErase':>6s} {'Shp':>5s} {'t':>6s}")
        for struct in structs:
            for split in ("ALL", "DEV", "HOLD"):
                ps = sub(struct, key, split if split != "ALL" else None)
                s = dist(ps)
                if not s:
                    continue
                hk = "exp_hold_td" if key == "exp_usd" else "hold_td"
                hold = np.mean([r[hk] for r in all_trades[struct] if split == "ALL" or r["split"] == split])
                we = abs(s["worst"]) / s["avg_win"] if s["avg_win"] > 0 else float("inf")
                print(f"  {struct:8s} {split:5s} {s['n']:4d} {s['mean']:7.1f} {s['median']:6.1f} {s['win']:4.0f}% "
                      f"{s['total']:9.0f} {s['worst']:8.0f} {s['p5']:7.1f} {hold:9.1f} {we:6.1f} {s['sharpe']:5.2f} {s['t']:6.2f}")
            print()

    # ---------- by year (hold-to-expiry) ----------
    years = ["2022", "2023", "2024", "2025", "2026"]
    print("=" * 118)
    print("BY YEAR (HOLD-TO-EXPIRY) — condor / put / call, mean$ / win% / worst$  (2022 bear = tail stress)")
    for struct in ("condor", "put", "call"):
        print(f"  [{struct}]")
        print(f"    {'':4s} " + " ".join(f"{y:>18s}" for y in years))
        cells = []
        for y in years:
            s = dist(sub(struct, "exp_usd", year=y))
            cells.append(f"{s['mean']:+5.0f}/{s['win']:2.0f}%/w{s['worst']:>5.0f}" if s else f"{'·':>16s}")
        print(f"    mean " + " ".join(f"{c:>18s}" for c in cells))
    print()

    # ---------- sequential NON-OVERLAPPING equity (honest maxDD / Sharpe / per-day) ----------
    print("=" * 118)
    print("SEQUENTIAL (NON-OVERLAPPING) equity, HOLD-TO-EXPIRY — re-enter the day after each expiry. "
          "Honest maxDD/Sharpe/per-day.")
    print(f"  {'struct':8s} {'split':5s} {'nTrades':>7s} {'mean$':>7s} {'total$':>9s} {'maxDD$':>9s} "
          f"{'worst$':>8s} {'Shp':>5s} {'$/day(cal)':>10s}")
    seq_csv = []
    for struct in structs:
        for split, lo, hi in (("DEV", V.DEV_START, V.DEV_END), ("HOLD", V.HOLDOUT_START, V.HOLDOUT_END)):
            seq = []
            i = 0
            n = len(spx_dates)
            while i < n:
                d = spx_dates[i]
                if not (lo <= d <= hi):
                    i += 1; continue
                res = simulate_trade(spx_dates, close_of, vix_of, i, [struct], BASE_WING, BASE_HS)
                if not res or struct not in res:
                    i += 1; continue
                r = res[struct]
                seq.append(dict(r, date=d))
                i += max(1, r["exp_hold_td"]) + 1     # re-enter day after expiry (hold-to-expiry)
            ps = [r["exp_usd"] for r in seq]
            s = dist(ps)
            if not s:
                continue
            tot_cal = sum(r["exp_hold_cal"] for r in seq)
            perday = s["total"] / tot_cal if tot_cal else float("nan")
            print(f"  {struct:8s} {split:5s} {s['n']:7d} {s['mean']:7.1f} {s['total']:9.0f} {s['maxdd']:9.0f} "
                  f"{s['worst']:8.0f} {s['sharpe']:5.2f} {perday:10.1f}")
            for r in seq:
                seq_csv.append(dict(struct=struct, split=split, date=r["date"], pnl_usd=r["exp_usd"],
                               hold_td=r["exp_hold_td"], hold_cal=r["exp_hold_cal"],
                               reason="expiry", credit=r["credit"], net_credit=r["net_credit"]))
        print()

    # ---------- cost sensitivity (condor, ALL, HOLD-TO-EXPIRY) ----------
    print("=" * 118)
    print("COST SENSITIVITY — condor, ALL-DAILY HOLD-TO-EXPIRY, across modeled entry half-spread (the #1 unknown)")
    print(f"  {'HS/leg':>7s} {'wing':>4s} {'n':>4s} {'mean$':>7s} {'win%':>5s} {'total$':>10s} {'worst$':>8s} {'Shp':>5s}")
    for wing in (10.0, 20.0, 30.0):
        for hs in (0.05, 0.15, 0.30):
            ps = []
            for i in range(len(spx_dates)):
                res = simulate_trade(spx_dates, close_of, vix_of, i, ["condor"], wing, hs)
                if res and "condor" in res:
                    ps.append(res["condor"]["exp_usd"])
            s = dist(ps)
            if s:
                print(f"  {hs:7.2f} {wing:4.0f} {s['n']:4d} {s['mean']:7.1f} {s['win']:4.0f}% "
                      f"{s['total']:10.0f} {s['worst']:8.0f} {s['sharpe']:5.2f}")
        print()

    # ---------- skew on/off (condor, ALL) ----------
    print("=" * 118)
    print("SKEW SENSITIVITY — condor, ALL-DAILY, base wing/HS, skew-ON (assumed) vs skew-OFF (flat VIX)")
    global BETA_PUT, BETA_CALL
    for label, bp, bc in (("skew-ON ", 2.0, 1.0), ("skew-OFF", 0.0, 0.0)):
        BETA_PUT, BETA_CALL = bp, bc
        ps = []
        for i in range(len(spx_dates)):
            res = simulate_trade(spx_dates, close_of, vix_of, i, ["condor"], BASE_WING, BASE_HS)
            if res and "condor" in res:
                ps.append(res["condor"]["exp_usd"])
        s = dist(ps)
        if s:
            print(f"  {label}: n={s['n']} mean=${s['mean']:.1f} win={s['win']:.0f}% total=${s['total']:.0f} "
                  f"worst=${s['worst']:.0f} Shp={s['sharpe']:.2f}")
    BETA_PUT, BETA_CALL = 2.0, 1.0

    # ---------- CSVs ----------
    path = os.path.join(SCRATCH, "vrp_synth_longdte_trades.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["struct", "date", "year", "split", "exp_usd", "mgd_usd",
                                          "exp_hold_cal", "hold_td", "reason", "credit", "net_credit"])
        w.writeheader()
        for struct in structs:
            for r in all_trades[struct]:
                w.writerow({"struct": struct, **{k: r[k] for k in
                            ("date", "year", "split", "exp_usd", "mgd_usd", "exp_hold_cal",
                             "hold_td", "reason", "credit", "net_credit")}})
    print(f"\n  trades CSV -> {path}")
    seqpath = os.path.join(SCRATCH, "vrp_synth_longdte_sequential.csv")
    with open(seqpath, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["struct", "split", "date", "pnl_usd", "hold_td",
                                          "hold_cal", "reason", "credit", "net_credit"])
        w.writeheader()
        for r in seq_csv:
            w.writerow(r)
    print(f"  sequential CSV -> {seqpath}")
    print("\nREAD: BSM approx. Verdict weighs mean$>0 with survivable worst/maxDD AND cost-robustness. "
          "2022 bear + costs are the stress. Recommend real 30-45d NBBO if promising.")
    print("DONE.")


if __name__ == "__main__":
    main()
