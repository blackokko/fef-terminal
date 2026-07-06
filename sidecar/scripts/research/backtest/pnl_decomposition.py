"""P&L DECOMPOSITION — stop asserting "theta", MEASURE it.  (READ-ONLY research)

For a representative sample of 0DTE long-option trades we split each trade's REALIZED option
P&L ($/contract) into first/second-order Greek components so we can state, with numbers, what
drives the corrected-timing convexity NULL (~ -$15..-$25/ct): DIRECTION vs THETA vs VEGA vs
SPREAD.

  realized_$ = (bid_exit - ask_entry) * 100                       (real fills: buy ask, sell bid)
  mid2mid_$  = (mid_exit  - mid_entry) * 100
  spread_$   = mid2mid_$ - realized_$   (= half-spread in + half-spread out, a >=0 COST)
  mid2mid_$ ≈ delta·ΔS·100 + ½·gamma·ΔS²·100 + theta·Δt·100 + vega·ΔIV·100 + residual
  => realized_$ = DIRECTION(delta+gamma) + THETA + VEGA + RESIDUAL - SPREAD

Greeks: BSM (scripts.research.backtest.greeks) inverted from the NBBO MID at the fill, using the
  parity-reconstructed spot (data.Session.spot) as S, r=0, calendar day-count (T = minutes-to-
  16:00-ET / (365*24*60)); theta is per calendar DAY, vega per 1 vol-POINT (greeks.py convention).
  Δt_days = elapsed minutes / 1440.  ΔIV in vol-points = (IV_exit - IV_entry)*100.
  REAL IBKR greeks (historical_option_chain_point, 2026-05-27+) are used to VALIDATE the BSM
  greeks (mean abs error) on the overlap, and are substituted per-trade where a snapshot matches.

Samples (both filled NO-LOOKAHEAD at t0+60s = the bar close; +30min fixed hold; ATM):
  BREAKOUT = the corrected-timing convexity trend-breakout entries (convexity_regime_exit).
  RAND     = matched count of random-minute / random-direction ATM entries (the neutral control).

Run:  .venv/Scripts/python.exe -m scripts.research.backtest.pnl_decomposition [STRIDE]
  STRIDE (default 4) subsamples sessions for speed; smaller = more trades.
"""
from __future__ import annotations

import datetime as dt
import sys

import numpy as np

from . import convexity_regime_exit as CX
from . import trend_breakout_entry as TB
from .data import BAD, ET, Session
from .greeks import greeks, implied_vol

sys.stdout.reconfigure(encoding="utf-8")

HOLD_MS = 30 * 60_000
FILL_LAG_MS = 60_000
YEAR_MIN = 365.0 * 24 * 60          # calendar minutes per year (greeks.py convention)
R = 0.0
RNG = np.random.default_rng(20260701)
WIN_LO, WIN_HI = TB.WIN_LO, TB.WIN_HI


def mins_to_1600(ts_ms: int) -> float:
    now = dt.datetime.fromtimestamp(ts_ms / 1000, ET)
    exp = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return (exp - now).total_seconds() / 60.0


def _valid_q(q):
    return q is not None and q[2] not in BAD and q[0] is not None and q[1] is not None and q[1] >= q[0] > 0


def decompose(sess: Session, tf: int, right: str, strike: float, real_g=None):
    """Decompose one long-option trade (fill at/after tf, +30min hold) into $ components.
    real_g: optional dict of REAL greeks to use instead of BSM (for the 2026-05-27+ overlap)."""
    em = sess.minute_at_or_after(tf)
    if em is None or em - tf > 90_000:
        return None
    qe = sess.quote(em, strike, right)
    if not _valid_q(qe):
        return None
    bid0, ask0, _ = qe
    mid0 = (bid0 + ask0) / 2.0
    if ask0 < CX.MIN_ASK:
        return None
    S0 = sess.spot(em)
    if S0 is None:
        return None
    T0 = mins_to_1600(em) / YEAR_MIN
    if T0 <= 0:
        return None
    iv0 = implied_vol(mid0, S0, strike, T0, right, r=R)
    if iv0 is None:
        return None
    g = real_g if real_g is not None else greeks(S0, strike, T0, iv0, right, r=R)
    if g is None:
        return None
    # exit: first quote at/after +30min (fallback: last quote on/before)
    xm = sess.minute_at_or_after(em + HOLD_MS) or sess.minute_at_or_before(em + HOLD_MS)
    if xm is None or xm <= em:
        return None
    qx = sess.quote(xm, strike, right)
    if not _valid_q(qx):
        return None
    bid1, ask1, _ = qx
    mid1 = (bid1 + ask1) / 2.0
    S1 = sess.spot(xm)
    if S1 is None:
        return None
    T1 = max(0.0, mins_to_1600(xm) / YEAR_MIN)
    iv1 = implied_vol(mid1, S1, strike, T1, right, r=R) if T1 > 0 else None

    dS = S1 - S0
    dt_days = (T0 - T1) * 365.0
    d_iv_volpts = ((iv1 - iv0) * 100.0) if iv1 is not None else 0.0

    delta_d = g["delta"] * dS * 100.0
    gamma_d = 0.5 * g["gamma"] * dS * dS * 100.0
    theta_d = g["theta"] * dt_days * 100.0           # theta per calendar day
    vega_d = g["vega"] * d_iv_volpts * 100.0          # vega per 1 vol-point
    mid2mid = (mid1 - mid0) * 100.0
    resid = mid2mid - (delta_d + gamma_d + theta_d + vega_d)
    realized = (bid1 - ask0) * 100.0                  # real fills
    spread = mid2mid - realized                       # >= 0 cost (both half-spreads)
    sgn = 1.0 if right == "C" else -1.0
    return dict(
        realized=realized, mid2mid=mid2mid, spread=spread,
        direction=delta_d + gamma_d, delta=delta_d, gamma=gamma_d,
        theta=theta_d, vega=vega_d, resid=resid,
        dS=dS, opt_move=sgn * dS, hold_min=(xm - em) / 60000.0,
        iv0=iv0, entry_px=ask0, mid0=mid0, hh=CX.et_h(em),
        used_real=(real_g is not None))


# ------------------------------------------------------------------ real-greek overlap
def load_real_greeks(con):
    """(session_date, round(ts/60000), strike, right) -> greek dict, for rows with full greeks."""
    out = {}
    for sd, ts, k, r, iv, de, ga, th, ve in con.execute(
        "SELECT session_date,timestamp_ms,strike,right,iv,delta,gamma,theta,vega "
        "FROM historical_option_chain_point WHERE delta IS NOT NULL AND gamma IS NOT NULL "
        "AND theta IS NOT NULL AND vega IS NOT NULL"):
        out[(sd, int(round(ts / 60000)), float(k), r)] = dict(
            iv=iv, delta=de, gamma=ga, theta=th, vega=ve)
    return out


def validate_bsm_vs_real(con, real):
    """Compare BSM greeks (from the matching NBBO quote mid) to the REAL IBKR greeks."""
    print("\n" + "=" * 96)
    print("BSM-vs-REAL GREEK VALIDATION  (overlap where historical_option_chain_point has full greeks")
    print("  AND option_quote_history has a quote at the same date/minute/strike/right)")
    by_sd = {}
    for (sd, tmin, k, r) in real:
        by_sd.setdefault(sd, []).append((tmin, k, r))
    errs = {g: [] for g in ("delta", "gamma", "theta", "vega", "iv")}
    n = 0
    for sd in sorted(by_sd):
        sess = Session(con, sd)
        if sess.empty():
            continue
        for (tmin, k, r) in by_sd[sd]:
            ts = tmin * 60000
            m = sess.minute_at_or_before(ts + 30000)
            if m is None or abs(m - ts) > 90_000:
                continue
            q = sess.quote(m, k, r)
            if not _valid_q(q):
                continue
            mid = (q[0] + q[1]) / 2.0
            S = sess.spot(m)
            if S is None:
                continue
            T = mins_to_1600(m) / YEAR_MIN
            if T <= 0:
                continue
            iv = implied_vol(mid, S, k, T, r, r=R)
            if iv is None:
                continue
            g = greeks(S, k, T, iv, r, r=R)
            rg = real[(sd, tmin, k, r)]
            if g is None or rg["iv"] is None:
                continue
            errs["delta"].append(abs(g["delta"] - rg["delta"]))
            errs["gamma"].append(abs(g["gamma"] - rg["gamma"]))
            errs["theta"].append(abs(g["theta"] - rg["theta"]))
            errs["vega"].append(abs(g["vega"] - rg["vega"]))
            errs["iv"].append(abs(iv - rg["iv"]))
            n += 1
    if n == 0:
        print("  no overlapping quote+greek rows found.")
        return
    print(f"  matched {n} contract-snapshots. mean |BSM - IBKR|:")
    print(f"    iv {np.mean(errs['iv']):.4f}   delta {np.mean(errs['delta']):.4f}   "
          f"gamma {np.mean(errs['gamma']):.5f}   theta {np.mean(errs['theta']):.3f}   "
          f"vega {np.mean(errs['vega']):.4f}")
    print("  (small errors => the BSM greeks used below are trustworthy)")


# ------------------------------------------------------------------ build the samples
def build(con, sessions, real):
    brk, rnd = [], []
    for d in sessions:
        rt, rp = CX._spx.get(d, (None, None))
        if rt is None:
            continue
        feat = CX.precompute_price(d, d[:4], rt, rp)
        ents = TB.entries_for_config(feat, CX.CFG)
        sess = Session(con, d)
        if sess.empty() or not sess.strikes:
            continue

        def one(t0, dr):
            tf = t0 + FILL_LAG_MS
            s_fill = TB.real_at(rt, rp, tf)
            if s_fill is None:
                return None
            right = "C" if dr > 0 else "P"
            k = min(sess.strikes, key=lambda kk: abs(kk - s_fill))
            key = (d, int(round(tf / 60000)), float(k), right)
            return decompose(sess, tf, right, float(k), real_g=real.get(key))

        n_here = 0
        for i, dr in ents:
            t0 = int(rt[i])
            r = one(t0, dr)
            if r:
                r["date"] = d
                brk.append(r)
                n_here += 1
        # matched RAND: same count, random window minute + random dir
        win_ts = [int(t) for t in rt if WIN_LO <= CX.et_h(int(t)) <= WIN_HI]
        tries = 0
        got = 0
        while got < n_here and tries < n_here * 8 + 10:
            tries += 1
            t0 = int(RNG.choice(win_ts))
            dr = 1 if RNG.random() < 0.5 else -1
            r = one(t0, dr)
            if r:
                r["date"] = d
                rnd.append(r)
                got += 1
    return brk, rnd


# ------------------------------------------------------------------ reporting
def clustered_t(recs, key):
    by = {}
    for r in recs:
        by.setdefault(r["date"], []).append(r[key])
    m = np.array([np.mean(v) for v in by.values()], float)
    if len(m) < 2:
        return float("nan")
    se = m.std(ddof=1) / np.sqrt(len(m))
    return float(m.mean() / se) if se > 0 else float("nan")


def summarize(name, recs):
    if not recs:
        print(f"\n  [{name}] no trades")
        return
    a = {k: np.array([r[k] for r in recs], float) for k in
         ("realized", "direction", "delta", "gamma", "theta", "vega", "resid", "spread", "mid2mid")}
    n = len(recs)
    win = 100.0 * np.mean(a["realized"] > 0)
    print(f"\n  [{name}]  n={n}  realized mean ${a['realized'].mean():+7.1f}/ct  "
          f"med ${np.median(a['realized']):+7.1f}  win {win:.0f}%  clustered-t {clustered_t(recs,'realized'):+.2f}")
    print(f"    {'component':16s} {'mean$/ct':>10s}   (sum of components reconstructs realized)")
    for lbl, key, sign in [("DIRECTION Δ+Γ", "direction", +1), ("  delta", "delta", +1),
                           ("  gamma", "gamma", +1), ("THETA", "theta", +1),
                           ("VEGA", "vega", +1), ("RESIDUAL", "resid", +1),
                           ("SPREAD (cost)", "spread", -1)]:
        print(f"    {lbl:16s} {sign*a[key].mean():+10.1f}")
    check = a["direction"].mean() + a["theta"].mean() + a["vega"].mean() + a["resid"].mean() - a["spread"].mean()
    print(f"    {'= realized':16s} {check:+10.1f}   (vs {a['realized'].mean():+.1f} actual)")


def split_report(name, recs):
    summarize(name + " (pooled)", recs)
    summarize(name + " WINNERS", [r for r in recs if r["realized"] > 0])
    summarize(name + " LOSERS", [r for r in recs if r["realized"] <= 0])


def main():
    stride = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    con = CX.sqlite3.connect(f"file:{CX.DB}?mode=ro", uri=True)
    print("=" * 96)
    print("P&L DECOMPOSITION — measuring DIRECTION vs THETA vs VEGA vs SPREAD ($/contract)")
    print("  corrected-timing (t0+60s, no-lookahead) ATM breakout trades + matched RAND control")
    print("=" * 96)

    print("loading SPX rulers + real greeks ...", flush=True)
    CX._spx = CX.load_spx_rulers(con)
    real = load_real_greeks(con)
    print(f"  SPX ruler sessions: {len(CX._spx)}   real-greek snapshots: {len(real)}")

    validate_bsm_vs_real(con, real)

    allsess = sorted(d for d in CX._spx if CX.DEV_START <= d <= CX.HOLD_END)
    sample = allsess[::stride]
    print(f"\n  decomposing {len(sample)} sessions (stride={stride}) of {len(allsess)} ...", flush=True)
    brk, rnd = build(con, sample, real)
    print(f"  BREAKOUT trades: {len(brk)}   RAND trades: {len(rnd)}   "
          f"(used real greeks: brk={sum(r['used_real'] for r in brk)} rnd={sum(r['used_real'] for r in rnd)})")

    print("\n" + "=" * 96)
    print("DECOMPOSITION TABLE  (mean $/contract per component; +30min hold; buy ask / sell bid)")
    print("=" * 96)
    split_report("BREAKOUT", brk)
    split_report("RAND", rnd)

    # ---- the three explicit answers -------------------------------------------------
    print("\n" + "=" * 96)
    print("EXPLICIT ANSWERS")
    print("=" * 96)
    ba = {k: np.array([r[k] for r in brk], float) for k in
          ("realized", "direction", "theta", "vega", "spread", "resid")}
    print(f"(a) Corrected BREAKOUT null realized mean = ${ba['realized'].mean():+.1f}/ct. It is the sum of:")
    print(f"      DIRECTION(Δ+Γ) ${ba['direction'].mean():+.1f}   THETA ${ba['theta'].mean():+.1f}   "
          f"VEGA ${ba['vega'].mean():+.1f}   RESID ${ba['resid'].mean():+.1f}   SPREAD -${ba['spread'].mean():.1f}")

    mid = [r for r in brk if 11.0 <= r["hh"] < 14.0]
    if mid:
        mt = np.array([r["theta"] for r in mid], float)
        mh = np.array([r["hold_min"] for r in mid], float)
        print(f"(b) Mid-day (11:00-14:00 ET) ATM 0DTE, mean hold {mh.mean():.0f}min: "
              f"mean measured THETA decay = ${mt.mean():+.1f}/ct.")
        flat = [r for r in mid if abs(r["dS"]) < 1.0]
        if flat:
            fm = np.array([r["mid2mid"] for r in flat], float)
            print(f"      cross-check (near-flat spot |ΔS|<1pt, n={len(flat)}): empirical mid-to-mid "
                  f"decay ${fm.mean():+.1f}/ct (theta+vega, no direction).")

    caught = [r for r in brk if r["opt_move"] >= 5.0]
    if caught:
        cd = np.array([r["direction"] for r in caught], float)
        ct = np.array([r["theta"] for r in caught], float)
        cr = np.array([r["realized"] for r in caught], float)
        print(f"(c) Trades that CAUGHT a move (favorable ΔS>=5pt, n={len(caught)}): "
              f"DIRECTION ${cd.mean():+.1f}/ct vs THETA ${ct.mean():+.1f}/ct  "
              f"-> direction/gamma {'DOMINATES' if abs(cd.mean())>abs(ct.mean()) else 'does NOT dominate'} theta; "
              f"realized ${cr.mean():+.1f}/ct.")

    # ---- example trades -------------------------------------------------------------
    print("\n" + "=" * 96)
    print("EXAMPLE TRADES (line-by-line, $/contract)")
    print("=" * 96)
    exs = []
    win = [r for r in brk if r["realized"] > 50]
    los = [r for r in brk if r["realized"] < -50]
    flatish = sorted(brk, key=lambda r: abs(r["dS"]))[:1]
    if win:
        exs.append(("WINNER (caught move)", max(win, key=lambda r: r["realized"])))
    if los:
        exs.append(("LOSER (theta+spread)", min(los, key=lambda r: r["realized"])))
    if flatish:
        exs.append(("FLAT spot (pure decay)", flatish[0]))
    print(f"  {'trade':24s} {'ΔS':>6s} {'entry$':>7s} {'realized':>9s} | "
          f"{'dir':>7s} {'theta':>7s} {'vega':>7s} {'resid':>7s} {'spread':>7s}")
    for lbl, r in exs:
        print(f"  {lbl:24s} {r['dS']:+6.1f} {r['entry_px']*100:7.0f} {r['realized']:+9.1f} | "
              f"{r['direction']:+7.1f} {r['theta']:+7.1f} {r['vega']:+7.1f} {r['resid']:+7.1f} {-r['spread']:+7.1f}")

    con.close()
    print("\nDONE.")


if __name__ == "__main__":
    main()
