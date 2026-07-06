"""Move-anchored precursor study (what you actually asked for).

For every decision minute, label whether a MAJOR move (>= THR points) follows within
WINDOW minutes (up = call op, down = put op, flat = no move). Then profile the
characteristics PRESENT AT/BEFORE that minute (price, OI positioning, flow, cheap skew)
by class -- revealing the fingerprint that precedes major up-moves vs down-moves.

The forward window only LABELS the move (study-time lookahead). The precursor features
use ONLY data at/before the decision minute, so a discriminating feature translates
directly into a tradeable entry signal (then tested OOS through the real exits).
"""
from __future__ import annotations

import argparse
import statistics

from .data import Session, connect, sessions
from .flow_features import flow_features
from .gex_features import load_oi

SAMPLE_MIN = 15
THR = 15      # a 'major' move = >= 15 SPX pts
WINDOW = 45   # within 45 minutes


def forward_label(sess, t, thr=THR, window=WINDOW):
    s0 = sess.spot(t)
    if s0 is None:
        return None
    hi = lo = s0
    for ts in sess.minutes:
        if ts <= t:
            continue
        if ts > t + window * 60000:
            break
        s = sess.spot(ts)
        if s is None:
            continue
        hi = max(hi, s)
        lo = min(lo, s)
    up, dn = hi - s0, s0 - lo
    if up >= thr and up > dn:
        return "up"
    if dn >= thr and dn > up:
        return "down"
    return "flat"


def cheap_features(con, sess, t, oi):
    s0 = sess.spot(t)
    if s0 is None:
        return None
    f = {}
    t_open = sess.minutes[0]
    s_open = sess.spot(t_open)
    f["ret_open"] = (s0 - s_open) if s_open else None
    back = sess.minute_at_or_before(t - 15 * 60000)
    sb = sess.spot(back) if back else None
    f["ret_15"] = (s0 - sb) if sb else None
    pre = [x for x in (sess.spot(ts) for ts in sess.minutes if ts <= t) if x is not None]
    if pre:
        hi, lo = max(pre), min(pre)
        f["or_pos"] = (s0 - lo) / (hi - lo) if hi > lo else 0.5
        f["range_so_far"] = hi - lo
        f["vwap_dist"] = s0 - (sum(pre) / len(pre))
    atm = sess.atm(t)
    pm = sess.mid(t, atm - 25, "P") if atm else None
    cm = sess.mid(t, atm + 25, "C") if atm else None
    f["skew_ratio"] = (pm / cm) if (pm and cm and cm > 0) else None
    cc = sess.mid(t, atm, "C") if atm else None
    pp = sess.mid(t, atm, "P") if atm else None
    f["straddle"] = (cc + pp) if (cc and pp) else None
    call_oi = {k: o for (k, r), o in oi.items() if r == "C"}
    put_oi = {k: o for (k, r), o in oi.items() if r == "P"}
    tc, tp = sum(call_oi.values()), sum(put_oi.values())
    f["pc_oi_ratio"] = (tp / tc) if tc else None
    strikes = sorted(set(call_oi) | set(put_oi))
    if strikes:
        mp = min(strikes, key=lambda P: sum(
            call_oi.get(k, 0) * max(0.0, P - k) + put_oi.get(k, 0) * max(0.0, k - P) for k in strikes))
        f["max_pain_dist"] = mp - s0
        cw = max((k for k in call_oi if k >= s0), key=lambda k: call_oi[k], default=None)
        pw = max((k for k in put_oi if k <= s0), key=lambda k: put_oi[k], default=None)
        f["call_wall_dist"] = (cw - s0) if cw else None
        f["put_wall_dist"] = (s0 - pw) if pw else None
    fl = flow_features(con, sess.date, t)
    if fl:
        for k in ("cum_signed_flow", "flow_slope15", "call_buy_ratio", "put_buy_ratio", "flow_norm"):
            f[k] = fl.get(k)
    return f


def study(start, end, sample_min=SAMPLE_MIN):
    con = connect()
    data = {"up": [], "down": [], "flat": []}
    for d in sessions(con, start, end):
        s = Session(con, d)
        if s.empty():
            continue
        oi = load_oi(con, d)
        if not oi:
            continue
        t_open = s.minutes[0]
        t = t_open + 15 * 60000
        end_t = t_open + 330 * 60000  # ~15:00
        while t <= end_t:
            tt = s.minute_at_or_before(t)
            if tt:
                lab = forward_label(s, tt)
                if lab:
                    feats = cheap_features(con, s, tt, oi)
                    if feats:
                        data[lab].append(feats)
            t += sample_min * 60000
    return data


def report(data):
    keys = set()
    for cls in data.values():
        for f in cls:
            keys |= set(f.keys())
    n = {k: len(v) for k, v in data.items()}
    tot = sum(n.values()) or 1
    print(f"samples: up={n['up']} ({100*n['up']/tot:.0f}%)  down={n['down']} ({100*n['down']/tot:.0f}%)  flat={n['flat']} ({100*n['flat']/tot:.0f}%)")
    print(f"\n{'feature':16s} {'UP-pre':>11} {'DOWN-pre':>11} {'FLAT':>11}   disc(up-dn)")
    rows = []
    for k in sorted(keys):
        vals = {cls: [f[k] for f in data[cls] if f.get(k) is not None] for cls in data}
        if not vals["up"] or not vals["down"]:
            continue
        med = {cls: statistics.median(v) if v else float("nan") for cls, v in vals.items()}
        sd_all = statistics.pstdev([x for v in vals.values() for x in v]) or 1.0
        disc = (med["up"] - med["down"]) / sd_all  # standardized up-vs-down separation
        rows.append((abs(disc), k, med, disc))
    for _, k, med, disc in sorted(rows, reverse=True):
        print(f"{k:16s} {med['up']:11.3f} {med['down']:11.3f} {med['flat']:11.3f}   {disc:+.2f} sd")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2023-01-01")
    ap.add_argument("--to", dest="end", default="2023-12-31")
    a = ap.parse_args()
    print(f"=== move-precursor study {a.start}..{a.end} (THR={THR}pt / {WINDOW}min) ===")
    report(study(a.start, a.end))


if __name__ == "__main__":
    main()
