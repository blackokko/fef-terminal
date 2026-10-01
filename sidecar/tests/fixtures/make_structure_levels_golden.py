"""Golden fixtures for tests/test_structure_levels.py -- DEV SCRIPT, run by hand, never by pytest.

Runs the `mtflevels` STUDY's own code (executor A, D:\\fef-data\\mtflevels\\A: mtfl_core, mtfl_marker,
mtfl_events, mtfl_mid -- verified 99.7 % against an independent implementation) on a QQQ fixture
and writes what the sidecar port (fef_sidecar.engine.structure_levels) must reproduce.

    C:\\Python314\\python.exe sidecar\\tests\\fixtures\\make_structure_levels_golden.py

Needs numpy + pandas (the study code imports them) and the research archive's `fefharness`
(imported by mtfl_mid) -- i.e. the system Python, not the sidecar venv.

Reads D:\\fef-data\\qqq_bars_cache.db::bars_1m READ-ONLY (file:...?mode=ro, pragma query_only=1;
size and mtime asserted unchanged). Writes only next to this file:

  structure_levels_qqq_1m.csv.gz   the fixture: QQQ RTH 1-minute bars, 61 complete consecutive
                                   sessions 2025-12-15 .. 2026-03-13 (early close 12-24, five
                                   holidays, the 2026-03-08 DST change); ts = bar start, epoch s
  structure_levels_golden.json.gz  the study's output on exactly those bars (read back from the CSV)

Contents of the golden file (all indices are 1-minute bar indices into the fixture, 0-based;
"future" values -- the study's era-end n or its BIG sentinel -- are written as null):
  levels      every real level of the 30 primary family-tau cells (lock 2, after the 2.11 dedupe):
              [fam, ti, kind, lo, hi, kf, ref, atr, exp, side0, side, armb, brk, brkc, actend,
               brkend, leg]
  fib05       the study's fib-ratio arm at ratio 0.5 (lock 4.3: F lifecycle, R7, no dedupe), same rows
  zones       [ti, kf, lo, hi, members, [touch kf ...]] -- a mirror of mtfl_marker.levels_Z that logs
              each touch, asserted identical to the study's zone set
  setups      every real-origin MIDENTRY setup (AMENDMENT_1, f = 0.5):
              [ti, d, otype, t0, Ob, O, conf, def_level, tq, qual, last, fb, entry, X, code, R, rbar, e]
              def_level = index into `levels` of the defining origin's level
  snapshots   ~12 times t (minute-of-session not a multiple of 5, so nothing becomes known exactly
              at t, and no fib / leg-midpoint level is expiring at t -- see the test's docstring);
              for each, the levels ACTIVE or BROKEN-in-window at t:
              [fam, ti, kind, lo, hi, kf, state, touches]
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
import os
import sqlite3
import sys
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(errors="replace")
STUDY = r"D:\fef-data\mtflevels\A"
sys.path.insert(0, STUDY)

import numpy as np  # noqa: E402

import mtfl_core as C  # noqa: E402
import mtfl_events as EV  # noqa: E402
import mtfl_marker as MK  # noqa: E402
import mtfl_mid as MID  # noqa: E402

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
D_FROM, D_TO = "2025-12-15", "2026-03-13"
CSV = os.path.join(HERE, "structure_levels_qqq_1m.csv.gz")
GOLD = os.path.join(HERE, "structure_levels_golden.json.gz")
# Long horizon (30m and up): the study runs on two years of 1-minute bars; the port is fed those
# sessions as 30-minute bars plus the main fixture's 1-minute bars for the last two sessions -- the
# shape of the live route's input. Covers the 4H / 1D families the 61-session fixture barely has.
LONG_FROM = "2024-03-04"
LONG_CSV = os.path.join(HERE, "structure_levels_qqq_30m_long.csv.gz")
LONG_GOLD = os.path.join(HERE, "structure_levels_golden_long.json.gz")
LONG_SNAPS = ["2026-03-12 10:42", "2026-03-12 15:13", "2026-03-13 11:57", "2026-03-13 14:22"]
LONG_TAUS = (2, 3, 4, 5)          # 30m, 1H, 4H, 1D
SNAP_CANDIDATES = [
    "2025-12-24 12:22",   # early-close session
    "2026-02-27 15:58",
    "2026-03-04 14:47",
    "2026-03-05 10:12",
    "2026-03-06 15:33",
    "2026-03-09 09:43",   # first session after the DST change
    "2026-03-09 13:28",
    "2026-03-10 11:02",
    "2026-03-11 12:33",
    "2026-03-11 15:58",
    "2026-03-12 10:42",
    "2026-03-13 11:57",
    "2026-03-13 14:22",
]
FAMC = {i: f for i, f in enumerate(C.FAMS)}
BIG = MK.BIG


def gz_write(path: str, data: bytes) -> None:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, filename="") as g:
        g.write(data)
    with open(path + ".tmp", "wb") as f:
        f.write(buf.getvalue())
    os.replace(path + ".tmp", path)


def extract() -> None:
    st0 = C.file_stat(C.D0_DB)
    con = sqlite3.connect(f"file:{C.D0_DB}?mode=ro", uri=True)
    con.execute("pragma query_only=1")
    rows = con.execute(
        "select ts_ms, session_date, o, h, l, c from bars_1m "
        "where session_date >= ? and session_date <= ? order by ts_ms", (D_FROM, D_TO)).fetchall()
    con.close()
    assert C.file_stat(C.D0_DB) == st0, "research DB changed!"
    dates = sorted({r[1] for r in rows})
    expect = C.nyse_sessions(dt.date.fromisoformat(D_FROM), dt.date.fromisoformat(D_TO))
    assert dates == expect, ("fixture must be complete consecutive NYSE sessions",
                             sorted(set(expect) - set(dates)))
    lines = ["ts,o,h,l,c"]
    for ts_ms, sd, o, h, lo, c in rows:
        for v in (o, h, lo, c):
            assert abs(v * 100 - round(v * 100)) < 1e-6, ("non-cent price", ts_ms, v)
        assert dt.datetime.fromtimestamp(ts_ms / 1000, ET).date().isoformat() == sd
        lines.append(f"{ts_ms // 1000},{o:.2f},{h:.2f},{lo:.2f},{c:.2f}")
    gz_write(CSV, ("\n".join(lines) + "\n").encode())
    print("fixture:", len(rows), "bars,", len(dates), "sessions ->", CSV, os.path.getsize(CSV), "bytes")


def load_era() -> C.Era:
    with gzip.open(CSV, "rt") as f:
        lines = f.read().splitlines()[1:]
    ts, o, h, lo, c = [], [], [], [], []
    for ln in lines:
        a = ln.split(",")
        ts.append(int(a[0]))
        o.append(float(a[1]))
        h.append(float(a[2]))
        lo.append(float(a[3]))
        c.append(float(a[4]))
    sd = [dt.datetime.fromtimestamp(t, ET).date().isoformat() for t in ts]
    return C.Era("FIXTURE", np.array(ts, dtype=np.int64) * 1000, np.array(sd), np.array(o),
                 np.array(h), np.array(lo), np.array(c), tag=0)


def zone_touch_log(E, ST):
    """mtfl_marker.levels_Z, line for line, plus a log of which pivot (kf) touched which zone."""
    out = []
    for S in ST:
        ti = S.ti
        P = S.piv
        npv = P["px"].size
        win = C.MAXAGE_1D if C.TAUS[ti] == "1D" else 200
        zl = dict(lo=[], hi=[], kf=[], actend=[], members=[], tk=[])
        active = []
        for p in range(npv):
            t = int(S.piv_kf[p])
            if t >= E.n:
                break
            active = [z for z in active if zl["actend"][z] > t]
            Ppx = P["px"][p]
            inside = [z for z in active if zl["lo"][z] <= Ppx <= zl["hi"][z]]
            if inside:
                zl["tk"][inside[0]].append(t)
                continue
            atrP = float(E.atr_at_bar(ti, t))
            if not np.isfinite(atrP):
                continue
            cand = np.arange(0, p + 1)
            cand = cand[P["bar"][cand] > P["conf"][p] - win]
            mem = cand[np.abs(P["px"][cand] - Ppx) <= 0.5 * atrP + C.EPS_TIE]
            if mem.size < 3:
                continue
            lo = float(P["px"][mem].min())
            hi = float(P["px"][mem].max())
            if any((zl["lo"][z] <= hi) and (lo <= zl["hi"][z]) for z in active):
                continue
            if any(max(0.0, zl["lo"][z] - hi, lo - zl["hi"][z]) < 0.1 * atrP - C.EPS_TIE for z in active):
                continue
            exp = int(MK.maxage_exp(E, ti, np.array([t]))[0])
            one = MK.make(fam=C.FAM["Z"], ti=ti, kind=0, arm=MK.ARM_REAL, lo=np.array([lo]), hi=np.array([hi]),
                          kf=np.array([t]), ref=np.array([E.c[t - 1]]), atr=np.array([atrP]),
                          exp=np.array([exp]), parent=-1, leg=-1, za=-1, zb=-1, sesskf=np.array([E.sess[t]]))
            one = MK.lifecycle(E, one)
            zl["lo"].append(lo)
            zl["hi"].append(hi)
            zl["kf"].append(t)
            zl["actend"].append(int(one["actend"][0]))
            zl["members"].append(int(mem.size))
            zl["tk"].append([])
            active.append(len(zl["lo"]) - 1)
        for z in range(len(zl["lo"])):
            out.append([ti, zl["kf"][z], zl["lo"][z], zl["hi"][z], zl["members"][z], zl["tk"][z]])
    return out


def trade_race_bars(E, ti, fb, entry, stop, target, d):
    """mtfl_mid.trade_race plus the resolution bar and the horizon (asserted equal in code)."""
    n = E.n
    N = fb.size
    code = np.full(N, -9, dtype=np.int64)
    R = np.full(N, np.nan)
    rbar = np.full(N, BIG, dtype=np.int64)
    ee = np.full(N, BIG, dtype=np.int64)
    ok = fb < n
    fbc = np.minimum(fb, n - 1)
    risk = np.abs(entry - stop)
    with np.errstate(invalid="ignore", divide="ignore"):
        rwin = np.round(np.abs(target - entry) / risk, 9)
    hit_stop = np.where(d == 1, E.lc[fbc] <= C.c_le(stop), E.hc[fbc] >= C.c_ge(stop))
    hit_tgt = np.where(d == 1, E.hc[fbc] >= C.c_ge(target), E.lc[fbc] <= C.c_le(target))
    amb = ok & (hit_stop | hit_tgt)
    code[amb] = -1
    rbar[amb] = fb[amb]
    rest = ok & ~amb
    if rest.any():
        s = fb[rest] + 1
        e = EV.horizon_end(E, ti, np.minimum(s, n - 1), fb[rest])
        U = np.where(d[rest] == 1, target[rest], stop[rest])
        Dn = np.where(d[rest] == 1, stop[rest], target[rest])
        cr, bar = EV.race(E, s, e, U, Dn)
        win = cr == d[rest]
        loss = cr == -d[rest]
        cc = np.full(cr.size, -2, dtype=np.int64)
        cc[win] = 1
        cc[loss] = 0
        cc[cr == 0] = -1
        code[rest] = cc
        rr = np.where(win, rwin[rest], np.where(loss, -1.0, np.nan))
        un = cc == -2
        if un.any():
            e2 = np.minimum(e[un], n - 1)
            rr[un] = (E.c[e2] - entry[rest][un]) * d[rest][un] / risk[rest][un]
        R[rest] = rr
        rb = np.where(un, e, bar)
        rbar[rest] = rb
        ee[rest] = e
    return code, R, rbar, ee


def fut(v, n):
    v = int(v)
    return None if v >= n else v


def fnum(v):
    v = float(v)
    return None if not np.isfinite(v) else round(v, 10)


def level_rows(L, n):
    rows = []
    for i in range(MK.nrow(L)):
        rows.append([FAMC[int(L["fam"][i])], int(L["ti"][i]), int(L["kind"][i]), fnum(L["lo"][i]),
                     fnum(L["hi"][i]), int(L["kf"][i]), fnum(L["ref"][i]), fnum(L["atr"][i]),
                     fut(L["exp"][i], n), int(L["side0"][i]), int(L["side"][i]), fut(L["armb"][i], n),
                     int(L["brk"][i]), fut(L["brkc"][i], n), fut(L["actend"][i], n),
                     fut(L["brkend"][i], n), int(L["leg"][i])])
    return rows


def state_at(L, i, t):
    if L["kf"][i] > t:
        return None
    if t < L["actend"][i]:
        return "active"
    if L["brk"][i] >= 0 and L["brkc"][i] <= t < L["brkend"][i]:
        return "broken"
    return None


def leg_beyond(E, ST):
    """the study's 'trades beyond the leg' bar for every completed leg (mtfl_marker.legs_of)."""
    out = {}
    for S in ST:
        P = S.piv
        for q in range(P["px"].size - 1):
            st, en = P["px"][q], P["px"][q + 1]
            kf = int(S.piv_kf[q + 1])
            if kf >= E.n:
                continue
            hiP, loP = max(st, en), min(st, en)
            j1 = int(E.sp_h.first_ge(np.array([kf]), C.c_gt(np.array([hiP])))[0])
            j2 = int(E.sp_l.first_le(np.array([kf]), C.c_lt(np.array([loP])))[0])
            out[q + 100000 * S.ti] = min(j1, j2)
    return out


def snapshot_levels(E, R, fr05, zones, t, taus=None):
    lv = []
    for L, tag in ((R, None), (fr05, "fib05")):
        for i in range(MK.nrow(L)):
            st = state_at(L, i, t)
            if st is None:
                continue
            fam = FAMC[int(L["fam"][i])]
            ti = int(L["ti"][i])
            if taus is not None and fam not in ("P", "D", "R") and ti not in taus:
                continue
            touches = None
            if fam == "Z":
                z = [zz for zz in zones if zz[0] == ti and zz[1] == int(L["kf"][i])]
                assert len(z) == 1
                touches = z[0][4] + sum(1 for k in z[0][5] if k <= t)
            lv.append([tag or fam, ti, int(L["kind"][i]), fnum(L["lo"][i]), fnum(L["hi"][i]),
                       int(L["kf"][i]), st, touches])
    return lv


def long_golden() -> None:
    """Study on 1m bars LONG_FROM..D_TO; the port's input = 30m bars for all but the last two
    sessions (written here) + the main fixture's 1m bars for the last two."""
    st0 = C.file_stat(C.D0_DB)
    E = C.load_era_from_db(C.D0_DB, "LONG", 0, d_from=LONG_FROM, d_to=D_TO)
    assert C.file_stat(C.D0_DB) == st0, "research DB changed!"
    n = E.n
    last2 = set(E.sess_date[-2:].tolist())
    # 30m bars (09:30-anchored, exact aggregation of the study's 1m bars) for the older sessions
    T30 = E.tb[2]
    lines = ["ts,o,h,l,c"]
    for b in range(T30["nb"]):
        s = int(T30["sess"][b])
        if E.sess_date[s] in last2:
            continue
        lines.append(f"{int(E.ts[T30['start'][b]]) // 1000},{T30['o'][b]:.2f},{T30['h'][b]:.2f},"
                     f"{T30['l'][b]:.2f},{T30['c'][b]:.2f}")
    gz_write(LONG_CSV, ("\n".join(lines) + "\n").encode())
    # the last two sessions must be the main fixture's last two (same bars)
    fx = load_era()
    assert list(fx.sess_date[-2:]) == list(E.sess_date[-2:])
    a0 = int(E.sess_start[-2])
    f0 = int(fx.sess_start[-2])
    assert np.array_equal(E.ts[a0:], fx.ts[f0:]) and np.array_equal(E.h[a0:], fx.h[f0:])
    M = MK.mark(E)
    R = M.real
    fr05 = MK.sub(M.fr, M.fr["kind"] == 1)
    zones = zone_touch_log(E, M.ST)
    lab = {dt.datetime.fromtimestamp(int(t) // 1000, ET).strftime("%Y-%m-%d %H:%M"): j for j, t in enumerate(E.ts)}
    beyond = leg_beyond(E, M.ST)
    leg_levels = [(L, i) for L in (R, fr05) for i in range(MK.nrow(L)) if L["leg"][i] >= 0]
    snaps = []
    for when in LONG_SNAPS:
        t = lab[when]
        assert E.mod[t] % 5 in (2, 3), when
        if any(int(L["exp"][i]) == t == beyond.get(int(L["leg"][i])) for L, i in leg_levels):
            print("  long snapshot", when, "skipped (beyond at t)")
            continue
        lv = snapshot_levels(E, R, fr05, zones, t, taus=LONG_TAUS)
        # the port knows bars by time, not by this era's index: key kf by the known_from instant
        for row in lv:
            kf = row[5]
            row[5] = int(E.ts[kf]) // 1000 if row[0] in ("P", "D", "R") else int(E.ts[kf - 1]) // 1000 + 60
        snaps.append({"label": when, "now_s": int(E.ts[t]) // 1000, "levels": lv})
        print("  long snapshot", when, "levels:", len(lv))
    import collections
    cnt = collections.Counter((FAMC[int(f)], C.TAUS[int(t)]) for f, t in zip(R["fam"], R["ti"]) if int(t) >= 2)
    gold = {"meta": {"from": LONG_FROM, "to": D_TO, "sessions": int(E.S), "n": int(n),
                     "fixture_30m": os.path.basename(LONG_CSV),
                     "fixture_30m_sha256": hashlib.sha256(open(LONG_CSV, "rb").read()).hexdigest(),
                     "missing_sessions": sorted(set(C.nyse_sessions(dt.date.fromisoformat(LONG_FROM),
                                                                     dt.date.fromisoformat(D_TO)))
                                                - set(E.sess_date.tolist())),
                     "taus": [C.TAUS[t] for t in LONG_TAUS],
                     "levels_by_cell": {f"{a}-{b}": v for (a, b), v in sorted(cnt.items())},
                     "snapshot_level_cols": ["fam", "ti", "kind", "lo", "hi", "known_from_s", "state", "touches"]},
            "snapshots": snaps}
    gz_write(LONG_GOLD, json.dumps(gold, separators=(",", ":")).encode())
    print("long golden ->", LONG_GOLD, os.path.getsize(LONG_GOLD), "bytes;", LONG_CSV, os.path.getsize(LONG_CSV),
          "bytes;", E.S, "sessions; missing:", gold["meta"]["missing_sessions"])


def main() -> int:
    if "--keep-fixture" not in sys.argv or not os.path.exists(CSV):
        extract()
    csv_sha = hashlib.sha256(open(CSV, "rb").read()).hexdigest()
    E = load_era()
    n = E.n
    print("era:", n, "bars,", E.S, "sessions")
    M = MK.mark(E)
    R = M.real
    FR = M.fr
    fr05 = MK.sub(FR, FR["kind"] == 1)        # FR_RATIOS[1] == 0.5
    assert abs(MK.FR_RATIOS[1] - 0.5) < 1e-12
    zones = zone_touch_log(E, M.ST)
    zr = MK.sub(R, R["fam"] == C.FAM["Z"])
    got = sorted((int(a), int(b), round(float(c), 6), round(float(d), 6)) for a, b, c, d in
                 zip(zr["ti"], zr["kf"], zr["lo"], zr["hi"]))
    mine = sorted((z[0], z[1], round(z[2], 6), round(z[3], 6)) for z in zones)
    assert got == mine, "zone mirror differs from mtfl_marker.levels_Z"
    # ------------------------------------------------------------------ MIDENTRY (AMENDMENT_1)
    ev = EV.detect_events(E, R, M.ST)
    ti_o, t0_o, d_o, ot_o, lev_o = MID.origins_from_events(ev, 0)
    setups = []
    for ti in range(C.NT):
        m = np.flatnonzero(ti_o == ti)
        if not m.size:
            continue
        S = MID.build_setups(E, M.ST, ti, t0_o[m], d_o[m], ot_o[m])
        S2 = MID.build_setups(E, M.ST, ti, t0_o[m], d_o[m], ot_o[m] * 10_000_000 + np.arange(m.size))
        assert np.array_equal(S["key"], S2["key"]) and np.array_equal(S["t0"], S2["t0"])
        assert np.array_equal(S["otype"], S2["otype"] // 10_000_000)
        def_lev = lev_o[m[S2["otype"] % 10_000_000]]
        fb, fp, fx = MID.fills(E, S, 0.5)
        code, Rv = MID.trade_race(E, ti, fb, fp, S["O"], fx, S["d"])
        code2, R2, rbar, ee = trade_race_bars(E, ti, fb, fp, S["O"], fx, S["d"])
        assert np.array_equal(code, code2)
        assert np.allclose(np.nan_to_num(Rv, nan=-99), np.nan_to_num(R2, nan=-99))
        for i in range(S["O"].size):
            filled = fb[i] < BIG
            setups.append([ti, int(S["d"][i]), int(S["otype"][i]), int(S["t0"][i]), int(S["Ob"][i]),
                           round(float(S["O"][i]), 2), int(S["conf"][i]), int(def_lev[i]),
                           fut(S["tq"][i], n), bool(S["qual"][i]), int(S["last"][i]),
                           int(fb[i]) if filled else None, fnum(fp[i]) if filled else None,
                           fnum(fx[i]) if filled else None, int(code[i]), fnum(Rv[i]),
                           int(rbar[i]) if rbar[i] < BIG else None, int(ee[i]) if ee[i] < BIG else None])
    print("setups:", len(setups), " qualified:", sum(1 for s in setups if s[9]),
          " filled:", sum(1 for s in setups if s[11] is not None))
    # ------------------------------------------------------------------ snapshots
    beyond = leg_beyond(E, M.ST)
    leg_levels = [(L, i) for L in (R, fr05) for i in range(MK.nrow(L))
                  if L["leg"][i] >= 0]
    ts_by_label = {dt.datetime.fromtimestamp(int(t) // 1000, ET).strftime("%Y-%m-%d %H:%M"): j
                   for j, t in enumerate(E.ts)}
    snaps = []
    for lab in SNAP_CANDIDATES:
        for shift in range(0, 30, 5):
            when = (dt.datetime.strptime(lab, "%Y-%m-%d %H:%M") + dt.timedelta(minutes=shift)).strftime("%Y-%m-%d %H:%M")
            t = ts_by_label.get(when)
            if t is None:
                continue
            assert E.mod[t] % 5 in (2, 3), when
            clash = [1 for L, i in leg_levels if int(L["exp"][i]) == t == beyond.get(int(L["leg"][i]))]
            if clash:
                print("  snapshot", when, "skipped: a leg level expires by 'beyond' exactly at t")
                continue
            lv = snapshot_levels(E, R, fr05, zones, t)
            snaps.append({"label": when, "t": int(t), "now_s": int(E.ts[t]) // 1000, "levels": lv})
            print("  snapshot", when, "t =", t, "levels:", len(lv))
            break
    code_sha = {f: hashlib.sha256(open(os.path.join(STUDY, f), "rb").read()).hexdigest()
                for f in ("mtfl_core.py", "mtfl_marker.py", "mtfl_events.py", "mtfl_mid.py")}
    gold = {
        "meta": {"fixture": os.path.basename(CSV), "fixture_sha256": csv_sha, "sessions": int(E.S), "n": int(n),
                 "from": D_FROM, "to": D_TO, "study_code_sha256": code_sha,
                 "generated": dt.datetime.now(ET).isoformat(timespec="seconds"),
                 "level_cols": ["fam", "ti", "kind", "lo", "hi", "kf", "ref", "atr", "exp", "side0", "side",
                                "armb", "brk", "brkc", "actend", "brkend", "leg"],
                 "setup_cols": ["ti", "d", "otype", "t0", "Ob", "O", "conf", "def_level", "tq", "qual", "last",
                                "fb", "entry", "X", "code", "R", "rbar", "e"],
                 "snapshot_level_cols": ["fam", "ti", "kind", "lo", "hi", "kf", "state", "touches"]},
        "levels": level_rows(R, n),
        "fib05": level_rows(fr05, n),
        "zones": zones,
        "setups": setups,
        "snapshots": snaps,
    }
    gz_write(GOLD, json.dumps(gold, separators=(",", ":")).encode())
    print("golden ->", GOLD, os.path.getsize(GOLD), "bytes;", len(gold["levels"]), "levels,",
          len(gold["fib05"]), "fib05,", len(zones), "zones,", len(setups), "setups,", len(snaps), "snapshots")
    long_golden()
    return 0


if __name__ == "__main__":
    sys.exit(main())
