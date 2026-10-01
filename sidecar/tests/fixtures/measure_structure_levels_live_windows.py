"""How much of the study's level set the LIVE route's data windows reproduce -- DEV SCRIPT, run by
hand, never by pytest (it reads the research DB).

    set PYTHONPATH=<repo>\\sidecar\\src
    <repo>\\sidecar\\.venv\\Scripts\\python.exe sidecar\\tests\\fixtures\\measure_structure_levels_live_windows.py

The route feeds the engine daily bars for 2 years, 30-minute bars for 3 months, 5-minute bars for
20 sessions and 1-minute bars for the last 2 (api._SL_*). Here those windows are rebuilt, exactly,
from D:\\fef-data\\qqq_bars_cache.db (opened READ-ONLY: file:...?mode=ro, pragma query_only=1) and the
engine's ACTIVE / BROKEN sets at the long-golden snapshots are compared with the study's
(structure_levels_golden_long.json.gz: the study run on 1-minute bars 2024-03-04 .. 2026-03-13),
for 30m / 1H / 4H / 1D and P/D/R at the 1H scale.

Result 2026-10-01: 100 % on every cell except 4H (S-4H 16/39, Z-4H 0/12, M-4H 0/4) -- 208/247.
A 4H level can live 300 4H-bars (~150 sessions) but 3 months of 30-minute bars hold ~63 sessions,
so the oldest 4H structure is not drawn. 1D (from the daily bars) and 1H (max age ~43 sessions)
are whole. With the 30-minute window widened (edit "di - 63"): 126 sessions -> 232/247,
168 sessions -> 247/247.
"""
from __future__ import annotations

import collections
import gzip
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from fef_sidecar.engine import structure_levels as SL
from fef_sidecar.et_time import ET

sys.stdout.reconfigure(errors="replace")
FIX = Path(__file__).parent
DB = r"D:\fef-data\qqq_bars_cache.db"


def agg(bs: list[dict], m: int) -> list[dict]:
    out: list[dict] = []
    key0 = None
    for b in bs:
        et = datetime.fromtimestamp(b["time"], ET)
        mod = et.hour * 60 + et.minute - 570
        key = (b["sd"], mod // m)
        if key != key0:
            out.append({"time": b["time"] - (mod % m) * 60, "open": b["open"], "high": b["high"],
                        "low": b["low"], "close": b["close"]})
            key0 = key
        else:
            c = out[-1]
            c["high"] = max(c["high"], b["high"])
            c["low"] = min(c["low"], b["low"])
            c["close"] = b["close"]
    return out


def daily(bs: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for b in bs:
        if b["sd"] not in out:
            out[b["sd"]] = {"date": b["sd"], "open": b["open"], "high": b["high"], "low": b["low"],
                            "close": b["close"]}
        else:
            c = out[b["sd"]]
            c["high"] = max(c["high"], b["high"])
            c["low"] = min(c["low"], b["low"])
            c["close"] = b["close"]
    return list(out.values())


def main() -> int:
    with gzip.open(FIX / "structure_levels_golden_long.json.gz", "rb") as f:
        gold = json.loads(f.read())
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.execute("pragma query_only=1")
    rows = con.execute("select ts_ms, session_date, o, h, l, c from bars_1m "
                       "where session_date >= ? and session_date <= ? order by ts_ms",
                       (gold["meta"]["from"], gold["meta"]["to"])).fetchall()
    con.close()
    bars = [{"time": r[0] // 1000, "sd": r[1], "open": r[2], "high": r[3], "low": r[4], "close": r[5]}
            for r in rows]
    dates = sorted({b["sd"] for b in bars})
    tot = agree = 0
    cell_n: collections.Counter = collections.Counter()
    cell_ok: collections.Counter = collections.Counter()
    for sn in gold["snapshots"]:
        now = sn["now_s"]
        today = datetime.fromtimestamp(now, ET).date().isoformat()
        di = dates.index(today)
        hist = [b for b in bars if b["sd"] < today]
        d30, d5, d1 = set(dates[max(0, di - 63):di]), set(dates[max(0, di - 20):di]), set(dates[max(0, di - 1):di + 1])
        inp = {"1D": daily(hist), "30m": agg([b for b in hist if b["sd"] in d30], 30),
               "5m": agg([b for b in hist if b["sd"] in d5], 5),
               "1m": [b for b in bars if b["sd"] in d1 and b["time"] < now]}
        A = SL.analyze(inp, now, "QQQ", pdr_scales=(0, 3))
        exp = {(f, ti, k, round(lo, 6), round(hi, 6), kfs): (st, tch)
               for f, ti, k, lo, hi, kfs, st, tch in sn["levels"] if not (f in "PDR" and ti == 0)}
        got = {}
        for lv in A.levels:
            st = lv.state_at(A.n)
            if st and (lv.ti >= SL.TI_30M or (lv.fam in "PDR" and lv.ti == SL.TI_1H)) \
                    and not (lv.fam in "SZMF" and lv.ti < SL.TI_30M):
                got[(lv.fam, lv.ti, lv.kind, round(lv.lo, 6), round(lv.hi, 6), A.known_from_s(lv))] = (
                    st, lv.touches if lv.fam == "Z" else None)
        for lv in A.fib05:
            st = lv.state_at(A.n)
            if st and lv.ti >= SL.TI_30M:
                got[("fib05", lv.ti, 1, round(lv.lo, 6), round(lv.hi, 6), A.known_from_s(lv))] = (st, None)
        ok = sum(1 for k in exp if got.get(k) == exp[k])
        tot += len(exp)
        agree += ok
        for k in exp:
            c = f"{k[0]}-{SL.TAUS[k[1]]}"
            cell_n[c] += 1
            cell_ok[c] += got.get(k) == exp[k]
        print(f"{sn['label']}: {ok}/{len(exp)}  (missing {len(set(exp) - set(got))}, extra {len(set(got) - set(exp))})")
    print(f"live-window agreement {agree}/{tot} = {100 * agree / max(tot, 1):.1f} %")
    for c in sorted(cell_n):
        print(f"  {c:12s} {cell_ok[c]}/{cell_n[c]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
