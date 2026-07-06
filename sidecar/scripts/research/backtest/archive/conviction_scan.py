"""Baseline single-feature directional-conviction scan. For each entry-time-knowable
feature, bucket sessions into terciles and report P(close up) + mean forward move per
bucket. A bucket whose P(up) is well off 50% is a directional-conviction candidate
(to be confirmed out-of-sample — single in-sample buckets overfit easily).
"""
from __future__ import annotations

import argparse
import statistics

from .data import connect
from .feature_label import build_dataset

NUMERIC = ["gap", "ret_open", "or_size", "or_pos", "iv_atm", "iv_skew", "straddle"]


def scan(rows, feat, label, nb=3):
    vals = [(r[feat], r[label], r.get("ret_close")) for r in rows
            if r.get(feat) is not None and r.get(label) is not None]
    vals.sort(key=lambda x: x[0])
    n = len(vals)
    b = n // nb
    out = []
    for i in range(nb):
        grp = vals[i * b:] if i == nb - 1 else vals[i * b:(i + 1) * b]
        if not grp:
            continue
        k = len(grp)
        pct = 100 * sum(x[1] for x in grp) / k
        rc = [x[2] for x in grp if x[2] is not None]
        out.append((grp[0][0], grp[-1][0], k, pct, statistics.mean(rc) if rc else 0.0))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2022-01-01")
    ap.add_argument("--to", dest="end", default="2023-03-01")
    ap.add_argument("--label", default="dir_up", choices=["dir_up", "dom_up"])
    a = ap.parse_args()
    con = connect()
    rows = build_dataset(con, a.start, a.end)
    base = 100 * sum(r[a.label] for r in rows) / len(rows)
    print(f"range {a.start}..{a.end}  n={len(rows)}  baseline P({a.label})={base:.1f}%\n")
    for feat in NUMERIC:
        print(f"{feat}:")
        for lo, hi, k, pct, mret in scan(rows, feat, a.label):
            flag = "  <<< conviction" if abs(pct - 50) >= 8 else ""
            print(f"   [{lo:8.2f} .. {hi:8.2f}]  n={k:3d}  P(up)={pct:4.1f}%  fwd={mret:+5.1f}pt{flag}")
    print("dow (0=Mon..4=Fri):")
    for dw in range(5):
        g = [r for r in rows if r["dow"] == dw]
        if g:
            pct = 100 * sum(r[a.label] for r in g) / len(g)
            mret = statistics.mean([r["ret_close"] for r in g if r["ret_close"] is not None])
            print(f"   dow={dw}  n={len(g):3d}  P(up)={pct:4.1f}%  fwd={mret:+5.1f}pt")


if __name__ == "__main__":
    main()
