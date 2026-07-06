"""FILTER-GATED 0DTE SPX PREMIUM SELLING — does selling 10-wide credit spreads, GATED by the
magnitude filter (sell only in the calm regime, flat on capitulation / high-VIX), net positive
with a SURVIVABLE tail?  (READ-ONLY research spike; engine/live code untouched.)

THE ONE STRUCTURAL EDGE UNDER TEST: variance-risk-premium (sellers are paid).  THE TAIL DEFENSE:
the magnitude filter (magnitude_filter.py / magnitude_router.py) — SELL ONLY when calm, go FLAT on
a big-move-predicted day.  Success = positive expectancy with a filter-managed tail, NOT "$500-1k/day".

GATE (causal, pre-entry only):  SELL iff
    CALM      = (VIX prior-close < 15)  OR  (realized-vol-so-far <= DEV bottom-quartile edge)
    AND  NO capitulation/blowoff EVENT has fired between the open and entry (MF cap/blow geometry)
    AND  VIX prior-close < 25   (never sell a high-VIX regime)
Go FLAT otherwise.  We also report the UN-gated blow-up the gate avoids, and the residual tail from
events that fire AFTER entry (which a causal gate cannot see).

STRUCTURES (0DTE SPXW, expiry = session date). Real NBBO from option_quote_history: SELL short legs
at the BID, BUY long legs at the ASK (full real spread paid).  10-wide.  Short strikes ~0.16 delta
(BSM delta from the ATM-straddle-implied move; T-convention-free).  Settlement = real IBKR SPX close
(PM cash-settle), fallback parity spot.  Entry ~10:00 ET (theta-later finding), 10:30/11:00 sensitivity.
  V1a  PUT credit spread (short 0.16d put, long 10 lower)            directional
  V1b  CALL credit spread (short 0.16d call, long 10 higher)         directional
  V2   IRON CONDOR (V1a + V1b)                                       direction-neutral VRP harvest
  V3a  CONDOR + long CALL (~0.20d, buy w/ credit)                    upside convexity
  V3b  CONDOR + long PUT  (~0.20d)                                   downside convexity
  V4   CONDOR + TAIL HEDGE (long put & long call ~0.06d beyond wings) caps both tails

Ruler / gate features on the INDEPENDENT IBKR SPX 1-min series (never a parity spot for the signal —
see MEMORY [[validate-against-independent-price]]).  DEV 2022-01-03..2025-08-12 / HOLDOUT
2025-08-13..2026-06-26 reported separately; 2022 bear called out as the real tail stress-test.

Run:  .venv/Scripts/python.exe -m scripts.research.backtest.premium_selling
CSVs -> scratchpad.
"""
from __future__ import annotations

import csv
import datetime as dt
import math
import os
import sys
from collections import defaultdict
from zoneinfo import ZoneInfo

import numpy as np

from . import confluence_expectancy as CE
from . import magnitude_filter as MF
from .data import BAD, connect
from .holdout import HOLDOUT_START
from .ibkr_ruler import ibkr_open, ibkr_series

sys.stdout.reconfigure(encoding="utf-8")
ET = ZoneInfo("America/New_York")
SCRATCH = CE.SCRATCH

DEV_START, DEV_END = "2022-01-03", "2025-08-12"
HOLD_START, HOLD_END = "2025-08-13", "2026-06-26"

ENTRY_ET = 10.0                 # primary entry (ET hours); sensitivity at 10.5 / 11.0
WING = 10.0                     # credit-spread width (pt)
SHORT_DELTA = 0.16              # target |delta| of the short legs (calm-day ~0.15-0.20)
CX_BUDGET_FRAC = 0.50           # V3 convexity long spends <= 50% of the condor credit
TAIL_BUDGET_SIDE = 0.50         # V4 tail hedge target <= 50% of credit per side (cheapest fallback)
STRADDLE_K = 0.7978845608       # ATM straddle = K * S * sigma*sqrt(T)  (=sqrt(2/pi))
VIX_CALM, VIX_HIGH = 15.0, 25.0
COMM_LEG = 0.65                 # $/contract/leg (consistent with credit.py harness)
CONTRACT = 100.0                # $/pt
TARGET_CREDIT = 750.0           # $/day net-credit sizing target (mid of $500-1k)

VARIANTS = ["V1a_putspread", "V1b_callspread", "V2_condor",
            "V3a_condor+longC", "V3b_condor+longP", "V4_condor+tailhedge"]


# ============================================================ math / pricing helpers
def et_h(ms):
    x = dt.datetime.fromtimestamp(ms / 1000, ET)
    return x.hour + x.minute / 60.0


def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def call_delta(S, K, v):
    """BSM call delta with total vol v = sigma*sqrt(T) (r=0). v>0."""
    if v <= 0 or S <= 0 or K <= 0:
        return float("nan")
    d1 = (math.log(S / K) + 0.5 * v * v) / v
    return ncdf(d1)


def pick_by_delta(strikes, S, v, target, side, beyond=None):
    """Nearest available strike whose |delta| ~ target. side='C'(K>S) or 'P'(K<S).
    If `beyond` given, restrict to strikes strictly further OTM than `beyond`."""
    best, bd = None, 1e9
    for K in strikes:
        if side == "C":
            if K <= S:
                continue
            if beyond is not None and K <= beyond:
                continue
            dl = call_delta(S, K, v)
        else:
            if K >= S:
                continue
            if beyond is not None and K >= beyond:
                continue
            dl = 1.0 - call_delta(S, K, v)   # |put delta|
        if not np.isfinite(dl):
            continue
        if abs(dl - target) < bd:
            bd, best = abs(dl - target), K
    return best


def pick_affordable(qd, strikes, S, side, budget, beyond=None):
    """Closest-to-money OTM long whose ASK <= budget (pt) — 'spend part of the credit'.
    side='C' (K>S, take smallest affordable K) / 'P' (K<S, take largest affordable K).
    beyond: restrict to strikes strictly further OTM than this (for a beyond-the-wing hedge)."""
    cands = []
    for K in strikes:
        if side == "C" and (K <= S or (beyond is not None and K <= beyond)):
            continue
        if side == "P" and (K >= S or (beyond is not None and K >= beyond)):
            continue
        ba = bidask(qd, K, side)
        if ba is None or ba[1] <= 0 or ba[1] > budget:
            continue
        cands.append((K, ba[1]))
    if not cands:
        return None
    # closest to the money = smallest K for calls, largest K for puts
    K, ask = (min(cands, key=lambda x: x[0]) if side == "C" else max(cands, key=lambda x: x[0]))
    return (K, ask)


def cheapest_beyond(qd, strikes, S, side, wing):
    """Cheapest valid long strike strictly beyond the wing (deepest-OTM tail insurance fallback)."""
    best = None
    for K in strikes:
        if side == "C" and K <= wing:
            continue
        if side == "P" and K >= wing:
            continue
        ba = bidask(qd, K, side)
        if ba is None or ba[1] <= 0:
            continue
        if best is None or ba[1] < best[1]:
            best = (K, ba[1])
    return best


def bidask(qd, K, r):
    """(bid, ask) for (K,right) from an entry-quote dict, or None if missing / bad-quality."""
    v = qd.get((K, r))
    if v is None:
        return None
    bid, ask, qual = v
    if qual in BAD or bid is None or ask is None:
        return None
    return (float(bid), float(ask))


# ============================================================ ruler cache
_RULER = {}


def ruler(ic, date):
    if date not in _RULER:
        _RULER[date] = ibkr_series(ic, date)   # (ts[], px[]) or None
    return _RULER[date]


def ruler_at(rs, target, tol=180_000):
    if rs is None:
        return None
    rt, rp = rs
    j = int(np.searchsorted(rt, target))
    best, bd = None, tol + 1
    for c in (j - 1, j):
        if 0 <= c < len(rt):
            dd = abs(int(rt[c]) - target)
            if dd <= tol and dd < bd:
                bd, best = dd, c
    return float(rp[best]) if best is not None else None


def parity_spot(qd, strikes):
    """median K + C_mid - P_mid over 5 nearest-money strikes, from an entry-quote dict."""
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


# ============================================================ per-session pricing
def load_entry_quotes(con, date, ts):
    qd = {}
    for k, r, bid, ask, q in con.execute(
        "SELECT strike,right,bid,ask,quote_quality FROM option_quote_history "
        "WHERE session_date=? AND timestamp_ms=?", (date, ts)):
        qd[(float(k), r)] = (bid, ask, q)
    return qd


def price_session(con, ic, date, entry_et=ENTRY_ET):
    """Price all six structures for one session at ~entry_et. Returns a dict of per-variant
    pnl_pts (+ diagnostics) or None if the chain / entry is unusable."""
    minutes = [r[0] for r in con.execute(
        "SELECT DISTINCT timestamp_ms FROM option_quote_history WHERE session_date=? "
        "ORDER BY timestamp_ms", (date,))]
    if len(minutes) < 30:
        return None
    entry_ts = next((m for m in minutes if et_h(m) >= entry_et), None)
    if entry_ts is None or et_h(entry_ts) > entry_et + 0.5:
        return None
    settle_ts = minutes[-1]
    qd = load_entry_quotes(con, date, entry_ts)
    if not qd:
        return None
    strikes = sorted({k for (k, _r) in qd})
    rs = ruler(ic, date)
    S = ruler_at(rs, entry_ts) or parity_spot(qd, strikes)
    if S is None:
        return None
    # settlement spot: real IBKR close, fallback parity at last minute
    S_close = ruler_at(rs, settle_ts, tol=600_000)
    if S_close is None:
        qd_last = load_entry_quotes(con, date, settle_ts)
        S_close = parity_spot(qd_last, sorted({k for (k, _r) in qd_last}))
    if S_close is None:
        return None

    # ATM-straddle-implied total vol v = sigma*sqrt(T)  (T-convention-free strike selection)
    atmK = min(strikes, key=lambda k: abs(k - S))
    c = qd.get((atmK, "C")); p = qd.get((atmK, "P"))
    if not c or not p or c[2] in BAD or p[2] in BAD:
        return None
    straddle = (c[0] + c[1]) / 2.0 + (p[0] + p[1]) / 2.0
    if straddle <= 0:
        return None
    v = straddle / (STRADDLE_K * S)                    # sigma*sqrt(T)
    em = S * v                                          # ~1-sigma move to close (pt)

    # ---- strike selection ----
    scs = pick_by_delta(strikes, S, v, SHORT_DELTA, "C")     # short call
    sps = pick_by_delta(strikes, S, v, SHORT_DELTA, "P")     # short put
    if scs is None or sps is None:
        return None
    scl = min(strikes, key=lambda k: abs(k - (scs + WING)))  # long call wing
    spl = min(strikes, key=lambda k: abs(k - (sps - WING)))  # long put wing
    if len({scs, scl}) < 2 or len({sps, spl}) < 2:
        return None
    wc = abs(scl - scs); wp = abs(sps - spl)                 # actual widths

    # ---- entry fills: SELL shorts @ bid, BUY longs @ ask ----
    q_scs = bidask(qd, scs, "C"); q_scl = bidask(qd, scl, "C")
    q_sps = bidask(qd, sps, "P"); q_spl = bidask(qd, spl, "P")
    if None in (q_scs, q_scl, q_sps, q_spl):
        return None
    call_credit = q_scs[0] - q_scl[1]                        # bid(short) - ask(long)
    put_credit = q_sps[0] - q_spl[1]
    if call_credit <= 0 or put_credit <= 0:
        return None

    # ---- settlement payouts (points we OWE) ----
    call_pay = min(max(0.0, S_close - scs), wc)
    put_pay = min(max(0.0, sps - S_close), wp)

    scd = call_delta(S, scs, v); spd = 1.0 - call_delta(S, sps, v)

    out = dict(date=date, split=("DEV" if date < HOLDOUT_START else "HOLD"), year=date[:4],
               entry_ts=entry_ts, S=S, S_close=S_close, em=em, vfrac=v, atmK=atmK,
               scs=scs, scl=scl, sps=sps, spl=spl, wc=wc, wp=wp,
               call_credit=call_credit, put_credit=put_credit,
               short_call_delta=scd, short_put_delta=spd,
               move_pts=(S_close - S))

    # V1a / V1b / V2
    out["V1a_putspread"] = dict(credit=put_credit, pnl=put_credit - put_pay,
                                risk=wp - put_credit, legs=2)
    out["V1b_callspread"] = dict(credit=call_credit, pnl=call_credit - call_pay,
                                 risk=wc - call_credit, legs=2)
    cond_credit = call_credit + put_credit
    out["V2_condor"] = dict(credit=cond_credit, pnl=cond_credit - call_pay - put_pay,
                            risk=max(wc, wp) - cond_credit, legs=4)

    # V3a: condor + long CALL, closest-to-money affordable within 50% of the credit
    cxc = pick_affordable(qd, strikes, S, "C", CX_BUDGET_FRAC * cond_credit)
    if cxc is not None:
        kcx, cost = cxc
        nc = cond_credit - cost
        pnl = nc - call_pay - put_pay + max(0.0, S_close - kcx)
        out["V3a_condor+longC"] = dict(credit=nc, pnl=pnl, risk=wp - nc, legs=5, kx=kcx, cost=cost,
                                       delta=call_delta(S, kcx, v))
    # V3b: condor + long PUT
    cxp = pick_affordable(qd, strikes, S, "P", CX_BUDGET_FRAC * cond_credit)
    if cxp is not None:
        kpx, cost = cxp
        nc = cond_credit - cost
        pnl = nc - call_pay - put_pay + max(0.0, kpx - S_close)
        out["V3b_condor+longP"] = dict(credit=nc, pnl=pnl, risk=wc - nc, legs=5, kx=kpx, cost=cost,
                                       delta=1.0 - call_delta(S, kpx, v))
    # V4: condor + tail hedge beyond each wing. Prefer closest-to-wing within 50% of credit/side;
    # else fall back to the cheapest (deepest-OTM) beyond-wing strike so the hedge almost always fits.
    tc = (pick_affordable(qd, strikes, S, "C", TAIL_BUDGET_SIDE * cond_credit, beyond=scl)
          or cheapest_beyond(qd, strikes, S, "C", scl))
    tp = (pick_affordable(qd, strikes, S, "P", TAIL_BUDGET_SIDE * cond_credit, beyond=spl)
          or cheapest_beyond(qd, strikes, S, "P", spl))
    if tc is not None and tp is not None and (cond_credit - tc[1] - tp[1]) > 0.05:
        kch, cc = tc; kph, cp = tp
        nc = cond_credit - cc - cp
        pnl = (nc - call_pay - put_pay
               + max(0.0, S_close - kch) + max(0.0, kph - S_close))
        out["V4_condor+tailhedge"] = dict(credit=nc, pnl=pnl, risk=max(wc, wp) - nc, legs=6,
                                          kch=kch, kph=kph, cost=cc + cp)
    return out


# ============================================================ gate
def build_gate(cache_dates, vix_prior, events_by_date, ruler_map, entry_et=ENTRY_ET):
    """Per-date gate features (causal). Returns {date: dict(vix, rvol_entry, pre_event, day_event,
    post_event)}.  rvol threshold set later from DEV."""
    g = {}
    for d in cache_dates:
        rs = ruler_map.get(d)
        vp = vix_prior(d) or {}
        vix = vp.get("vix")
        rvol_entry = np.nan
        entry_ms = None
        if rs is not None:
            rt, rp = rs
            # entry index = first ruler bar at/after entry_et
            idx = None
            for i in range(len(rt)):
                if et_h(int(rt[i])) >= entry_et:
                    idx = i; break
            if idx is not None and idx >= 3:
                entry_ms = int(rt[idx])
                rvol_entry = MF.sofar(rt, rp, idx)[1]
        evs = events_by_date.get(d, [])
        pre = any(ts <= entry_ms for ts in evs) if entry_ms is not None else False
        post = any(ts > entry_ms for ts in evs) if entry_ms is not None else bool(evs)
        g[d] = dict(vix=(vix if vix is not None else np.nan), rvol_entry=rvol_entry,
                    pre_event=pre, post_event=post, day_event=bool(evs))
    return g


def gate_pass(gf, rvol_q1):
    """Causal sell-gate decision from gate features + DEV rvol bottom-quartile edge."""
    vix = gf["vix"]
    calm = ((np.isfinite(vix) and vix < VIX_CALM)
            or (np.isfinite(gf["rvol_entry"]) and gf["rvol_entry"] <= rvol_q1))
    high_vix = np.isfinite(vix) and vix >= VIX_HIGH
    return bool(calm and (not gf["pre_event"]) and (not high_vix))


# ============================================================ tail / distribution stats
def dist(pnls):
    """$-per-day distribution stats. pnls in $ (already sized/commissioned)."""
    a = np.array([p for p in pnls if p is not None and np.isfinite(p)], float)
    if len(a) == 0:
        return None
    wins = a[a > 0]; losses = a[a < 0]
    order = np.sort(a)
    worst = float(order[0]); worst_wk = float(np.sort(_weekly(a))[0]) if len(a) else float("nan")
    avg_win = float(wins.mean()) if len(wins) else 0.0
    return dict(
        n=len(a), total=float(a.sum()), mean=float(a.mean()), median=float(np.median(a)),
        win=100.0 * len(wins) / len(a), worst=worst,
        p1=float(np.percentile(a, 1)), p5=float(np.percentile(a, 5)),
        p95=float(np.percentile(a, 95)), best=float(order[-1]),
        std=float(a.std(ddof=1)) if len(a) > 1 else 0.0,
        maxdd=_maxdd(a), avg_win=avg_win,
        worst_erases=(abs(worst) / avg_win if avg_win > 0 else float("inf")),
        sharpe=(float(a.mean() / a.std(ddof=1)) if len(a) > 1 and a.std(ddof=1) > 0 else float("nan")),
    )


def _weekly(a):
    # chunk the daily series into 5-day "weeks" for a worst-week readout
    return np.array([a[i:i + 5].sum() for i in range(0, len(a), 5)], float)


def _maxdd(a):
    eq = np.cumsum(a)
    peak = np.maximum.accumulate(eq)
    return float((eq - peak).min())


def clustered_t(pnls):
    """Session-clustered t of mean daily $ (unit = day; daily already 1 obs/session)."""
    a = np.array([p for p in pnls if p is not None and np.isfinite(p)], float)
    if len(a) < 2 or a.std(ddof=1) == 0:
        return float("nan")
    return float(a.mean() / (a.std(ddof=1) / math.sqrt(len(a))))


# ============================================================ main
def main():
    ap_entry = float(sys.argv[1]) if len(sys.argv) > 1 else ENTRY_ET
    print("=" * 120)
    print("FILTER-GATED 0DTE SPX PREMIUM SELLING — 10-wide credit spreads gated by the magnitude filter")
    print(f"  real NBBO (sell@bid/buy@ask), IBKR-close settle, entry ~{ap_entry:.1f} ET, short ~{SHORT_DELTA:.2f}d")
    print(f"  DEV {DEV_START}..{DEV_END} | HOLDOUT {HOLD_START}..{HOLD_END}  (2022 = bear tail stress-test)")
    print("=" * 120)

    con = connect(); ic = ibkr_open()

    # ---- gate inputs ----
    print("\n[1] building capitulation/blowoff events + VIX prior (gate inputs) ...", flush=True)
    events = MF.build_events()
    events_by_date = defaultdict(list)
    for e in events:
        events_by_date[e["date"]].append(int(e["ts"]))
    for d in events_by_date:
        events_by_date[d].sort()
    vix_prior = CE.load_vix_prior()

    # session universe = option-quote sessions in range
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_quote_history WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (DEV_START, HOLD_END))]
    print(f"    option-quote sessions in range: {len(dates)}", flush=True)

    ruler_map = {d: ruler(ic, d) for d in dates}
    gate_feats = build_gate(dates, vix_prior, events_by_date, ruler_map, ap_entry)
    # DEV bottom-quartile rvol-at-entry edge (calm threshold; DEV-only, applied to both splits)
    dev_rvol = np.array([gate_feats[d]["rvol_entry"] for d in dates
                         if d < HOLDOUT_START and np.isfinite(gate_feats[d]["rvol_entry"])], float)
    rvol_q1 = float(np.percentile(dev_rvol, 25))
    print(f"    DEV rvol-at-entry Q1 edge (calm-B threshold): {rvol_q1:.3f} pt/min", flush=True)

    # ---- price every session (gated + ungated) ----
    print("\n[2] pricing all six structures on every session (this is the slow pass) ...", flush=True)
    rows = []
    n_priced = 0
    for k, d in enumerate(dates):
        r = price_session(con, ic, d, ap_entry)
        if r is not None:
            r["gate"] = gate_feats[d]
            r["sell"] = gate_pass(gate_feats[d], rvol_q1)
            rows.append(r)
            n_priced += 1
        if (k + 1) % 150 == 0:
            print(f"    {k+1}/{len(dates)} sessions, {n_priced} priced", flush=True)
    print(f"    priced {n_priced}/{len(dates)} sessions", flush=True)

    # ---- sizing: qty per variant so median GATED-day credit ~ TARGET_CREDIT ----
    def gated(split):
        return [r for r in rows if r["sell"] and (r["split"] == split if split else True)]
    qty = {}
    for var in VARIANTS:
        creds = [r[var]["credit"] * CONTRACT for r in gated(None) if var in r]
        med = float(np.median(creds)) if creds else float("nan")
        qty[var] = max(1, int(round(TARGET_CREDIT / med))) if med and med > 0 else 1

    def pnl_series(subset, var, sized=True):
        out = []
        for r in subset:
            if var not in r:
                continue
            q = qty[var] if sized else 1
            legs = r[var]["legs"]
            out.append((r[var]["pnl"] * CONTRACT - COMM_LEG * legs) * q)
        return out

    def credit_series(subset, var, sized=True):
        out = []
        for r in subset:
            if var not in r:
                continue
            q = qty[var] if sized else 1
            out.append(r[var]["credit"] * CONTRACT * q)
        return out

    # ---- gate coverage ----
    print("\n" + "=" * 120)
    print("GATE COVERAGE — sessions passing the causal sell-gate (calm & no pre-entry event & VIX<25)")
    for split, lo, hi in (("DEV", DEV_START, DEV_END), ("HOLD", HOLD_START, HOLD_END)):
        allr = [r for r in rows if r["split"] == split]
        sell = [r for r in allr if r["sell"]]
        flat = [r for r in allr if not r["sell"]]
        post = [r for r in sell if r["gate"]["post_event"]]
        print(f"  {split:4s}: priced {len(allr):4d} | SELL(gate pass) {len(sell):4d} "
              f"({100*len(sell)/max(1,len(allr)):4.1f}%) | FLAT {len(flat):4d} | "
              f"of SELL days, {len(post)} had a cap/blow event fire AFTER entry (residual tail)")
    # 2022 sub-cut
    r2022 = [r for r in rows if r["year"] == "2022"]
    s2022 = [r for r in r2022 if r["sell"]]
    print(f"  2022 (bear): priced {len(r2022)} | SELL {len(s2022)} ({100*len(s2022)/max(1,len(r2022)):.1f}%)")

    # ---- sizing summary ----
    print("\n" + "=" * 120)
    print(f"SIZING — qty chosen so MEDIAN gated-day net credit ~ ${TARGET_CREDIT:.0f}. Max-loss/day = "
          f"qty x (width - net credit) x $100 (mid-tail defined risk).")
    print(f"  {'variant':22s} {'qty':>4s} {'med credit/day $':>16s} {'med maxloss/day $':>18s}")
    for var in VARIANTS:
        g = [r for r in gated(None) if var in r]
        creds = [r[var]["credit"] * CONTRACT * qty[var] for r in g]
        risks = [r[var]["risk"] * CONTRACT * qty[var] for r in g]
        if not creds:
            continue
        print(f"  {var:22s} {qty[var]:4d} {np.median(creds):16.0f} {-np.median(risks):18.0f}")

    # ---- MAIN VARIANT TABLE: expectancy + tail, DEV vs HOLD (sized to ~$750 credit/day) ----
    for split in ("DEV", "HOLD"):
        print("\n" + "=" * 120)
        print(f"[{split}]  GATED premium-selling — sized to ~${TARGET_CREDIT:.0f} credit/day.  "
              f"EXPECTANCY + TAIL (primary).  n=days traded")
        print(f"  {'variant':22s} {'n':>4s} {'mean$':>7s} {'med$':>6s} {'win%':>5s} {'total$':>9s} "
              f"{'t':>5s} | {'worst$':>8s} {'p5$':>7s} {'maxDD$':>9s} {'wErase':>6s} {'Shp':>5s}")
        sub = gated(split)
        for var in VARIANTS:
            ps = pnl_series(sub, var, sized=True)
            s = dist(ps); t = clustered_t(ps)
            if s is None:
                continue
            print(f"  {var:22s} {s['n']:4d} {s['mean']:7.0f} {s['median']:6.0f} {s['win']:4.0f}% "
                  f"{s['total']:9.0f} {t:5.2f} | {s['worst']:8.0f} {s['p5']:7.0f} {s['maxdd']:9.0f} "
                  f"{s['worst_erases']:6.1f} {s['sharpe']:5.2f}")
        print(f"  (wErase = worst single day / avg winning day; Shp = mean/std of daily $)")

    # ---- PER-CONTRACT (sizing-invariant) core, DEV vs HOLD ----
    for split in ("DEV", "HOLD"):
        print("\n" + "-" * 120)
        print(f"[{split}]  PER-CONTRACT (1 structure/day, sizing-invariant) — mean$/med$/win%/worst$/p5$")
        sub = gated(split)
        for var in VARIANTS:
            ps = pnl_series(sub, var, sized=False)
            s = dist(ps)
            if s is None:
                continue
            print(f"  {var:22s} n={s['n']:4d} mean={s['mean']:6.1f} med={s['median']:6.1f} "
                  f"win={s['win']:4.0f}% worst={s['worst']:7.0f} p5={s['p5']:6.1f} best={s['best']:6.1f}")

    # ---- 2022 BEAR stress-test (gated), the real tail regime ----
    print("\n" + "=" * 120)
    print("2022 BEAR STRESS-TEST (gated selling in the high-vol bear year — the tail the holdout lacks)")
    sub = [r for r in rows if r["year"] == "2022" and r["sell"]]
    for var in VARIANTS:
        ps = pnl_series(sub, var, sized=True)
        s = dist(ps)
        if s is None:
            continue
        print(f"  {var:22s} n={s['n']:3d} mean={s['mean']:7.0f} total={s['total']:8.0f} "
              f"win={s['win']:4.0f}% worst={s['worst']:8.0f} maxDD={s['maxdd']:8.0f}")

    # ---- DOES THE GATE KEEP CATASTROPHES OUT? gated vs un-gated, condor ----
    print("\n" + "=" * 120)
    print("GATE VALUE — same V2 CONDOR, GATED (sell days) vs UN-GATED (every session) vs FLAT-only days")
    print(f"  {'set':28s} {'n':>4s} {'mean$':>7s} {'win%':>5s} {'worst$':>9s} {'p1$':>8s} {'total$':>9s}")
    for lbl, subset in (("GATED (sell days)", [r for r in rows if r["sell"]]),
                        ("UN-GATED (all sessions)", rows),
                        ("FLAT days only (gate skipped)", [r for r in rows if not r["sell"]])):
        ps = pnl_series(subset, "V2_condor", sized=True)
        s = dist(ps)
        if s:
            print(f"  {lbl:28s} {s['n']:4d} {s['mean']:7.0f} {s['win']:4.0f}% {s['worst']:9.0f} "
                  f"{s['p1']:8.0f} {s['total']:9.0f}")
    # what the worst flat-day blow-ups would have been (the gate avoids these)
    flatps = [(r["date"], (r["V2_condor"]["pnl"] * CONTRACT - COMM_LEG * 4) * qty["V2_condor"])
              for r in rows if not r["sell"] and "V2_condor" in r]
    flatps.sort(key=lambda x: x[1])
    print("  worst 8 UN-gated (flat-day) condor blow-ups the gate AVOIDS (sized):")
    for d, p in flatps[:8]:
        gf = gate_feats[d]
        why = ("event" if gf["day_event"] else "") + (" VIX%.0f" % gf["vix"] if np.isfinite(gf["vix"]) else "")
        print(f"    {d}  ${p:9.0f}   ({why.strip()})")

    # ---- MANAGEMENT SENSITIVITY (V2 condor, gated): hold-to-close vs stop 2x vs 50% target ----
    print("\n" + "=" * 120)
    print("MANAGEMENT SENSITIVITY — V2 condor, gated, sized. hold-to-close (base) vs stop@2xcredit vs 50%PT")
    _management_sensitivity(con, ic, [r for r in rows if r["sell"]], qty["V2_condor"], ap_entry)

    # ---- CSVs ----
    _dump_csvs(rows, qty)

    # ---- verdict scaffold ----
    print("\n" + "=" * 120)
    print("VERDICT INPUTS (see final report)")
    for split in ("DEV", "HOLD"):
        sub = gated(split)
        ps = pnl_series(sub, "V2_condor", sized=True)
        s = dist(ps)
        if s:
            print(f"  [{split}] V2 condor: mean ${s['mean']:.0f}/day  win {s['win']:.0f}%  "
                  f"worst ${s['worst']:.0f} (={s['worst_erases']:.0f} avg wins)  maxDD ${s['maxdd']:.0f}  "
                  f"total ${s['total']:.0f} over {s['n']} days")
    print("\nDONE.")


def _management_sensitivity(con, ic, sell_rows, q, entry_et):
    """Re-price the gated condor under intraday management by loading full sessions (walk)."""
    from .data import Session
    hold, stop, targ = [], [], []
    for r in sell_rows:
        d = r["date"]
        sess = Session(con, d)
        if sess.empty():
            continue
        entry_ts = r["entry_ts"]
        legs = [(r["scs"], "C", -1), (r["scl"], "C", +1), (r["sps"], "P", -1), (r["spl"], "P", +1)]
        credit = r["call_credit"] + r["put_credit"]
        # hold-to-close (recompute from stored settle pnl)
        hold.append((r["V2_condor"]["pnl"] * CONTRACT - COMM_LEG * 4) * q)

        def cost_to_close(ts):
            tot = 0.0
            for K, rt_, sgn in legs:
                bx = sess.quote(ts, K, rt_)
                if not bx or bx[2] in BAD:
                    return None
                # to CLOSE: buy back shorts @ ask (sgn=-1 was sold), sell longs @ bid
                tot += (bx[1] if sgn < 0 else -bx[0])
            return tot   # positive = debit to close

        exit_stop = exit_targ = None
        for ts in sess.minutes:
            if ts <= entry_ts:
                continue
            cc = cost_to_close(ts)
            if cc is None:
                continue
            if exit_stop is None and cc >= 2.0 * credit:
                exit_stop = credit - cc
            if exit_targ is None and cc <= 0.5 * credit:
                exit_targ = credit - cc
            if exit_stop is not None and exit_targ is not None:
                break
        # if never triggered, settle at close (use stored settle pnl before commissions)
        settle_pnl = r["V2_condor"]["pnl"]
        s_pnl = (exit_stop if exit_stop is not None else settle_pnl)
        t_pnl = (exit_targ if exit_targ is not None else settle_pnl)
        # commissions: entry 4 legs; +4 exit legs if managed exit occurred
        stop.append((s_pnl * CONTRACT - COMM_LEG * (8 if exit_stop is not None else 4)) * q)
        targ.append((t_pnl * CONTRACT - COMM_LEG * (8 if exit_targ is not None else 4)) * q)
    for lbl, ps in (("hold-to-close", hold), ("stop @ 2x credit", stop), ("50% profit target", targ)):
        s = dist(ps)
        if s:
            print(f"  {lbl:20s} n={s['n']:3d} mean=${s['mean']:6.0f} win={s['win']:4.0f}% "
                  f"worst=${s['worst']:8.0f} p5=${s['p5']:7.0f} maxDD=${s['maxdd']:8.0f} total=${s['total']:8.0f}")


def _dump_csvs(rows, qty):
    # per-session per-variant pnl
    path = os.path.join(SCRATCH, "premium_selling_sessions.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "split", "year", "sell_gate", "vix_prior", "rvol_entry", "pre_event",
                    "post_event", "S_entry", "S_close", "move_pts", "em_pts",
                    "short_call_K", "short_put_K", "short_call_delta", "short_put_delta",
                    "cond_credit"] + [f"pnl_{v}" for v in VARIANTS])
        for r in rows:
            gf = r["gate"]
            row = [r["date"], r["split"], r["year"], int(r["sell"]),
                   f"{gf['vix']:.2f}" if np.isfinite(gf["vix"]) else "",
                   f"{gf['rvol_entry']:.3f}" if np.isfinite(gf["rvol_entry"]) else "",
                   int(gf["pre_event"]), int(gf["post_event"]),
                   f"{r['S']:.2f}", f"{r['S_close']:.2f}", f"{r['move_pts']:.2f}", f"{r['em']:.2f}",
                   r["scs"], r["sps"], f"{r['short_call_delta']:.3f}", f"{r['short_put_delta']:.3f}",
                   f"{r['call_credit'] + r['put_credit']:.3f}"]
            for v in VARIANTS:
                row.append(f"{(r[v]['pnl']*CONTRACT - COMM_LEG*r[v]['legs'])*qty[v]:.2f}" if v in r else "")
            w.writerow(row)
    print(f"\n  sessions CSV -> {path}  ({len(rows)} rows)")


if __name__ == "__main__":
    main()
