"""CONFLUENCE EXPECTANCY (READ-ONLY research spike; engine code untouched).

QUESTION (from the operator): stop chasing a binary HIT-RATE. Find CONFLUENCE conditions
where the *conditional expected forward move* is meaningfully directional -- "when THIS
alignment of signals happens, price tends to move our way more than against." With option
convexity + cheap real costs, a ~50%-hit signal whose winners outweigh losers is tradeable.
So we measure  E[forward move | confluence]  and its UP/DOWN ASYMMETRY, not win%.

RULER  : INDEPENDENT IBKR SPX 1-min close (phase_pillars.build_session -> ibkr_ruler). The bar
         stamped rt[i] is START-of-bar; rp[i] is its CLOSE == the price at rt[i]+60s. So rp[i]
         IS the confirmation point. We enter at rp[i] (confirmation close) and measure the
         forward move to rp at rt[i]+H*60s -- NO +1-min start-stamp lookahead (a signal built
         from closes <= rp[i] is actionable exactly at rp[i]). A one-bar-DELAYED entry (rp[i+1])
         is reported as a robustness column.

FEATURES (all causal, per 1-min bar):
  price   : short-window (5m) range / off-low / off-high / velocity(pt/min over ~2m);
            long-window (30m) extension from the rolling low/high; EMA9 level+slope; higher-
            lows / lower-highs structure (20m); opening-range (09:30-09:45) break.
  flow    : call%-put% aggression spread + drift_bias flip  (option_flow_minute; full coverage).
  maxpain : session max-pain pin +/- overshoot            (option_open_interest; full coverage).
  VIX     : PRIOR-DAY close of VIX / VIX9D / VIX3M (known at the open) -> level ladder
            (15/20/25/30/40) + term-structure backwardation (VIX9D>VIX).  historical_bar daily.
  (IV pillar is DEV-ONLY: option_dealer_exposure.iv_atm ends 2025-08-12 == the holdout wall, so
   it is deliberately NOT used in any holdout-testable condition.)

CONDITIONS  (few, MECHANISM-MOTIVATED, thresholds PRE-REGISTERED from the engine's calibrated
  values -- NOT swept to fit DEV):
  CAPFADE_UP  (buy CALLS)  sharp down-move + velocity stall + bounce off the low + at a 30m
                           extension low + elevated prior VIX -> panic exhausts, mean-revert UP.
  BLOWOFF_DN  (buy PUTS)   mirror: sharp rip + stall + reject off the high + at a 30m extension
                           high + elevated VIX -> vertical move exhausts, revert DOWN.
  TREND_DN    (buy PUTS)   price<falling EMA9 + lower-highs + broke opening-range low + high VIX
                           -> established downtrend in a trending-vol regime, momentum persists.
  TREND_UP    (buy CALLS)  price>rising EMA9 + higher-lows + broke opening-range high + calm VIX
                           -> bull drift/momentum in a low-vol regime.
  + VIX-ladder variants of CAPFADE_UP (>=15/25/30, backwardation) and two flow-corroborated cuts
    (all counted in the multiple-testing penalty).

METHOD : forward-move DISTRIBUTION at +10min / +30min / trailing-exit (8pt trail, 60m cap):
  mean, median, FAVORABLE-MASS % (= sum(+fav)/sum|fav|; 50 = symmetric, >50 = our way),
  favorable tail (%>15pt, %>30pt), adverse tail (%<-15pt), N episodes / N sessions, and a
  SESSION-CLUSTERED t on mean fav (unit of inference = session). Reported DEV vs HOLDOUT
  side by side. Consecutive same-condition minutes are collapsed to one EPISODE (first bar).

GUARD  : find on DEV (2022-01-03..2025-08-12), confirm on HOLDOUT (2025-08-13..2026-06-26).
  A condition only counts if SIGN + FAVORABLE-MASS hold out-of-sample. Multiple-testing:
  K conditions scanned -> Bonferroni bar |t|>~2.9 at family-wise 0.05 is printed.

Run: .venv/Scripts/python.exe -m scripts.research.backtest.confluence_expectancy
"""
from __future__ import annotations

import datetime as dt
import os
import pickle
import sys
from collections import defaultdict
from zoneinfo import ZoneInfo

import numpy as np

from .data import connect
from .holdout import HOLDOUT_START
from .ibkr_ruler import ibkr_open
from .phase_pillars import build_session

sys.stdout.reconfigure(encoding="utf-8")
ET = ZoneInfo("America/New_York")
SIG_DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
SCRATCH = (r"C:\Users\diant\AppData\Local\Temp\claude\C--Users-diant"
           r"\13885fae-da20-4300-a83a-94697ac91ce4\scratchpad")
FULL_CACHE = os.path.join(SCRATCH, "conf_cache_full.pkl")
DEV_CACHE = os.path.join(os.environ.get("TEMP", "."), "phase_cache.pkl")

START, END = "2022-01-03", "2026-06-26"
WIN_LO, WIN_HI = 9.75, 15.0       # entries 09:45..15:00 ET (OR defined, +30m/trail room exists)
GLITCH = 0.05                     # drop a forward reading with |move|/entry > 5%
EP_GAP_MS = 180_000               # consecutive same-condition bars within 3min = one episode
TRAIL_PT = 8.0                    # trailing-stop give-back (pt)
MAXHOLD_MIN = 60                  # trailing-exit time cap (min)
HORIZONS = [(10, "fav10"), (30, "fav30")]

# ---- PRE-REGISTERED thresholds (from phase_detector's calibrated engine values) -------------
MIN_MOVE = 15.0     # sharp-move floor over the short window (pt)
VEL_STALL = 3.0     # |pt/min| under which a move has stalled
BOUNCE_PT = 3.0     # pt off the short-window extreme confirming a reversal
EXT_NEAR = 6.0      # within this many pt of the 30-min extreme = "at the extension"
MP_OVER = 8.0       # pt beyond the max-pain pin = overshoot
SHORT_MIN = 5       # short window (min)
LONG_MIN = 30       # long / extension window (min)
VEL_SEC = 120       # velocity lookback (s)
EMA_SPAN = 9        # short EMA (bars)
OR_MIN = 15         # opening-range window (min from 09:30)


# ============================================================ VIX prior-close regime
def load_vix_prior():
    """date(str) -> dict(vix, vix9d, vix3m) using the PRIOR trading day's daily close."""
    import sqlite3
    c = sqlite3.connect(f"file:{SIG_DB}?mode=ro", uri=True)

    def daily(sym):
        rows = c.execute("SELECT timestamp_ms, close FROM historical_bar "
                         "WHERE symbol=? AND bar_size='1 day' AND close IS NOT NULL "
                         "ORDER BY timestamp_ms", (sym,)).fetchall()
        out = {}
        for ts, cl in rows:
            d = dt.datetime.fromtimestamp(ts / 1000, ET).date()
            out[d] = float(cl)          # last write per date wins
        return out

    vix, v9, v3 = daily("VIX"), daily("VIX9D"), daily("VIX3M")
    all_d = sorted(vix)                 # VIX is the spine
    arr = np.array([d.toordinal() for d in all_d])

    def prior(dstr):
        d = dt.date.fromisoformat(dstr)
        j = int(np.searchsorted(arr, d.toordinal())) - 1   # strictly-before
        if j < 0:
            return None
        pd = all_d[j]
        return dict(vix=vix.get(pd), vix9d=v9.get(pd), vix3m=v3.get(pd), pdate=pd.isoformat())

    c.close()
    return prior


# ============================================================ per-session features
def _et_h(ms):
    x = dt.datetime.fromtimestamp(ms / 1000, ET)
    return x.hour + x.minute / 60.0


def _ema(px, span):
    a = 2.0 / (span + 1.0)
    out = np.empty(len(px))
    out[0] = px[0]
    for i in range(1, len(px)):
        out[i] = a * px[i] + (1 - a) * out[i - 1]
    return out


def session_features(sess):
    """Return per-bar causal feature dicts for one built session (rt, rp, samples)."""
    rt, rp = sess["rt"], sess["rp"]
    smp = sess["samples"]
    n = len(rt)
    ema = _ema(rp, EMA_SPAN)
    # opening-range (09:30..09:30+OR_MIN)
    t0 = int(rt[0])
    or_hi = or_lo = None
    or_end = t0 + OR_MIN * 60_000
    orseg = rp[(rt >= t0) & (rt <= or_end)]
    if len(orseg) >= 3:
        or_hi, or_lo = float(orseg.max()), float(orseg.min())

    feats = [None] * n
    for i in range(n):
        ti = int(rt[i])
        h = _et_h(ti)
        # short window [ti-SHORT_MIN, ti]
        s_lo = int(np.searchsorted(rt, ti - SHORT_MIN * 60_000, "left"))
        seg_s = rp[s_lo:i + 1]
        seg_ts = rt[s_lo:i + 1]
        # long window [ti-LONG_MIN, ti]
        l_lo = int(np.searchsorted(rt, ti - LONG_MIN * 60_000, "left"))
        seg_l = rp[l_lo:i + 1]
        if len(seg_s) < 3 or len(seg_l) < 5:
            continue
        low_s, high_s = float(seg_s.min()), float(seg_s.max())
        i_low = int(np.argmin(seg_s)); i_high = int(np.argmax(seg_s))
        rng_s = high_s - low_s
        off_low = rp[i] - low_s
        off_high = high_s - rp[i]
        down_leg = i_low > i_high        # the low came after the high in the short window
        up_leg = i_high > i_low
        # velocity pt/min over ~VEL_SEC
        vref = int(np.searchsorted(rt, ti - VEL_SEC * 1000, "left"))
        vref = min(vref, i - 1) if i >= 1 else i
        vel = None
        if vref < i and rt[i] > rt[vref]:
            vel = (rp[i] - rp[vref]) / ((rt[i] - rt[vref]) / 60000.0)
        low_l, high_l = float(seg_l.min()), float(seg_l.max())
        ext_from_low = rp[i] - low_l      # how far above the 30m low
        ext_from_high = high_l - rp[i]    # how far below the 30m high
        # EMA slope over last 3 bars
        ema_slope = ema[i] - ema[max(0, i - 3)]
        trend_up = rp[i] > ema[i] and ema_slope > 0
        trend_dn = rp[i] < ema[i] and ema_slope < 0
        # higher-lows / lower-highs over last ~20 bars (two 10-bar halves)
        hl = lh = False
        if i >= 20:
            recent = rp[i - 9:i + 1]; older = rp[i - 19:i - 9]
            hl = recent.min() > older.min()
            lh = recent.max() < older.max()
        above_or = or_hi is not None and rp[i] > or_hi
        below_or = or_lo is not None and rp[i] < or_lo
        sp = smp[i]
        spread = (sp.call_pct or 0.0) - (sp.put_pct or 0.0) if (sp.call_pct is not None or sp.put_pct is not None) else None
        mp = sp.max_pain
        feats[i] = dict(
            i=i, ts=ti, h=h, px=float(rp[i]),
            rng_s=rng_s, off_low=off_low, off_high=off_high, down_leg=down_leg, up_leg=up_leg,
            vel=vel, ext_from_low=ext_from_low, ext_from_high=ext_from_high,
            trend_up=trend_up, trend_dn=trend_dn, hl=hl, lh=lh,
            above_or=above_or, below_or=below_or,
            spread=spread, drift=sp.drift_bias,
            below_mp=(mp is not None and rp[i] < mp - MP_OVER),
            above_mp=(mp is not None and rp[i] > mp + MP_OVER),
        )
    return rt, rp, feats


# ============================================================ conditions
def _vge(v, x):
    return v is not None and v >= x


def conditions(vprev):
    """List of (name, dir, tag, predicate(feat)->bool). dir: +1 buy CALLS(up), -1 buy PUTS(down).
    vprev: session prior-VIX dict (or None). Thresholds are pre-registered (no DEV fitting)."""
    vix = vprev.get("vix") if vprev else None
    v9 = vprev.get("vix9d") if vprev else None
    backwardation = (v9 is not None and vix is not None and v9 > vix)

    def cap(f):  # core capitulation-fade geometry (direction UP)
        return (f["down_leg"] and f["rng_s"] >= MIN_MOVE and f["vel"] is not None
                and f["vel"] >= -VEL_STALL and f["off_low"] >= BOUNCE_PT
                and f["ext_from_low"] <= EXT_NEAR)

    def blow(f):  # mirror blow-off geometry (direction DOWN)
        return (f["up_leg"] and f["rng_s"] >= MIN_MOVE and f["vel"] is not None
                and f["vel"] <= VEL_STALL and f["off_high"] >= BOUNCE_PT
                and f["ext_from_high"] <= EXT_NEAR)

    C = []
    # -- primary mechanism set (holdout-testable; no IV) --
    C.append(("CAPFADE_UP@VIX>=20", +1, "primary", lambda f: cap(f) and _vge(vix, 20)))
    C.append(("BLOWOFF_DN@VIX>=20", -1, "primary", lambda f: blow(f) and _vge(vix, 20)))
    C.append(("TREND_DN@VIX>=20", -1, "primary",
              lambda f: f["trend_dn"] and f["lh"] and f["below_or"] and _vge(vix, 20)))
    C.append(("TREND_UP@VIX<20", +1, "primary",
              lambda f: f["trend_up"] and f["hl"] and f["above_or"] and (vix is not None and vix < 20)))
    # -- VIX-ladder variants of the capitulation fade (exploratory) --
    C.append(("CAPFADE_UP@VIX>=15", +1, "ladder", lambda f: cap(f) and _vge(vix, 15)))
    C.append(("CAPFADE_UP@VIX>=25", +1, "ladder", lambda f: cap(f) and _vge(vix, 25)))
    C.append(("CAPFADE_UP@VIX>=30", +1, "ladder", lambda f: cap(f) and _vge(vix, 30)))
    C.append(("CAPFADE_UP@backwrd9", +1, "ladder", lambda f: cap(f) and backwardation))
    # -- flow-corroborated cuts (holdout-testable; flow has full coverage) --
    C.append(("CAPFADE_UP+aggrcall", +1, "flow",
              lambda f: cap(f) and _vge(vix, 20) and (f["spread"] is not None and f["spread"] > 0)))
    C.append(("TREND_DN+driftBear", -1, "flow",
              lambda f: f["trend_dn"] and f["lh"] and _vge(vix, 20) and f["drift"] == "BEARISH"))
    return C


def controls(vprev):
    """BASELINE controls (NOT counted in the multiple-testing K). Isolate whether the exhaustion
    CONFLUENCE adds value beyond 'a drop on a high-VIX day'. DROP_ONLY = sharp down-leg + VIX gate
    but WITHOUT the stall/bounce/extension geometry."""
    vix = vprev.get("vix") if vprev else None

    def drop(f):
        return f["down_leg"] and f["rng_s"] >= MIN_MOVE and f["vel"] is not None

    return [
        ("DROP_ONLY@VIX>=20", +1, "control", lambda f: drop(f) and _vge(vix, 20)),
        ("DROP_ONLY@VIX>=25", +1, "control", lambda f: drop(f) and _vge(vix, 25)),
    ]


VIX_BUCKETS = [("<15", 0, 15), ("15-20", 15, 20), ("20-25", 20, 25),
               ("25-30", 25, 30), (">=30", 30, 999)]


def vix_bucket(vprev):
    v = vprev.get("vix") if vprev else None
    if v is None:
        return None
    for lbl, lo, hi in VIX_BUCKETS:
        if lo <= v < hi:
            return lbl
    return None


# ============================================================ forward-move measurement
def idx_nearest(rt, target, tol=120_000):
    j = int(np.searchsorted(rt, target))
    best, bd = -1, tol + 1
    for c in (j - 1, j):
        if 0 <= c < len(rt):
            dd = abs(int(rt[c]) - target)
            if dd <= tol and dd < bd:
                bd, best = dd, c
    return best if best >= 0 else None


def fwd_fav(rt, rp, i, d, hmin, e=None):
    """Signed forward move in the trade direction from entry price e (default confirmation close
    rp[i]) to the ruler bar nearest rt[i]+hmin. None if missing/glitch."""
    if e is None:
        e = float(rp[i])
    j = idx_nearest(rt, int(rt[i]) + hmin * 60_000)
    if j is None:
        return None
    mv = float(rp[j]) - e
    if abs(mv) > GLITCH * e:
        return None
    return d * mv


def trail_fav(rt, rp, i, d):
    """Trailing-stop realized fav from confirmation close: stop TRAIL_PT below the running peak
    favorable excursion (initial stop = TRAIL_PT), time-capped at MAXHOLD_MIN. Tracks from i+1."""
    e = float(rp[i])
    hi_ms = int(rt[i]) + MAXHOLD_MIN * 60_000
    peak = 0.0
    last_exc = 0.0
    k = i + 1
    while k < len(rt) and int(rt[k]) <= hi_ms:
        exc = d * (float(rp[k]) - e)
        if abs(float(rp[k]) - e) > GLITCH * e:
            k += 1
            continue
        last_exc = exc
        if exc > peak:
            peak = exc
        if exc <= peak - TRAIL_PT:          # trailing (and initial, since peak>=0) stop hit
            return exc
        k += 1
    return last_exc


# ============================================================ episode extraction
def episodes(rt, rp, feats, dirn, pred):
    """First bar of each episode where pred holds (same-condition bars within EP_GAP collapse).
    dirn is the trade direction (+1 buy CALLS / -1 buy PUTS)."""
    out = []
    last_ts = None
    for f in feats:
        if f is None:
            continue
        if f["h"] < WIN_LO or f["h"] > WIN_HI:
            continue
        if not pred(f):
            continue
        i = f["i"]
        if last_ts is not None and f["ts"] - last_ts <= EP_GAP_MS:
            last_ts = f["ts"]                # same episode -> skip (already entered)
            continue
        last_ts = f["ts"]
        rec = dict(i=i, ts=f["ts"], px=f["px"])
        for hmin, key in HORIZONS:
            rec[key] = fwd_fav(rt, rp, i, dirn, hmin)
        rec["favTR"] = trail_fav(rt, rp, i, dirn)
        # robustness: one-bar-delayed entry (rp[i+1]) at +30
        rec["fav30_delay"] = (fwd_fav(rt, rp, i, dirn, 30, e=float(rp[i + 1]))
                              if i + 1 < len(rp) else None)
        out.append(rec)
    return out


# ============================================================ stats
def dist_stats(vals):
    a = np.array([v for v in vals if v is not None and np.isfinite(v)], float)
    if len(a) == 0:
        return None
    absum = np.abs(a).sum()
    return dict(
        n=len(a), mean=float(a.mean()), median=float(np.median(a)),
        favmass=(100.0 * a[a > 0].sum() / absum) if absum > 0 else float("nan"),
        p15=100.0 * np.mean(a > 15), p30=100.0 * np.mean(a > 30),
        adv15=100.0 * np.mean(a < -15),
    )


def clustered_t(recs, key):
    per = defaultdict(list)
    for r in recs:
        v = r.get(key)
        if v is not None and np.isfinite(v):
            per[r["date"]].append(v)
    sm = np.array([np.mean(v) for v in per.values()], float)
    n = len(sm)
    if n < 2 or sm.std(ddof=1) == 0:
        return float("nan"), n
    return float(sm.mean() / (sm.std(ddof=1) / np.sqrt(n))), n


# ============================================================ cache build
def build_full_cache():
    if os.path.exists(FULL_CACHE):
        try:
            c = pickle.load(open(FULL_CACHE, "rb"))
            print(f"  loaded full cache: {len(c)} sessions", flush=True)
            return c
        except Exception:
            pass
    con, ic = connect(), ibkr_open()
    fl = {r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ?",
        (START, END))}
    oi = {r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_open_interest WHERE session_date BETWEEN ? AND ?",
        (START, END))}
    universe = sorted(fl & oi)
    print(f"  universe (flow & OI, {START}..{END}): {len(universe)} sessions", flush=True)
    # reuse the existing DEV cache to skip rebuilding 2022..2025-08
    cache, have = [], set()
    if os.path.exists(DEV_CACHE):
        try:
            dev = pickle.load(open(DEV_CACHE, "rb"))
            for s in dev:
                cache.append(s); have.add(s["date"])
            print(f"  reused DEV cache: {len(dev)} sessions", flush=True)
        except Exception:
            pass
    todo = [d for d in universe if d not in have]
    print(f"  building {len(todo)} remaining sessions ...", flush=True)
    for k, d in enumerate(todo):
        s = build_session(con, ic, d)
        if s is not None:
            cache.append(s)
        if (k + 1) % 50 == 0:
            print(f"    {k+1}/{len(todo)} built", flush=True)
    cache.sort(key=lambda s: s["date"])
    pickle.dump(cache, open(FULL_CACHE, "wb"))
    print(f"  full cache built + saved: {len(cache)} sessions", flush=True)
    return cache


# ============================================================ main
def _fmt(s):
    if s is None:
        return f"    {'(none)':>52s}"
    return (f"n={s['n']:4d} mean={s['mean']:+6.2f} med={s['median']:+6.2f} "
            f"mass={s['favmass']:4.0f}% >15={s['p15']:4.0f}% >30={s['p30']:3.0f}% "
            f"adv15={s['adv15']:3.0f}%")


def main():
    print("=" * 118)
    print("CONFLUENCE EXPECTANCY — conditional forward-move E[move] + asymmetry (not hit-rate)")
    print("  independent IBKR SPX ruler; enter at confirmation close rp[i] (=price at bar start+60s)")
    print(f"  DEV {START}..2025-08-12  |  HOLDOUT {HOLDOUT_START}..{END}")
    print("=" * 118)

    cache = build_full_cache()
    prior = load_vix_prior()

    # collect episodes per condition, tagged DEV/HOLD
    by_cond = defaultdict(list)     # name -> list of episode records (+date,split,dir,tag)
    cond_meta = {}                  # name -> (dir, tag)
    ambient = defaultdict(list)     # (split, 'UP'/'DN') -> per-bar fav30 for a drift baseline
    n_dev = n_hold = 0
    for si, sess in enumerate(cache):
        d = sess["date"]
        if d < START or d > END:
            continue
        split = "DEV" if d < HOLDOUT_START else "HOLD"
        vprev = prior(d)
        vb = vix_bucket(vprev)
        rt, rp, feats = session_features(sess)
        # ambient drift baseline: unconditional +30 fav for UP(+1) at every in-window bar,
        # split overall AND by prior-VIX bucket (the fear-mean-reversion base rate to beat)
        for f in feats:
            if f is None or f["h"] < WIN_LO or f["h"] > WIN_HI:
                continue
            up = fwd_fav(rt, rp, f["i"], +1, 30)
            if up is not None:
                ambient[(split, "UP")].append(up)
                if vb is not None:
                    ambient[(split, vb)].append(up)
        for name, dirn, tag, pred in list(conditions(vprev)) + list(controls(vprev)):
            cond_meta[name] = (dirn, tag)
            eps = episodes(rt, rp, feats, dirn, pred)
            for e in eps:
                e.update(date=d, split=split, dir=dirn, tag=tag)
                by_cond[name].append(e)
        if split == "DEV":
            n_dev += 1
        else:
            n_hold += 1
        if (si + 1) % 200 == 0:
            print(f"  ...scored {si+1}/{len(cache)} sessions", flush=True)

    print(f"\n  scored sessions: DEV={n_dev}  HOLDOUT={n_hold}\n")

    # ---------- ambient drift baseline ----------
    print("=" * 118)
    print("AMBIENT +30min drift baseline (every in-window bar; the base-rate to beat)")
    for split in ("DEV", "HOLD"):
        su = dist_stats(ambient[(split, "UP")])
        if su:
            print(f"  {split:5s} UP(+1) ALL: mean={su['mean']:+.3f}pt  median={su['median']:+.3f}  "
                  f"favmass={su['favmass']:.1f}%  n={su['n']}")
    print("  -- AMBIENT UP by PRIOR-VIX bucket (does high VIX alone predict up-drift?) --")
    for split in ("DEV", "HOLD"):
        cells = []
        for lbl, _, _ in VIX_BUCKETS:
            s = dist_stats(ambient[(split, lbl)])
            cells.append(f"{lbl}:{(s['mean'] if s else float('nan')):+.2f}({(s['n'] if s else 0)})")
        print(f"    {split:5s} " + "  ".join(cells))
    print()

    # ---------- MECHANISM CONTROL: confluence vs drop-only vs ambient, matched VIX gate ----------
    print("=" * 118)
    print("MECHANISM CONTROL — does the exhaustion CONFLUENCE beat 'just a drop on a high-VIX day'?")
    print("  compare CAPFADE_UP (full stall+bounce+extension confluence) vs DROP_ONLY (down-leg+VIX")
    print("  only, no geometry) vs AMBIENT, at matched gates. +30min meanFav / favmass / n(ep).")
    for gate, capname, dropname in (("VIX>=20", "CAPFADE_UP@VIX>=20", "DROP_ONLY@VIX>=20"),
                                    ("VIX>=25", "CAPFADE_UP@VIX>=25", "DROP_ONLY@VIX>=25")):
        print(f"  --- gate {gate} ---")
        for split in ("DEV", "HOLD"):
            cs = dist_stats([r.get("fav30") for r in by_cond[capname] if r["split"] == split])
            dr = dist_stats([r.get("fav30") for r in by_cond[dropname] if r["split"] == split])
            def g(s):
                return f"{s['mean']:+6.2f}pt {s['favmass']:3.0f}% n={s['n']:<4d}" if s else f"{'—':>18s}"
            print(f"    {split:5s}  CONFLUENCE {g(cs)}   DROP_ONLY {g(dr)}")
    print()

    # ---------- primary table: +30min, DEV vs HOLDOUT ----------
    order = ["CAPFADE_UP@VIX>=20", "BLOWOFF_DN@VIX>=20", "TREND_DN@VIX>=20", "TREND_UP@VIX<20",
             "CAPFADE_UP@VIX>=15", "CAPFADE_UP@VIX>=25", "CAPFADE_UP@VIX>=30",
             "CAPFADE_UP@backwrd9", "CAPFADE_UP+aggrcall", "TREND_DN+driftBear"]
    K = len(order)
    bonf_t = 2.9   # ~ z at family-wise 0.05 / K for K~10-12 (two-sided)

    def block(name, split, key):
        recs = [r for r in by_cond[name] if r["split"] == split]
        st = dist_stats([r.get(key) for r in recs])
        t, ns = clustered_t([r for r in recs], key)
        return st, t, ns

    for key, hlabel in (("fav30", "+30min"), ("fav10", "+10min"), ("favTR", "trailing 8pt/60m")):
        print("=" * 118)
        print(f"HORIZON {hlabel}  — conditional forward move (fav = signed move in trade dir); "
              f"DEV vs HOLDOUT")
        print(f"  {'condition':22s} {'dir':4s} | DEV: "
              f"{'n/sess':>10s} {'meanFav':>7s} {'mass%':>5s} {'>15':>4s} {'>30':>4s} {'tcl':>5s} "
              f"|| HOLD: {'n/sess':>10s} {'meanFav':>7s} {'mass%':>5s} {'tcl':>5s}")
        for name in order:
            dirn, tag = cond_meta[name]
            ds, dt_, dns = block(name, "DEV", key)
            hs, ht_, hns = block(name, "HOLD", key)
            dside = "CALL" if dirn > 0 else "PUT"

            def cell(st, t, ns):
                if st is None:
                    return f"{'—':>10s} {'—':>7s} {'—':>5s} {'—':>4s} {'—':>4s} {'—':>5s}"
                return (f"{st['n']:4d}/{ns:<5d} {st['mean']:+7.2f} {st['favmass']:4.0f}% "
                        f"{st['p15']:3.0f}% {st['p30']:3.0f}% {t:+5.2f}")

            def cellh(st, t, ns):
                if st is None:
                    return f"{'—':>10s} {'—':>7s} {'—':>5s} {'—':>5s}"
                return f"{st['n']:4d}/{ns:<5d} {st['mean']:+7.2f} {st['favmass']:4.0f}% {t:+5.2f}"

            print(f"  {name:22s} {dside:4s} | {cell(ds, dt_, dns)} || {cellh(hs, ht_, hns)}")
        print()

    # ---------- robustness: delayed-entry +30 (top primary conditions) ----------
    print("=" * 118)
    print("ROBUSTNESS — one-bar-DELAYED entry (rp[i+1]) at +30min vs confirmation-close entry")
    print(f"  {'condition':22s} | {'DEV close':>10s} {'DEV delay':>10s} || {'HOLD close':>10s} {'HOLD delay':>10s}")
    for name in order[:4] + ["CAPFADE_UP@VIX>=25", "CAPFADE_UP@VIX>=30"]:
        row = f"  {name:22s} |"
        for split in ("DEV", "HOLD"):
            recs = [r for r in by_cond[name] if r["split"] == split]
            c = dist_stats([r.get("fav30") for r in recs])
            dl = dist_stats([r.get("fav30_delay") for r in recs])
            row += f" {(c['mean'] if c else float('nan')):+10.2f} {(dl['mean'] if dl else float('nan')):+10.2f}"
            if split == "DEV":
                row += " ||"
        print(row)
    print()

    # ---------- verdict scoring ----------
    print("=" * 118)
    print("VERDICT — a condition SURVIVES if: DEV & HOLD favmass>50 (same-side asymmetry), DEV & HOLD")
    print(f"  meanFav same sign as intended, DEV clustered-t clears the Bonferroni bar |t|>{bonf_t:.1f}")
    print(f"  (K={K} conditions scanned, family-wise 0.05). HOLD is small-n -> require sign+mass, not |t|.")
    print("=" * 118)
    print(f"  {'condition':22s} {'DEVmean':>8s} {'DEVmass':>7s} {'DEVt':>6s} {'HOLDmean':>8s} "
          f"{'HOLDmass':>8s} {'HOLDt':>6s}  verdict")
    survivors, suggestive = [], []
    for name in order:
        dirn = cond_meta[name][0]
        ds, dt_, dns = block(name, "DEV", "fav30")
        hs, ht_, hns = block(name, "HOLD", "fav30")
        if ds is None or hs is None:
            print(f"  {name:22s} {'insufficient data':>50s}")
            continue
        # fav is already signed in the trade direction, so ">0 & mass>50" = favorable for either side
        dev_fav = ds["mean"] > 0 and ds["favmass"] > 50
        hold_fav = hs["mean"] > 0 and hs["favmass"] >= 50
        if dev_fav and abs(dt_) > bonf_t and hold_fav:
            verdict = "SURVIVES (confirmed)"; survivors.append(name)
        elif dev_fav and hold_fav:
            verdict = "SUGGESTIVE (mech ok, sub-thresh)"; suggestive.append(name)
        elif dev_fav and abs(dt_) > 2 and not hold_fav:
            verdict = "DEV-only MIRAGE (dies OOS)"
        elif not dev_fav:
            verdict = "ADVERSE (wrong-sign/mass<50 on DEV)"
        else:
            verdict = "no edge"
        print(f"  {name:22s} {ds['mean']:+8.2f} {ds['favmass']:6.0f}% {dt_:+6.2f} {hs['mean']:+8.2f} "
              f"{hs['favmass']:7.0f}% {ht_:+6.2f}  {verdict}")
    print(f"\n  SURVIVORS (DEV clustered-|t|>{bonf_t} + favorable OOS): "
          f"{', '.join(survivors) if survivors else 'NONE'}")
    print(f"  SUGGESTIVE (favorable sign+asymmetry DEV & OOS, but DEV t below the Bonferroni bar): "
          f"{', '.join(suggestive) if suggestive else 'NONE'}")

    # ---------- CSV dump ----------
    csv = os.path.join(SCRATCH, "confluence_expectancy.csv")
    with open(csv, "w") as f:
        f.write("condition,dir,tag,split,horizon,n_ep,n_sess,mean,median,favmass,p15,p30,adv15,tclust\n")
        for name in order:
            dirn, tag = cond_meta[name]
            for split in ("DEV", "HOLD"):
                recs = [r for r in by_cond[name] if r["split"] == split]
                for key, hl in (("fav10", "+10"), ("fav30", "+30"), ("favTR", "trail")):
                    st = dist_stats([r.get(key) for r in recs])
                    t, ns = clustered_t(recs, key)
                    if st is None:
                        continue
                    f.write(f"{name},{dirn},{tag},{split},{hl},{st['n']},{ns},{st['mean']:.3f},"
                            f"{st['median']:.3f},{st['favmass']:.1f},{st['p15']:.1f},{st['p30']:.1f},"
                            f"{st['adv15']:.1f},{t:.3f}\n")
    print(f"\n  per-condition CSV -> {csv}")
    print("=" * 118)


if __name__ == "__main__":
    main()
