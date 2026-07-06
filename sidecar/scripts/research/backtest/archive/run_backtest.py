"""CLI backtest runner: one family + param set over a date range -> JSON metrics.

Deterministic and reproducible. The Workflow agents call THIS — they don't reinvent
fills — so every variant is judged on identical execution truth.

    python -m scripts.research.backtest.run_backtest --family orb --from 2022-01-01 \
        --to 2023-03-01 --params '{"or_min":30}' --offset 0 --hold 60 --target 15 --stop 10
"""
from __future__ import annotations

import argparse
import json
import math
import statistics

from .data import Session, connect, sessions
from .executor import simulate_naked_long
from .families import FAMILIES


def run(family, params, start, end, *, strike_offset=0, hold_min=30,
        und_target=None, und_stop=None, opt_stop_pct=None):
    con = connect()
    fam = FAMILIES[family]
    sess_list = sessions(con, start, end)
    trades, traded = [], 0
    for d in sess_list:
        s = Session(con, d)
        if s.empty():
            continue
        sigs = fam(s, **params)
        if not sigs:
            continue
        traded += 1
        for entry_ts, right in sigs:
            atm = s.atm(entry_ts)
            if atm is None:
                continue
            off = strike_offset if right == "C" else -strike_offset  # +offset = OTM
            strike = min(s.strikes, key=lambda k: abs(k - (atm + off)))
            t = simulate_naked_long(s, entry_ts, right, strike, hold_min=hold_min,
                                    und_target=und_target, und_stop=und_stop,
                                    opt_stop_pct=opt_stop_pct)
            if t:
                t["date"] = d
                trades.append(t)
    return summarize(trades, len(sess_list), traded)


def summarize(trades, n_sessions, traded_sessions):
    pnls = [t["pnl"] for t in trades]
    n = len(pnls)
    if n == 0:
        return {"n_trades": 0, "n_sessions": n_sessions, "note": "no trades generated"}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    mean = total / n
    sd = statistics.pstdev(pnls) if n > 1 else 0.0
    tstat = (mean / sd * math.sqrt(n)) if sd > 0 else 0.0
    pf = (sum(wins) / -sum(losses)) if (losses and sum(losses) < 0) else None
    by_right = {}
    for r in ("C", "P"):
        rp = [t["pnl"] for t in trades if t["right"] == r]
        if rp:
            by_right[r] = {"n": len(rp),
                           "win%": round(100 * sum(1 for p in rp if p > 0) / len(rp), 1),
                           "total$": round(sum(rp), 0),
                           "mean$": round(sum(rp) / len(rp), 1)}
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {
        "n_trades": n, "n_sessions": n_sessions, "traded_sessions": traded_sessions,
        "win%": round(100 * len(wins) / n, 1),
        "total$": round(total, 0), "mean$_per_trade": round(mean, 1),
        "median$": round(statistics.median(pnls), 1),
        "avg_win$": round(statistics.mean(wins), 1) if wins else 0,
        "avg_loss$": round(statistics.mean(losses), 1) if losses else 0,
        "profit_factor": round(pf, 2) if pf is not None else None,
        "t_stat": round(tstat, 2),
        "by_right": by_right, "exit_reasons": reasons,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", required=True, choices=list(FAMILIES))
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--params", default="{}", help="JSON dict of family params")
    ap.add_argument("--offset", type=float, default=0, help="strike offset in pts (+OTM)")
    ap.add_argument("--hold", type=int, default=30, help="time stop in minutes")
    ap.add_argument("--target", type=float, default=None, help="underlying target (pts)")
    ap.add_argument("--stop", type=float, default=None, help="underlying stop (pts)")
    ap.add_argument("--opt-stop", type=float, default=None, help="premium stop (fraction)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = run(a.family, json.loads(a.params), a.start, a.end,
              strike_offset=a.offset, hold_min=a.hold,
              und_target=a.target, und_stop=a.stop, opt_stop_pct=a.opt_stop)
    res["_config"] = {"family": a.family, "params": json.loads(a.params),
                      "offset": a.offset, "hold": a.hold, "target": a.target,
                      "stop": a.stop, "opt_stop": a.opt_stop, "range": [a.start, a.end]}
    print(json.dumps(res, indent=None if a.json else 2))


if __name__ == "__main__":
    main()
