"""engine/structure_levels.py -- the display-only multi-timeframe structure overlay.

GOLDEN (the port must reproduce the `mtflevels` study's own code)
-----------------------------------------------------------------
``fixtures/make_structure_levels_golden.py`` (run by hand) ran executor A's reference code
(D:\\fef-data\\mtflevels\\A: mtfl_core / mtfl_marker / mtfl_events / mtfl_mid) on
``fixtures/structure_levels_qqq_1m.csv.gz`` -- QQQ regular-hours 1-minute bars, 61 complete
sessions 2025-12-15 .. 2026-03-13 (an early close, five holidays, the March DST change) -- and on
two years of 1-minute bars for the 30m-and-up timeframes. Agreement measured when these tests were
written (all asserted below):

* every real level of the era (3,378; all 30 family-tau cells): identity AND every lifecycle field
  (ref, ATR, expiry, side, arming, BREAK bar, ACTIVE end, BROKEN window) -- 100 %;
* the fib-set 0.5 levels (312): 100 %;
* ACTIVE / BROKEN level sets incl. zone touch counts at 12 snapshot times -- 882 / 882;
* MIDENTRY: all 726 setups by identity (tau, direction, O bar), and defining origin, O, t0,
  qualification, fill bar, entry, X, outcome and resolution bar on 725. The one difference is
  documented, not a port error: STUDY ERA-END TRUNCATION -- the study caps a 4H / 1D race at its
  data's last session and scores it UNRESOLVED; live, that race is still running ("filled");
* setup status at the 12 snapshots (forming / unqualified / waiting / filled / expired / won /
  lost, and the trailing X of waiting setups) -- 7,645 / 7,645;
* the route's input shape (1m x 2 sessions + 5m x 20 + 30m + daily, aggregated from the same bars)
  at the last six snapshots -- 458 / 458;
* long horizon (study on 2 years of 1m bars; port fed 30m bars + 1m for the last two sessions),
  30m / 1H / 4H / 1D and P/D/R at the 1H scale, 4 snapshots -- 247 / 247 (4H zones, 1D zone and
  swings included).

The ONE live-vs-study convention difference, and why the snapshots are where they are: the study
expires a fib / leg-midpoint level at the START of the 1-minute bar that trades beyond its leg (one
bar of lookahead). Live, that bar is only known once it has printed. Snapshot minutes are therefore
not multiples of 5 (nothing becomes known exactly at the snapshot) and the generator skips any
snapshot at which a leg level expires by "beyond" exactly at that bar (one was skipped).

Measured, NOT asserted (dev run against the research DB, 2026-10-01): the route's actual windows
(30m bars for 3 months) agree 100 % on every cell except 4H -- S-4H 16/39, Z-4H 0/12, M-4H 0/4 --
because 4H levels can live 300 4H-bars (~150 sessions) and 3 months of 30m bars hold ~63.

Also here: DST-safe anchoring, 1H/4H anchoring, early closes, the round-number step, empty and
short inputs, the payload contract, and speed.
"""

from __future__ import annotations

import gzip
import json
import math
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from fef_sidecar.engine import structure_levels as SL
from fef_sidecar.et_time import ET

FIX = Path(__file__).parent / "fixtures"


# =====================================================================================
# fixture loading
# =====================================================================================
def _read_csv(name: str) -> list[dict]:
    out = []
    with gzip.open(FIX / name, "rt") as f:
        next(f)
        for ln in f:
            a = ln.rstrip("\n").split(",")
            out.append({"time": int(a[0]), "open": float(a[1]), "high": float(a[2]),
                        "low": float(a[3]), "close": float(a[4])})
    return out


@pytest.fixture(scope="module")
def bars() -> list[dict]:
    return _read_csv("structure_levels_qqq_1m.csv.gz")


@pytest.fixture(scope="module")
def gold() -> dict:
    with gzip.open(FIX / "structure_levels_golden.json.gz", "rb") as f:
        return json.loads(f.read())


@pytest.fixture(scope="module")
def gold_long() -> dict:
    with gzip.open(FIX / "structure_levels_golden_long.json.gz", "rb") as f:
        return json.loads(f.read())


@pytest.fixture(scope="module")
def full(bars) -> SL.Analysis:
    """The port on the whole fixture, both P/D/R reaction scales (as the study)."""
    return SL.analyze({"1m": bars}, bars[-1]["time"] + 60, "QQQ", pdr_scales=(0, 3))


def _fut(v, n):
    return None if v is None or v >= n else v


def _lkey(fam, ti, kind, lo, hi, kf):
    return (fam, ti, kind, round(lo, 6), round(hi, 6), kf)


def _agg(bs: list[dict], minutes: int) -> list[dict]:
    """09:30-anchored aggregation of 1m bars (exact OHLC)."""
    out: list[dict] = []
    cur_key = None
    for b in bs:
        et = datetime.fromtimestamp(b["time"], ET)
        mod = et.hour * 60 + et.minute - 570
        key = (et.date(), mod // minutes)
        if key != cur_key:
            out.append({"time": b["time"] - (mod % minutes) * 60, "open": b["open"],
                        "high": b["high"], "low": b["low"], "close": b["close"]})
            cur_key = key
        else:
            c = out[-1]
            c["high"] = max(c["high"], b["high"])
            c["low"] = min(c["low"], b["low"])
            c["close"] = b["close"]
    return out


def _daily(bs: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for b in bs:
        d = datetime.fromtimestamp(b["time"], ET).date().isoformat()
        if d not in out:
            out[d] = {"date": d, "open": b["open"], "high": b["high"], "low": b["low"],
                      "close": b["close"]}
        else:
            c = out[d]
            c["high"] = max(c["high"], b["high"])
            c["low"] = min(c["low"], b["low"])
            c["close"] = b["close"]
    return list(out.values())


# =====================================================================================
# GOLDEN -- the whole era
# =====================================================================================
def test_fixture_integrity(bars, gold) -> None:
    import hashlib
    assert hashlib.sha256((FIX / gold["meta"]["fixture"]).read_bytes()).hexdigest() == \
        gold["meta"]["fixture_sha256"]
    assert len(bars) == gold["meta"]["n"] == 23610
    assert gold["meta"]["sessions"] == 61


def _cmp_levels(study_rows, cols, mine, n, remap=None):
    S = {}
    for r in study_rows:
        d = dict(zip(cols, r))
        S[_lkey(d["fam"], d["ti"], d["kind"], d["lo"], d["hi"], d["kf"])] = d
    M = {}
    for lv in mine:
        if lv.kf >= n:              # the study only knows levels with kf < n (era-end convention)
            continue
        fam, kind = (lv.fam, lv.kind) if remap is None else remap
        M[_lkey(fam, lv.ti, kind, lv.lo, lv.hi, lv.kf)] = lv
    bad = []
    for k in set(S) & set(M):
        d, lv = S[k], M[k]
        mine_f = dict(ref=round(lv.ref, 6), atr=round(lv.atr, 6), exp=_fut(lv.exp, n),
                      side0=lv.side0, side=lv.side, armb=_fut(lv.armb, n), brk=lv.brk,
                      brkc=_fut(lv.brkc, n), actend=_fut(lv.actend, n), brkend=_fut(lv.brkend, n),
                      leg=lv.leg)
        theirs = dict(ref=round(d["ref"], 6), atr=round(d["atr"], 6), exp=d["exp"],
                      side0=d["side0"], side=d["side"], armb=d["armb"], brk=d["brk"],
                      brkc=d["brkc"], actend=d["actend"], brkend=d["brkend"], leg=d["leg"])
        diff = {f: (theirs[f], mine_f[f]) for f in mine_f if theirs[f] != mine_f[f]}
        if diff:
            bad.append((k, diff))
    return sorted(set(S) - set(M)), sorted(set(M) - set(S)), bad, len(S)


def test_golden_every_level_and_lifecycle_identical(full, gold) -> None:
    """All 3,378 real levels of the 30 primary family-tau cells: same set, same lifecycle."""
    only_s, only_m, bad, ns = _cmp_levels(gold["levels"], gold["meta"]["level_cols"], full.levels, full.n)
    assert ns == 3378
    assert only_s == [], only_s[:5]
    assert only_m == [], only_m[:5]
    assert bad == [], bad[:5]
    # every family x tau the study produces is represented, the 4H / 1D ones included
    cells = {(d[0], d[1]) for d in gold["levels"]}
    for need in [("S", 5), ("S", 4), ("Z", 4), ("F", 5), ("M", 5), ("P", 0), ("P", 3), ("D", 3), ("R", 3)]:
        assert need in cells


def test_golden_fib05_identical(full, gold) -> None:
    only_s, only_m, bad, ns = _cmp_levels(gold["fib05"], gold["meta"]["level_cols"], full.fib05,
                                          full.n, remap=("FR", 1))
    assert ns == 312 and only_s == [] and only_m == [] and bad == []


def test_golden_midentry_setups(full, gold, bars) -> None:
    n = full.n
    cols = gold["meta"]["setup_cols"]
    lcols = gold["meta"]["level_cols"]
    S = {(d["ti"], d["d"], d["Ob"]): d for d in (dict(zip(cols, r)) for r in gold["setups"])}
    M = {(st.ti, st.d, st.Ob): st for st in full.setups}
    assert len(S) == 726
    assert set(S) == set(M)
    n_sess = len(full.sessions)
    truncated = []
    for k, d in S.items():
        st = M[k]
        lv = dict(zip(lcols, gold["levels"][d["def_level"]]))
        assert (st.level.fam, st.level.ti, st.level.kind, round(st.level.lo, 6), st.level.kf) == \
            (lv["fam"], lv["ti"], lv["kind"], round(lv["lo"], 6), lv["kf"]), k
        assert (st.otype, st.t0, round(st.O, 2), st.conf, _fut(st.tq, n), st.qual) == \
            (d["otype"], d["t0"], d["O"], d["conf"], d["tq"], d["qual"]), k
        assert (st.fb if st.fb < n else None) == d["fb"], k
        if d["fb"] is None:
            continue
        assert abs(st.entry - d["entry"]) < 1e-9 and abs(st.Xf - d["X"]) < 1e-9, k
        if d["code"] == SL.UNRES and st.code == SL.PEND:
            # STUDY ERA-END TRUNCATION: the study caps a 4H/1D race at its last session; live the
            # race simply has not finished. Only 4H/1D fills whose 5-session horizon runs past
            # the fixture can be in this branch.
            assert st.ti >= SL.TI_4H and full.sess_of(st.fb + 1) + 4 >= n_sess, k
            assert st.status == "filled"
            truncated.append(k)
            continue
        assert st.code == d["code"], (k, st.code, d["code"])
        if d["code"] in (SL.SUCC, SL.FAIL, SL.AMB):
            assert st.rbar == d["rbar"], k
        if d["code"] in (SL.SUCC, SL.FAIL):
            assert abs(st.R - d["R"]) < 1e-6, k
    assert len(truncated) == 1


def test_golden_snapshot_level_sets(bars, gold) -> None:
    """ACTIVE / BROKEN-in-window sets (and zone touch counts) at 12 times, the port fed only the
    bars before each time."""
    total = 0
    for sn in gold["snapshots"]:
        t = sn["t"]
        A = SL.analyze({"1m": bars[:t]}, sn["now_s"], "QQQ", pdr_scales=(0, 3))
        assert A.n == t
        exp = {_lkey(f, ti, kind, lo, hi, kf): (st, tch)
               for f, ti, kind, lo, hi, kf, st, tch in sn["levels"]}
        got = {}
        for lv in A.levels:
            st = lv.state_at(t)
            if st:
                got[_lkey(lv.fam, lv.ti, lv.kind, lv.lo, lv.hi, lv.kf)] = (
                    st, lv.touches if lv.fam == "Z" else None)
        for lv in A.fib05:
            st = lv.state_at(t)
            if st:
                got[_lkey("fib05", lv.ti, 1, lv.lo, lv.hi, lv.kf)] = (st, None)
        assert got == exp, sn["label"]
        total += len(exp)
    assert len(gold["snapshots"]) == 12 and total == 882


def _expected_status(d: dict, t: int) -> str | None:
    """A study setup's status at snapshot t (bars < t complete), from its indices."""
    if d["t0"] + 1 > t:
        return None
    if d["tq"] is None or d["tq"] + 1 > t:
        return "forming"
    if not d["qual"]:
        return "unqualified"
    if d["fb"] is not None and d["fb"] + 1 <= t:
        if d["code"] in (SL.SUCC, SL.FAIL, SL.AMB) and d["rbar"] + 1 <= t:
            return {SL.SUCC: "won", SL.FAIL: "lost", SL.AMB: "lost"}[d["code"]]
        if d["code"] == SL.UNRES and d["e"] is not None and d["e"] + 1 <= t:
            return "expired"
        return "filled"
    if d["last"] + 1 <= t:
        return "expired"
    return "waiting"


def test_golden_snapshot_setup_statuses(bars, gold) -> None:
    cols = gold["meta"]["setup_cols"]
    setups = [dict(zip(cols, r)) for r in gold["setups"]]
    checked = waiting = 0
    for sn in gold["snapshots"]:
        t = sn["t"]
        A = SL.analyze({"1m": bars[:t]}, sn["now_s"], "QQQ", pdr_scales=(0, 3))
        mine = {(st.ti, st.d, st.Ob): st for st in A.setups}
        known = set()
        for d in setups:
            es = _expected_status(d, t)
            if es is None:
                continue
            k = (d["ti"], d["d"], d["Ob"])
            known.add(k)
            assert k in mine, (sn["label"], k)
            assert mine[k].status == es, (sn["label"], k, mine[k].status, es)
            if es == "waiting":
                seg = bars[d["Ob"]:t]
                x = max(b["high"] for b in seg) if d["d"] == 1 else min(b["low"] for b in seg)
                assert abs(mine[k].X - x) < 1e-9
                waiting += 1
            checked += 1
        assert set(mine) == known, sn["label"]          # nothing the study does not know yet
    assert checked == 7645 and waiting >= 10


# =====================================================================================
# GOLDEN -- the input shapes the route actually supplies
# =====================================================================================
def test_golden_mixed_resolution_like_the_route(bars, gold) -> None:
    """1m for the last 2 sessions, 5m for the 20 before, 30m and daily for everything older --
    aggregated from the same bars -- must give the study's sets (last six snapshots)."""
    sd = [datetime.fromtimestamp(b["time"], ET).date() for b in bars]
    dates = sorted(set(sd))
    total = 0
    for sn in gold["snapshots"][-6:]:
        t = sn["t"]
        today = sd[t]
        di = dates.index(today)
        d1 = set(dates[di - 1: di + 1])
        d5 = set(dates[di - 20: di])
        hist = [(b, d) for b, d in zip(bars[:t], sd[:t])]
        inp = {"1m": [b for b, d in hist if d in d1],
               "5m": _agg([b for b, d in hist if d in d5], 5),
               "30m": _agg([b for b, d in hist if d != today], 30),
               "1D": _daily([b for b, d in hist if d != today])}
        A = SL.analyze(inp, sn["now_s"], "QQQ", pdr_scales=(0, 3))
        assert {se.res for se in A.sessions} == {1, 5, 30}
        exp = {}
        for f, ti, kind, lo, hi, kf, st, tch in sn["levels"]:
            kfs = bars[kf]["time"] if f in ("P", "D", "R") else bars[kf - 1]["time"] + 60
            exp[(f, ti, kind, round(lo, 6), round(hi, 6), kfs)] = (st, tch)
        got = {}
        for lv in A.levels:
            st = lv.state_at(A.n)
            if st:
                got[(lv.fam, lv.ti, lv.kind, round(lv.lo, 6), round(lv.hi, 6), A.known_from_s(lv))] = (
                    st, lv.touches if lv.fam == "Z" else None)
        for lv in A.fib05:
            st = lv.state_at(A.n)
            if st:
                got[("fib05", lv.ti, 1, round(lv.lo, 6), round(lv.hi, 6), A.known_from_s(lv))] = (st, None)
        assert got == exp, sn["label"]
        total += len(exp)
    assert total == 458


def test_golden_long_horizon_30m_and_up(bars, gold_long) -> None:
    """Two years of 30m bars + 1m for the last two sessions vs the study on two years of 1m bars:
    30m / 1H / 4H / 1D levels and P/D/R at the 1H scale. (P/D/R at the 5m scale need a 5m series,
    which this input only has for its last two sessions -- covered by the tests above.)"""
    b30 = _read_csv("structure_levels_qqq_30m_long.csv.gz")
    sd = [datetime.fromtimestamp(b["time"], ET).date() for b in bars]
    last2 = sorted(set(sd))[-2:]
    b1 = [b for b, d in zip(bars, sd) if d in last2]
    total = 0
    zones_4h_1d = 0
    for sn in gold_long["snapshots"]:
        now = sn["now_s"]
        A = SL.analyze({"30m": b30, "1m": [b for b in b1 if b["time"] < now]}, now, "QQQ",
                       taus=("30m", "1H", "4H", "1D"), pdr_scales=(0, 3))
        exp = {(f, ti, kind, round(lo, 6), round(hi, 6), kfs): (st, tch)
               for f, ti, kind, lo, hi, kfs, st, tch in sn["levels"]
               if not (f in ("P", "D", "R") and ti == SL.TI_5M)}
        got = {}
        for lv in A.levels:
            st = lv.state_at(A.n)
            if st and not (lv.fam in ("P", "D", "R") and lv.ti == SL.TI_5M):
                got[(lv.fam, lv.ti, lv.kind, round(lv.lo, 6), round(lv.hi, 6), A.known_from_s(lv))] = (
                    st, lv.touches if lv.fam == "Z" else None)
        for lv in A.fib05:
            st = lv.state_at(A.n)
            if st:
                got[("fib05", lv.ti, 1, round(lv.lo, 6), round(lv.hi, 6), A.known_from_s(lv))] = (st, None)
        assert got == exp, sn["label"]
        total += len(exp)
        zones_4h_1d += sum(1 for k in exp if k[0] == "Z" and k[1] >= SL.TI_4H)
    assert total == 247 and zones_4h_1d >= 8


# =====================================================================================
# synthetic sessions for the unit tests
# =====================================================================================
def _session(d: date, *, base: float = 100.0, close_hm: tuple[int, int] = (16, 0),
             extra_before: int = 0, extra_after: int = 0, wiggle: float = 0.4,
             phase: float = 0.0) -> list[dict]:
    """1m bars 09:30 ET -> close (bar starts), deterministic wave; optional out-of-RTH bars."""
    o = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
    end = datetime(d.year, d.month, d.day, close_hm[0], close_hm[1], tzinfo=ET)
    out = []
    t = o - timedelta(minutes=extra_before)
    stop = end + timedelta(minutes=extra_after)
    k = 0
    while t < stop:
        p = base + 3.0 * math.sin((k + phase) / 37.0) + wiggle * math.sin((k + phase) / 3.0)
        p = round(p, 2)
        out.append({"time": int(t.timestamp()), "open": p, "high": round(p + 0.07, 2),
                    "low": round(p - 0.06, 2), "close": round(p + 0.02, 2)})
        t += timedelta(minutes=1)
        k += 1
    return out


def _sessions(days: list[date], **kw) -> list[dict]:
    out = []
    for i, d in enumerate(days):
        out += _session(d, phase=i * 390.0, **kw)
    return out


def _hm(ts: int) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%H:%M")


def _weekdays(start: date, k: int) -> list[date]:
    out = []
    d = start
    while len(out) < k:
        if SL.is_session_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


# =====================================================================================
# anchoring: DST, 1H / 4H, early closes
# =====================================================================================
@pytest.mark.parametrize("before, after", [
    (date(2026, 3, 6), date(2026, 3, 9)),     # EST -> EDT (2026-03-08)
    (date(2025, 10, 31), date(2025, 11, 3)),  # EDT -> EST (2025-11-02)
])
def test_dst_safe_session_anchoring(before: date, after: date) -> None:
    """Sessions are cut at 09:30-16:00 ET on both sides of a DST change; bars an hour off (what a
    fixed UTC offset would keep) are dropped."""
    bs = _session(before, extra_before=90, extra_after=90) + _session(after, extra_before=90, extra_after=90)
    A = SL.analyze({"1m": bs}, bs[-1]["time"] + 3600, "QQQ")
    assert [se.date for se in A.sessions] == [before, after]
    for se in A.sessions:
        assert se.complete and se.end - se.start + 1 == 390
        assert _hm(A.t0[se.start]) == "09:30" and _hm(A.t1[se.end]) == "16:00"
    T = A.taus[SL.TI_1H]
    assert [_hm(A.t0[s]) for s in T.start] == ["09:30", "10:30", "11:30", "12:30", "13:30",
                                               "14:30", "15:30"] * 2
    # the UTC offset really changed between the two sessions
    off = [datetime.fromtimestamp(A.t0[se.start], ET).utcoffset() for se in A.sessions]
    assert off[0] != off[1]


def test_1h_and_4h_anchoring_and_aggregation() -> None:
    bs = _session(date(2026, 3, 11))
    A = SL.analyze({"1m": bs}, bs[-1]["time"] + 60, "QQQ")
    T1, T4, TD = A.taus[SL.TI_1H], A.taus[SL.TI_4H], A.taus[SL.TI_1D]
    assert [(_hm(A.t0[s]), _hm(A.t1[e])) for s, e in zip(T1.start, T1.end)] == [
        ("09:30", "10:30"), ("10:30", "11:30"), ("11:30", "12:30"), ("12:30", "13:30"),
        ("13:30", "14:30"), ("14:30", "15:30"), ("15:30", "16:00")]
    assert [(_hm(A.t0[s]), _hm(A.t1[e])) for s, e in zip(T4.start, T4.end)] == [
        ("09:30", "13:30"), ("13:30", "16:00")]
    assert TD.nb == 1 and TD.start == [0] and TD.end == [389]
    # OHLC of the first 4H bar = the first 240 minutes
    seg = bs[:240]
    assert T4.o[0] == seg[0]["open"] and T4.c[0] == seg[-1]["close"]
    assert T4.h[0] == max(b["high"] for b in seg) and T4.l[0] == min(b["low"] for b in seg)
    # 30m path bars build the same 1H / 4H bars
    A30 = SL.analyze({"30m": _agg(bs, 30)}, bs[-1]["time"] + 60, "QQQ")
    for ti in (SL.TI_30M, SL.TI_1H, SL.TI_4H, SL.TI_1D):
        a, b = A.taus[ti], A30.taus[ti]
        assert (a.o, a.h, a.l, a.c) == (b.o, b.h, b.l, b.c)
    assert A30.taus[SL.TI_5M].nb == 0 and A30.taus[SL.TI_15M].nb == 0   # too coarse for them


def test_early_close_session_truncates() -> None:
    """2025-11-28 closes 13:00: extended-hours bars after 13:00 (use_rth=False data) are dropped,
    1H ends 12:30-13:00, 4H is one bar, the session is complete at 13:00."""
    d = date(2025, 11, 28)
    assert SL.close_minute(d) == 780
    bs = _session(d, close_hm=(16, 0))                    # bars run on to 16:00 (ETH)
    A = SL.analyze({"1m": bs}, int(datetime(2025, 11, 28, 13, 1, tzinfo=ET).timestamp()), "QQQ")
    se = A.sessions[0]
    assert se.complete and se.close == 210 and se.end - se.start + 1 == 210
    assert not A.in_session
    T1, T4 = A.taus[SL.TI_1H], A.taus[SL.TI_4H]
    assert [(_hm(A.t0[s]), _hm(A.t1[e])) for s, e in zip(T1.start, T1.end)][-1] == ("12:30", "13:00")
    assert T1.nb == 4 and T4.nb == 1 and _hm(A.t1[T4.end[0]]) == "13:00"
    # 30m bars, ETH included, on the early close: only 7 regular-hours bars survive
    A30 = SL.analyze({"30m": _agg(bs, 30)}, bs[-1]["time"] + 3600, "QQQ")
    assert A30.n == 7 and A30.sessions[0].complete


def test_holiday_bars_are_ignored_and_in_progress_session() -> None:
    days = [date(2025, 12, 24), date(2025, 12, 26)]       # 12-25 is a holiday
    bs = _session(days[0], close_hm=(13, 0)) + _session(date(2025, 12, 25)) + _session(days[1])
    now = int(datetime(2025, 12, 26, 11, 15, 30, tzinfo=ET).timestamp())
    A = SL.analyze({"1m": bs}, now, "QQQ")
    assert [se.date for se in A.sessions] == days
    cur = A.sessions[-1]
    assert not cur.complete and A.in_session and cur.cov_end == 105       # 09:30 .. 11:14 done
    assert _hm(A.asof) == "11:15"
    # the forming 1H bar (10:30-11:30) starts at its first 1m bar; the 15m one (11:15-11:30)
    # started by the clock with no bar complete yet
    assert _hm(A.t0[A.taus[SL.TI_1H].forming_start]) == "10:30"
    assert A.taus[SL.TI_15M].forming_start == A.n
    assert A.sess_end_idx(len(A.sessions) - 1) == cur.start + 389
    # a level whose 300-bar max age ends at the forming bar's start is already expired
    T = A.taus[SL.TI_15M]
    assert T.start_of(T.nb) == A.n and T.start_of(T.nb + 1) == SL._BIG


# =====================================================================================
# round numbers
# =====================================================================================
@pytest.mark.parametrize("sym, price, step", [
    ("QQQ", 600.0, 5.0), ("QQQ", 300.0, 5.0),      # the study's $5, whatever the price
    ("SPY", 760.0, 5.0),
    ("SPX", 7600.0, 50.0), ("ES", 7600.0, 50.0),
    ("NQ", 30000.0, 250.0),
    ("IWM", 250.0, 2.0), ("GLD", 380.0, 2.5), ("TLT", 90.0, 1.0),
])
def test_round_number_step(sym: str, price: float, step: float) -> None:
    assert SL.round_step(sym, price) == step


def test_round_levels_spx_scale_are_multiples_of_50() -> None:
    days = _weekdays(date(2026, 1, 5), 20)
    bs = _sessions(days, base=7600.0, wiggle=6.0)
    for b in bs:            # SPX-sized swings
        for k in ("open", "high", "low", "close"):
            b[k] = round(7600.0 + (b[k] - 7600.0) * 8.0, 2)
    P = SL.compute({"1m": bs[:-200]}, bs[-200]["time"], "SPX")
    assert P["status"] == "ok" and P["round_step"] == 50.0
    rounds = [r for r in P["levels"] if r["kind"] == "round"]
    assert rounds and all(abs(r["price"] / 50.0 - round(r["price"] / 50.0)) < 1e-9 for r in rounds)
    a1d = P["atr"]["1D"]                     # today is in progress: = the prior session's ATR
    A = SL.analyze({"1m": bs[:-200]}, bs[-200]["time"], "SPX")
    op = A.o[A.sessions[-1].start]
    assert all(abs(r["price"] - op) <= 2 * a1d + 1e-6 for r in rounds)


# =====================================================================================
# empty / short / bad inputs, and the mock provider's bars
# =====================================================================================
def test_empty_and_short_inputs_do_not_crash() -> None:
    P = SL.compute({}, 1_773_000_000, "QQQ")
    assert P["status"] == "no regular-hours bars" and P["levels"] == [] and P["setups"] == []
    P = SL.compute({"1m": []}, 1_773_000_000, "QQQ")
    assert P["levels"] == []
    bs = _session(date(2026, 3, 11))[:20]                  # 20 minutes: no ATR anywhere yet
    P = SL.compute({"1m": bs}, bs[-1]["time"] + 60, "QQQ")
    assert P["status"] == "ok" and P["levels"] == [] and P["groups"] == [] and P["setups"] == []
    assert all(v is None for v in P["atr"].values())
    one_day = _session(date(2026, 3, 11))
    P = SL.compute({"1m": one_day}, one_day[-1]["time"] + 60, "QQQ")
    assert P["status"] == "ok"
    # only extended-hours bars -> nothing
    eth = _session(date(2026, 3, 11), extra_before=120)[:120]
    assert SL.compute({"1m": eth}, eth[-1]["time"] + 60, "QQQ")["status"] == "no regular-hours bars"


def test_garbage_bars_are_skipped() -> None:
    good = _session(date(2026, 3, 11))
    bad = [{"time": "x"}, {"open": 1}, {"time": good[5]["time"], "open": float("nan"), "high": 1,
                                        "low": 1, "close": 1},
           {"time": good[6]["time"], "open": -1, "high": 1, "low": 1, "close": 1}, None]
    ms = [dict(b, time=b["time"] * 1000) for b in good]          # milliseconds slipped in
    P = SL.compute({"1m": [b for b in bad if b is not None] + ms, "7m": good, "weird": good},
                   good[-1]["time"] + 60, "QQQ")
    assert P["status"] == "ok" and P["sessions"] == 1
    assert SL.parse_resolution("5 mins") == 5 and SL.parse_resolution("1 day") == 0
    assert SL.parse_resolution("1 hour") == 60 and SL.parse_resolution("tick") is None


def test_missing_minutes_are_filled_flat_up_to_a_cap() -> None:
    """IBKR omits a TRADES bar when nothing printed: a few gaps are filled flat at the last price
    (the session keeps its 1m path); too many and the session falls back to coarser bars."""
    d = date(2026, 3, 11)
    full_day = _session(d)
    few = [b for i, b in enumerate(full_day) if i not in (0, 100, 389)]      # 3 gaps incl. both ends
    A = SL.analyze({"1m": few, "5m": _agg(full_day, 5)}, full_day[-1]["time"] + 60, "QQQ")
    se = A.sessions[0]
    assert se.res == 1 and se.complete and A.n == 390
    assert A.o[0] == A.h[0] == A.l[0] == A.c[0] == full_day[1]["open"]       # opening gap: first print
    assert A.h[100] == A.l[100] == A.c[100] == full_day[99]["close"]          # interior gap: flat
    many = [b for i, b in enumerate(full_day) if i % 40 != 7]                # 10 gaps > cap (7)
    A = SL.analyze({"1m": many, "5m": _agg(full_day, 5)}, full_day[-1]["time"] + 60, "QQQ")
    assert A.sessions[0].res == 5 and A.n == 78
    # in progress: an interior gap does not freeze the path at the gap
    now = full_day[200]["time"]
    part = [b for i, b in enumerate(full_day[:200]) if i != 150]
    A = SL.analyze({"1m": part}, now, "QQQ")
    assert A.in_session and A.n == 200 and A.asof == now


def test_forming_bars_and_partial_daily_are_not_used() -> None:
    bs = _session(date(2026, 3, 11))
    now = bs[100]["time"] + 30                     # mid-minute: bar 100 still forming
    daily = [{"date": "2026-03-11", "open": 1.0, "high": 999.0, "low": 0.5, "close": 2.0}]
    A = SL.analyze({"1m": bs, "1D": daily}, now, "QQQ")
    assert A.n == 100 and A.in_session
    assert max(A.h) < 999                          # today's partial daily bar ignored


@pytest.mark.asyncio
async def test_mock_provider_spx_scale_bars_do_not_crash() -> None:
    """The mock provider's synthetic bars (naive local timestamps, SPX ~7400 whatever the
    symbol) must give a payload, not an exception."""
    from fef_sidecar.api import _sl_daily, _sl_intraday
    from fef_sidecar.ibkr.mock import MockIBKRProvider
    from fef_sidecar.ibkr.models import Contract

    p = MockIBKRProvider()
    inp = {}
    for key, bar_size, dur in (("1m", "1 min", "2 D"), ("5m", "5 mins", "20 D"),
                               ("30m", "30 mins", "3 M")):
        inp[key] = _sl_intraday(await p.fetch_historical_bars(
            Contract.for_symbol("QQQ"), bar_size=bar_size, duration=dur))
    inp["1D"] = _sl_daily(await p.fetch_historical_bars(Contract.for_symbol("QQQ"),
                                                        bar_size="1 day", duration="2 Y"))
    for sym in ("QQQ", "SPX"):
        P = SL.compute(inp, int(time.time()), sym)
        assert isinstance(P["status"], str) and isinstance(P["levels"], list)


# =====================================================================================
# the payload contract
# =====================================================================================
LEVEL_KINDS = {"swing_high", "swing_low", "zone", "mid_leg", "mid_range", "fib_0.5", "fib_0.618",
               "fib_0.75", "fib_0.87", "P", "R1", "S1", "R2", "S2", "PDH", "PDL", "PDC", "PWH",
               "PWL", "round"}


def _snap(gold: dict, label: str) -> dict:
    return next(sn for sn in gold["snapshots"] if sn["label"] == label)


def test_payload_contract_at_a_snapshot(bars, gold) -> None:
    sn = _snap(gold, "2026-03-09 13:28")            # the first session after the DST change
    t = sn["t"]
    P = SL.compute({"1m": bars[:t]}, sn["now_s"], "QQQ")
    assert P["status"] == "ok" and P["asof"] == sn["now_s"] and P["in_session"]
    assert P["last_price"] == bars[t - 1]["close"]
    assert set(P["atr"]) == set(SL.TAUS) and all(v > 0 for v in P["atr"].values())
    ids = set()
    for r in P["levels"]:
        for k in ("id", "family", "tf", "kind", "lo", "hi", "price", "known_from", "state", "label",
                  "group", "role"):
            assert k in r
        assert r["kind"] in LEVEL_KINDS and r["tf"] in SL.TAUS
        assert r["state"] in ("active", "broken") and r["known_from"] <= P["asof"]
        assert r["id"] not in ids
        ids.add(r["id"])
        if r["family"] in ("P", "D", "R"):
            assert r["tf"] == "1D" and r["scale"] == "1H"
        if r["family"] == "Z":
            assert r["touches"] >= 3 and r["lo"] < r["hi"] and r["group"] is None
        else:
            assert r["lo"] == r["hi"] and r["group"] is not None
    # P/D/R once each (the 1H-scale instance), not twice
    pdr = [(r["kind"], r["price"]) for r in P["levels"] if r["family"] in ("P", "D", "R")]
    assert len(pdr) == len(set(pdr)) and any(k == "PDH" for k, _ in pdr)
    # groups: members exist, within 0.25 ATR_5m of each other, labels merged
    radius = 0.25 * P["atr"]["5m"]
    by_id = {r["id"]: r for r in P["levels"]}
    for g in P["groups"]:
        prices = [by_id[m]["price"] for m in g["members"]]
        assert max(prices) - min(prices) <= radius + 1e-9
        assert all(by_id[m]["group"] == g["id"] for m in g["members"])
        assert g["label"] and g["confluence"] >= 1
    assert any(g["confluence"] >= 2 for g in P["groups"])
    # setups: only waiting / filled, or recent
    for s in P["setups"]:
        assert s["status"] in ("waiting", "filled") or s["recent"]
        assert s["direction"] in ("long", "short") and s["origin"] in SL.ORIGINS
        assert set(s["deeper"]) == {"0.618", "0.75", "0.87"}
        lo, hi = sorted((s["O"], s["X"]))
        assert lo <= s["M"] <= hi
        if s["status"] == "waiting":
            assert abs(s["M"] - 0.5 * (s["O"] + s["X"])) < 1e-6
    assert any(s["status"] == "waiting" for s in P["setups"])


def test_payload_filters(bars, gold) -> None:
    sn = _snap(gold, "2026-03-09 13:28")
    t = sn["t"]
    P = SL.compute({"1m": bars[:t]}, sn["now_s"], "QQQ", taus=("1H",), families=("S", "D"),
                   with_setups=False)
    fams = {r["family"] for r in P["levels"]}
    assert fams <= {"S", "D"} and "S" in fams and "D" in fams
    assert {r["tf"] for r in P["levels"] if r["family"] == "S"} == {"1H"}
    assert P["setups"] == []
    P2 = SL.compute({"1m": bars[:t]}, sn["now_s"], "QQQ", taus=("15m",), families=("F",))
    kinds = {r["kind"] for r in P2["levels"]}
    assert "fib_0.5" in kinds and kinds <= {"fib_0.5", "fib_0.618", "fib_0.75", "fib_0.87"}
    assert all(s["tf"] == "15m" for s in P2["setups"])


def test_pending_next_session_levels_after_the_close(bars) -> None:
    """Between sessions the study has no P/D/R active; the next session's are listed 'pending'
    (prices from the last session; P and D exact)."""
    now = bars[-1]["time"] + 3600 * 5                       # Friday 2026-03-13, 21:00 ET
    P = SL.compute({"1m": bars}, now, "QQQ", families=("P", "D", "R"))
    assert not P["in_session"]
    pend = [r for r in P["levels"] if r["state"] == "pending"]
    assert pend and P["next_session_open"] == int(datetime(2026, 3, 16, 9, 30, tzinfo=ET).timestamp())
    A = SL.analyze({"1m": bars}, now, "QQQ")
    T1 = A.taus[SL.TI_1D]
    H, L, C = T1.h[-1], T1.l[-1], T1.c[-1]
    by_kind = {r["kind"]: r["price"] for r in pend}
    assert abs(by_kind["P"] - round((H + L + C) / 3.0, 4)) < 1e-9
    assert by_kind.get("PDH", H) == H and by_kind.get("PDL", L) == L
    assert "PWH" in by_kind or "PWL" in by_kind           # Monday opens a new week
    assert all(r["known_from"] == P["next_session_open"] for r in pend)
    # in session: nothing pending
    P2 = SL.compute({"1m": bars[:-100]}, bars[-100]["time"], "QQQ")
    assert all(r["state"] != "pending" for r in P2["levels"])


def test_merge_labels() -> None:
    assert SL.merge_labels(["PWL", "4H SwL", "1H SwL", "30m .618"]) == "PWL · 4H/1H SwL · 30m .618"
    assert SL.merge_labels(["30m .5", "30m mid"]) == "30m .5"
    assert SL.merge_labels(["1H mid", "15m .5"]) == "1H mid · 15m .5"
    assert SL.merge_labels(["$610", "15m rng mid"]) == "$610 · 15m rng mid"


def test_speed_on_the_routes_input_size(bars) -> None:
    """2 years of daily, 3 months of 30m, 20 sessions of 5m, 2 sessions of 1m: well under 1 s."""
    b30 = _read_csv("structure_levels_qqq_30m_long.csv.gz")
    sd = [datetime.fromtimestamp(b["time"], ET).date() for b in bars]
    dates = sorted(set(sd))
    t = len(bars) - 150
    today = sd[t]
    di = dates.index(today)
    d30 = sorted({datetime.fromtimestamp(b["time"], ET).date() for b in b30})
    old = set(d30[:-63])
    inp = {"1D": _daily([b for b in b30 if datetime.fromtimestamp(b["time"], ET).date() in old])
           + _daily([b for b, d in zip(bars[:t], sd[:t]) if d != today]),
           "30m": [b for b in b30 if datetime.fromtimestamp(b["time"], ET).date() not in old],
           "5m": _agg([b for b, d in zip(bars, sd) if d in set(dates[di - 20: di])], 5),
           "1m": [b for b, d in zip(bars[:t], sd[:t]) if d in set(dates[di - 1: di + 1])]}
    t0 = time.perf_counter()
    P = SL.compute(inp, bars[t]["time"], "QQQ")
    dt_ = time.perf_counter() - t0
    assert P["status"] == "ok" and P["sessions"] > 450 and P["levels"]
    assert dt_ < 1.0, dt_
