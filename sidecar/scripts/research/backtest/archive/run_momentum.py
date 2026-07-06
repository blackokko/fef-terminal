"""Backtest momentum-confirmation entries x exit configs (with the user's fast exits),
TRAIN (2022-23) vs OOS (2024-26), 3 contracts net friction. Signal computed once per
(session, entry); all exit configs run on it.
"""
from __future__ import annotations

import math
import statistics

from .data import Session, connect, sessions
from .exits import simulate
from .momentum import ENTRIES

EXITS = {
    "be_dflt": ("be_ladder", {}),
    "be_prem$1": ("be_ladder", {"prem_stop": 1.0}),
    "be_cut15": ("be_ladder", {"cut_if_flat_min": 15}),
    "bcd": ("bcd", {}),
}


def run(start, end):
    con = connect()
    res = {}
    for d in sessions(con, start, end):
        s = Session(con, d)
        if s.empty():
            continue
        for ename, efn in ENTRIES.items():
            sig = efn(s)
            if not sig:
                continue
            ets, direction = sig
            atm = s.atm(ets)
            if atm is None:
                continue
            right = "C" if direction == "CALL" else "P"
            for xname, (v, kw) in EXITS.items():
                t = simulate(v, s, ets, right, atm, qty=3, **kw)
                if t:
                    res.setdefault((ename, xname), []).append(t["pnl"])
    return res


def stat(p):
    n = len(p)
    if n == 0:
        return None
    m = statistics.mean(p)
    sd = statistics.pstdev(p) if n > 1 else 0.0
    return {"n": n, "win": 100 * sum(1 for x in p if x > 0) / n, "mean": m,
            "total": sum(p), "t": (m / sd * math.sqrt(n)) if sd > 0 else 0.0}


def main():
    tr = run("2022-01-01", "2023-12-31")
    oos = run("2024-01-01", "2026-06-26")
    print("entry x exit       |       TRAIN 2022-23         |        OOS 2024-26")
    print("                   |  n  win%  $/day  total   t   |  n  win%  $/day  total   t")
    for ename in ENTRIES:
        for xname in EXITS:
            a = stat(tr.get((ename, xname), []))
            b = stat(oos.get((ename, xname), []))
            if not a:
                continue
            bb = b or {"n": 0, "win": 0, "mean": 0, "total": 0, "t": 0}
            flag = "  <<<" if (b and b["mean"] > 0 and b["t"] >= 1.5) else ""
            print(f"{ename:9s} {xname:8s}| {a['n']:4d} {a['win']:4.0f} {a['mean']:+5.0f} {a['total']:+7.0f} {a['t']:+4.1f} "
                  f"| {bb['n']:4d} {bb['win']:4.0f} {bb['mean']:+5.0f} {bb['total']:+7.0f} {bb['t']:+4.1f}{flag}")
        print()


if __name__ == "__main__":
    main()
