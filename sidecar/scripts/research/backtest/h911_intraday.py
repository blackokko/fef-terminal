"""H9 (momentum) + H11 (mean-reversion) INTRADAY, step-3 diagnostic: does each
strategy beat INTRADAY BUY-AND-HOLD on annualized Sharpe, and what is its max
drawdown?  Reported BEFORE the full stack; dead strategies stop here.

We have NO true intraday SPY (data-subscription gap), so the underlying path is the
parity-reconstructed intraday SPX series (data.Session.spot — put/call parity forward
per minute). DEV set only (2022-01-03..2025-08-12); holdout never touched.

CONVENTIONS (stated for the record):
  * Bar = 1 ET minute of RTH (09:30..16:00, ~391 bars/session). Spot = parity forward.
  * Return series is built PER SESSION, then pooled. Headline Sharpe annualizes the
    per-SESSION return series with sqrt(252) (252 sessions/yr). Per-session is the
    honest choice: per-MINUTE Sharpe is inflated by 1-min autocorrelation and the huge
    sqrt(~98k min/yr) factor. (Per-minute Sharpe also printed, clearly labelled, as a
    sanity cross-check only.)
  * Long = +1 unit exposure to per-minute spot returns while in position. No lookahead:
    a signal observed at the close of minute i sets the position for minute i+1's
    return (fill at s[i], slip makes it adverse).
  * Slippage = 0.01% of spot (~0.5 SPX pt at 5000) charged on every entry AND every
    exit. Applied identically to the strategies AND to buy-and-hold (intraday B&H does
    one round-trip per session by construction) so the comparison is net-vs-net. Gross
    B&H also shown for reference.
  * VWAP: we have no underlying volume, so VWAP is proxied by the session running mean
    (TWAP). Documented substitution — flagged because it can move H9.

STRATEGIES (locked, long-only — intraday SPX drifts up like SPY):
  H9  momentum     : enter when spot > VWAP(proxy) AND spot > prior-30-min high;
                     exit when spot < VWAP OR session close. One position at a time.
  H11 mean-revert  : enter when trailing-20-min z-score < -1.5;
                     exit when z >= 0 (reverts to the 20-min mean) OR session close.
"""
from __future__ import annotations

import os
import pickle
import sys
import tempfile
import time

import numpy as np

from scripts.research.backtest import data, holdout

SLIP = 0.0001            # 0.01% of spot per transition (~0.5 SPX pt at 5000)
MIN_BARS = 60            # skip degenerate sessions shorter than this
CACHE = os.path.join(tempfile.gettempdir(), "fef_h911_intraday_spot_dev.pkl")


# ----------------------------------------------------------------------------- cache
def build_cache(path: str = CACHE):
    """Compute the parity spot for every RTH minute of every DEV session once and
    pickle a list of (date, ts:int64[], spot:float64[]) with nulls dropped."""
    start, end = holdout.dev_range()
    holdout.assert_dev(end)
    con = data.connect()
    dates = data.sessions(con, start, end)
    out, t0 = [], time.time()
    for n, d in enumerate(dates, 1):
        S = data.Session(con, d)
        if S.empty():
            continue
        ts, sp = [], []
        for m in S.minutes:
            s = S.spot(m)
            if s is not None and s > 0:
                ts.append(m)
                sp.append(s)
        if len(sp) >= MIN_BARS:
            out.append((d, np.asarray(ts, np.int64), np.asarray(sp, np.float64)))
        if n % 100 == 0 or n == len(dates):
            print(f"  cached {n}/{len(dates)} sessions  ({time.time()-t0:.0f}s)",
                  flush=True)
    con.close()
    with open(path, "wb") as f:
        pickle.dump(out, f)
    print(f"  wrote {len(out)} sessions -> {path}\n", flush=True)
    return out


def load_cache(path: str = CACHE):
    if not os.path.exists(path):
        print(f"building spot cache (one-time, ~2 min) -> {path}", flush=True)
        return build_cache(path)
    with open(path, "rb") as f:
        return pickle.load(f)


# ------------------------------------------------------------------- per-session sim
def _session_returns(s: np.ndarray, kind: str, slip_pt: float = SLIP):
    """Return (strat_ret[n], pos[n], n_trades) for one session's spot path `s`.

    strat_ret[i] = pos[i]*ret[i] - slip[i]; ret[i] = s[i]/s[i-1]-1, ret[0]=0.
    pos[i]==1 means exposed to minute-i return. Position for minute i+1 is decided
    from information available at minute i (no lookahead). Open positions are force-
    closed at the session's last bar (flat by close), paying one exit slip.
    `slip_pt`=0 isolates the gross signal (no friction).
    """
    n = len(s)
    ret = np.zeros(n)
    ret[1:] = s[1:] / s[:-1] - 1.0
    pos = np.zeros(n)
    slip = np.zeros(n)

    if kind == "bh":  # intraday buy-and-hold: hold first->last bar, 1 round-trip
        pos[1:] = 1.0
        slip[1] += slip_pt       # entry at open
        slip[n - 1] += slip_pt   # exit at close
        return pos * ret - slip, pos, 1

    # rolling features
    vwap = np.cumsum(s) / np.arange(1, n + 1)          # running mean (TWAP proxy)
    in_pos, ntr = False, 0
    for i in range(n - 1):
        if kind == "h9":
            if i < 30:
                enter = False
            else:
                enter = (s[i] > vwap[i]) and (s[i] > s[i - 30:i].max())
            exit_ = s[i] < vwap[i]
        elif kind == "h11":
            if i < 19:
                enter = exit_ = False
            else:
                w = s[i - 19:i + 1]
                sd = w.std()
                z = (s[i] - w.mean()) / sd if sd > 0 else 0.0
                enter = z < -1.5
                exit_ = z >= 0.0
        else:
            raise ValueError(kind)

        if not in_pos:
            if enter:
                in_pos = True
                ntr += 1
                pos[i + 1] = 1.0
                slip[i + 1] += slip_pt
        else:
            if exit_:
                in_pos = False
                slip[i + 1] += slip_pt
            else:
                pos[i + 1] = 1.0
    if in_pos:                    # force flat at close
        slip[n - 1] += slip_pt
    return pos * ret - slip, pos, ntr


def run(sessions, kind: str, slip_pt: float = SLIP):
    """Aggregate one strategy across all sessions -> per-session + per-minute series."""
    sess_ret, minute_ret, in_mkt_min, tot_min, ntr = [], [], 0, 0, 0
    for _, _, s in sessions:
        r, pos, t = _session_returns(s, kind, slip_pt)
        sess_ret.append(float(np.prod(1.0 + r) - 1.0))   # compounded intraday return
        minute_ret.append(r)
        in_mkt_min += int(pos.sum())
        tot_min += len(s)
        ntr += t
    return {
        "sess": np.asarray(sess_ret),
        "minute": np.concatenate(minute_ret),
        "tim": in_mkt_min / tot_min,
        "trades": ntr,
        "n_sess": len(sess_ret),
    }


# ------------------------------------------------------------------------- reporting
def perf(sess_ret: np.ndarray):
    a = sess_ret[~np.isnan(sess_ret)]
    if len(a) < 2 or a.std() == 0:
        return None
    eq = np.cumprod(1.0 + a)
    dd = eq / np.maximum.accumulate(eq) - 1.0
    return {
        "sharpe": a.mean() / a.std() * np.sqrt(252),   # per-session, sqrt(252)
        "tot": eq[-1] - 1.0,
        "maxdd": dd.min(),
        "mean_bps": a.mean() * 1e4,
    }


def minute_sharpe(minute_ret: np.ndarray):
    a = minute_ret[minute_ret != 0.0]      # exclude flat minutes (no exposure)
    if len(a) < 2 or a.std() == 0:
        return float("nan")
    return a.mean() / a.std() * np.sqrt(252 * 391)      # ~391 RTH min/session


def show(tag, res, p, bh_sharpe=None):
    if not p:
        print(f"  {tag:16s} (n/a)")
        return
    beat = ""
    if bh_sharpe is not None:
        beat = "  >> BEATS B&H" if p["sharpe"] >= bh_sharpe else "  -- loses to B&H"
    print(f"  {tag:16s} Sharpe={p['sharpe']:+6.2f}  maxDD={100*p['maxdd']:+6.1f}%  "
          f"time-in-mkt={100*res['tim']:4.0f}%  totRet={100*p['tot']:+7.1f}%  "
          f"trades={res['trades']:5d}{beat}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sess = load_cache()
    n_sess = len(sess)
    bars = sum(len(s) for _, _, s in sess)
    print(f"H9/H11 INTRADAY step-3 — DEV {sess[0][0]}..{sess[-1][0]}  "
          f"({n_sess} sessions, {bars:,} minute-bars, parity-SPX spot)\n")
    print("Convention: per-session returns, Sharpe annualized sqrt(252); slippage "
          "0.01%/transition on strategies AND B&H; VWAP=running-mean (TWAP) proxy "
          "[no volume].\n")

    bh = run(sess, "bh")
    h9 = run(sess, "h9")
    h11 = run(sess, "h11")
    p_bh, p_h9, p_h11 = perf(bh["sess"]), perf(h9["sess"]), perf(h11["sess"])

    # gross (no-slip) variants -> isolate signal from friction
    g_bh, g_h9, g_h11 = (run(sess, k, 0.0) for k in ("bh", "h9", "h11"))
    pg_bh, pg_h9, pg_h11 = perf(g_bh["sess"]), perf(g_h9["sess"]), perf(g_h11["sess"])

    print("--- INTRADAY BUY-AND-HOLD (the bar to beat) ---")
    show("B&H (net)", bh, p_bh)
    if pg_bh:
        print(f"  {'B&H (gross)':16s} Sharpe={pg_bh['sharpe']:+6.2f}  "
              f"maxDD={100*pg_bh['maxdd']:+6.1f}%  time-in-mkt= 100%  "
              f"totRet={100*pg_bh['tot']:+7.1f}%   (no-slip reference)")
    print("\n--- H9 MOMENTUM (>VWAP & breaks 30-min high) ---")
    show("H9 momentum", h9, p_h9, p_bh["sharpe"])
    print("\n--- H11 MEAN-REVERSION (z(20-min) < -1.5, dip buyer) ---")
    show("H11 mean-rev", h11, p_h11, p_bh["sharpe"])

    print("\n--- GROSS vs NET (isolate signal from friction) ---")
    print(f"  {'strategy':14s}{'gross Sharpe':>13s}{'gross totRet':>14s}"
          f"{'net totRet':>12s}{'friction drag':>15s}{'trades/sess':>13s}")
    for tag, pn, pg, res in [("B&H", p_bh, pg_bh, bh), ("H9 mom", p_h9, pg_h9, h9),
                             ("H11 meanrev", p_h11, pg_h11, h11)]:
        if pn and pg:
            print(f"  {tag:14s}{pg['sharpe']:+13.2f}{100*pg['tot']:+13.1f}%"
                  f"{100*pn['tot']:+11.1f}%{100*(pg['tot']-pn['tot']):+14.1f}%"
                  f"{res['trades']/res['n_sess']:13.1f}")

    print("\n--- per-minute Sharpe cross-check (sqrt(252*391); inflated, sanity only) ---")
    print(f"  B&H  {minute_sharpe(bh['minute']):+6.2f}   "
          f"H9  {minute_sharpe(h9['minute']):+6.2f}   "
          f"H11 {minute_sharpe(h11['minute']):+6.2f}")

    # ---- headline summary table ----
    print("\n================= STEP-3 GATE: SHARPE vs BUY-AND-HOLD =================")
    print(f"  {'strategy':14s}{'Sharpe':>9s}{'maxDD':>10s}{'time-in-mkt':>13s}"
          f"{'totRet':>10s}{'beats B&H?':>12s}")
    rows = [("B&H (net)", p_bh, bh, None),
            ("H9 mom", p_h9, h9, p_bh["sharpe"]),
            ("H11 meanrev", p_h11, h11, p_bh["sharpe"])]
    for tag, p, res, ref in rows:
        if not p:
            continue
        verdict = "-" if ref is None else ("YES" if p["sharpe"] >= ref else "NO")
        print(f"  {tag:14s}{p['sharpe']:+9.2f}{100*p['maxdd']:+9.1f}%"
              f"{100*res['tim']:11.0f}% {100*p['tot']:+9.1f}%{verdict:>12s}")
    print("======================================================================")


if __name__ == "__main__":
    main()
