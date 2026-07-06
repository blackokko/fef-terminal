"""Backtest each OI/price setup x each exit variant, TRAIN (2022-23) vs OOS (2024-26),
at 3 contracts net of friction. A setup survives only if it nets positive OOS with a
decent t-stat. Signal computed once per (session, setup); all 3 exits run on it.
"""
from __future__ import annotations

import math
import statistics

from .data import Session, connect, sessions
from .exits import VARIANTS, simulate
from .gex_features import load_oi
from .setups import SETUPS


def run(start, end):
    con = connect()
    res = {}  # (setup, variant) -> [pnl per trade at 3ct]
    for d in sessions(con, start, end):
        s = Session(con, d)
        if s.empty():
            continue
        oi = load_oi(con, d)
        if not oi:
            continue
        for sname, sfn in SETUPS.items():
            sig = sfn(s, oi)
            if not sig:
                continue
            ets, direction = sig
            atm = s.atm(ets)
            if atm is None:
                continue
            right = "C" if direction == "CALL" else "P"
            for v in VARIANTS:
                t = simulate(v, s, ets, right, atm, qty=3)
                if t:
                    res.setdefault((sname, v), []).append(t["pnl"])
    return res


def stat(pnls):
    n = len(pnls)
    if n == 0:
        return None
    mean = statistics.mean(pnls)
    sd = statistics.pstdev(pnls) if n > 1 else 0.0
    t = (mean / sd * math.sqrt(n)) if sd > 0 else 0.0
    return {"n": n, "win": 100 * sum(1 for p in pnls if p > 0) / n,
            "mean": mean, "total": sum(pnls), "t": t}


def main():
    tr = run("2022-01-01", "2023-12-31")
    oos = run("2024-01-01", "2026-06-26")
    print("setup x exit         |        TRAIN 2022-23           |         OOS 2024-26")
    print("                     |  n  win%  $/day   total   t    |  n  win%  $/day   total   t")
    for sname in SETUPS:
        for v in VARIANTS:
            a = stat(tr.get((sname, v), []))
            b = stat(oos.get((sname, v), []))
            if not a:
                continue
            bb = b or {"n": 0, "win": 0, "mean": 0, "total": 0, "t": 0}
            flag = "  <<<" if (b and b["mean"] > 0 and b["t"] >= 1.5) else ""
            print(f"{sname:13s} {v:10s}| {a['n']:3d} {a['win']:4.0f} {a['mean']:+6.0f} {a['total']:+7.0f} {a['t']:+4.1f}  "
                  f"| {bb['n']:3d} {bb['win']:4.0f} {bb['mean']:+6.0f} {bb['total']:+7.0f} {bb['t']:+4.1f}{flag}")
        print()


if __name__ == "__main__":
    main()
