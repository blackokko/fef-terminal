"""TASK: does BIG TWO-SIDED 0DTE FLOW VOLUME precede an imminent BIG MOVE?

Hypothesis (user, 2026-06-30): when heavy CALL *and* PUT volume hit 0DTE *together*, a large
SPX move is about to happen -- regardless of DIRECTION. This is a VOLATILITY-EXPANSION signal,
orthogonal to the (falsified) directional net-flow edge. Everything the flow-surge retraction
killed was about DIRECTION; nobody has tested two-sided VOLUME -> |move|.

CRITICAL methodology (same trap as the directional artifact): magnitude is measured on REAL SPX
(QuantData net-drift `stockPrice`), NEVER the parity-reconstructed spot. A two-sided surge lifts
BOTH call and put mids, so it would inflate the reconstructed ruler's |move| even worse than the
directional case. Real spot only.  See [[validate-against-independent-price]].

SIGNALS (per minute, option_flow_minute volumes; surge = >= per-session p90, ET 09:45-15:00):
  TWOSIDED : min(call_tot_vol, put_tot_vol)         <- the hypothesis; min() => BOTH sides heavy
  TOTAL    : call_tot_vol + put_tot_vol             <- ablation: just lots of volume, any mix
  BALANCED : TOTAL surge AND min/max(call,put) >=0.5 <- ablation: high total AND two-sided
  (call_tot_vol = call_buy_vol + call_sell_vol ; put_tot_vol likewise.)

OUTCOME (direction-agnostic), on REAL SPX over +10/+30 min from each surge minute:
  RANGE = max-min of real SPX in (t, t+h]   (captures a big move even if it round-trips)
  |MOVE|= |spx(t+h) - spx(t)|               (endpoint displacement)
  SIGNED= spx(t+h) - spx(t)                 (sanity: should be ~0 for a pure vol signal)

CONTROL: each surge is paired with a TIME-OF-DAY-MATCHED non-surge minute (same session, within
  +-30 ET-min) so we are not just rediscovering "the open is volatile." Edge = surge RANGE/|MOVE|
  exceeds matched-control, SESSION-CLUSTERED (N_eff = #sessions), by year + DEV/HOLDOUT.

Read-only DB + network-read API. No engine/live code touched.
Run:  .venv/Scripts/python.exe -m scripts.research.backtest.magnitude_twosided_validate [START END]
"""
from __future__ import annotations

import sys

import numpy as np

from .data import connect
from .directional_validate_0dte_netdrift import _api, _post
from .holdout import HOLDOUT_START
from .thetadata_real_spot_validate import (
    MOVE_GLITCH_FRAC, SANE_HI, SANE_LO, SURGE_Q, TOL_MS, WIN_HI, WIN_LO,
    _et_h, _nearest, real_at, real_series,
)

sys.stdout.reconfigure(encoding="utf-8")

HORIZONS = [("+10min", 10), ("+30min", 30)]
MATCH_TOD_H = 0.5          # matched-control: within +-30 ET-min of the surge
RNG = np.random.default_rng(0)

# surge definitions: name -> (intensity(c_tot, p_tot) -> array, extra-mask(c_tot,p_tot) -> bool array | None)
def _defs(c_tot, p_tot):
    tot = c_tot + p_tot
    mn = np.minimum(c_tot, p_tot)
    mx = np.maximum(c_tot, p_tot).astype(float)
    bal = np.divide(mn, mx, out=np.zeros_like(mx), where=mx > 0)   # min/max in [0,1]
    return {
        "TWOSIDED": (mn.astype(float), None),
        "TOTAL":    (tot.astype(float), None),
        "BALANCED": (tot.astype(float), bal >= 0.5),
    }


def fwd_range(rt, rp, t0, t1):
    """max-min of real SPX over (t0, t1]; None if <2 sane points."""
    lo = int(np.searchsorted(rt, t0, side="right"))
    hi = int(np.searchsorted(rt, t1, side="right"))
    seg = rp[lo:hi]
    seg = seg[(seg >= SANE_LO) & (seg <= SANE_HI)]
    if len(seg) < 2:
        return None
    return float(seg.max() - seg.min())


def measure(rt, rp, ts, hmin):
    """(range, |move|, signed) on real SPX, glitch-filtered. None if unmeasurable."""
    s0 = real_at(rt, rp, ts)
    sH = real_at(rt, rp, ts + hmin * 60_000)
    if s0 is None or sH is None:
        return None
    mv = sH - s0
    if abs(mv) > MOVE_GLITCH_FRAC * s0:
        return None
    rng = fwd_range(rt, rp, ts, ts + hmin * 60_000)
    if rng is None or rng > MOVE_GLITCH_FRAC * s0:
        return None
    return (rng, abs(mv), mv)


def load_flow(con, d):
    rows = con.execute(
        "SELECT ts_ms, call_buy_vol, call_sell_vol, put_buy_vol, put_sell_vol "
        "FROM option_flow_minute WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
    if len(rows) < 20:
        return None
    ts = np.array([r[0] for r in rows], np.int64)
    c_tot = np.array([(r[1] or 0) + (r[2] or 0) for r in rows], float)
    p_tot = np.array([(r[3] or 0) + (r[4] or 0) for r in rows], float)
    return ts, c_tot, p_tot


# -------------------------------------------------------------------------- stats
def clustered(vals):
    a = np.array([v for v in vals if v is not None and np.isfinite(v)], float)
    n = len(a)
    if n < 2:
        return (float(a.mean()) if n else float("nan")), float("nan"), n
    se = a.std(ddof=1) / np.sqrt(n)
    return float(a.mean()), (float(a.mean() / se) if se > 0 else float("nan")), n


GROUPS = [
    ("2025", lambda r: r["year"] == "2025"),
    ("2026", lambda r: r["year"] == "2026"),
    ("ALL", lambda r: True),
    ("DEV", lambda r: r["split"] == "DEV"),
    ("HOLDOUT", lambda r: r["split"] == "HOLD"),
]


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute "
        "WHERE session_date BETWEEN ? AND ? ORDER BY session_date", (start, end))]

    print("=" * 108)
    print("TWO-SIDED 0DTE VOLUME -> imminent BIG MOVE?  (magnitude, direction-agnostic, REAL SPX)")
    print(f"  overlap {start}..{end}  ({len(dates)} candidate ThetaData sessions)")
    print(f"  surge = per-session p{SURGE_Q} of intensity, ET {WIN_LO}-{WIN_HI}; matched control +-30 ET-min")
    print(f"  outcome on REAL SPX (QuantData stockPrice): forward RANGE (max-min) and |MOVE|")
    print("=" * 108)

    recs = []
    n_sess_used = 0
    n_api_fail = 0
    with _api() as cli:
        for k, d in enumerate(dates):
            fl = load_flow(con, d)
            if fl is None:
                continue
            ts, c_tot, p_tot = fl
            eth = np.array([_et_h(int(t)) for t in ts])
            win = (eth >= WIN_LO) & (eth <= WIN_HI)
            if win.sum() < 10:
                continue
            data = _post(cli, {"sessionDate": d, "filter": {"ticker": "SPX", "expirationDate": d}})
            if not data:
                n_api_fail += 1
                continue
            rs = real_series(data)
            if rs is None:
                continue
            rt, rp = rs
            year, split = d[:4], ("DEV" if d < HOLDOUT_START else "HOLD")
            defs = _defs(c_tot, p_tot)
            used = False
            for dname, (inten, extra) in defs.items():
                in_win = inten[win]
                if len(in_win) < 10:
                    continue
                p90 = np.percentile(inten, SURGE_Q)
                is_surge = (inten >= p90)
                if extra is not None:
                    is_surge = is_surge & extra
                surge_idx = np.where(win & is_surge)[0]
                ctrl_pool = np.where(win & ~is_surge)[0]
                if len(surge_idx) == 0 or len(ctrl_pool) == 0:
                    continue
                pool_eth = eth[ctrl_pool]
                for hname, hmin in HORIZONS:
                    rs_s, am_s, sg_s = [], [], []   # surge: range, |move|, signed
                    rs_c, am_c = [], []             # matched control: range, |move|
                    for i in surge_idx:
                        m = measure(rt, rp, int(ts[i]), hmin)
                        if m is None:
                            continue
                        # time-of-day-matched control
                        cand = ctrl_pool[np.abs(pool_eth - eth[i]) <= MATCH_TOD_H]
                        cm = None
                        if len(cand):
                            j = int(cand[RNG.integers(len(cand))])
                            cm = measure(rt, rp, int(ts[j]), hmin)
                        if cm is None:
                            continue   # require a matched pair (clean within-session contrast)
                        rs_s.append(m[0]); am_s.append(m[1]); sg_s.append(m[2])
                        rs_c.append(cm[0]); am_c.append(cm[1])
                    if len(rs_s) >= 1:
                        recs.append(dict(
                            date=d, year=year, split=split, defn=dname, h=hname,
                            n=len(rs_s),
                            rng_s=float(np.mean(rs_s)), rng_c=float(np.mean(rs_c)),
                            am_s=float(np.mean(am_s)), am_c=float(np.mean(am_c)),
                            signed=float(np.mean(sg_s)),
                            drng=float(np.mean(rs_s) - np.mean(rs_c)),
                            dam=float(np.mean(am_s) - np.mean(am_c))))
                        used = True
            if used:
                n_sess_used += 1
            if (k + 1) % 25 == 0:
                print(f"  ...{k+1}/{len(dates)} fetched ({d})  sess_used={n_sess_used}", flush=True)

    print(f"\n  usable sessions: {n_sess_used}   api_fail={n_api_fail}")

    for dname in ("TWOSIDED", "TOTAL", "BALANCED"):
        print("\n" + "=" * 108)
        print(f"SIGNAL = {dname}")
        print(f"  {'group':8s} {'horizon':8s} {'#sess':>5s} {'#surge':>6s} "
              f"{'rngSurge':>8s} {'rngCtrl':>8s} {'rng x':>6s} {'t_rng':>6s} "
              f"{'|mv|Srg':>7s} {'|mv|Ctl':>7s} {'t_|mv|':>6s} {'signed':>7s}")
        for glabel, gpred in GROUPS:
            for hname, _ in HORIZONS:
                rr = [r for r in recs if r["defn"] == dname and r["h"] == hname and gpred(r)]
                if not rr:
                    continue
                nsess = len(rr)
                nsurge = sum(r["n"] for r in rr)
                rng_s = float(np.mean([r["rng_s"] for r in rr]))
                rng_c = float(np.mean([r["rng_c"] for r in rr]))
                am_s = float(np.mean([r["am_s"] for r in rr]))
                am_c = float(np.mean([r["am_c"] for r in rr]))
                _, t_rng, _ = clustered([r["drng"] for r in rr])
                _, t_am, _ = clustered([r["dam"] for r in rr])
                signed = float(np.mean([r["signed"] for r in rr]))
                ratio = (rng_s / rng_c) if rng_c else float("nan")
                print(f"  {glabel:8s} {hname:8s} {nsess:5d} {nsurge:6d} "
                      f"{rng_s:8.3f} {rng_c:8.3f} {ratio:6.2f} {t_rng:+6.2f} "
                      f"{am_s:7.3f} {am_c:7.3f} {t_am:+6.2f} {signed:+7.3f}")

    print("\n" + "=" * 108)
    print("READ: rng x = surge_range / matched-control_range (>1 + significant t_rng = vol expansion).")
    print("  TWOSIDED beating TOTAL/BALANCED => the TWO-SIDEDNESS carries the signal, not raw volume.")
    print("  signed ~ 0 confirms it's a magnitude (not direction) effect.")
    print("=" * 108)


if __name__ == "__main__":
    main()
