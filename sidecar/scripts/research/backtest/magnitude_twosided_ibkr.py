"""COMPREHENSIVE (2022-2026, INDEPENDENT IBKR ruler) re-run of the two-sided-volume magnitude test.
Same logic as magnitude_twosided_validate.py but the forward-move ruler is IBKR SPX 1-min (full
history) instead of QuantData stockPrice (2025-26 only), and results are reported BY YEAR.
Run:  .venv/Scripts/python.exe -m scripts.research.backtest.magnitude_twosided_ibkr [START END]
"""
from __future__ import annotations

import sys

import numpy as np

from .data import connect
from .ibkr_ruler import ibkr_open, ibkr_series
from .magnitude_twosided_validate import MATCH_TOD_H, RNG, _defs, clustered, load_flow, measure
from .thetadata_real_spot_validate import SURGE_Q, WIN_HI, WIN_LO, _et_h

sys.stdout.reconfigure(encoding="utf-8")
HORIZONS = [("+10min", 10), ("+30min", 30)]
GROUPS = [("2022", lambda r: r["year"] == "2022"), ("2023", lambda r: r["year"] == "2023"),
          ("2024", lambda r: r["year"] == "2024"), ("2025", lambda r: r["year"] == "2025"),
          ("2026", lambda r: r["year"] == "2026"), ("ALL", lambda r: True)]


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2022-01-03"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    ic = ibkr_open()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 104)
    print("TWO-SIDED 0DTE VOLUME -> BIG MOVE?  (magnitude; INDEPENDENT IBKR SPX ruler; BY YEAR)")
    print(f"  {start}..{end} ({len(dates)} sess); surge=p{SURGE_Q} intensity, ET {WIN_LO}-{WIN_HI};"
          f" matched control +-30 ET-min")
    print("=" * 104)

    recs = []
    n_sess = n_noib = 0
    for k, d in enumerate(dates):
        fl = load_flow(con, d)
        if fl is None:
            continue
        ts, c_tot, p_tot = fl
        eth = np.array([_et_h(int(t)) for t in ts])
        win = (eth >= WIN_LO) & (eth <= WIN_HI)
        if win.sum() < 10:
            continue
        rs = ibkr_series(ic, d)
        if rs is None:
            n_noib += 1
            continue
        rt, rp = rs
        year = d[:4]
        for dname, (inten, extra) in _defs(c_tot, p_tot).items():
            if len(inten[win]) < 10:
                continue
            p90 = np.percentile(inten, SURGE_Q)
            is_surge = inten >= p90
            if extra is not None:
                is_surge = is_surge & extra
            surge_idx = np.where(win & is_surge)[0]
            ctrl_pool = np.where(win & ~is_surge)[0]
            if len(surge_idx) == 0 or len(ctrl_pool) == 0:
                continue
            pool_eth = eth[ctrl_pool]
            for hname, hmin in HORIZONS:
                rs_s, am_s, sg_s, rs_c, am_c = [], [], [], [], []
                for i in surge_idx:
                    m = measure(rt, rp, int(ts[i]), hmin)
                    if m is None:
                        continue
                    cand = ctrl_pool[np.abs(pool_eth - eth[i]) <= MATCH_TOD_H]
                    cm = measure(rt, rp, int(ts[int(cand[RNG.integers(len(cand))])]), hmin) if len(cand) else None
                    if cm is None:
                        continue
                    rs_s.append(m[0]); am_s.append(m[1]); sg_s.append(m[2])
                    rs_c.append(cm[0]); am_c.append(cm[1])
                if rs_s:
                    recs.append(dict(date=d, year=year, defn=dname, h=hname, n=len(rs_s),
                                     rng_s=float(np.mean(rs_s)), rng_c=float(np.mean(rs_c)),
                                     am_s=float(np.mean(am_s)), am_c=float(np.mean(am_c)),
                                     signed=float(np.mean(sg_s)),
                                     drng=float(np.mean(rs_s) - np.mean(rs_c)),
                                     dam=float(np.mean(am_s) - np.mean(am_c))))
        n_sess += 1
        if (k + 1) % 100 == 0:
            print(f"  ...{k+1}/{len(dates)} ({d}) sess={n_sess} no_ibkr={n_noib}", flush=True)

    print(f"\n  usable sessions: {n_sess}  missing IBKR ruler: {n_noib}\n")

    for dname in ("TWOSIDED", "TOTAL", "BALANCED"):
        print("=" * 104)
        print(f"SIGNAL = {dname}   (rng x = surge_range/matched-control; >1 + sig t = vol expansion)")
        print(f"  {'group':6s} {'horizon':8s} {'#sess':>5s} {'#surge':>6s} {'rngSrg':>7s} {'rngCtl':>7s} "
              f"{'rng x':>6s} {'t_rng':>6s} {'|mv|x':>6s} {'signed':>7s}")
        for glabel, gp in GROUPS:
            for hname, _ in HORIZONS:
                rr = [r for r in recs if r["defn"] == dname and r["h"] == hname and gp(r)]
                if not rr:
                    continue
                rng_s = float(np.mean([r["rng_s"] for r in rr])); rng_c = float(np.mean([r["rng_c"] for r in rr]))
                am_s = float(np.mean([r["am_s"] for r in rr])); am_c = float(np.mean([r["am_c"] for r in rr]))
                _, t_rng, _ = clustered([r["drng"] for r in rr])
                signed = float(np.mean([r["signed"] for r in rr]))
                print(f"  {glabel:6s} {hname:8s} {len(rr):5d} {sum(r['n'] for r in rr):6d} "
                      f"{rng_s:7.3f} {rng_c:7.3f} {(rng_s/rng_c if rng_c else float('nan')):6.2f} {t_rng:+6.2f} "
                      f"{(am_s/am_c if am_c else float('nan')):6.2f} {signed:+7.3f}")
    print("=" * 104)


if __name__ == "__main__":
    main()
