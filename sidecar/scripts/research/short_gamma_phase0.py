"""Phase 0 — short-gamma directional continuation test (Chat spec 2026-05-29).

Tests whether a directional commitment in the direction of an established move,
taken in SHORT_GAMMA regimes, has edge. Signal = regime + directional commitment
(not a generic momentum signal gated by regime).

Exits (a priori, each a ledger variant): ATR 2:1 baseline; trailing 1xATR;
gamma-flip-target (mechanism-matched: ride to the flip where short-gamma fuel
exhausts, fall back to trailing when the flip is behind the move).
Controls (must beat all): buy-the-move (regime-blind), random-direction,
long-gamma comparison (same signal in LONG_GAMMA — hypothesis predicts negative).

Stratified: INTRADAY-exposure sessions (precise) vs single-EOD-snapshot sessions
(weak secondary) — NEVER pooled. R in ATR-risk units. Forward-walk on 1-min SPX.

Run:  python -m scripts.research.short_gamma_phase0 [--friction-pts 2.0]
"""

from __future__ import annotations

import argparse
import bisect
import random
import sqlite3
import statistics as stats
from collections import defaultdict

from fef_sidecar.engine.signals import gamma_regime as gr
from fef_sidecar.engine.signals import short_gamma_continuation as sgc
from fef_sidecar.engine.signals.dale import Bar, CALLS
from scripts.research.dale_vwap_backtest import DB, load_bars, et_date, stat_block, sharpe
from scripts.research.dale_vwap_backtest_v2 import atr_series, walk

RNG = random.Random(7)
INTRADAY_MIN_SNAPS = 5
# Risk unit = user-locked directional-long 1R = 7 SPX pts. (1-min ATR ~2-3pt is
# far too tight: friction/ATR then dominates and stop-out rate is unrealistic.)
RISK_PTS = 7.0


def build_regime_lookup(c, session_date):
    """Return regime_lookup(ts, spot) using the nearest GAMMA snapshot at/before
    ts (flip + net_gex precomputed), classified against the intraday spot."""
    snaps = c.execute(
        "SELECT DISTINCT timestamp_ms FROM exposure_chain_point "
        "WHERE greek_type='GAMMA' AND session_date=? ORDER BY timestamp_ms", (session_date,),
    ).fetchall()
    tss = [s[0] for s in snaps]
    flips: dict[int, tuple] = {}
    for ts in tss:
        rows = c.execute("SELECT strike,net_m FROM exposure_chain_point WHERE greek_type='GAMMA' "
                         "AND session_date=? AND timestamp_ms=?", (session_date, ts)).fetchall()
        res = gr.classify([(r[0], r[1]) for r in rows], spot=0.0)  # spot irrelevant for flip/net
        flips[ts] = (res.gamma_flip, res.net_gex_m)

    def lookup(ts_ms, spot):
        if not tss:
            return None
        j = bisect.bisect_right(tss, ts_ms) - 1
        if j < 0:
            j = 0  # before first snapshot -> use earliest (session-level fallback)
        flip, net = flips[tss[j]]
        if flip is None:
            return gr.LONG_GAMMA if net > 0 else gr.SHORT_GAMMA
        if abs(spot - flip) <= gr.DEFAULT_TRANSITION_BAND:
            return gr.TRANSITION
        if spot > flip and net > 0:
            return gr.LONG_GAMMA
        if spot < flip or net < 0:
            return gr.SHORT_GAMMA
        return gr.SHORT_GAMMA

    def flip_at(ts_ms):
        if not tss:
            return None
        j = max(0, bisect.bisect_right(tss, ts_ms) - 1)
        return flips[tss[j]][0]

    return lookup, flip_at


def walk_trail(entry, risk, is_long, bars, start_idx, horizon):
    stop = entry - risk if is_long else entry + risk
    best = entry
    last = None
    for k in range(start_idx, len(bars)):
        b = bars[k]
        if et_date(b.ts_ms) > horizon:
            break
        last = b
        if is_long:
            best = max(best, b.high)
            stop = max(stop, best - risk)
            if b.low <= stop:
                return (stop - entry) / risk
        else:
            best = min(best, b.low)
            stop = min(stop, best + risk)
            if b.high >= stop:
                return (entry - stop) / risk
    if last is None:
        return None
    return ((last.close - entry) if is_long else (entry - last.close)) / risk


def start_index(bars, close_time):
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid].ts_ms < close_time:
            lo = mid + 1
        else:
            hi = mid
    return lo


def score(sig, bars, horizon, flip, exit_kind, friction_pts):
    is_long = sig.direction == CALLS
    entry = sig.price
    risk = RISK_PTS
    close_time = sig.ts_ms + 60_000
    si = start_index(bars, close_time)
    if si >= len(bars):
        return None
    if exit_kind == "fixed2to1":
        stop = entry - risk if is_long else entry + risk
        target = entry + 2 * risk if is_long else entry - 2 * risk
        p = walk(entry, target, stop, is_long, bars, close_time, horizon)
        if p is None:
            return None
        r = p * 2 + (1 - p) * (-1)
    elif exit_kind == "trail":
        r = walk_trail(entry, risk, is_long, bars, si, horizon)
        if r is None:
            return None
    else:  # flip_target (mechanism-matched), fall back to trail when flip is behind
        flip_ahead = flip is not None and ((flip > entry) if is_long else (flip < entry))
        if not flip_ahead:
            r = walk_trail(entry, risk, is_long, bars, si, horizon)
            if r is None:
                return None
        else:
            stop = entry - risk if is_long else entry + risk
            p = walk(entry, flip, stop, is_long, bars, close_time, horizon)
            if p is None:
                return None
            rr = abs(flip - entry) / risk
            r = p * rr + (1 - p) * (-1)
    return r - friction_pts / risk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--friction-pts", type=float, default=2.0)
    args = ap.parse_args()
    c = sqlite3.connect(DB)

    counts = gr.session_snapshot_counts(c)
    spx = load_bars(c, "SPX")
    by_sess = defaultdict(list)
    for b in spx:
        by_sess[et_date(b.ts_ms)].append(b)

    # variants: (name, require_regime, randomize)
    variants = [
        ("short_gamma", gr.SHORT_GAMMA, False),
        ("buy_the_move", None, False),          # control: regime-blind
        ("long_gamma", gr.LONG_GAMMA, False),   # control: opposite regime
        ("random_dir", gr.SHORT_GAMMA, True),   # control: random direction
    ]
    exits = ["fixed2to1", "trail", "flip_target"]

    # results[stratum][variant][exit] = list of R
    results = {s: {v[0]: {e: [] for e in exits} for v in variants} for s in ("INTRADAY", "SINGLE")}

    for sd, bars in by_sess.items():
        if sd not in counts:
            continue
        stratum = "INTRADAY" if counts[sd] >= INTRADAY_MIN_SNAPS else "SINGLE"
        lookup, flip_at = build_regime_lookup(c, sd)
        horizon = sd
        for name, req, rnd in variants:
            rd = (lambda: RNG.random() < 0.5) if rnd else None
            sigs = sgc.detect(bars, lookup, require_regime=req, random_direction=rd)
            for s in sigs:
                for e in exits:
                    r = score(s, bars, horizon, flip_at(s.ts_ms), e, args.friction_pts)
                    if r is not None:
                        results[stratum][name][e].append(r)

    # honest N_trials = variants*exits*strata + ledger
    try:
        n_ledger = c.execute("SELECT COUNT(*) FROM variant_ledger").fetchone()[0]
    except Exception:
        n_ledger = 0
    n_trials = len(variants) * len(exits) * 2 + n_ledger
    # crude N_eff: variants*exits are highly correlated (nested) -> shrink hard
    n_eff = max(4.0, n_trials * 0.25)

    print(f"\n{'='*94}\nPHASE 0 — SHORT-GAMMA DIRECTIONAL CONTINUATION")
    print(f"move>=5pt/15min, cooldown 15min, friction {args.friction_pts}pt, R in ATR units")
    print(f"N_trials={n_trials} N_eff~{n_eff:.0f}  (INTRADAY sessions are the precise test; SINGLE = weak secondary)\n{'='*94}")

    for stratum in ("INTRADAY", "SINGLE"):
        sess = [sd for sd in by_sess if sd in counts and (counts[sd] >= INTRADAY_MIN_SNAPS) == (stratum == "INTRADAY")]
        print(f"\n#### {stratum}  sessions={sorted(sess)}")
        print(f"  {'variant/exit':<28}{'n':>5}{'win%':>7}{'E[R]':>8}{'Shrp':>7}{'bootCI':>16}{'DSR':>8}")
        all_sr = [sharpe(results[stratum][v[0]][e]) for v in variants for e in exits if results[stratum][v[0]][e]]
        var_sr = stats.pvariance(all_sr) if len(all_sr) > 1 else 0.0
        for name, _, _ in variants:
            for e in exits:
                rs = results[stratum][name][e]
                st = stat_block(rs, tf_min=1, n_trials=n_eff, var_sr=var_sr)
                if st.get("n"):
                    ci = st["boot_ci"]
                    print(f"  {name+'/'+e:<28}{st['n']:>5}{st['win_rate']*100:>6.1f}{st['expectancy_r']:>8.3f}"
                          f"{st['sharpe']:>7.2f}{f'[{ci[0]:+.2f},{ci[1]:+.2f}]':>16}{st['dsr']:>8.3f}")
                else:
                    print(f"  {name+'/'+e:<28}{0:>5}  (no signals)")

        # decision: best short_gamma exit vs controls (same exit)
        print(f"  -- gate ({stratum}) --")
        for e in exits:
            sg = results[stratum]["short_gamma"][e]
            if not sg:
                continue
            sge = stats.fmean(sg)
            btm = stats.fmean(results[stratum]["buy_the_move"][e]) if results[stratum]["buy_the_move"][e] else float("nan")
            lg = stats.fmean(results[stratum]["long_gamma"][e]) if results[stratum]["long_gamma"][e] else float("nan")
            rnd = stats.fmean(results[stratum]["random_dir"][e]) if results[stratum]["random_dir"][e] else float("nan")
            beats = sge > btm and sge > rnd and (lg != lg or sge > lg)
            print(f"    exit={e:<12} SG E[R]={sge:+.3f}  vs buy_move={btm:+.3f} long_gamma={lg:+.3f} random={rnd:+.3f}  "
                  f"-> {'beats all controls' if beats else 'FAILS a control'}")

    print(f"\n  >>> NOTE: INTRADAY = 2 sessions only -> severely underpowered; treat as directional read, not a verdict.")
    print("  >>> Gate: needs DSR@N_eff>=0.30 AND beat all 3 controls on intraday data. HUMAN decision point.")


if __name__ == "__main__":
    main()
