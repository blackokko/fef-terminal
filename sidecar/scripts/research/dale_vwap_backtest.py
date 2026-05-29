"""Phase 0 — backward-looking validation of the Dale + VWAP directional signals.

Pure analysis. No live code paths change; no shadow variants. Validates whether
filtered directional signals beat (a) raw signals, (b) a random-direction
control, and (c) frictions — the decision gate before any Phase 1 work.

Parameters locked with the user (2026-05-29):
  - 1R = 7 SPX points (forward-walk stop; T1 at 1R).
  - Timeframes as written: 0DTE detect 5-min (gate 30-min); 1DTE detect 30-min
    (gate daily/6-month). Resample for detection; forward-walk on 1-min bars.
  - Dale on SPX (clean index price); VWAP on ES (needs volume). Forward-walk
    each signal on its own instrument's 1-min path, R in points (ES≈SPX 1:1).

Methodology: win rate, expectancy R, per-trade Sharpe, moving-block bootstrap CI
(~14-min horizon), Deflated Sharpe Ratio @ correlation-corrected N_eff. Stratified
by gamma regime. Random-direction control at the best subset's fire cadence.

Run:  python -m scripts.research.dale_vwap_backtest [--friction-r 0.25]
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sqlite3
import statistics as stats
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from fef_sidecar.engine.signals import dale, vwap
from fef_sidecar.engine.signals.dale import Bar

DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
ET_OFFSET_H = -4
R_POINTS = 7.0            # 1R = 7 SPX points (user-locked)
TRANSITION_PTS = 10.0     # |spot - flip| within this => TRANSITION regime
RNG = random.Random(42)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_bars(c: sqlite3.Connection, symbol: str) -> list[Bar]:
    rows = c.execute(
        "SELECT timestamp_ms,open,high,low,close,volume FROM historical_bar "
        "WHERE symbol=? AND bar_size='1 min' ORDER BY timestamp_ms", (symbol,),
    ).fetchall()
    return [Bar(ts_ms=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5] or 0) for r in rows]


def et_date(ts_ms: int) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts_ms / 1000, dt.timezone(dt.timedelta(hours=ET_OFFSET_H))).strftime("%Y-%m-%d")


def daily_bars(bars_1m: list[Bar]) -> list[Bar]:
    """Aggregate 1-min into daily ET-session OHLCV (for the 6-month daily gate)."""
    by_day: dict[str, list[Bar]] = defaultdict(list)
    for b in bars_1m:
        by_day[et_date(b.ts_ms)].append(b)
    out = []
    for day in sorted(by_day):
        g = by_day[day]
        out.append(Bar(ts_ms=g[0].ts_ms, open=g[0].open, high=max(x.high for x in g),
                       low=min(x.low for x in g), close=g[-1].close, volume=sum(x.volume for x in g)))
    return out


# ---------------------------------------------------------------------------
# Regime (gamma flip + net GEX) per session from exposure_chain_point
# ---------------------------------------------------------------------------


@dataclass
class SessionRegime:
    flip_strike: float | None
    net_gex_m: float


def load_session_regimes(c: sqlite3.Connection) -> dict[str, SessionRegime]:
    """Per session: gamma-flip strike (cumulative net-gamma sign change nearest
    spot) + total net GEX, from the GAMMA exposure chain. For sessions with one
    EOD snapshot this is a session-level proxy; intraday spot_vs_flip still
    varies via the signal's own spot."""
    out: dict[str, SessionRegime] = {}
    sessions = [r[0] for r in c.execute(
        "SELECT DISTINCT session_date FROM exposure_chain_point WHERE greek_type='GAMMA'").fetchall()]
    for sd in sessions:
        # use the LAST snapshot of the session (most complete)
        ts = c.execute("SELECT MAX(timestamp_ms) FROM exposure_chain_point "
                       "WHERE greek_type='GAMMA' AND session_date=?", (sd,)).fetchone()[0]
        rows = c.execute("SELECT strike, net_m, spot_at_capture FROM exposure_chain_point "
                         "WHERE greek_type='GAMMA' AND session_date=? AND timestamp_ms=? ORDER BY strike",
                         (sd, ts)).fetchall()
        if not rows:
            continue
        spot = rows[0][2]
        net_gex = sum(r[1] for r in rows)
        # cumulative net gamma; flip = strike nearest spot where cumulative crosses 0
        cum = 0.0
        crossings = []
        prev_cum = None
        for strike, net_m, _ in rows:
            cum += net_m
            if prev_cum is not None and (prev_cum < 0 <= cum or prev_cum > 0 >= cum):
                crossings.append(strike)
            prev_cum = cum
        flip = min(crossings, key=lambda s: abs(s - spot)) if crossings else None
        out[sd] = SessionRegime(flip_strike=flip, net_gex_m=net_gex)
    return out


def classify_regime(reg: SessionRegime | None, spot: float) -> str:
    if reg is None or reg.flip_strike is None:
        return "UNKNOWN"
    if abs(spot - reg.flip_strike) <= TRANSITION_PTS:
        return "TRANSITION"
    if reg.net_gex_m < 0:
        return "ACCELERATION"
    if spot > reg.flip_strike:
        return "SUPPRESSION"
    return "ACCELERATION"


# ---------------------------------------------------------------------------
# Forward-walk on 1-min bars (order-preserving, t1_hit_probability proxy)
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    ts_ms: int
    kind: str
    direction: str
    session: str
    timeframe_min: int
    regime: str
    mtf_pass: bool
    win: float          # 1.0 win / 0.0 loss / fractional on ambiguous bar
    r_outcome: float    # R multiple (path result)
    mfe: float          # max favorable excursion (pts)
    mae: float          # max adverse excursion (pts)
    detail: dict = field(default_factory=dict)


def _t1_prob(open_px: float, target: float, stop: float) -> float:
    """P(target hit before stop) when a single bar touches BOTH — bar-geometry
    proxy: closer the bar open sits to the stop, the more likely stop fired
    first. p = dist(open,stop)/(dist(open,target)+dist(open,stop))."""
    ds = abs(open_px - stop)
    dt_ = abs(open_px - target)
    return ds / (ds + dt_) if (ds + dt_) > 0 else 0.5


def forward_walk(sig, bars_1m: list[Bar], idx_by_ts: list[int], horizon_session: str) -> Outcome | None:
    """Walk 1-min bars from the first bar at/after the signal bar's close.
    direction CALLS: target=entry+7, stop=entry-7 (reversed for PUTS).
    Cap at the end of horizon_session (ET date)."""
    close_time = sig.ts_ms + sig.timeframe_min * 60_000
    # first 1-min bar at/after the signal bar close
    lo, hi = 0, len(bars_1m)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars_1m[mid].ts_ms < close_time:
            lo = mid + 1
        else:
            hi = mid
    start = lo
    if start >= len(bars_1m):
        return None
    entry = bars_1m[start].open
    is_call = sig.direction == dale.CALLS
    target = entry + R_POINTS if is_call else entry - R_POINTS
    stop = entry - R_POINTS if is_call else entry + R_POINTS
    mfe = mae = 0.0
    for k in range(start, len(bars_1m)):
        b = bars_1m[k]
        if et_date(b.ts_ms) > horizon_session:
            break
        # excursions (signed by direction)
        fav = (b.high - entry) if is_call else (entry - b.low)
        adv = (entry - b.low) if is_call else (b.high - entry)
        mfe = max(mfe, fav)
        mae = max(mae, adv)
        hit_t = (b.high >= target) if is_call else (b.low <= target)
        hit_s = (b.low <= stop) if is_call else (b.high >= stop)
        if hit_t and hit_s:
            p = _t1_prob(b.open, target, stop)
            return _mk(sig, horizon_session, p, 2 * p - 1, mfe, mae)
        if hit_t:
            return _mk(sig, horizon_session, 1.0, 1.0, mfe, mae)
        if hit_s:
            return _mk(sig, horizon_session, 0.0, -1.0, mfe, mae)
    # timeout — exit at last close
    last = None
    for k in range(len(bars_1m) - 1, start - 1, -1):
        if et_date(bars_1m[k].ts_ms) <= horizon_session:
            last = bars_1m[k]
            break
    if last is None:
        return None
    r = ((last.close - entry) if is_call else (entry - last.close)) / R_POINTS
    return _mk(sig, horizon_session, 1.0 if r > 0 else 0.0, r, mfe, mae)


def _mk(sig, session, win, r, mfe, mae) -> Outcome:
    return Outcome(ts_ms=sig.ts_ms, kind=sig.kind, direction=sig.direction, session=session,
                   timeframe_min=sig.timeframe_min, regime="", mtf_pass=False,
                   win=win, r_outcome=r, mfe=mfe, mae=mae, detail=dict(sig.detail))


# ---------------------------------------------------------------------------
# Methodology stack
# ---------------------------------------------------------------------------


def sharpe(rs: list[float]) -> float:
    if len(rs) < 2:
        return 0.0
    sd = stats.pstdev(rs)
    return (stats.fmean(rs) / sd) if sd > 0 else 0.0


def block_bootstrap_ci(rs: list[float], block: int, B: int = 2000) -> tuple[float, float]:
    """Moving-block bootstrap CI (5-95%) for mean R — block captures the
    ~14-min autocorrelation horizon."""
    n = len(rs)
    if n < 3:
        return (float("nan"), float("nan"))
    block = max(1, min(block, n))
    means = []
    for _ in range(B):
        out: list[float] = []
        while len(out) < n:
            s = RNG.randint(0, n - block)
            out.extend(rs[s:s + block])
        means.append(stats.fmean(out[:n]))
    means.sort()
    return (means[int(0.05 * B)], means[int(0.95 * B)])


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _phi_inv(p: float) -> float:
    # Acklam's rational approximation
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    cc = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
          -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    p = min(max(p, 1e-9), 1 - 1e-9)
    pl = 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((cc[0]*q+cc[1])*q+cc[2])*q+cc[3])*q+cc[4])*q+cc[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= 1 - pl:
        q = p - 0.5; r = q*q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((cc[0]*q+cc[1])*q+cc[2])*q+cc[3])*q+cc[4])*q+cc[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def psr(sr: float, n: int, skew: float, kurt: float, sr_star: float) -> float:
    """Probabilistic Sharpe Ratio: P(true SR > sr_star) given non-normal returns."""
    if n < 2:
        return 0.0
    denom = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr * sr, 1e-9))
    return _phi((sr - sr_star) * math.sqrt(n - 1) / denom)


def deflated_sharpe(sr: float, n: int, skew: float, kurt: float, n_trials: float, var_sr: float) -> float:
    """DSR = PSR with the benchmark set to the expected MAX Sharpe under the
    null across n_trials (Bailey & López de Prado)."""
    nt = max(n_trials, 1.0)
    if nt <= 1 or var_sr <= 0:
        return psr(sr, n, skew, kurt, 0.0)
    euler = 0.5772156649
    sr_star = math.sqrt(var_sr) * ((1 - euler) * _phi_inv(1 - 1 / nt) + euler * _phi_inv(1 - 1 / (nt * math.e)))
    return psr(sr, n, skew, kurt, sr_star)


def stat_block(rs: list[float], *, tf_min: int, n_trials: float, var_sr: float) -> dict:
    if not rs:
        return {"n": 0}
    n = len(rs)
    sk = _safe_skew(rs)
    ku = _safe_kurt(rs)
    sr = sharpe(rs)
    block = max(1, round(14 / tf_min))
    lo, hi = block_bootstrap_ci(rs, block)
    return {
        "n": n,
        "win_rate": sum(1 for r in rs if r > 0) / n,
        "expectancy_r": stats.fmean(rs),
        "sharpe": sr,
        "boot_ci": (lo, hi),
        "boot_pos": lo > 0,
        "dsr": deflated_sharpe(sr, n, sk, ku, n_trials, var_sr),
    }


def _safe_skew(rs: list[float]) -> float:
    n = len(rs)
    if n < 3:
        return 0.0
    m = stats.fmean(rs); sd = stats.pstdev(rs)
    return (sum((x - m) ** 3 for x in rs) / n) / (sd ** 3) if sd > 0 else 0.0


def _safe_kurt(rs: list[float]) -> float:
    n = len(rs)
    if n < 4:
        return 3.0
    m = stats.fmean(rs); sd = stats.pstdev(rs)
    return (sum((x - m) ** 4 for x in rs) / n) / (sd ** 4) if sd > 0 else 3.0


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def build_outcomes(c, friction_r: float) -> dict[str, list[Outcome]]:
    spx = load_bars(c, "SPX")
    es = load_bars(c, "ES")
    regimes = load_session_regimes(c)

    spx5, spx30 = dale.resample(spx, 5), dale.resample(spx, 30)
    es5, es30 = dale.resample(es, 5), dale.resample(es, 30)
    spx_daily, es_daily = daily_bars(spx), daily_bars(es)

    def annotate(sigs, inst_1m, htf_bars, next_day_horizon):
        out: list[Outcome] = []
        for s in sigs:
            sd = et_date(s.ts_ms)
            horizon = _next_session(inst_1m, sd) if next_day_horizon else sd
            oc = forward_walk(s, inst_1m, [], horizon)
            if oc is None:
                continue
            oc.regime = classify_regime(regimes.get(sd), s.price)
            oc.mtf_pass = dale.mtf_gate_pass(s.direction, dale.trend_state_at(htf_bars, s.ts_ms))
            # apply friction haircut to R (round-trip slippage/spread proxy)
            oc.r_outcome -= friction_r
            out.append(oc)
        return out

    res: dict[str, list[Outcome]] = {}
    res["dale_0dte"] = annotate(dale.detect(spx5, timeframe_min=5), spx, spx30, False)
    res["dale_1dte"] = annotate(dale.detect(spx30, timeframe_min=30), spx, spx_daily, True)
    res["vwap_0dte"] = annotate(vwap.detect(es5, timeframe_min=5), es, es30, False)
    res["vwap_1dte"] = annotate(vwap.detect(es30, timeframe_min=30), es, es_daily, True)
    return res


def _next_session(bars_1m: list[Bar], sd: str) -> str:
    days = sorted({et_date(b.ts_ms) for b in bars_1m})
    if sd in days:
        i = days.index(sd)
        if i + 1 < len(days):
            return days[i + 1]
    return sd


def subsets(res: dict[str, list[Outcome]]) -> dict[str, list[Outcome]]:
    dale_all = res["dale_0dte"] + res["dale_1dte"]
    vwap_all = res["vwap_0dte"] + res["vwap_1dte"]

    def regime_armed(ocs):
        return [o for o in ocs if o.regime == "ACCELERATION"]

    def mtf(ocs):
        return [o for o in ocs if o.mtf_pass]

    s = {
        "raw_dale": dale_all,
        "dale_regime": regime_armed(dale_all),
        "dale_regime_mtf": mtf(regime_armed(dale_all)),
        "raw_vwap": vwap_all,
        "vwap_regime": regime_armed(vwap_all),
    }
    # Dale+VWAP confluence: a Dale and a VWAP signal within 5 signal-bars (~25min
    # @5min), same direction, regime+MTF filtered.
    conf = _confluence(mtf(regime_armed(dale_all)), mtf(regime_armed(vwap_all)))
    s["dale_vwap_confluence"] = conf
    return s


def _confluence(a: list[Outcome], b: list[Outcome], window_ms: int = 25 * 60_000) -> list[Outcome]:
    out = []
    bt = sorted(b, key=lambda o: o.ts_ms)
    for oa in a:
        for ob in bt:
            if abs(ob.ts_ms - oa.ts_ms) <= window_ms and ob.direction == oa.direction:
                out.append(oa)
                break
    return out


def random_control(best: list[Outcome], friction_r: float) -> list[float]:
    """Same fire cadence as `best`, randomized direction → re-score R from MFE/MAE
    is not possible without re-walk; instead flip sign of r_outcome with p=0.5
    (a direction coin-flip on the same entries)."""
    rs = []
    for o in best:
        flip = RNG.random() < 0.5
        r = -o.r_outcome - 2 * friction_r if flip else o.r_outcome
        # note: flipping direction inverts the path result around the friction;
        # approximate by negating the gross (pre-friction) R then re-applying friction
        gross = o.r_outcome + friction_r
        rs.append((-gross if flip else gross) - friction_r)
    return rs


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--friction-r", type=float, default=0.25,
                    help="round-trip friction haircut in R (slippage+spread proxy)")
    args = ap.parse_args()

    c = sqlite3.connect(DB)
    res = build_outcomes(c, args.friction_r)
    subs = subsets(res)

    # honest N_trials: subsets this run + existing variant_ledger rows
    try:
        n_ledger = c.execute("SELECT COUNT(*) FROM variant_ledger").fetchone()[0]
    except Exception:
        n_ledger = 0
    n_trials_raw = len(subs) + n_ledger
    # N_eff: nested/overlapping subsets are correlated. Estimate avg pairwise
    # Jaccard overlap of signal-timestamp sets as a correlation proxy.
    n_eff = _n_eff(subs, n_trials_raw)
    # variance of per-subset Sharpe (for DSR benchmark)
    srs = [sharpe([o.r_outcome for o in v]) for v in subs.values() if v]
    var_sr = stats.pvariance(srs) if len(srs) > 1 else 0.0

    print(f"\n{'='*100}")
    print("PHASE 0 — Dale + VWAP directional signal validation")
    print(f"1R = {R_POINTS} SPX pts | friction = {args.friction_r}R round-trip | "
          f"N_trials={n_trials_raw} (ledger {n_ledger}+{len(subs)}) | N_eff={n_eff:.1f}")
    print(f"{'='*100}")
    hdr = f"{'subset':<24}{'n':>5}{'win%':>7}{'E[R]':>8}{'Sharpe':>8}{'bootCI(5-95%)':>20}{'DSR@Neff':>10}"
    print(hdr); print("-" * len(hdr))

    rows_for_decision = {}
    for name, ocs in subs.items():
        rs = [o.r_outcome for o in ocs]
        st = stat_block(rs, tf_min=5, n_trials=n_eff, var_sr=var_sr)
        rows_for_decision[name] = st
        if st["n"]:
            ci = st["boot_ci"]
            print(f"{name:<24}{st['n']:>5}{st['win_rate']*100:>6.1f}{st['expectancy_r']:>8.3f}"
                  f"{st['sharpe']:>8.2f}{f'[{ci[0]:+.3f},{ci[1]:+.3f}]':>20}{st['dsr']:>10.3f}")
        else:
            print(f"{name:<24}{0:>5}  (no signals)")

    # Random-direction control at the best subset's cadence
    best_name = max((k for k in subs if subs[k]), key=lambda k: rows_for_decision[k].get("expectancy_r", -9))
    ctrl = random_control(subs[best_name], args.friction_r)
    cst = stat_block(ctrl, tf_min=5, n_trials=n_eff, var_sr=var_sr)
    print("-" * len(hdr))
    cci = cst.get("boot_ci", (float("nan"), float("nan")))
    print(f"{'RANDOM ctrl ('+best_name+')':<24}{cst['n']:>5}{cst['win_rate']*100:>6.1f}"
          f"{cst['expectancy_r']:>8.3f}{cst['sharpe']:>8.2f}"
          f"{f'[{cci[0]:+.3f},{cci[1]:+.3f}]':>20}{cst['dsr']:>10.3f}")

    # Per-regime breakdown (Dale+VWAP all signals)
    print(f"\n{'-- per-regime (all signals) --':<40}")
    allo = sum(res.values(), [])
    by_reg = defaultdict(list)
    for o in allo:
        by_reg[o.regime].append(o.r_outcome)
    for reg in ("ACCELERATION", "SUPPRESSION", "TRANSITION", "UNKNOWN"):
        rs = by_reg.get(reg, [])
        if rs:
            print(f"  {reg:<14} n={len(rs):>4}  win%={sum(1 for r in rs if r>0)/len(rs)*100:>5.1f}  E[R]={stats.fmean(rs):+.3f}")

    # Signal-type breakdown
    print(f"\n{'-- per signal-type --':<40}")
    by_kind = defaultdict(list)
    for o in allo:
        by_kind[o.kind].append(o.r_outcome)
    for k in sorted(by_kind):
        rs = by_kind[k]
        print(f"  {k:<22} n={len(rs):>4}  win%={sum(1 for r in rs if r>0)/len(rs)*100:>5.1f}  E[R]={stats.fmean(rs):+.3f}")

    # ---- MFE/MAE asymmetry (would a trailing exit beat the fixed 1:1?) ----
    print(f"\n{'-- MFE/MAE asymmetry (pts) — does a trailing exit help? --':<40}")
    print(f"  {'subset':<22}{'n':>5}{'medMFE':>8}{'medMAE':>8}{'MFE>2R%':>9}{'MFE/MAE':>9}")
    for name in ("raw_dale", "dale_regime_mtf", "raw_vwap", "vwap_regime"):
        ocs = subs.get(name, [])
        if not ocs:
            continue
        mfes = [o.mfe for o in ocs]; maes = [o.mae for o in ocs]
        med_mfe = stats.median(mfes); med_mae = stats.median(maes)
        big = sum(1 for m in mfes if m >= 2 * R_POINTS) / len(mfes) * 100
        ratio = (med_mfe / med_mae) if med_mae > 0 else float("nan")
        print(f"  {name:<22}{len(ocs):>5}{med_mfe:>8.1f}{med_mae:>8.1f}{big:>8.1f}%{ratio:>9.2f}")
    # winners-only MFE vs losers-only MAE on the cleanest trend subset
    wd = [o for o in subs.get("raw_dale", []) if o.r_outcome > 0]
    ld = [o for o in subs.get("raw_dale", []) if o.r_outcome <= 0]
    if wd and ld:
        print(f"  raw_dale winners medMFE={stats.median([o.mfe for o in wd]):.1f}  "
              f"losers medMAE={stats.median([o.mae for o in ld]):.1f}")

    # ---- Decision gate ----
    print(f"\n{'='*100}\nDECISION GATE\n{'='*100}")
    _gate(rows_for_decision, cst, best_name)


def _n_eff(subs: dict[str, list[Outcome]], n_raw: float) -> float:
    keys = [k for k in subs if subs[k]]
    sets = {k: {o.ts_ms for o in subs[k]} for k in keys}
    pairs = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = sets[keys[i]], sets[keys[j]]
            u = len(a | b)
            pairs.append(len(a & b) / u if u else 0.0)
    avg = stats.fmean(pairs) if pairs else 0.0
    return n_raw / (1 + (n_raw - 1) * avg) if n_raw > 1 else n_raw


def _gate(rows: dict, ctrl: dict, best_name: str) -> None:
    raw_dale = rows.get("raw_dale", {})
    raw_vwap = rows.get("raw_vwap", {})
    filt = {k: v for k, v in rows.items() if "regime" in k or "confluence" in k}
    best = rows.get(best_name, {})
    beats_raw = any(
        filt.get(k, {}).get("expectancy_r", -9) > max(raw_dale.get("expectancy_r", -9),
                                                       raw_vwap.get("expectancy_r", -9))
        for k in filt
    )
    beats_random = best.get("expectancy_r", -9) > ctrl.get("expectancy_r", 9)
    positive = best.get("expectancy_r", -9) > 0 and best.get("boot_pos", False)
    print(f"  Best subset: {best_name}  E[R]={best.get('expectancy_r', float('nan')):+.3f}  "
          f"DSR@Neff={best.get('dsr', float('nan')):.3f}")
    print(f"  [{'PASS' if beats_raw else 'FAIL'}] filtered subset beats raw signals")
    print(f"  [{'PASS' if beats_random else 'FAIL'}] best beats random-direction control "
          f"(ctrl E[R]={ctrl.get('expectancy_r', float('nan')):+.3f})")
    print(f"  [{'PASS' if positive else 'FAIL'}] best has positive expectancy after frictions "
          f"AND bootstrap CI lower bound > 0")
    verdict = "PROCEED to Phase 1 (human go-ahead required)" if (beats_raw and beats_random and positive) \
        else "STOP — directional-automation premise not supported by Phase 0"
    print(f"\n  >>> {verdict}")
    print("  >>> Kill criterion (Phase 2): DSR @ N_eff >= 0.30 after 10 shadow sessions.")
    print("  >>> This is a HUMAN decision point — do not auto-proceed.")


if __name__ == "__main__":
    main()
