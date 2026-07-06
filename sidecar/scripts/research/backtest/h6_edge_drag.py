"""Phase 3 - H6: does a directional 0DTE-style edge SURVIVE at longer DTE (1/2/3)
where theta decay is gentler? DEV SET ONLY.

THE H6 QUESTION
  A naked ATM 0DTE long pays ~$40 of directional edge but bleeds ~$130 of theta+spread
  drag, so the edge dies after friction. If the directional signal is real, holding the
  SAME signal in a longer-dated option (gentler theta) should let more of that edge
  survive. H6 sweeps DTE 0/1/2/3 and reports gross edge vs drag, by DTE.

HARD DATA CONSTRAINT / CAVEAT
  Multi-DTE OPEN INTEREST was never ingested (option_open_interest is 0DTE-only), so we
  CANNOT recompute per-DTE dealer CEX/VEX. We therefore use the 0DTE charm signal
  (option_dealer_exposure.cex) as a CONSISTENT directional trigger across ALL DTEs.
  Holding the trigger (entry minute + direction) AND the underlying exit path fixed
  isolates the pure theta/spread DRAG-by-DTE effect, which is exactly what H6 asks.
  It does NOT test a DTE-appropriate charm signal (that needs multi-DTE OI we lack).

SIGNAL (H2 locked rule, reused verbatim)
  Window 14:30-16:00 ET; threshold |cex| >= dev-set 70th percentile of |cex| in that
  window; trigger = first minute in the window where |cex| >= p70; direction cex>0 ->
  long calls, cex<0 -> long puts. ATM-at-entry.

EXIT (single rule, applied IDENTICALLY across DTEs)
  1.5R target / 1.0R stop, R=8 SPX points, evaluated on the SAME 0DTE parity-spot
  underlying path for every DTE; else settle at the last quote on/before session close.

P&L per contract, two ways
  GROSS = mid-to-mid.  NET = real fills (buy ask+0.05, sell bid-0.05) minus $1.30/ct
  round-trip commission.  drag = gross - net (the spread+slippage+commission tax).

DRIFT-NEUTRAL control (the H6 headline): longer DTE = more delta/beta, so a longer-DTE
long can look better purely because it rode the day's drift. We regress NET per-contract
P&L on the session drift (spot_close - spot_entry) and report the INTERCEPT per DTE — the
edge that remains once the day's directional drift is removed.
"""
from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

import numpy as np

from .data import BAD, Session, connect, sessions
from .holdout import assert_dev, dev_range

ET = ZoneInfo("America/New_York")

R = 8.0            # SPX points per unit risk
TARGET_MULT = 1.5  # 1.5R favorable target
STOP_MULT = 1.0    # 1.0R adverse stop
SLIP = 0.05        # $/ct adverse slippage beyond the touch, per leg (task-specified)
COMMISSION_RT = 1.30  # $/ct round-trip
DTES = (0, 1, 2, 3)


# ---------------------------------------------------------------------------
# Multi-DTE quote loader — mirrors data.Session, sourced from option_quote_multidte.
# ---------------------------------------------------------------------------
class DTEChain(Session):
    """One (session_date, dte) chain loaded once and indexed like data.Session, so it
    inherits the identical parity-spot / atm / mid / quote / minute logic."""

    def __init__(self, con, date, dte):
        self.date = date
        self.dte = dte
        self.q = {}
        mins, strikes = set(), set()
        for ts, k, r, bid, ask, qual in con.execute(
            "SELECT timestamp_ms,strike,right,bid,ask,quote_quality "
            "FROM option_quote_multidte WHERE session_date=? AND dte=?", (date, dte)):
            self.q[(ts, k, r)] = (bid, ask, qual)
            mins.add(ts)
            strikes.add(k)
        self.minutes = sorted(mins)
        self.strikes = sorted(strikes)
        self._spot = {}


# ---------------------------------------------------------------------------
# Charm signal (0DTE, H2 locked rule).
# ---------------------------------------------------------------------------
def _ph_rows(con, d):
    """(ts, cex) for the 14:30-16:00 ET power-hour window of session d."""
    out = []
    for ts, cex in con.execute(
        "SELECT ts_ms,cex FROM option_dealer_exposure WHERE session_date=? ORDER BY ts_ms", (d,)):
        if cex is None:
            continue
        h = dt.datetime.fromtimestamp(ts / 1000, ET)
        hh = h.hour + h.minute / 60.0
        if 14.5 <= hh < 16.0:
            out.append((ts, cex))
    return out


def compute_cex_p70(con, dev_sessions):
    """Dev-set 70th percentile of |cex| within the power-hour window."""
    vals = []
    for d in dev_sessions:
        vals.extend(abs(c) for _, c in _ph_rows(con, d))
    return float(np.percentile(np.asarray(vals, float), 70)), len(vals)


# ---------------------------------------------------------------------------
# Exit: 1.5R/1.0R on the canonical (0DTE) underlying path; fills from `chain`.
# ---------------------------------------------------------------------------
def _valid_quote_at_or_before(chain, ts, strike, right):
    q = chain.quote(ts, strike, right)
    if q and q[2] not in BAD:
        return q
    prev = None
    for m in chain.minutes:
        if m > ts:
            break
        qq = chain.quote(m, strike, right)
        if qq and qq[2] not in BAD:
            prev = qq
    return prev


def _close_spot(path):
    for t in reversed(path.minutes):
        s = path.spot(t)
        if s is not None:
            return s
    return None


def trade_dte(path, chain, ts, right, strike):
    """Enter ATM `strike` of this chain at `ts`, manage on the 0DTE `path` underlying.
    Returns per-contract GROSS (mid-to-mid) and NET (real fills + commission)."""
    qe = chain.quote(ts, strike, right)
    if not qe or qe[2] in BAD:
        return None
    bid_e, ask_e, _ = qe
    entry_mid = (bid_e + ask_e) / 2.0
    entry_buy = ask_e + SLIP
    if entry_mid <= 0.05 or entry_buy <= 0.05:
        return None
    spot0 = path.spot(ts)
    if spot0 is None:
        return None
    sgn = 1.0 if right == "C" else -1.0
    tgt, stp = TARGET_MULT * R, STOP_MULT * R

    exit_ts, reason = None, None
    for t in path.minutes:
        if t <= ts:
            continue
        s = path.spot(t)
        if s is None:
            continue
        fav = sgn * (s - spot0)
        if fav >= tgt:
            exit_ts, reason = t, "target"
            break
        if fav <= -stp:
            exit_ts, reason = t, "stop"
            break
    if exit_ts is None:                       # never hit -> settle on/before close
        exit_ts, reason = path.minutes[-1], "settle"

    qx = _valid_quote_at_or_before(chain, exit_ts, strike, right)
    if qx is None:
        return None
    bid_x, ask_x, _ = qx
    exit_mid = (bid_x + ask_x) / 2.0
    exit_sell = max(0.0, bid_x - SLIP)
    gross = (exit_mid - entry_mid) * 100.0
    net = (exit_sell - entry_buy) * 100.0 - COMMISSION_RT
    return {"gross": gross, "net": net, "reason": reason,
            "entry_mid": entry_mid, "hold_min": round((exit_ts - ts) / 60000)}


# ---------------------------------------------------------------------------
# Build.
# ---------------------------------------------------------------------------
def build():
    con = connect()
    start, end = dev_range()
    assert_dev(end)
    dev = sessions(con, start, end)
    p70, nvals = compute_cex_p70(con, dev)
    print(f"dev-set |cex| p70 (14:30-16:00 ET) = {p70:.4e}  over {nvals} window-minutes "
          f"across {len(dev)} dev sessions", flush=True)

    recs = []
    fired = 0
    for i, d in enumerate(dev):
        rows = _ph_rows(con, d)
        if not rows:
            continue
        trig = next(((ts, cex) for ts, cex in rows if abs(cex) >= p70), None)
        if not trig:
            continue
        ts, cex = trig
        right = "C" if cex > 0 else "P"
        path = Session(con, d)
        if path.empty():
            continue
        spot0 = path.spot(ts)
        if spot0 is None:
            continue
        cs = _close_spot(path)
        drift = (cs - spot0) if cs is not None else None
        rec = {"date": d, "year": d[:4], "dir": right, "drift": drift, "dte": {}}
        for dte in DTES:
            chain = path if dte == 0 else DTEChain(con, d, dte)
            atm = chain.atm(ts)
            if atm is None:
                continue
            r = trade_dte(path, chain, ts, right, atm)
            if r:
                rec["dte"][dte] = r
        if rec["dte"]:
            recs.append(rec)
            fired += 1
        if (i + 1) % 100 == 0:
            print(f"  ..{i+1}/{len(dev)} sessions scanned, {fired} fired", flush=True)
    return recs, p70


# ---------------------------------------------------------------------------
# Stats.
# ---------------------------------------------------------------------------
def _tstat(a):
    a = np.asarray(a, float)
    n = len(a)
    if n < 2:
        return float("nan")
    sd = a.std(ddof=1)
    return (a.mean() / sd * math.sqrt(n)) if sd > 0 else 0.0


def _drift_intercept(x, y):
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    if len(x) < 3:
        return float("nan"), float("nan"), float("nan")
    A = np.vstack([np.ones_like(x), x]).T
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    resid = y - A @ coef
    dof = len(x) - 2
    s2 = (resid @ resid) / dof if dof > 0 else 0.0
    cov = s2 * np.linalg.inv(A.T @ A)
    se0 = math.sqrt(cov[0, 0]) if cov[0, 0] > 0 else 0.0
    a0 = coef[0]
    return a0, (a0 / se0 if se0 > 0 else 0.0), coef[1]


def main():
    np.random.seed(0)
    recs, p70 = build()
    ncall = sum(1 for r in recs if r["dir"] == "C")
    nput = sum(1 for r in recs if r["dir"] == "P")
    print(f"\nH6 DEV: {len(recs)} sessions fired the charm trigger  "
          f"(LONG_CALLS={ncall}, LONG_PUTS={nput})")

    print("\n================  EDGE-VS-DRAG BY DTE  (per contract, charm-triggered ATM long)  ================")
    print(f"{'DTE':>4} {'n':>5} {'gross $/ct':>12} {'net $/ct':>11} {'drag $/ct':>11} "
          f"{'net win%':>9} {'net t':>7}")
    print("-" * 74)
    table = {}
    for dte in DTES:
        g = [r["dte"][dte]["gross"] for r in recs if dte in r["dte"]]
        n = [r["dte"][dte]["net"] for r in recs if dte in r["dte"]]
        if not g:
            print(f"{dte:>4}  (no trades)")
            continue
        gm, nm = float(np.mean(g)), float(np.mean(n))
        win = 100.0 * np.mean(np.asarray(n) > 0)
        table[dte] = (len(n), gm, nm, gm - nm, win, _tstat(n))
        print(f"{dte:>4} {len(n):>5} {gm:>12.1f} {nm:>11.1f} {gm-nm:>11.1f} {win:>8.0f}% {_tstat(n):>+7.1f}")

    print("\n================  DRIFT-NEUTRAL CONTROL BY DTE  (NET per-ct regressed on session drift)  ============")
    print("  intercept = edge remaining after removing the day's SPX drift (spot_close - spot_entry)")
    print(f"{'DTE':>4} {'n':>5} {'intercept $/ct':>15} {'t(intercept)':>13} {'beta_drift':>11} {'verdict':>10}")
    print("-" * 64)
    for dte in DTES:
        xy = [(r["drift"], r["dte"][dte]["net"]) for r in recs
              if dte in r["dte"] and r["drift"] is not None]
        if len(xy) < 3:
            print(f"{dte:>4}  (insufficient)")
            continue
        x, y = zip(*xy)
        a0, t0, b = _drift_intercept(x, y)
        verdict = "SURVIVES" if (a0 > 0 and t0 >= 1.5) else "fails"
        print(f"{dte:>4} {len(xy):>5} {a0:>+15.1f} {t0:>+13.1f} {b:>+11.3f} {verdict:>10}")

    # context: exit-reason mix + by-year net, to read theta behavior per DTE
    print("\n--- exit-reason mix by DTE (target / stop / settle-at-close) ---")
    for dte in DTES:
        rs = [r["dte"][dte]["reason"] for r in recs if dte in r["dte"]]
        if not rs:
            continue
        tot = len(rs)
        print(f"  DTE {dte}: target={100*rs.count('target')/tot:3.0f}%  "
              f"stop={100*rs.count('stop')/tot:3.0f}%  settle={100*rs.count('settle')/tot:3.0f}%")

    print("\n--- NET $/ct by year x DTE (regime robustness) ---")
    years = sorted({r["year"] for r in recs})
    print(f"  {'year':>6} " + " ".join(f"{'DTE'+str(d):>9}" for d in DTES) + f" {'n':>5}")
    for yr in years:
        yrec = [r for r in recs if r["year"] == yr]
        cells = []
        for dte in DTES:
            n = [r["dte"][dte]["net"] for r in yrec if dte in r["dte"]]
            cells.append(f"{np.mean(n):>+9.0f}" if n else f"{'-':>9}")
        print(f"  {yr:>6} " + " ".join(cells) + f" {len(yrec):>5}")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
