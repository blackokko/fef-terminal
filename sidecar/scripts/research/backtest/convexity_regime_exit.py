"""CONVEXITY x REGIME x EXIT — does the flow-independent trend-breakout ENTRY, which is a
points-null (session-clustered t~0, price==flow), turn DOLLAR-POSITIVE when scored in REAL
0DTE OPTION P&L, regime-gated by VIX, with an excursion-capturing scale-out?   (READ-ONLY)

*** LOOKAHEAD FIX (default since methodology audit): the SPX historical_bar 1-min bars are
START-stamped, so a breakout confirmed by bar[i].CLOSE happens at wall-clock ~t0+60s (verified:
median|SPXclose[t]-parity[t+60s]|=0.29pt << |...-parity[t]|=0.90pt). The option is therefore
FILLED at the first quote at/after t0+FILL_LAG_MS (the bar close), and the +30min hold / scale-out
are anchored there — NOT at t0 (which would buy ~3pt/~$150 of free favorable move = a +1min
lookahead that faked a +$170/ct "edge"). Set FILL_LAG_MS=0 to reproduce the old (lookahead) run;
convexity_lag.py reports both side-by-side. The (a) SPX-points null is close-to-close and
fill-timing-independent, so it stays anchored at t0.  triggers-on-bar-close=YES  fills-same-minute=NO
(fixed)  had-lookahead=YES→FIXED.

Score the SAME entry (trend_breakout_entry, orh M15 V5 primary) FOUR ways:
  (a) SPX points  @ +30min                         -> reproduce the null baseline (~0, clustered)
  (b) option-$    @ +30min hold, ungated           -> isolates CONVEXITY (option-$ vs points, same horizon/entries)
  (c) option-$    @ +30min hold, VIX-gated         -> isolates REGIME (VIX gate vs ungated)
  (d) option-$    managed scale-out, VIX-gated     -> isolates EXIT (bank excursion + trail vs fixed hold)

Entry signal: reuse scripts.research.backtest.trend_breakout_entry primitives verbatim
  (breakout > trailing-M-min high AND vel_3min>=V AND 2 up bars; orh filter = above OR high;
   long & short symmetric; 10-min per-dir cooldown; entries 09:50-15:30 ET).  Price ruler =
  historical_bar SPX 1-min (VERIFIED byte-identical to the phase_cache IBKR ruler, so this
  reproduces the null exactly) — durable across DEV and the untouched HOLDOUT.

Option $ source: option_quote_history (ThetaData SPXW, 1-min NBBO, one 0DTE expiry/session,
  391 quoted minutes 09:30-16:00, 40 strikes @5pt).  BUY at ASK, SELL at BID (real spread in).
  Strike rules (both reported): (i) nearest-ATM, (ii) ~20pt OTM (~0.30-0.35 delta, user style).
  Right = C (long) / P (short).  Skip a trade if its strike/minute quote is missing or bad.

VIX regime gate (daily prior-close from historical_bar VIX 1-day; causal SOD value): ladder
  none / >=18 / >=22 / >=28 / >=40.

Sets:  DEV 2022-01-03..2025-08-12  (form the hypothesis).   HOLDOUT 2025-08-13..2026-06-26
  (true OOS — scored separately, reported explicitly).  Metrics per scoring/gate: mean & median
  net option-$ per contract, win%, tail (%ret>+100/+200), max drawdown, by-year sign, and
  SESSION-CLUSTERED t (sessions = the unit).  Engine/live code untouched.

Run:  .venv/Scripts/python.exe -m scripts.research.backtest.convexity_regime_exit
Outputs CSVs + this report to the scratchpad.
"""
from __future__ import annotations

import bisect
import datetime as dt
import os
import sqlite3
import sys
from zoneinfo import ZoneInfo

import numpy as np

from . import trend_breakout_entry as TB

sys.stdout.reconfigure(encoding="utf-8")
ET = ZoneInfo("America/New_York")
DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
SCRATCH = (r"C:\Users\diant\AppData\Local\Temp\claude\C--Users-diant"
           r"\13885fae-da20-4300-a83a-94697ac91ce4\scratchpad")

# ---- sets --------------------------------------------------------------------------
DEV_START, DEV_END = "2022-01-03", "2025-08-12"
HOLD_START, HOLD_END = "2025-08-13", "2026-06-26"

# ---- entry config (trend_breakout documented best) ---------------------------------
CFG = dict(mode="orh", M=15, V=5.0, span=TB.DEF_SPAN, N=TB.DEF_N, **{"or": TB.DEF_OR})

# ---- option / exit constants -------------------------------------------------------
MULT = 100.0                    # SPX option multiplier ($ per premium point per contract)
BAD_Q = {"crossed", "locked", "zero"}
MIN_ASK = 0.10                  # drop degenerate near-zero entries (report skips)
OTM_PT = 20.0                   # OTM strike offset (pt) for the ~0.30-0.35 delta rule
MATCH_TOL = 90_000              # ms tolerance matching entry minute to an option quote
HOLD_MS = 30 * 60_000           # fixed comparator horizon (+30 min) — same as the points null
FILL_LAG_MS = 60_000            # NO-LOOKAHEAD default: fill at the bar CLOSE (t0+60s), not t0.
                                # Bars are START-stamped (proof in module docstring). Set 0 to
                                # reproduce the old +1-min-lookahead run.
# scale-out (d)
SO_R1, SO_R2 = 1.5, 2.0         # sell 1/3 at +50%, 1/3 at +100%
SO_GIVE = 0.35                  # trail: give back 35% from peak bid (peak init = entry ask)
SO_CAP_H = 15.75                # hard time cap 15:45 ET (never hold 0DTE to close)
# bracket fixed exit (secondary robustness): +100% target / -60% stop
BR_TGT, BR_STOP = 2.0, 0.40

VIX_RUNGS = [("none", 0.0), (">=18", 18.0), (">=22", 22.0), (">=28", 28.0), (">=40", 40.0)]
YEARS = ["2022", "2023", "2024", "2025", "2026"]


def et_h(ms: int) -> float:
    x = dt.datetime.fromtimestamp(ms / 1000, ET)
    return x.hour + x.minute / 60.0


def et_date(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, ET).strftime("%Y-%m-%d")


# ============================================================ data loaders
def load_spx_rulers(con):
    """{date: (rt[int64], rp[float])} RTH 1-min SPX closes, glitch-filtered (median +/-5%),
    for the full history — identical source to the phase_cache IBKR ruler (verified)."""
    rows = con.execute(
        "SELECT timestamp_ms, close FROM historical_bar "
        "WHERE symbol='SPX' AND bar_size='1 min' ORDER BY timestamp_ms").fetchall()
    byd: dict[str, list] = {}
    for t, px in rows:
        byd.setdefault(et_date(t), []).append((t, px))
    out = {}
    for d, lst in byd.items():
        lst.sort()
        rt = np.array([r[0] for r in lst], np.int64)
        rp = np.array([r[1] for r in lst], float)
        keep = np.array([TB._et_h(int(t)) >= 9.5 and TB._et_h(int(t)) <= 16.0 for t in rt])
        rt, rp = rt[keep], rp[keep]
        if len(rt) < 30:
            continue
        med = np.median(rp)
        mask = np.abs(rp - med) <= 0.05 * med
        rt, rp = rt[mask], rp[mask]
        if len(rt) >= 30:
            out[d] = (rt, rp)
    return out


def load_vix_priorclose(con):
    """{session_date: prior-trading-day VIX close}. Causal SOD regime value."""
    rows = con.execute(
        "SELECT timestamp_ms, close FROM historical_bar "
        "WHERE symbol='VIX' AND bar_size='1 day' ORDER BY timestamp_ms").fetchall()
    dates = [et_date(t) for t, _ in rows]
    closes = [c for _, c in rows]

    def prior(d):
        j = bisect.bisect_left(dates, d)          # first index with date >= d
        return closes[j - 1] if j - 1 >= 0 else None
    return dates, closes, prior


def load_option_session(con, d: str):
    """Per-(strike,right) sorted NBBO arrays for the session's single 0DTE expiry.
    Returns (by_kr, strikes_sorted) or (None, None) if sparse."""
    rows = con.execute(
        "SELECT timestamp_ms,strike,right,bid,ask,quote_quality "
        "FROM option_quote_history WHERE session_date=? ORDER BY timestamp_ms", (d,)).fetchall()
    if len(rows) < 200:
        return None, None
    tmp: dict[tuple, list] = {}
    strikes = set()
    for t, k, r, bid, ask, qual in rows:
        tmp.setdefault((float(k), r), []).append((int(t), bid, ask, qual))
        strikes.add(float(k))
    by_kr = {}
    for kr, lst in tmp.items():
        lst.sort()
        by_kr[kr] = (np.array([x[0] for x in lst], np.int64),
                     np.array([x[1] for x in lst], float),
                     np.array([x[2] for x in lst], float),
                     [x[3] for x in lst])
    return by_kr, np.array(sorted(strikes), float)


# ============================================================ entry precompute (price-only)
def precompute_price(date: str, year: str, rt: np.ndarray, rp: np.ndarray) -> dict:
    """Replicates trend_breakout_entry.precompute's PRICE features (no flow samples needed —
    entries_for_config uses only price-derived arrays). Guarantees identical breakout entries."""
    n = len(rp)
    vel = np.full(n, np.nan)
    vel[TB.VEL_WIN:] = rp[TB.VEL_WIN:] - rp[:-TB.VEL_WIN]
    up2 = np.zeros(n, bool); dn2 = np.zeros(n, bool)
    up2[2:] = (rp[2:] > rp[1:-1]) & (rp[1:-1] > rp[:-2])
    dn2[2:] = (rp[2:] < rp[1:-1]) & (rp[1:-1] < rp[:-2])
    feat = dict(date=date, year=year, rt=rt, rp=rp, n=n,
                drift=np.zeros(n, int), aggr=np.zeros(n, int),
                vel=vel, up2=up2, dn2=dn2, fwd={})
    for m in TB.GRID_M:
        feat[f"pmax{m}"] = TB._roll_prior_max(rp, m)
        feat[f"pmin{m}"] = TB._roll_prior_min(rp, m)
    for span in (15, 20, 30):
        e = TB._ema(rp, span)
        er = np.zeros(n, bool); ef = np.zeros(n, bool)
        er[3:] = (e[3:] > e[:-3]) & (rp[3:] > e[3:])
        ef[3:] = (e[3:] < e[:-3]) & (rp[3:] < e[3:])
        feat[f"emaR{span}"] = er; feat[f"emaF{span}"] = ef
    for nn in (8, 10, 15):
        cmin = TB._roll_win_min(rp, nn); cmax = TB._roll_win_max(rp, nn)
        hl = np.zeros(n, bool); lh = np.zeros(n, bool)
        hl[2 * nn - 1:] = cmin[2 * nn - 1:] > cmin[nn - 1:n - nn]
        lh[2 * nn - 1:] = cmax[2 * nn - 1:] < cmax[nn - 1:n - nn]
        feat[f"hl{nn}"] = hl; feat[f"lh{nn}"] = lh
    for orm in (15, 30):
        seg = rp[:orm]
        feat[f"orh{orm}"] = float(seg.max()) if len(seg) >= orm // 2 else np.inf
        feat[f"orl{orm}"] = float(seg.min()) if len(seg) >= orm // 2 else -np.inf
    rng = float(rp.max() - rp.min()); net = float(rp[-1] - rp[0])
    feat["rng"] = rng; feat["er"] = abs(net) / (rng + 1e-9)
    feat["_win"] = TB._entry_window_mask(rt)
    return feat


# ============================================================ option fill / exits
def _nearest_idx(ts_arr: np.ndarray, target: int, tol: int = MATCH_TOL):
    j = int(np.searchsorted(ts_arr, target))
    best, bd = None, tol + 1
    for c in (j - 1, j):
        if 0 <= c < len(ts_arr):
            dd = abs(int(ts_arr[c]) - target)
            if dd <= tol and dd < bd:
                bd, best = dd, c
    return best


def _valid(bid, ask, qual):
    return (qual not in BAD_Q and ask is not None and bid is not None
            and ask > 0 and bid >= 0 and ask >= bid)


def exit_hold(ts, bd, qq, j0, a0, t0, horizon):
    """Pure hold: sell at BID at the first minute >= t0+horizon (or last available bid)."""
    tcap = t0 + horizon
    last = None
    peak = a0
    for j in range(j0 + 1, len(ts)):
        if qq[j] in BAD_Q:
            continue
        b = bd[j]
        if b < 0:
            continue
        peak = max(peak, b)
        last = b
        if ts[j] >= tcap:
            return b, peak
    return last, peak            # session ended before horizon (rare)


def exit_bracket(ts, bd, qq, j0, a0, t0, tgt, stop, horizon):
    """+tgt% target / -stop% stop / else +horizon time, on the 1-min BID snapshots."""
    tcap = t0 + horizon
    last = None
    for j in range(j0 + 1, len(ts)):
        if qq[j] in BAD_Q:
            continue
        b = bd[j]
        if b < 0:
            continue
        last = b
        if b <= stop * a0:
            return b, "stop"
        if b >= tgt * a0:
            return b, "target"
        if ts[j] >= tcap:
            return b, "time"
    return last, "eod"


def exit_scaleout(ts, bd, qq, j0, a0, t0, r1, r2, give, cap_h, stop0=0.5):
    """Managed RATCHET scale-out (bank the excursion, ride a runner):
      - initial hard stop at stop0*a0 (-50%) on the whole position;
      - sell 1/3 when bid>=+50%, then raise the stop to breakeven (a0) on the remainder;
      - sell 1/3 when bid>=+100%, then TRAIL the runner give% (35%) off the running peak;
      - hard time cap cap_h ET (never hold 0DTE to close).
    Returns the blended exit price over the three thirds (direction already in strike/right)."""
    sold = [None, None, None]
    peak = a0
    stop = stop0 * a0                                    # ratcheting stop level
    for j in range(j0 + 1, len(ts)):
        if qq[j] in BAD_Q:
            continue
        b = bd[j]
        if b < 0:
            continue
        if et_h(int(ts[j])) > cap_h:                    # time cap: dump remaining at this bid
            for k in range(3):
                if sold[k] is None:
                    sold[k] = b
            break
        peak = max(peak, b)
        if b <= stop:                                   # stop/trail hit -> sell all remaining
            for k in range(3):
                if sold[k] is None:
                    sold[k] = b
            break
        if sold[0] is None and b >= r1 * a0:            # +50% rung -> bank 1/3, stop to breakeven
            sold[0] = b
            stop = max(stop, a0)
        if sold[1] is None and b >= r2 * a0:            # +100% rung -> bank 1/3, begin trailing
            sold[1] = b
        if sold[1] is not None:                         # runner active: trail give% off peak
            stop = max(stop, peak * (1.0 - give))
    # any still unsold (ran out of quotes): sell at last valid bid
    last = None
    for j in range(len(ts) - 1, j0, -1):
        if qq[j] not in BAD_Q and bd[j] >= 0:
            last = bd[j]; break
    for k in range(3):
        if sold[k] is None:
            sold[k] = last if last is not None else a0
    return (sold[0] + sold[1] + sold[2]) / 3.0


# ============================================================ build all trades
def build_trades(spx, vixprior, con, sessions, setname):
    """One pass: for every session build entries and price+option outcomes (both strike rules)."""
    trades = []
    sk = dict(nostrike=0, badquote=0, minask=0, ok=0)
    nsess_entries = 0
    for si, d in enumerate(sessions):
        if d not in spx:
            continue
        rt, rp = spx[d]
        feat = precompute_price(d, d[:4], rt, rp)
        ents = TB.entries_for_config(feat, CFG)
        if not ents:
            continue
        nsess_entries += 1
        vix = vixprior(d)
        by_kr, strikes = load_option_session(con, d)
        for i, dr in ents:
            t0 = int(rt[i]); s0 = float(rp[i])
            if not (TB.SANE_LO <= s0 <= TB.SANE_HI):
                continue
            # NO-LOOKAHEAD: the breakout is confirmed by bar[i].CLOSE (~wall-clock t0+60s on the
            # start-stamped ruler), so the option is filled at the first quote at/after t0+FILL_LAG_MS
            # and the hold/scale-out anchor there. Strike is centered on the confirmed fill-time spot.
            tf = t0 + FILL_LAG_MS
            s_fill = TB.real_at(rt, rp, tf)
            # (a) SPX points @ +30 — price-only null; close-to-close, fill-timing-independent (anchor t0)
            sH = TB.real_at(rt, rp, t0 + HOLD_MS)
            pts = None
            if sH is not None and abs(sH - s0) <= TB.MOVE_GLITCH_FRAC * s0:
                pts = dr * (sH - s0)
            right = "C" if dr > 0 else "P"
            for rule in ("ATM", "OTM"):
                rec = dict(set=setname, date=d, year=d[:4], t0=t0, dir=dr, spot=s0,
                           vix=vix, rule=rule, pts=pts, right=right,
                           opt_hold=None, ret_hold=None, opt_br=None, opt_so=None,
                           ret_so=None, mfe=None, strike=None, a0=None)
                if by_kr is None or strikes is None or len(strikes) == 0 or s_fill is None:
                    sk["nostrike"] += 1; trades.append(rec); continue
                target = s_fill + (0.0 if rule == "ATM" else dr * OTM_PT)
                k = float(strikes[int(np.argmin(np.abs(strikes - target)))])
                arr = by_kr.get((k, right))
                if arr is None:
                    sk["nostrike"] += 1; trades.append(rec); continue
                ts, bd, ak, qq = arr
                j0 = _nearest_idx(ts, tf)                 # fill at the bar CLOSE (t0+60s), not t0
                if j0 is None or not _valid(bd[j0], ak[j0], qq[j0]):
                    sk["badquote"] += 1; trades.append(rec); continue
                a0 = float(ak[j0])
                if a0 < MIN_ASK:
                    sk["minask"] += 1; trades.append(rec); continue
                rec["strike"] = k; rec["a0"] = a0
                xh, peak = exit_hold(ts, bd, qq, j0, a0, tf, HOLD_MS)
                if xh is not None:
                    rec["opt_hold"] = (xh - a0) * MULT
                    rec["ret_hold"] = xh / a0 - 1.0
                    rec["mfe"] = peak / a0 - 1.0
                xb, _ = exit_bracket(ts, bd, qq, j0, a0, tf, BR_TGT, BR_STOP, HOLD_MS)
                if xb is not None:
                    rec["opt_br"] = (xb - a0) * MULT
                xs = exit_scaleout(ts, bd, qq, j0, a0, tf, SO_R1, SO_R2, SO_GIVE, SO_CAP_H)
                rec["opt_so"] = (xs - a0) * MULT
                rec["ret_so"] = xs / a0 - 1.0
                sk["ok"] += 1
                trades.append(rec)
        if (si + 1) % 150 == 0:
            print(f"    [{setname}] {si+1}/{len(sessions)} sessions", flush=True)
    print(f"  [{setname}] sessions-with-entries={nsess_entries}  option fills: ok={sk['ok']} "
          f"nostrike={sk['nostrike']} badquote={sk['badquote']} minask={sk['minask']}")
    return trades, sk


# ============================================================ stats
def clustered_t(recs, key):
    by = {}
    for r in recs:
        if r.get(key) is not None:
            by.setdefault(r["date"], []).append(r[key])
    means = [float(np.mean(v)) for v in by.values()]
    a = np.array(means, float)
    if len(a) < 2:
        return float("nan"), len(a)
    se = a.std(ddof=1) / np.sqrt(len(a))
    return (float(a.mean() / se) if se > 0 else float("nan")), len(a)


def maxdd(recs, key):
    rr = sorted([r for r in recs if r.get(key) is not None], key=lambda r: r["t0"])
    if not rr:
        return 0.0
    cum = np.cumsum([r[key] for r in rr])
    peak = np.maximum.accumulate(cum)
    return float((peak - cum).max())


def summ(recs, key, retkey=None):
    """Summary for a $-or-points value key over recs. None if empty."""
    rr = [r for r in recs if r.get(key) is not None]
    if not rr:
        return None
    a = np.array([r[key] for r in rr], float)
    tcl, nsess = clustered_t(rr, key)
    out = dict(n=len(rr), nsess=nsess, mean=float(a.mean()), med=float(np.median(a)),
               win=100.0 * np.mean(a > 0), tcl=tcl, dd=maxdd(rr, key))
    if retkey:
        rv = np.array([r[retkey] for r in rr if r.get(retkey) is not None], float)
        out["t100"] = 100.0 * np.mean(rv > 1.0) if len(rv) else float("nan")
        out["t200"] = 100.0 * np.mean(rv > 2.0) if len(rv) else float("nan")
    # by-year sign
    ys = {}
    for y in YEARS:
        ay = np.array([r[key] for r in rr if r["year"] == y], float)
        ys[y] = float(ay.mean()) if len(ay) else None
    out["ys"] = ys
    return out


def yr_sign(ys):
    return "".join(("+" if (ys[y] is not None and ys[y] > 0) else ("-" if ys[y] is not None else ".")) for y in YEARS)


# ============================================================ reporting
def four_way(trades, setname, rule, gate_thr, gate_lbl):
    """Print the 4-way table for one set/rule at the chosen VIX gate."""
    # restrict to the OPTION-FILLED universe so (a)points and (b)option-$ are the SAME trades
    rr = [t for t in trades if t["set"] == setname and t["rule"] == rule and t["a0"] is not None]
    gated = [t for t in rr if t["vix"] is not None and t["vix"] >= gate_thr]
    print(f"\n  [{setname}] strike={rule}   gate for (c)/(d): VIX {gate_lbl}  "
          f"(option-filled ungated n={len(rr)}, gated n={len(gated)})")
    print(f"    {'scoring':34s} {'n':>4s} {'sess':>4s} {'mean$':>8s} {'med$':>7s} {'win%':>5s} "
          f"{'>100%':>5s} {'>200%':>5s} {'maxDD$':>8s} {'t_clus':>6s}  {'yr(22-26)':>9s}")
    a = summ(rr, "pts")           # (a) points — ungated, option-filled universe
    if a:
        print(f"    {'(a) SPX points @+30 [null]':34s} {a['n']:4d} {a['nsess']:4d} {a['mean']:+8.2f} "
              f"{a['med']:+7.2f} {a['win']:4.0f}% {'--':>5s} {'--':>5s} {a['dd']:8.1f} {a['tcl']:+6.2f}  {yr_sign(a['ys'])}")
    b = summ(rr, "opt_hold", "ret_hold")   # (b) option-$ hold ungated
    if b:
        print(f"    {'(b) option-$ +30hold UNGATED':34s} {b['n']:4d} {b['nsess']:4d} {b['mean']:+8.1f} "
              f"{b['med']:+7.1f} {b['win']:4.0f}% {b['t100']:4.0f}% {b['t200']:4.0f}% {b['dd']:8.0f} {b['tcl']:+6.2f}  {yr_sign(b['ys'])}")
    c = summ(gated, "opt_hold", "ret_hold")  # (c) option-$ hold gated
    if c:
        print(f"    {'(c) option-$ +30hold VIX-GATED':34s} {c['n']:4d} {c['nsess']:4d} {c['mean']:+8.1f} "
              f"{c['med']:+7.1f} {c['win']:4.0f}% {c['t100']:4.0f}% {c['t200']:4.0f}% {c['dd']:8.0f} {c['tcl']:+6.2f}  {yr_sign(c['ys'])}")
    dd = summ(gated, "opt_so", "ret_so")     # (d) scale-out gated
    if dd:
        print(f"    {'(d) option-$ SCALE-OUT VIX-GATED':34s} {dd['n']:4d} {dd['nsess']:4d} {dd['mean']:+8.1f} "
              f"{dd['med']:+7.1f} {dd['win']:4.0f}% {dd['t100']:4.0f}% {dd['t200']:4.0f}% {dd['dd']:8.0f} {dd['tcl']:+6.2f}  {yr_sign(dd['ys'])}")
    return dict(a=a, b=b, c=c, d=dd)


def vix_ladder(trades, setname, rule, valkey, retkey, label):
    rr = [t for t in trades if t["set"] == setname and t["rule"] == rule and t["a0"] is not None]
    print(f"\n  [{setname}] strike={rule}  VIX ladder — {label}")
    print(f"    {'gate':7s} {'n':>4s} {'sess':>4s} {'mean$':>8s} {'med$':>7s} {'win%':>5s} "
          f"{'>100%':>5s} {'>200%':>5s} {'maxDD$':>8s} {'t_clus':>6s}  {'yr(22-26)':>9s}")
    for lbl, thr in VIX_RUNGS:
        g = [t for t in rr if t["vix"] is not None and t["vix"] >= thr]
        s = summ(g, valkey, retkey)
        if s is None:
            print(f"    {lbl:7s} {'--':>4s}")
            continue
        print(f"    {lbl:7s} {s['n']:4d} {s['nsess']:4d} {s['mean']:+8.1f} {s['med']:+7.1f} "
              f"{s['win']:4.0f}% {s['t100']:4.0f}% {s['t200']:4.0f}% {s['dd']:8.0f} {s['tcl']:+6.2f}  {yr_sign(s['ys'])}")


def choose_gate(trades, rule):
    """On DEV only: pick the VIX rung maximizing (d) scale-out mean$ with clustered t>=1 and
    adequate size (n>=100, sess>=40). Fallback to 'none'."""
    rr = [t for t in trades if t["set"] == "DEV" and t["rule"] == rule and t["a0"] is not None]
    best = ("none", 0.0, None)
    for lbl, thr in VIX_RUNGS:
        g = [t for t in rr if t["vix"] is not None and t["vix"] >= thr]
        s = summ(g, "opt_so", "ret_so")
        if s and s["n"] >= 100 and s["nsess"] >= 40 and s["tcl"] >= 1.0:
            if best[2] is None or s["mean"] > best[2]["mean"]:
                best = (lbl, thr, s)
    return best


# ============================================================ main
def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    print("=" * 120)
    print("CONVEXITY x REGIME x EXIT — trend-breakout entry (orh M15 V5) scored 4 ways in real 0DTE option $")
    print("=" * 120)
    print("loading SPX 1-min rulers + VIX prior-close ...", flush=True)
    spx = load_spx_rulers(con)
    _, _, vixprior = load_vix_priorclose(con)
    print(f"  SPX ruler sessions: {len(spx)}")

    dev_sess = sorted([d for d in spx if DEV_START <= d <= DEV_END])
    hold_sess = sorted([d for d in spx if HOLD_START <= d <= HOLD_END])
    print(f"  DEV sessions: {len(dev_sess)}  HOLDOUT sessions: {len(hold_sess)}")
    print(f"  entry cfg: {CFG}")
    print(f"  fills: buy ASK / sell BID at t0+{FILL_LAG_MS//1000}s (NO-LOOKAHEAD bar-close fill); "
          f"strike rules ATM & {OTM_PT:.0f}pt-OTM; MIN_ASK={MIN_ASK}; "
          f"hold=+30min; scale-out 1/3@+50% 1/3@+100% trail{int(SO_GIVE*100)}% cap15:45\n")

    print("BUILDING DEV trades ...", flush=True)
    dev_tr, dev_sk = build_trades(spx, vixprior, con, dev_sess, "DEV")
    print("BUILDING HOLDOUT trades ...", flush=True)
    hold_tr, hold_sk = build_trades(spx, vixprior, con, hold_sess, "HOLDOUT")
    trades = dev_tr + hold_tr

    # sample quote magnitude sanity
    samp = [t for t in dev_tr if t["a0"] is not None][:3]
    print("\n  sample fills (sanity):")
    for t in samp:
        print(f"    {t['date']} dir={t['dir']:+d} {t['rule']} strike={t['strike']} a0={t['a0']:.2f} "
              f"pts={t['pts']} hold$={t['opt_hold']:+.0f} so$={t['opt_so']:+.0f} mfe={t['mfe']:+.2f}")

    # SPX-POINTS NULL over ALL entries (one row per entry; ATM/OTM identical for points) — sanity
    print("\n  SPX-points null over ALL entries (the established baseline; should be ~0, t~0):")
    for setname in ("DEV", "HOLDOUT"):
        base = [t for t in trades if t["set"] == setname and t["rule"] == "ATM"]
        s = summ(base, "pts")
        if s:
            print(f"    [{setname}] n={s['n']} sess={s['nsess']}  mean {s['mean']:+.3f}pt  med {s['med']:+.3f}pt  "
                  f"win {s['win']:.1f}%  t_clustered {s['tcl']:+.2f}   yr(22-26) {yr_sign(s['ys'])}")

    # ---- VIX ladders (DEV then HOLDOUT), for the fixed-hold (c) and scale-out (d) ----
    for rule in ("ATM", "OTM"):
        print("\n" + "=" * 120)
        print(f"VIX REGIME LADDER — strike rule = {rule}")
        print("=" * 120)
        vix_ladder(trades, "DEV", rule, "opt_hold", "ret_hold", "(c) fixed +30min hold, option-$/contract")
        vix_ladder(trades, "DEV", rule, "opt_so", "ret_so", "(d) SCALE-OUT, option-$/contract")
        vix_ladder(trades, "HOLDOUT", rule, "opt_hold", "ret_hold", "(c) fixed +30min hold, option-$/contract")
        vix_ladder(trades, "HOLDOUT", rule, "opt_so", "ret_so", "(d) SCALE-OUT, option-$/contract")

    # ---- choose gate on DEV, apply to HOLDOUT; print 4-way for both sets ----
    print("\n" + "=" * 120)
    print("FOUR-WAY COMPARISON  (gate chosen on DEV by max (d) mean$ with t>=1, n>=100, sess>=40; applied to HOLDOUT)")
    print("=" * 120)
    results = {}
    for rule in ("ATM", "OTM"):
        glbl, gthr, _ = choose_gate(trades, rule)
        results[rule] = dict(gate=glbl, thr=gthr)
        results[rule]["DEV"] = four_way(trades, "DEV", rule, gthr, glbl)
        results[rule]["HOLDOUT"] = four_way(trades, "HOLDOUT", rule, gthr, glbl)
        # also always show the 'none' gate holdout 4-way for reference
        if glbl != "none":
            print("    -- reference: same but gate=none --")
            four_way(trades, "HOLDOUT", rule, 0.0, "none")

    # ---- decomposition: convexity vs regime vs exit (DEV, ATM, chosen gate) ----
    print("\n" + "=" * 120)
    print("DECOMPOSITION — where does any edge come from? (mean $/contract per trade)")
    print("=" * 120)
    for rule in ("ATM", "OTM"):
        gthr = results[rule]["thr"]; glbl = results[rule]["gate"]
        for setname in ("DEV", "HOLDOUT"):
            rr = [t for t in trades if t["set"] == setname and t["rule"] == rule]
            g = [t for t in rr if t["vix"] is not None and t["vix"] >= gthr]
            b = summ(rr, "opt_hold"); c = summ(g, "opt_hold"); dd = summ(g, "opt_so")
            bo = summ(rr, "opt_so")   # scale-out ungated (exit effect w/o regime)
            def m(x): return f"{x['mean']:+7.1f}(t{x['tcl']:+.1f})" if x else "   --   "
            print(f"  [{setname}] {rule} gate={glbl}:  "
                  f"(b)hold-ungated {m(b)} | (c)hold-gated {m(c)} | scaleout-ungated {m(bo)} | (d)scaleout-gated {m(dd)}")
        print(f"     -> CONVEXITY = (b) vs points;  REGIME = (c)-(b);  EXIT = (d)-(c).")

    # ---- CSVs ----
    import csv
    path = os.path.join(SCRATCH, "convexity_trades.csv")
    cols = ["set", "date", "year", "t0", "dir", "right", "rule", "spot", "strike", "a0",
            "vix", "pts", "opt_hold", "ret_hold", "opt_br", "opt_so", "ret_so", "mfe"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            w.writerow(t)
    print(f"\n  per-trade CSV -> {path}  ({len(trades)} rows)")
    con.close()
    print("\nDONE.")


if __name__ == "__main__":
    main()
