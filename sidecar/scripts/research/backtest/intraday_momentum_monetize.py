"""MARKET INTRADAY MOMENTUM — PART 2: MONETIZE as a SINGLE-LEG LONG 0DTE option.

Part 1 found the CLASSIC first-half-hour momentum (r1) is dead in 2022-2026 SPX, but the
penultimate-half-hour continuation (r12: 15:00->15:30 predicts 15:30->16:00) survives thinly
(t~3, hit ~52-53% DEV+HOLD, R2~0.75-2.3%). This script asks the only question that matters for
the operator (single-leg long only): does that thin directional tilt PAY after the option spread?

STRATEGY (no lookahead): at ~15:30 ET take the sign of the signal and buy ONE 0DTE option in that
direction (up->CALL, down->PUT); hold to the 16:00 cash close; 0DTE SPXW PM cash-settle => exit at
INTRINSIC vs the independent IBKR SPX close (the 15:59 ruler bar). Entry is the REAL NBBO ASK + slip
at the option minute nearest 15:30; friction = COMMISSION_RT + SLIP (harness constants).

  SIGNALS : r1_on (task headline: 10:00/prevclose-1) ; r1_id (10:00/open-1) ; r12 (15:30/15:00-1,
            the actually-predictive one) ; combo (trade only when sign r1_on == sign r12).
  STRIKES : ATM (nearest listed) and ~0.30-delta OTM (solved from the entry ATM IV).
  GATES   : all days ; VIX_prev>=20 ; VIX_prev>=25 ; realized-vol-so-far top DEV quartile ;
            |signal| top DEV quartile (big first-half-hour move) -- where the effect/move is largest.
  SETTLE  : intrinsic@close (primary, exact for cash-settle) ; sell-at-16:00-NBBO-bid (secondary).
  GREEKS  : exact reprice decomposition delta/gamma/theta + spread + commission (hold=30min so the
            TIME-decay $ is what it is near expiry -- reported, not assumed tiny).

Per-contract $ mean + session-clustered t (1 trade/session/config => the plain t over sessions IS
the clustered t), win%, BY YEAR + DEV/HOLDOUT, CALL vs PUT split. Read-only.
Run: .venv/Scripts/python.exe -m scripts.research.backtest.intraday_momentum_monetize [START END]
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import sqlite3
import sys

import numpy as np

from .data import BAD
from .exits import COMMISSION_RT, SLIP
from .greeks import bs_price, greeks, implied_vol
from .holdout import DEV_END, HOLDOUT_START
from .ibkr_ruler import ET
from .intraday_momentum import (SCRATCH, SIG_DB, bucket, build_rows, dev_quartile_edges, et_ms)

sys.stdout.reconfigure(encoding="utf-8")

YEAR_MIN = 365.0 * 24 * 60
ENTRY_TARGET = (15, 30)        # ET
EXIT_TARGET = (16, 0)          # ET (for the sell-at-bid secondary; intrinsic settle uses ruler close)
ENTRY_TOL_MS = 150_000         # option minute must be within 2.5 min of 15:30
YEARS = ["2022", "2023", "2024", "2025", "2026", "ALL", "HOLD"]


# ------------------------------------------------------------------ targeted option loader
def load_entry_exit_quotes(con, d):
    """Two thin windows only (fast): quotes near 15:30 and near 16:00 for session d.
    Returns (entry_ts, exit_ts, {(strike,right):(bid,ask,qual)} @entry, {...}@exit, strikes)."""
    e_lo = et_ms(d, 15, 27); e_hi = et_ms(d, 15, 33)
    x_lo = et_ms(d, 15, 56); x_hi = et_ms(d, 16, 1)
    rows = con.execute(
        "SELECT timestamp_ms,strike,right,bid,ask,quote_quality FROM option_quote_history "
        "WHERE session_date=? AND ((timestamp_ms BETWEEN ? AND ?) OR (timestamp_ms BETWEEN ? AND ?))",
        (d, e_lo, e_hi, x_lo, x_hi)).fetchall()
    if not rows:
        return None
    etgt = et_ms(d, *ENTRY_TARGET); xtgt = et_ms(d, *EXIT_TARGET)
    e_ts = min((r[0] for r in rows if e_lo <= r[0] <= e_hi), default=None,
               key=lambda t: abs(t - etgt))
    x_ts = min((r[0] for r in rows if x_lo <= r[0] <= x_hi), default=None,
               key=lambda t: abs(t - xtgt))
    if e_ts is None or abs(e_ts - etgt) > ENTRY_TOL_MS:
        return None
    eq, xq, strikes = {}, {}, set()
    for ts, k, r, bid, ask, ql in rows:
        if ts == e_ts:
            eq[(float(k), r)] = (bid, ask, ql); strikes.add(float(k))
        if x_ts is not None and ts == x_ts:
            xq[(float(k), r)] = (bid, ask, ql)
    return e_ts, x_ts, eq, xq, sorted(strikes)


def mid_of(q):
    if not q or q[2] in BAD:
        return None
    if q[0] is None or q[1] is None or q[1] <= 0:
        return None
    return (q[0] + q[1]) / 2.0


# ------------------------------------------------------------------ strike selection
def pick_atm(strikes, s):
    return min(strikes, key=lambda k: abs(k - s)) if strikes else None


def pick_delta(strikes, eq, s, right, iv, T, target=0.30):
    """OTM strike whose |BSM delta| is nearest `target`, restricted to the OTM side."""
    if iv is None or iv <= 0:
        return None
    best, bd = None, 1e9
    for k in strikes:
        if right == "C" and k <= s:      # OTM call: strike above spot
            continue
        if right == "P" and k >= s:      # OTM put: strike below spot
            continue
        q = eq.get((k, right))
        if mid_of(q) is None:
            continue
        g = greeks(s, k, T, iv, right)
        if not g:
            continue
        if abs(abs(g["delta"]) - target) < bd:
            bd, best = abs(abs(g["delta"]) - target), k
    return best


def atm_iv(eq, s, k_atm, T):
    """entry ATM implied vol from the ATM mid (call+put avg for robustness)."""
    ivs = []
    for right in ("C", "P"):
        m = mid_of(eq.get((k_atm, right)))
        if m is None:
            continue
        iv = implied_vol(m, s, k_atm, T, right)
        if iv:
            ivs.append(iv)
    return float(np.mean(ivs)) if ivs else None


# ------------------------------------------------------------------ one trade
def trade(eq, xq, s_entry, s_settle, k, right, T, iv):
    """Return dict with actual $/ct (intrinsic + sell-bid) and the greek decomposition, or None."""
    q = eq.get((k, right))
    if not q or q[2] in BAD or q[1] is None or q[1] <= 0:
        return None
    ask, bid = q[1], q[0]
    mid = (bid + ask) / 2.0
    buy = ask + SLIP
    if buy <= 0.05:
        return None
    intrinsic = max(0.0, (s_settle - k) if right == "C" else (k - s_settle))
    pnl_intr = (intrinsic - buy) * 100 - COMMISSION_RT
    # secondary: sell at the 16:00 NBBO bid
    xqq = xq.get((k, right))
    pnl_bid = None
    if xqq and xqq[2] not in BAD and xqq[0] is not None:
        sell = max(0.0, xqq[0] - SLIP)
        pnl_bid = (sell - buy) * 100 - COMMISSION_RT
    # greek decomposition (exact reprice at entry IV; ties out to model P&L)
    dg = th = dl = ga = np.nan
    if iv and iv > 0:
        dS = s_settle - s_entry
        p0 = bs_price(s_entry, k, T, iv, right)
        p_spot = bs_price(s_settle, k, T, iv, right)         # spot move, entry time-value
        spot_term = (p_spot - p0) * 100
        g = greeks(s_entry, k, T, iv, right)
        dl = (g["delta"] * dS) * 100 if g else np.nan
        ga = spot_term - dl if np.isfinite(dl) else np.nan
        th = (intrinsic - p_spot) * 100                       # time decay to settle (at settle spot)
        dg = spot_term
    spread_cost = (buy - mid) * 100                           # half-spread + slip paid at entry
    return dict(pnl_intr=pnl_intr, pnl_bid=pnl_bid, entry=buy, mid=mid, spread=(ask - bid),
                delta_pnl=dl, gamma_pnl=ga, theta_pnl=th, spot_pnl=dg,
                spread_cost=spread_cost, intrinsic=intrinsic)


# ------------------------------------------------------------------ build all trades
SIGNALS = ["r1_on", "r1_id", "r12", "combo"]
STRIKES = ["ATM", "OTM30d"]
GATES = ["all", "VIX>=20", "VIX>=25", "rvolQ4", "sigQ4"]


def signal_dir(row, sig):
    if sig == "combo":
        a, b = row.get("r1_on"), row.get("r12")
        if a is None or b is None or np.sign(a) != np.sign(b) or a == 0:
            return None
        return np.sign(a)
    v = row.get(sig)
    if v is None or v == 0 or not np.isfinite(v):
        return None
    return np.sign(v)


def build_trades():
    rows = build_rows()
    # DEV quartile edges (top-quartile gates)
    rv_e = dev_quartile_edges(rows, "rvol_sofar")
    sig_e = {s: dev_quartile_edges(rows, ("r1_on" if s in ("combo",) else s), absval=True)
             for s in SIGNALS}
    con = sqlite3.connect(f"file:{SIG_DB}?mode=ro", uri=True)
    recs = []
    n_sess = 0
    for row in rows:
        d = row["date"]
        s_entry, s_settle = row["p1530"], row["p1600"]
        if s_entry is None or s_settle is None:
            continue
        loaded = load_entry_exit_quotes(con, d)
        if loaded is None:
            continue
        e_ts, x_ts, eq, xq, strikes = loaded
        if not strikes:
            continue
        mins = max(1.0, (et_ms(d, 16, 0) - e_ts) / 60000.0)   # minutes from entry to 16:00 settle
        T = mins / YEAR_MIN
        k_atm = pick_atm(strikes, s_entry)
        iv = atm_iv(eq, s_entry, k_atm, T) if k_atm else None
        n_sess += 1
        # gate membership (per row)
        gate_ok = {
            "all": True,
            "VIX>=20": (row["vix_prev"] is not None and row["vix_prev"] >= 20),
            "VIX>=25": (row["vix_prev"] is not None and row["vix_prev"] >= 25),
            "rvolQ4": bucket(row["rvol_sofar"], rv_e) == "Q4",
        }
        for sig in SIGNALS:
            dirn = signal_dir(row, sig)
            if dirn is None:
                continue
            right = "C" if dirn > 0 else "P"
            sigval = row["r1_on"] if sig == "combo" else row[sig]
            g_sigQ4 = bucket(abs(sigval) if sigval is not None else None, sig_e[sig]) == "Q4"
            for sname in STRIKES:
                if sname == "ATM":
                    k = k_atm
                else:
                    k = pick_delta(strikes, eq, s_entry, right, iv, T, 0.30)
                if k is None:
                    continue
                tr = trade(eq, xq, s_entry, s_settle, k, right, T, iv)
                if tr is None:
                    continue
                for gate, ok in list(gate_ok.items()) + [("sigQ4", g_sigQ4)]:
                    if not ok:
                        continue
                    recs.append(dict(date=d, split=row["split"], year=d[:4], sig=sig, strike=sname,
                                     gate=gate, side=("CALL" if dirn > 0 else "PUT"),
                                     vix_prev=row["vix_prev"], **tr))
    con.close()
    return recs, n_sess


# ------------------------------------------------------------------ stats + reporting
def cl(vals):
    a = np.array([v for v in vals if v is not None and np.isfinite(v)], float)
    n = len(a)
    if n < 2 or a.std(ddof=1) == 0:
        return (float(a.mean()) if n else float("nan")), float("nan"), n
    return float(a.mean()), float(a.mean() / (a.std(ddof=1) / np.sqrt(n))), n


def cell(vals):
    m, t, n = cl(vals)
    return f"{m:+6.0f}(t{t:+4.1f})" if n >= 2 else f"{'·':>13s}"


def sel(recs, sig, strike, gate, yr, field="pnl_intr", side=None):
    out = []
    for r in recs:
        if r["sig"] != sig or r["strike"] != strike or r["gate"] != gate:
            continue
        if side is not None and r["side"] != side:
            continue
        if yr == "HOLD" and r["split"] != "HOLD":
            continue
        if yr not in ("ALL", "HOLD") and r["year"] != yr:
            continue
        v = r.get(field)
        if v is not None and np.isfinite(v):
            out.append(v)
    return out


def block(t):
    print("\n" + "=" * 118)
    print(t)
    print("=" * 118)


def main():
    print("=" * 118)
    print("PART 2 — MONETIZE intraday momentum as a SINGLE-LEG LONG 0DTE (real NBBO ask, intrinsic PM settle)")
    print(f"  entry ~15:30 ET, hold to 16:00 close; friction ${COMMISSION_RT} RT + ${SLIP} slip; qty=1 (per-ct $)")
    print("=" * 118)

    recs, n_sess = build_trades()
    print(f"\n  sessions with a valid 15:30 entry: {n_sess}   trade-records: {len(recs)}")
    # context: entry premium + spread hurdle (ATM, all-days, r1_on)
    atm_all = [r for r in recs if r["strike"] == "ATM" and r["gate"] == "all" and r["sig"] == "r1_on"]
    if atm_all:
        prem = np.array([r["entry"] for r in atm_all]); sprd = np.array([r["spread"] for r in atm_all])
        print(f"  ATM 0DTE @15:30 hurdle: entry ask≈{prem.mean():.2f}pt (${prem.mean()*100:.0f}/ct), "
              f"NBBO spread≈{sprd.mean():.2f}pt (${sprd.mean()*100:.0f}/ct) -> half-spread+slip≈"
              f"${(sprd.mean()/2+SLIP)*100:.0f}/ct to overcome")

    # ---- main $ table: per signal x strike, all-days, by year ----
    for strike in STRIKES:
        block(f"0DTE LONG  STRIKE={strike}  SETTLE=intrinsic@close   (per-contract $, net friction; +=PAYS)")
        print(f"  {'signal (gate=all)':18s} " + " ".join(f"{y:>13s}" for y in YEARS) + "   win%")
        for sig in SIGNALS:
            cells = [cell(sel(recs, sig, strike, "all", yr)) for yr in YEARS]
            allv = sel(recs, sig, strike, "all", "ALL")
            w = 100.0 * np.mean([p > 0 for p in allv]) if allv else float("nan")
            print(f"  {sig:18s} " + " ".join(f"{c:>13s}" for c in cells) + f"   {w:4.0f}%")

    # ---- gating: does high-vol / big-move rescue it? (ATM + OTM, ALL and HOLD) ----
    for strike in STRIKES:
        block(f"GATING — STRIKE={strike}: does high-vol / big-move gating rescue it? (per-ct $, intrinsic)")
        print(f"  {'signal':10s} {'gate':10s} | {'DEV/ALL mean(t,n)':>22s} | {'HOLD mean(t,n)':>20s} | win%ALL")
        for sig in SIGNALS:
            for gate in GATES:
                av = sel(recs, sig, strike, gate, "ALL")
                hv = sel(recs, sig, strike, gate, "HOLD")
                if len(av) < 5:
                    continue
                ma, ta, na = cl(av); mh, th, nh = cl(hv)
                w = 100.0 * np.mean([p > 0 for p in av])
                hs = f"{mh:+6.0f}(t{th:+4.1f},{nh})" if nh >= 2 else f"{'(n<2)':>20s}"
                print(f"  {sig:10s} {gate:10s} | {ma:+7.0f}(t{ta:+4.1f},{na:4d}) | {hs:>20s} | {w:4.0f}%")

    # ---- CALL vs PUT split (headline configs) ----
    block("CALL vs PUT split — ATM, gate=all, ALL years (is one side the loser, per the project's history?)")
    for sig in SIGNALS:
        for side in ("CALL", "PUT"):
            v = sel(recs, sig, "ATM", "all", "ALL", side=side)
            m, t, n = cl(v)
            w = 100.0 * np.mean([p > 0 for p in v]) if v else float("nan")
            print(f"  {sig:10s} {side:4s}  n={n:4d}  mean={m:+7.1f}$/ct (t{t:+4.1f})  win={w:4.0f}%")

    # ---- greek decomposition (ATM, gate=all, ALL) ----
    block("GREEK DECOMPOSITION — ATM, gate=all, ALL (mean $/ct; ties out to net actual)")
    print(f"  {'signal':10s} {'delta$':>8s} {'gamma$':>8s} {'theta$':>8s} {'spread$':>8s} "
          f"{'comm$':>7s} {'= net$':>8s} {'(sanity)':>9s}")
    for sig in SIGNALS:
        rr = [r for r in recs if r["sig"] == sig and r["strike"] == "ATM" and r["gate"] == "all"]
        if not rr:
            continue
        dl = np.nanmean([r["delta_pnl"] for r in rr]); ga = np.nanmean([r["gamma_pnl"] for r in rr])
        th = np.nanmean([r["theta_pnl"] for r in rr]); sp = np.nanmean([r["spread_cost"] for r in rr])
        net = np.nanmean([r["pnl_intr"] for r in rr])
        recon = dl + ga + th - sp - COMMISSION_RT
        print(f"  {sig:10s} {dl:+8.1f} {ga:+8.1f} {th:+8.1f} {-sp:+8.1f} {-COMMISSION_RT:+7.2f} "
              f"{net:+8.1f} {recon:+9.1f}")

    # ---- settle sensitivity: intrinsic vs sell-at-bid (ATM, all, ALL) ----
    block("SETTLE SENSITIVITY — intrinsic@close vs sell-at-16:00-bid (ATM, gate=all, ALL)")
    print(f"  {'signal':10s} {'intrinsic $/ct(t,n)':>24s} {'sell-at-bid $/ct(t,n)':>24s}")
    for sig in SIGNALS:
        vi = sel(recs, sig, "ATM", "all", "ALL", field="pnl_intr")
        vb = sel(recs, sig, "ATM", "all", "ALL", field="pnl_bid")
        mi, ti, ni = cl(vi); mb, tb, nb = cl(vb)
        print(f"  {sig:10s} {mi:+9.1f}(t{ti:+4.1f},{ni:4d})   {mb:+9.1f}(t{tb:+4.1f},{nb:4d})")

    # ---- CSV ----
    csvp = os.path.join(SCRATCH, "intraday_momentum_trades.csv")
    cols = ["date", "split", "year", "sig", "strike", "gate", "side", "vix_prev", "entry", "mid",
            "spread", "intrinsic", "pnl_intr", "pnl_bid", "delta_pnl", "gamma_pnl", "theta_pnl",
            "spot_pnl", "spread_cost"]
    with open(csvp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in recs:
            w.writerow({k: r.get(k) for k in cols})
    print(f"\n  trades CSV -> {csvp}  ({len(recs)} rows)")

    block("PART 2 READ")
    print("  Viable = +ve $/ct with |t|>~2 that REPEATS across years AND holds on HOLDOUT, for a single")
    print("  strike/gate. The last-30-min move is ~sub-point, so the delta$ gain must beat half-spread+")
    print("  slip+commission. If every cell is <=0 (spread eats the thin tilt), the effect is REAL-but-")
    print("  unmonetizable as a naked long; high-vol gating only helps if bigger moves outrun wider spreads.")


if __name__ == "__main__":
    main()
