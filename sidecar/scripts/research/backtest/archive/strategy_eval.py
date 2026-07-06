"""Evaluate a directional signal through the REAL managed exits (the user's actual
staged partials + trailing/BE-ladder + runner-protect) in option-dollars.

Net P&L over OOS through these exits — NOT close-direction, NOT hold-to-close — is the
objective that decides whether a candidate is a tradeable long strategy. A signal only
has to be good enough that the profit-locking exits net positive.
"""
from __future__ import annotations

import argparse
import math
import statistics

from .data import Session, connect, sessions
from .executor import simulate_managed_long
from .label_dir import decision_ts


def eval_signal(signal_fn, start, end, *, stop_pts=8, offset=0, t_mults=(1.5, 2.5, 5.0),
                con=None, by_year=False):
    """signal_fn(sess, dec_ts) -> 'CALL'|'PUT'|None. Run the managed long on each fired
    day; aggregate per-contract option-$ P&L. Returns a metrics dict."""
    if con is None:
        con = connect()
    pcs, wins = [], 0
    bydir = {"CALL": [], "PUT": []}
    byyear = {}
    for d in sessions(con, start, end):
        s = Session(con, d)
        if s.empty():
            continue
        dec = decision_ts(s)
        if dec is None:
            continue
        try:
            sig = signal_fn(s, dec)
        except Exception:
            continue
        if sig not in ("CALL", "PUT"):
            continue
        atm = s.atm(dec)
        if atm is None:
            continue
        right = "C" if sig == "CALL" else "P"
        soff = offset if right == "C" else -offset
        strike = min(s.strikes, key=lambda k: abs(k - (atm + soff)))
        t = simulate_managed_long(s, dec, right, strike, stop_pts=stop_pts, t_mults=t_mults)
        if not t:
            continue
        pc = t["pnl_per_contract"]
        pcs.append(pc)
        bydir[sig].append(pc)
        byyear.setdefault(d[:4], []).append(pc)
        wins += 1 if t["pnl"] > 0 else 0
    n = len(pcs)
    if n == 0:
        return {"n": 0, "note": "no fires"}
    mean = statistics.mean(pcs)
    sd = statistics.pstdev(pcs) if n > 1 else 0.0
    tstat = (mean / sd * math.sqrt(n)) if sd > 0 else 0.0
    out = {"n": n, "win_pct": round(100 * wins / n, 1), "mean_pnl_ctr": round(mean, 1),
           "total_15lot": round(sum(pcs) * 15, 0), "t_stat": round(tstat, 2)}
    for k in ("CALL", "PUT"):
        v = bydir[k]
        if v:
            out[k] = {"n": len(v), "mean_ctr": round(statistics.mean(v), 1),
                      "total15": round(sum(v) * 15, 0)}
    if by_year:
        out["by_year"] = {y: {"n": len(v), "mean_ctr": round(statistics.mean(v), 1),
                              "total15": round(sum(v) * 15, 0)} for y, v in sorted(byyear.items())}
    return out


# A few baseline signal_fns for sanity (always-call, always-put, skew-gated-call).
def always_call(sess, dec):
    return "CALL"


def always_put(sess, dec):
    return "PUT"


if __name__ == "__main__":
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2022-01-01")
    ap.add_argument("--to", dest="end", default="2022-12-31")
    ap.add_argument("--stop", type=int, default=8)
    a = ap.parse_args()
    con = connect()
    print("always-CALL :", json.dumps(eval_signal(always_call, a.start, a.end, stop_pts=a.stop, con=con)))
    print("always-PUT  :", json.dumps(eval_signal(always_put, a.start, a.end, stop_pts=a.stop, con=con)))
