"""Isolate the entry signals' raw DIRECTIONAL accuracy — no options, no exits.

For each signal, does the underlying actually move the signaled way over the next
30/60 min and into the close? If favorable% <= ~50% with no positive drift, the
signals have no directional edge and that — not the exit logic — is why naked longs
lose. This is the decisive root-cause test.
"""
from __future__ import annotations

import statistics

from .data import Session, connect, sessions
from .families import FAMILIES

FAM_PARAMS = {
    "orb": {"or_min": 30},
    "trend_ema": {"ema_len": 20},
    "momentum": {"lookback_min": 15, "thresh_pts": 8.0},
}


def main():
    con = connect()
    for fam, fp in FAM_PARAMS.items():
        favs = {"30m": [], "60m": [], "close": []}
        n = 0
        for d in sessions(con, "2022-01-01", "2023-03-01"):
            s = Session(con, d)
            if s.empty():
                continue
            sigs = FAMILIES[fam](s, **fp)
            if not sigs:
                continue
            entry_ts, right = sigs[0]
            sgn = 1.0 if right == "C" else -1.0
            s0 = s.spot(entry_ts)
            if s0 is None:
                continue
            n += 1
            for tag, dt_ms in (("30m", 30 * 60000), ("60m", 60 * 60000)):
                m = s.minute_at_or_before(entry_ts + dt_ms)
                sp = s.spot(m) if m else None
                if sp is not None:
                    favs[tag].append(sgn * (sp - s0))
            spc = s.spot(s.minutes[-1])
            if spc is not None:
                favs["close"].append(sgn * (spc - s0))
        print(f"\n{fam}  (n={n} signals)")
        for tag in ("30m", "60m", "close"):
            v = favs[tag]
            if not v:
                continue
            favpct = 100 * sum(1 for x in v if x > 0) / len(v)
            print(f"   {tag:5s}  favorable={favpct:4.1f}%   mean={statistics.mean(v):+5.1f}pt   "
                  f"median={statistics.median(v):+5.1f}pt")


if __name__ == "__main__":
    main()
