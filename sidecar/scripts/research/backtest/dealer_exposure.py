"""Phase 1: per-minute dealer-exposure reconstruction from OI + chain greeks.

Computes, per session-minute, the aggregate dealer GEX (gamma), gamma-flip strike,
CEX (charm) and VEX (vanna), plus the LONG_GAMMA / SHORT_GAMMA / TRANSITION regime.
Vectorized over the chain with numpy. Sign convention (call +, put -) is the naive
SqueezeMetrics-style one; Phase-1 validation checks it against real day behavior
(SHORT_GAMMA/negative GEX should coincide with trending days, LONG_GAMMA with pins).

Inputs: option_open_interest (start-of-day OI) + option_quote_history (per-minute mids
-> per-strike IV). r=q=0 (0DTE, hours to expiry).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime

import numpy as np
from scipy.special import erf

from .data import ET, Session, connect, load_oi, sessions
from .holdout import assert_dev, dev_range

_S2 = np.sqrt(2.0)
_S2PI = np.sqrt(2 * np.pi)
_YEAR_MIN = 365.0 * 24 * 60
DB = os.path.expanduser("~/.fef-terminal/signal_log.db")


def _cdf(x):
    return 0.5 * (1 + erf(x / _S2))


def _pdf(x):
    return np.exp(-0.5 * x * x) / _S2PI


def _iv(price, S, K, T, is_call, iters=16):
    sig = np.full(K.shape, 0.6)
    sqT = np.sqrt(T)
    for _ in range(iters):
        srt = sig * sqT
        d1 = (np.log(S / K) + 0.5 * sig * sig * T) / srt
        d2 = d1 - srt
        p = np.where(is_call, S * _cdf(d1) - K * _cdf(d2), K * _cdf(-d2) - S * _cdf(-d1))
        vega = S * _pdf(d1) * sqT
        sig = np.clip(sig - (p - price) / np.maximum(vega, 1e-6), 1e-3, 5.0)
    return sig


def _gamma(S, K, T, sig):
    sqT = np.sqrt(T)
    srt = sig * sqT
    d1 = (np.log(S / K) + 0.5 * sig * sig * T) / srt
    return _pdf(d1) / (S * srt)


def _charm_vanna(S, K, T, sig):
    sqT = np.sqrt(T)
    srt = sig * sqT
    d1 = (np.log(S / K) + 0.5 * sig * sig * T) / srt
    d2 = d1 - srt
    pdf = _pdf(d1)
    charm = pdf * d2 / (2 * T)      # r=q=0 simplification, per-year
    vanna = -pdf * d2 / sig
    return charm, vanna


def _chain(sess, oi, ts):
    S = sess.spot(ts)
    if S is None:
        return None
    Ks, cm, pm, coi, poi = [], [], [], [], []
    for K in sess.strikes:
        c = sess.mid(ts, K, "C")
        p = sess.mid(ts, K, "P")
        if c is None or p is None:
            continue
        oc = oi.get((K, "C"), 0)
        op = oi.get((K, "P"), 0)
        if oc <= 0 and op <= 0:
            continue
        Ks.append(K); cm.append(c); pm.append(p); coi.append(oc); poi.append(op)
    if len(Ks) < 6:
        return None
    return (S, np.array(Ks, float), np.array(cm, float), np.array(pm, float),
            np.array(coi, float), np.array(poi, float))


def exposures_at(sess, oi, ts):
    ch = _chain(sess, oi, ts)
    if ch is None:
        return None
    S, K, cm, pm, coi, poi = ch
    now = datetime.fromtimestamp(ts / 1000, ET)
    mins = (now.replace(hour=16, minute=0, second=0, microsecond=0) - now).total_seconds() / 60
    if mins <= 1:
        return None
    T = mins / _YEAR_MIN
    ivc = _iv(cm, S, K, T, True)
    ivp = _iv(pm, S, K, T, False)
    gc = _gamma(S, K, T, ivc); gp = _gamma(S, K, T, ivp)
    cch, vc = _charm_vanna(S, K, T, ivc)
    pch, vp = _charm_vanna(S, K, T, ivp)
    scale = S * S * 0.01 * 100
    gex = float(np.sum(gc * coi - gp * poi) * scale)
    cex = float(np.sum(cch * coi - pch * poi) * 100)
    vex = float(np.sum(vc * coi - vp * poi) * 100)
    # gamma flip: GEX(S') zero-crossing (gamma recomputed at hypothetical spot, IV fixed)
    grid = S + np.linspace(-60, 60, 49)
    gexg = np.array([float(np.sum(_gamma(s2, K, T, ivc) * coi - _gamma(s2, K, T, ivp) * poi) * s2 * s2 * 0.01 * 100)
                     for s2 in grid])
    flip = None
    sg = np.sign(gexg)
    idx = np.where(np.diff(sg) != 0)[0]
    if len(idx):
        i = idx[np.argmin(np.abs(grid[idx] - S))]
        y0, y1 = gexg[i], gexg[i + 1]
        flip = float(grid[i] + (grid[i + 1] - grid[i]) * (0 - y0) / (y1 - y0)) if y1 != y0 else float(grid[i])
    regime = "LONG_GAMMA" if gex > 0 else "SHORT_GAMMA"
    if flip is not None and abs(S - flip) < 5:
        regime = "TRANSITION"
    return {"spot": round(S, 2), "gex": gex, "cex": cex, "vex": vex,
            "gamma_flip": round(flip, 2) if flip is not None else None,
            "flip_dist": round(S - flip, 2) if flip is not None else None,
            "regime": regime, "iv_atm": float(np.median(ivc))}


# ---------------------------------------------------------------------------
# Phase-1 validation: does the reconstruction agree with real day behavior?
# ---------------------------------------------------------------------------
def validate(dates):
    con = connect()
    print(f"{'date':11s} {'open':>7} {'close':>7} {'range':>6}  {'10:00 regime':>13} {'gex@10:00':>13} {'flip@10:00':>10}")
    for d in dates:
        s = Session(con, d)
        if s.empty():
            print(f"{d}  no data"); continue
        oi = load_oi(con, d)
        if not oi:
            print(f"{d}  no OI"); continue
        alls = [x for x in (s.spot(t) for t in s.minutes) if x is not None]
        o = alls[0] if alls else 0
        c = alls[-1] if alls else 0
        rng = max(alls) - min(alls) if alls else 0
        dec = s.minute_at_or_before(s.minutes[0] + 30 * 60000)
        e = exposures_at(s, oi, dec) if dec else None
        if not e:
            print(f"{d}  no exposures"); continue
        print(f"{d} {o:7.0f} {c:7.0f} {rng:6.0f}  {e['regime']:>13} {e['gex']:13.3e} {str(e['gamma_flip']):>10}")
    print("\nEXPECT: trending/large-range days -> SHORT_GAMMA (gex<0); quiet/pinned days -> LONG_GAMMA (gex>0).")
    print("If reversed, flip the sign convention before Phase 3.")


def _ensure_table():
    c = sqlite3.connect(DB)
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("""CREATE TABLE IF NOT EXISTS option_dealer_exposure(
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_date TEXT, ts_ms INTEGER,
        spot REAL, gex REAL, cex REAL, vex REAL, gamma_flip REAL, flip_dist REAL,
        regime TEXT, iv_atm REAL)""")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_dex_unique "
              "ON option_dealer_exposure(session_date, ts_ms)")
    c.commit()
    c.close()


def build():
    """Store the per-minute dealer-exposure series for the DEV set only (Phase 1
    foundation). Resumable; never touches the reserved holdout."""
    _ensure_table()
    start, end = dev_range()
    assert_dev(end)
    con = connect()
    done = {r[0] for r in con.execute("SELECT DISTINCT session_date FROM option_dealer_exposure")}
    todo = [d for d in sessions(con, start, end) if d not in done]
    print(f"dealer-exposure build (DEV): {len(todo)} sessions to do ({len(done)} done)", flush=True)
    for i, d in enumerate(todo):
        s = Session(con, d)
        if s.empty():
            continue
        oi = load_oi(con, d)
        if not oi:
            continue
        rows = []
        for ts in s.minutes:
            e = exposures_at(s, oi, ts)
            if e:
                rows.append((d, ts, e["spot"], e["gex"], e["cex"], e["vex"],
                             e["gamma_flip"], e["flip_dist"], e["regime"], e["iv_atm"]))
        if rows:
            c = sqlite3.connect(DB)
            c.execute("PRAGMA busy_timeout=30000")
            c.executemany("INSERT OR IGNORE INTO option_dealer_exposure"
                          "(session_date,ts_ms,spot,gex,cex,vex,gamma_flip,flip_dist,regime,iv_atm)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            c.commit()
            c.close()
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(todo)} ({d})", flush=True)
    print("dealer-exposure build done", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--build", action="store_true")
    a = ap.parse_args()
    if a.build:
        build()
        return
    if a.validate:
        # reference days across regimes: big trend days vs quiet days
        validate(["2022-06-13", "2022-11-10", "2022-09-13", "2023-02-02",
                  "2023-08-21", "2024-05-15", "2024-12-18", "2025-04-07"])


if __name__ == "__main__":
    main()
