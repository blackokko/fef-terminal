"""Full-day, multi-duration move map + per-time-of-day precursor discrimination.

Foundation for clustering distinct setups. Samples the WHOLE session (incl. the 9:30
open), and for each decision minute records whether a major move (>= THR pts) follows
at 15/30/45/60/90-min horizons, plus the precursor features present at/before it.

Output: (1) a move map -- where (time-of-day) and over what duration major moves
actually happen, up vs down; (2) per time-of-day bucket, the precursors that best
separate up- from down-moves. Each strong bucket+precursor is a candidate setup.
"""
from __future__ import annotations

import argparse
import statistics
from datetime import datetime

from .data import ET, Session, connect, sessions
from .gex_features import load_oi
from .move_study import cheap_features

THR = 15
HORIZONS = [15, 30, 45, 60, 90]
REF_H = 45
BUCKETS = [("OPEN", 9.5, 10.0), ("AM", 10.0, 11.5), ("MID", 11.5, 13.5), ("PM", 13.5, 15.0)]


def et_hour(ts):
    d = datetime.fromtimestamp(ts / 1000, ET)
    return d.hour + d.minute / 60.0


def bucket_of(h):
    for name, lo, hi in BUCKETS:
        if lo <= h < hi:
            return name
    return None


def forward_outcomes(sess, t, thr=THR):
    s0 = sess.spot(t)
    if s0 is None:
        return None
    out = {}
    for h in HORIZONS:
        hi = lo = s0
        for ts in sess.minutes:
            if ts <= t:
                continue
            if (ts - t) / 60000 > h:
                break
            s = sess.spot(ts)
            if s is None:
                continue
            hi, lo = max(hi, s), min(lo, s)
        up, dn = hi - s0, s0 - lo
        out[h] = "up" if (up >= thr and up > dn) else ("down" if (dn >= thr and dn > up) else "flat")
    return out


def study(start, end):
    con = connect()
    # samples[bucket][horizon] = {'up':n,'down':n,'flat':n}; feats[bucket][outcome@REF_H] = [feature dicts]
    samples = {b[0]: {h: {"up": 0, "down": 0, "flat": 0} for h in HORIZONS} for b in BUCKETS}
    feats = {b[0]: {"up": [], "down": []} for b in BUCKETS}
    for d in sessions(con, start, end):
        s = Session(con, d)
        if s.empty():
            continue
        oi = load_oi(con, d)
        if not oi:
            continue
        t_open = s.minutes[0]
        t = t_open
        end_t = t_open + 330 * 60000  # ~15:00
        while t <= end_t:
            tt = s.minute_at_or_before(t)
            if tt:
                b = bucket_of(et_hour(tt))
                oc = forward_outcomes(s, tt)
                if b and oc:
                    for h in HORIZONS:
                        samples[b][h][oc[h]] += 1
                    if oc[REF_H] in ("up", "down"):
                        f = cheap_features(con, s, tt, oi)
                        if f:
                            feats[b][oc[REF_H]].append(f)
            t += 15 * 60000
    return samples, feats


def report(samples, feats):
    print(f"\n=== MOVE MAP: % of samples with a >={THR}pt move within horizon (up/down) ===")
    print(f"{'bucket':6s} " + "  ".join(f"{h:>11}m" for h in HORIZONS))
    for b, _, _ in BUCKETS:
        cells = []
        for h in HORIZONS:
            d = samples[b][h]
            tot = sum(d.values()) or 1
            mv = 100 * (d["up"] + d["down"]) / tot
            cells.append(f"{mv:4.0f}% {d['up']:>3}/{d['down']:<3}")
        print(f"{b:6s} " + "  ".join(cells))
    print("  (cell = move% , up-count/down-count at that horizon)")

    print(f"\n=== PRECURSORS by bucket (at {REF_H}min): top features separating UP vs DOWN ===")
    for b, _, _ in BUCKETS:
        up, dn = feats[b]["up"], feats[b]["down"]
        if len(up) < 15 or len(dn) < 15:
            print(f"\n{b}: too few moves (up={len(up)} dn={len(dn)})")
            continue
        keys = set()
        for f in up + dn:
            keys |= set(f.keys())
        rows = []
        for k in keys:
            uv = [f[k] for f in up if f.get(k) is not None]
            dv = [f[k] for f in dn if f.get(k) is not None]
            if len(uv) < 10 or len(dv) < 10:
                continue
            allv = uv + dv
            sd = statistics.pstdev(allv) or 1.0
            disc = (statistics.median(uv) - statistics.median(dv)) / sd
            rows.append((abs(disc), k, statistics.median(uv), statistics.median(dv), disc))
        rows.sort(reverse=True)
        print(f"\n{b} (up={len(up)} dn={len(dn)}):")
        for _, k, um, dm, disc in rows[:5]:
            print(f"   {k:16s} up={um:10.3f}  dn={dm:10.3f}  disc={disc:+.2f} sd")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2023-01-01")
    ap.add_argument("--to", dest="end", default="2024-12-31")
    a = ap.parse_args()
    print(f"=== full-day setup study {a.start}..{a.end} ===")
    s, f = study(a.start, a.end)
    report(s, f)


if __name__ == "__main__":
    main()
