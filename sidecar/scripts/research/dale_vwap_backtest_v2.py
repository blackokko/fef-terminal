"""Phase 0 v2 — redesigned per Claude Chat methodology review (2026-05-29).

Fixes the two flaws that made v1 an unfair test of reversion signals:
  1. SAME-BAR-CLOSE ENTRY (no 2-bar hold / 5-bar delay) — enters at the close of
     the confirming bar, look-ahead-clean, before the initial move is gone.
  2. SIGNAL-MATCHED EXITS (variable R), assigned a priori (no exit-shopping):
       REVERSION (dale_*_pullback, vwap_band_reject_*): target = the mean
         (session VWAP / STD-mean); stop = beyond the invalidating extreme.
       MOMENTUM  (dale_*_cross, vwap_reclaim_long, vwap_loss_short): ATR-scaled
         measured move — stop = 1*ATR, target = 2*ATR.
  3. Momentum vs reversion reported SEPARATELY; reversion stratified by
     trend/chop session (its edge is regime-dependent and the ES sample is
     mostly up-trend).
R is expressed in each trade's OWN risk units (entry->stop) so variable-R trades
are comparable. Friction applied in POINTS then converted per-trade.

Reuses v1's data loaders + methodology stack (DSR @ N_eff, bootstrap).
Run:  python -m scripts.research.dale_vwap_backtest_v2 [--friction-pts 1.5]
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics as stats
from collections import defaultdict
from dataclasses import dataclass, field

from fef_sidecar.engine.signals import dale, vwap
from fef_sidecar.engine.signals.vwap import anchored_vwap, session_key
from scripts.research.dale_vwap_backtest import (
    DB, et_date, daily_bars, load_bars, load_session_regimes, classify_regime,
    sharpe, stat_block, _n_eff, _t1_prob,
)

MOMENTUM = {"dale_bull_cross", "dale_bear_cross", "vwap_reclaim_long", "vwap_loss_short"}
REVERSION = {"dale_bull_pullback", "dale_bear_pullback", "vwap_band_reject_long", "vwap_band_reject_short"}
STOP_BUF = 0.5          # pts beyond the structural extreme
ATR_PERIOD = 14
ATR_STOP_MULT = 1.0
ATR_TARGET_MULT = 2.0
MIN_RISK_PTS = 1.0      # skip degenerate (too-tight) structural stops
MAX_RR = 8.0            # skip absurd reward:risk (bad VWAP-far-away artifacts)


@dataclass
class OutcomeV2:
    ts_ms: int
    kind: str
    family: str
    direction: str
    session: str
    regime: str
    session_kind: str           # TREND / CHOP
    risk_pts: float
    rr: float                   # reward:risk at entry
    r_outcome: float            # in risk units, net of friction
    win: float
    detail: dict = field(default_factory=dict)


def atr_series(bars, period=ATR_PERIOD):
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            trs.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    out = [None] * len(bars)
    for i in range(len(bars)):
        if i + 1 >= period:
            out[i] = sum(trs[i - period + 1: i + 1]) / period
    return out


def session_kinds(spx5) -> dict[str, str]:
    """Classify each ET session TREND vs CHOP from 5-min trend-state coverage."""
    by_sess = defaultdict(list)
    for b in spx5:
        by_sess[et_date(b.ts_ms)].append(b)
    out = {}
    for sd, bars in by_sess.items():
        st = dale.trend_states(bars)
        n = len(st)
        if n == 0:
            out[sd] = "CHOP"; continue
        frac = max(st.count(dale.BULL), st.count(dale.BEAR)) / n
        out[sd] = "TREND" if frac >= 0.5 else "CHOP"
    return out


def walk(entry, target, stop, is_long, inst_1m, close_time, horizon_session):
    lo, hi = 0, len(inst_1m)
    while lo < hi:
        mid = (lo + hi) // 2
        if inst_1m[mid].ts_ms < close_time:
            lo = mid + 1
        else:
            hi = mid
    for k in range(lo, len(inst_1m)):
        b = inst_1m[k]
        if et_date(b.ts_ms) > horizon_session:
            break
        hit_t = (b.high >= target) if is_long else (b.low <= target)
        hit_s = (b.low <= stop) if is_long else (b.high >= stop)
        if hit_t and hit_s:
            return _t1_prob(b.open, target, stop)   # prob target-first
        if hit_t:
            return 1.0
        if hit_s:
            return 0.0
    # timeout: mark-to-last-close as fraction of the way to target
    last = None
    for k in range(len(inst_1m) - 1, lo - 1, -1):
        if et_date(inst_1m[k].ts_ms) <= horizon_session:
            last = inst_1m[k]; break
    if last is None:
        return None
    prog = ((last.close - entry) if is_long else (entry - last.close)) / abs(target - entry)
    return max(0.0, min(1.0, 0.5 + 0.5 * prog))      # soft win prob from progress


def build(c, friction_pts):
    spx, es = load_bars(c, "SPX"), load_bars(c, "ES")
    regimes = load_session_regimes(c)
    spx5, spx30 = dale.resample(spx, 5), dale.resample(spx, 30)
    es5, es30 = dale.resample(es, 5), dale.resample(es, 30)
    spx_daily, es_daily = daily_bars(spx), daily_bars(es)
    skind = session_kinds(spx5)

    ctx = {
        ("SPX", 5): (spx5, spx, spx30, atr_series(spx5), None),
        ("SPX", 30): (spx30, spx, spx_daily, atr_series(spx30), None),
        ("ES", 5): (es5, es, es30, atr_series(es5), anchored_vwap(es5, session_key)),
        ("ES", 30): (es30, es, es_daily, atr_series(es30), anchored_vwap(es30, session_key)),
    }
    idx_map = {key: {b.ts_ms: i for i, b in enumerate(v[0])} for key, v in ctx.items()}

    detectors = [
        ("SPX", 5, dale.detect(spx5, timeframe_min=5)),
        ("SPX", 30, dale.detect(spx30, timeframe_min=30)),
        ("ES", 5, vwap.detect(es5, timeframe_min=5, hold_bars=1, reject_hold_bars=0)),
        ("ES", 30, vwap.detect(es30, timeframe_min=30, hold_bars=1, reject_hold_bars=0)),
    ]

    out: list[OutcomeV2] = []
    for inst, tf, sigs in detectors:
        bars, inst_1m, htf, atr, vw = ctx[(inst, tf)]
        imap = idx_map[(inst, tf)]
        std_means, _ = dale.rolling_mean_std([b.close for b in bars], 20)
        for s in sigs:
            i = imap.get(s.ts_ms)
            if i is None:
                continue
            bar = bars[i]
            is_long = s.direction == dale.CALLS
            entry = s.price
            # --- exit levels by family ---
            if s.kind in REVERSION:
                if s.kind.startswith("vwap"):
                    mean = vw[i].vwap if vw else None
                else:  # dale pullback -> STD mean
                    mean = std_means[i]
                if mean is None:
                    continue
                if is_long:
                    stop = bar.low - STOP_BUF
                    target = mean
                else:
                    stop = bar.high + STOP_BUF
                    target = mean
            else:  # MOMENTUM — ATR measured move
                a = atr[i]
                if a is None or a <= 0:
                    continue
                if is_long:
                    stop = entry - ATR_STOP_MULT * a
                    target = entry + ATR_TARGET_MULT * a
                else:
                    stop = entry + ATR_STOP_MULT * a
                    target = entry - ATR_TARGET_MULT * a
            risk = abs(entry - stop)
            reward = abs(target - entry)
            # validity: target on correct side, sane risk + RR
            if is_long and target <= entry:
                continue
            if (not is_long) and target >= entry:
                continue
            if risk < MIN_RISK_PTS or reward <= 0 or reward / risk > MAX_RR:
                continue

            sd = et_date(s.ts_ms)
            horizon = _next(inst_1m, sd) if tf == 30 else sd
            close_time = s.ts_ms + tf * 60_000
            p = walk(entry, target, stop, is_long, inst_1m, close_time, horizon)
            if p is None:
                continue
            rr = reward / risk
            gross_r = p * rr + (1 - p) * (-1.0)        # expected R in risk units
            fric_r = friction_pts / risk
            out.append(OutcomeV2(
                ts_ms=s.ts_ms, kind=s.kind,
                family="MOMENTUM" if s.kind in MOMENTUM else "REVERSION",
                direction=s.direction, session=sd,
                regime=classify_regime(regimes.get(sd), s.price),
                session_kind=skind.get(sd, "CHOP"),
                risk_pts=risk, rr=rr, r_outcome=gross_r - fric_r, win=p,
                detail={"entry": entry, "target": target, "stop": stop},
            ))
    return out


def _next(bars_1m, sd):
    days = sorted({et_date(b.ts_ms) for b in bars_1m})
    if sd in days and days.index(sd) + 1 < len(days):
        return days[days.index(sd) + 1]
    return sd


def _report(label, ocs, n_eff, var_sr, tf=5):
    rs = [o.r_outcome for o in ocs]
    st = stat_block(rs, tf_min=tf, n_trials=n_eff, var_sr=var_sr)
    if not st.get("n"):
        print(f"  {label:<30} (no signals)"); return st
    ci = st["boot_ci"]
    print(f"  {label:<30}{st['n']:>5}{st['win_rate']*100:>7.1f}{st['expectancy_r']:>8.3f}"
          f"{st['sharpe']:>8.2f}{f'[{ci[0]:+.2f},{ci[1]:+.2f}]':>16}{st['dsr']:>9.3f}")
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--friction-pts", type=float, default=1.5, help="round-trip friction in points")
    args = ap.parse_args()
    c = sqlite3.connect(DB)
    ocs = build(c, args.friction_pts)

    try:
        n_ledger = c.execute("SELECT COUNT(*) FROM variant_ledger").fetchone()[0]
    except Exception:
        n_ledger = 0

    # subsets within each family
    def armed(o): return o.regime == "ACCELERATION"
    groups = {
        "MOMENTUM raw": [o for o in ocs if o.family == "MOMENTUM"],
        "MOMENTUM regime": [o for o in ocs if o.family == "MOMENTUM" and armed(o)],
        "REVERSION raw": [o for o in ocs if o.family == "REVERSION"],
        "REVERSION regime": [o for o in ocs if o.family == "REVERSION" and armed(o)],
        "REVERSION chop-only": [o for o in ocs if o.family == "REVERSION" and o.session_kind == "CHOP"],
        "REVERSION trend-only": [o for o in ocs if o.family == "REVERSION" and o.session_kind == "TREND"],
    }
    n_trials = len(groups) + n_ledger
    setmap = {k: {o.ts_ms for o in v} for k, v in groups.items() if v}
    keys = list(setmap)
    pairs = [len(setmap[keys[i]] & setmap[keys[j]]) / max(len(setmap[keys[i]] | setmap[keys[j]]), 1)
             for i in range(len(keys)) for j in range(i + 1, len(keys))]
    avg = stats.fmean(pairs) if pairs else 0.0
    n_eff = n_trials / (1 + (n_trials - 1) * avg) if n_trials > 1 else n_trials
    srs = [sharpe([o.r_outcome for o in v]) for v in groups.values() if v]
    var_sr = stats.pvariance(srs) if len(srs) > 1 else 0.0

    print(f"\n{'='*92}\nPHASE 0 v2 — signal-matched exits, same-bar entry (per Chat redesign)")
    print(f"friction={args.friction_pts}pt rt | variable R (own risk units) | "
          f"N_trials={n_trials} | N_eff={n_eff:.1f}\n{'='*92}")
    print(f"  {'subset':<30}{'n':>5}{'win%':>7}{'E[R]':>8}{'Shrp':>8}{'bootCI':>16}{'DSR':>9}")
    print("  " + "-" * 80)
    sts = {k: _report(k, v, n_eff, var_sr) for k, v in groups.items()}

    print(f"\n  -- per signal-type (own exit) --")
    by_kind = defaultdict(list)
    for o in ocs:
        by_kind[o.kind].append(o)
    for k in sorted(by_kind):
        v = by_kind[k]; rs = [o.r_outcome for o in v]
        print(f"     {k:<24} n={len(rs):>4}  win%={sum(1 for r in rs if r>0)/len(rs)*100:>5.1f}  "
              f"E[R]={stats.fmean(rs):+.3f}  medRR={stats.median([o.rr for o in v]):.2f}")

    print(f"\n  -- reversion by regime x session-kind --")
    for reg in ("ACCELERATION", "SUPPRESSION", "TRANSITION"):
        for sk in ("TREND", "CHOP"):
            v = [o.r_outcome for o in ocs if o.family == "REVERSION" and o.regime == reg and o.session_kind == sk]
            if v:
                print(f"     {reg:<13} {sk:<6} n={len(v):>4}  win%={sum(1 for r in v if r>0)/len(v)*100:>5.1f}  E[R]={stats.fmean(v):+.3f}")

    # decision
    print(f"\n{'='*92}\nDECISION GATE (v2)\n{'='*92}")
    best_k = max((k for k in groups if groups[k]), key=lambda k: sts[k].get("expectancy_r", -9))
    best = sts[best_k]
    print(f"  Best subset: {best_k}  E[R]={best.get('expectancy_r', float('nan')):+.3f}  "
          f"DSR@Neff={best.get('dsr', float('nan')):.3f}  n={best.get('n')}")
    pos = best.get("expectancy_r", -9) > 0 and best.get("boot_pos", False)
    clears = best.get("dsr", 0) >= 0.30
    print(f"  [{'PASS' if pos else 'FAIL'}] positive E[R] after friction AND bootstrap CI lower>0")
    print(f"  [{'PASS' if clears else 'FAIL'}] DSR @ N_eff >= 0.30 (Phase-2 kill criterion)")
    print(f"\n  >>> {'Edge candidate — worth shadow validation (HUMAN call)' if pos else 'No edge after redesign on this sample'}")
    print("  >>> NOTE: ES sample is ~12 sessions, mostly up-trend. Reversion-long in a")
    print("  >>>       trend tape is the worst case; chop/down sessions needed to judge fairly.")
    print("  >>> HUMAN decision point — do not auto-proceed to Phase 1.")


if __name__ == "__main__":
    main()
