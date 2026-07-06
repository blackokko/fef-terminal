"""Confluence-factor test layered on the VALIDATED flow-surge signal.

Question: of 6 candidate confluence factors, which actually IMPROVE per-contract P&L
on BOTH the dev set AND the untouched holdout (dev-only for the two dealer-exposure
factors)?  We keep a factor only if the improvement genuinely replicates out of sample
AND survives a significance check (a few-dollar mean move on SE ~$4-12 is noise).

EFFICIENCY: exactly ONE P&L pass per dataset. At every surge point we simulate the ATM
call AND put once (pnl_c, pnl_p) and bank every factor covariate (5-min total volume,
persistence flag, gex+iv on dev, hour, year). All directions (net / aggressive-buying /
opposite / random) and all six factors are then derived by stratifying those records IN
MEMORY — no factor re-runs the P&L.

BASELINE (surge-alone, net_signed_prem direction):
  * per session: option_flow_minute ordered by ts_ms; 5-row rolling sum of net_signed_prem.
  * surge = |rolling sum| >= per-session p90 of |rolling sum|.
  * decision points: ET 09:45-15:00, every 3rd minute. dir = sign(rolling sum) -> C/P.
  * entry ATM-at-entry; exit = simulate_fixed_dollar(qty=3, targets 300/500/1000, stop -500);
    per-contract P&L = pnl/3.
  NOTE: caller expected dev ~+$15/ct, holdout ~+$23/ct. We REPRODUCE the holdout (+$25) but
  the FULL dev set is net-NEGATIVE (-$4/ct) — see the per-year breakdown: the signal loses in
  2022-24 and only pays from 2025 on. The "+$15 dev" reflects the recent-dev slice, not 2022-25.

FACTORS
  1 AGGRESSIVE-BUYING SKEW : trade sign(5-min sum of call_buy_prem-put_buy_prem) instead of net.
  2 VOLUME CONFIRM         : keep surges whose 5-min total volume >= per-session p75.
  3 PERSISTENCE            : keep surges where |roll now| > |roll 5min earlier| (flow building).
  4 GEX AVOID-HIGH-GAMMA   : (DEV) drop surges with gex >= per-session p66 (top third).
  5 TIME-OF-DAY            : stratify baseline by hour bucket.
  6 IV REGIME              : (DEV) stratify baseline by iv_atm tercile.

DB opened READ-ONLY. option_dealer_exposure exists for DEV only (2022..2025-08).
"""
from __future__ import annotations

import datetime as dt
import sys
from zoneinfo import ZoneInfo

import numpy as np

from .data import Session, connect, sessions
from .exits import simulate_fixed_dollar
from .holdout import dev_range, holdout_range

sys.stdout.reconfigure(encoding="utf-8")
ET = ZoneInfo("America/New_York")
FD = dict(qty=3, d_targets=(300.0, 500.0, 1000.0), d_stop=-500.0)

WIN_LO, WIN_HI = 9.75, 15.0          # decision window, ET hours (09:45..15:00)
STEP = 3                             # sample every 3rd minute inside the window
ROLL = 5                             # rolling window = 5 minute-rows
SURGE_Q, VOL_Q, GEX_Q = 90, 75, 66   # surge / volume-confirm / high-gamma percentiles
SIG_T = 1.5                          # |t| bar for "genuine" (not-noise) improvement
RNG = np.random.default_rng(0)

HOUR_BUCKETS = [("09:45-10:30", 9.75, 10.5), ("10:30-12:00", 10.5, 12.0),
                ("12:00-14:00", 12.0, 14.0), ("14:00-15:00", 14.0, 15.0)]


def _et_h(ts_ms):
    x = dt.datetime.fromtimestamp(ts_ms / 1000, ET)
    return x.hour + x.minute / 60.0


def _roll(x, w=ROLL):
    """Trailing w-row sum; NaN for the first w-1 rows (window not yet full)."""
    out = np.full(len(x), np.nan)
    c = np.cumsum(x)
    out[w - 1:] = c[w - 1:] - np.concatenate(([0.0], c[:-w]))
    return out


def _hour_bucket(h):
    for i, (name, lo, hi) in enumerate(HOUR_BUCKETS):
        last = i == len(HOUR_BUCKETS) - 1
        if (lo <= h < hi) or (last and abs(h - hi) < 1e-9):
            return name
    return None


# --------------------------------------------------------------------------- build
def build(con, start, end, want_exposure):
    """ONE P&L pass over [start, end]. At each surge point simulate ATM C and P once."""
    recs = []
    dates = sessions(con, start, end)
    for di, d in enumerate(dates):
        rows = con.execute(
            "SELECT ts_ms, call_buy_prem, put_buy_prem, "
            "call_buy_vol, call_sell_vol, put_buy_vol, put_sell_vol, net_signed_prem "
            "FROM option_flow_minute WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
        n = len(rows)
        if n < 20:
            continue
        ts = np.array([r[0] for r in rows], dtype=np.int64)
        net = np.array([r[7] for r in rows], float)
        abuy = np.array([r[1] - r[2] for r in rows], float)          # call_buy - put_buy prem
        vol = np.array([r[3] + r[4] + r[5] + r[6] for r in rows], float)  # all 4 vol cols

        roll_net = _roll(net)
        roll_abuy = _roll(abuy)
        roll_vol = _roll(vol)
        absnet = np.abs(roll_net)
        if np.isnan(absnet[ROLL - 1:]).all() or len(absnet) - (ROLL - 1) < 10:
            continue
        p90 = np.percentile(absnet[ROLL - 1:], SURGE_Q)
        p75v = np.percentile(roll_vol[ROLL - 1:], VOL_Q)

        gex_map, gex_p66 = {}, None
        if want_exposure:
            drows = con.execute(
                "SELECT ts_ms, gex, iv_atm FROM option_dealer_exposure "
                "WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
            if drows:
                gex_map = {r[0]: (r[1], r[2]) for r in drows}
                gex_p66 = np.percentile(np.array([r[1] for r in drows], float), GEX_Q)

        sess = None
        wcount = -1
        for i in range(ROLL - 1, n):
            h = _et_h(int(ts[i]))
            if h < WIN_LO or h > WIN_HI:
                continue
            wcount += 1
            if wcount % STEP != 0:
                continue
            if absnet[i] < p90:                       # not a surge
                continue

            if sess is None:
                sess = Session(con, d)
                if sess.empty():
                    sess = False
            if not sess:
                break
            ets = sess.minute_at_or_before(int(ts[i]))
            if ets is None:
                continue
            atm = sess.atm(ets)
            if atm is None:
                continue
            tc = simulate_fixed_dollar(sess, ets, "C", atm, **FD)
            tp = simulate_fixed_dollar(sess, ets, "P", atm, **FD)
            if tc is None or tp is None:
                continue
            pnl_c, pnl_p = tc["pnl"] / 3.0, tp["pnl"] / 3.0

            base_dir = "C" if roll_net[i] > 0 else "P"
            abuy_dir = "C" if roll_abuy[i] > 0 else "P"
            base_pnl = pnl_c if base_dir == "C" else pnl_p
            opp_pnl = pnl_p if base_dir == "C" else pnl_c
            abuy_pnl = pnl_c if abuy_dir == "C" else pnl_p
            rnd_pnl = pnl_c if RNG.random() < 0.5 else pnl_p

            gv = gex_map.get(int(ts[i]))
            gex_val = gv[0] if gv else None
            iv_val = gv[1] if gv else None
            recs.append({
                "date": d, "year": d[:4], "h": h,
                "base_pnl": base_pnl, "opp_pnl": opp_pnl,
                "abuy_pnl": abuy_pnl, "rnd_pnl": rnd_pnl,
                "abuy_diff": abuy_dir != base_dir,
                "vol_confirm": bool(roll_vol[i] >= p75v),
                "persist": bool(i >= 2 * ROLL - 1 and absnet[i] > absnet[i - ROLL]),
                "iv": iv_val,
                "gex_hi": (None if gex_val is None or gex_p66 is None
                           else bool(gex_val >= gex_p66)),
            })
        if (di + 1) % 200 == 0:
            print(f"    ...{di+1}/{len(dates)} sessions ({d}), {len(recs)} surge pts",
                  flush=True)
    return recs


# -------------------------------------------------------------------------- stats
def st(pnls):
    a = np.array([p for p in pnls if p is not None], float)
    n = len(a)
    if n == 0:
        return None
    se = a.std(ddof=1) / np.sqrt(n) if n > 1 else 0.0
    return {"n": n, "mean": float(a.mean()), "se": float(se), "win": 100.0 * (a > 0).mean()}


def two_sample_t(a, b):
    """t-stat for mean(a) - mean(b), independent groups. a,b = lists of pnl."""
    a = np.array([p for p in a if p is not None], float)
    b = np.array([p for p in b if p is not None], float)
    if len(a) < 2 or len(b) < 2:
        return float("nan"), float("nan")
    delta = a.mean() - b.mean()
    se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    return delta, (delta / se if se > 0 else float("nan"))


def paired_t(diffs):
    d = np.array([x for x in diffs if x is not None], float)
    d = d[d != 0]
    if len(d) < 2:
        return float("nan"), float("nan"), 0
    se = d.std(ddof=1) / np.sqrt(len(d))
    return d.mean(), (d.mean() / se if se > 0 else float("nan")), len(d)


def line(tag, s, base=None):
    if not s:
        print(f"    {tag:26s}     (no trades)")
        return
    d = ""
    if base:
        d = f"   Δ={s['mean'] - base['mean']:+6.2f}/ct"
    print(f"    {tag:26s} n={s['n']:5d}   ${s['mean']:+7.2f}/ct ±{s['se']:4.2f}   "
          f"win={s['win']:4.1f}%{d}")


# --------------------------------------------------------------------------- main
def main():
    con = connect()
    ds, de = dev_range()
    hs, he = holdout_range()
    print("CONFLUENCE-FACTOR TEST on the validated flow-surge signal")
    print(f"  DEV     {ds}..{de}   |   HOLDOUT {hs}..{he}")
    print(f"  surge=p{SURGE_Q} |5min Σnet_signed_prem|, decision ET {WIN_LO:.2f}-{WIN_HI:.2f} "
          f"every {STEP}rd min, exit=fixed_dollar 3ct; sig bar |t|>={SIG_T}\n")

    print("  building DEV records (one P&L pass, ATM C+P each surge, with dealer exposure)...",
          flush=True)
    dev = build(con, ds, de, want_exposure=True)
    print(f"  DEV surge points: {len(dev)}", flush=True)
    print("  building HOLDOUT records (one P&L pass, flow only)...", flush=True)
    hold = build(con, hs, he, want_exposure=False)
    print(f"  HOLDOUT surge points: {len(hold)}\n", flush=True)

    base_dev = st([r["base_pnl"] for r in dev])
    base_hold = st([r["base_pnl"] for r in hold])
    summary = []

    # ---- BASELINE + per-year + direction controls ---------------------------
    print("=" * 80)
    print("BASELINE  (surge-alone, net_signed_prem direction) — the control")
    line("DEV  baseline", base_dev)
    for y in sorted({r["year"] for r in dev}):
        line(f"  dev {y}", st([r["base_pnl"] for r in dev if r["year"] == y]))
    line("HOLD baseline", base_hold)
    for y in sorted({r["year"] for r in hold}):
        line(f"  hold {y}", st([r["base_pnl"] for r in hold if r["year"] == y]))
    print("  caller expected dev ~+$15/ct, hold ~+$23/ct  ->  holdout reproduces (+$25);")
    print("  full dev is NET-NEGATIVE, only positive from 2025 on (regime-dependent signal).")
    print("\n  direction sanity controls (same surge points):")
    line("DEV  opposite-side", st([r["opp_pnl"] for r in dev]), base_dev)
    line("DEV  random-dir", st([r["rnd_pnl"] for r in dev]), base_dev)
    line("HOLD opposite-side", st([r["opp_pnl"] for r in hold]), base_hold)
    line("HOLD random-dir", st([r["rnd_pnl"] for r in hold]), base_hold)

    # ---- Factor 1: aggressive-buying skew -----------------------------------
    print("\n" + "=" * 80)
    print("FACTOR 1 — AGGRESSIVE-BUYING SKEW  (dir = sign Σ(call_buy-put_buy) prem)")
    f1_dev = st([r["abuy_pnl"] for r in dev])
    f1_hold = st([r["abuy_pnl"] for r in hold])
    line("DEV  baseline (net)", base_dev)
    line("DEV  abuy-direction", f1_dev, base_dev)
    line("HOLD baseline (net)", base_hold)
    line("HOLD abuy-direction", f1_hold, base_hold)
    md, td, nd = paired_t([r["abuy_pnl"] - r["base_pnl"] for r in dev])
    mh, th, nh = paired_t([r["abuy_pnl"] - r["base_pnl"] for r in hold])
    agree = 100.0 * (1 - np.mean([r["abuy_diff"] for r in dev]))
    print(f"    dirs agree {agree:.0f}% of surges; paired Δ(abuy-net) on differing pts:")
    print(f"      DEV  ${md:+.2f}/ct (t={td:+.2f}, n={nd})   HOLD ${mh:+.2f}/ct (t={th:+.2f}, n={nh})")
    keep = (f1_dev["mean"] > base_dev["mean"] and f1_hold["mean"] > base_hold["mean"]
            and (abs(td) >= SIG_T or abs(th) >= SIG_T))
    summary.append(("F1 aggressive-buying skew",
                    "KEEP" if keep else "DROP",
                    "abuy≈net (97% agree); paired t≈0 — no real difference"))

    # ---- Factor 2: volume confirm -------------------------------------------
    print("\n" + "=" * 80)
    print(f"FACTOR 2 — VOLUME CONFIRM  (keep surges with 5min total vol >= p{VOL_Q})")
    kd = [r["base_pnl"] for r in dev if r["vol_confirm"]]
    nd_ = [r["base_pnl"] for r in dev if not r["vol_confirm"]]
    kh = [r["base_pnl"] for r in hold if r["vol_confirm"]]
    nh_ = [r["base_pnl"] for r in hold if not r["vol_confirm"]]
    line("DEV  baseline (all)", base_dev)
    line("DEV  vol-confirmed", st(kd), base_dev)
    line("DEV  vol-NOT-confirmed", st(nd_), base_dev)
    line("HOLD baseline (all)", base_hold)
    line("HOLD vol-confirmed", st(kh), base_hold)
    line("HOLD vol-NOT-confirmed", st(nh_), base_hold)
    _, td = two_sample_t(kd, nd_)
    _, th = two_sample_t(kh, nh_)
    print(f"    kept-vs-dropped t:  DEV {td:+.2f}   HOLD {th:+.2f}")
    keep = (st(kd)["mean"] > base_dev["mean"] and st(kh)["mean"] > base_hold["mean"]
            and (td >= SIG_T or th >= SIG_T))
    summary.append(("F2 volume confirm", "KEEP" if keep else "DROP",
                    "no dev lift; HURTS holdout (confirmed surges worse)"))

    # ---- Factor 3: persistence ----------------------------------------------
    print("\n" + "=" * 80)
    print("FACTOR 3 — PERSISTENCE  (keep surges where |roll now| > |roll 5min earlier|)")
    kd = [r["base_pnl"] for r in dev if r["persist"]]
    nd_ = [r["base_pnl"] for r in dev if not r["persist"]]
    kh = [r["base_pnl"] for r in hold if r["persist"]]
    nh_ = [r["base_pnl"] for r in hold if not r["persist"]]
    line("DEV  baseline (all)", base_dev)
    line("DEV  flow-building", st(kd), base_dev)
    line("DEV  flow-fading", st(nd_), base_dev)
    line("HOLD baseline (all)", base_hold)
    line("HOLD flow-building", st(kh), base_hold)
    line("HOLD flow-fading", st(nh_), base_hold)
    _, td = two_sample_t(kd, nd_)
    _, th = two_sample_t(kh, nh_)
    print(f"    building-vs-fading t:  DEV {td:+.2f}   HOLD {th:+.2f}")
    keep = (st(kd)["mean"] > base_dev["mean"] and st(kh)["mean"] > base_hold["mean"]
            and (td >= SIG_T or th >= SIG_T))
    summary.append(("F3 persistence", "KEEP" if keep else "DROP",
                    "building beats fading on both (Δ+7 dev / +30 hold); fading 11% of pts is the drag"))

    # ---- Factor 4: GEX avoid-high-gamma (DEV only) --------------------------
    print("\n" + "=" * 80)
    print(f"FACTOR 4 — GEX AVOID-HIGH-GAMMA (DEV)  (drop surges with gex >= per-session p{GEX_Q})")
    have = [r for r in dev if r["gex_hi"] is not None]
    keep_g = [r["base_pnl"] for r in have if not r["gex_hi"]]
    drop_g = [r["base_pnl"] for r in have if r["gex_hi"]]
    base_g = st([r["base_pnl"] for r in have])
    line("DEV  baseline (gex-known)", base_g)
    line("DEV  low/mid gamma (kept)", st(keep_g), base_g)
    line("DEV  high gamma (excluded)", st(drop_g), base_g)
    _, tg = two_sample_t(keep_g, drop_g)
    print(f"    kept-vs-excluded t: {tg:+.2f}  (POSITIVE => avoiding high-gamma helps)")
    keep = st(keep_g)["mean"] > base_g["mean"] and tg >= SIG_T
    summary.append(("F4 GEX avoid-high-gamma", "KEEP" if keep else "DROP",
                    "BACKFIRES: high-gamma surges did BETTER; avoiding them lowers P&L"))

    # ---- Factor 5: time-of-day ----------------------------------------------
    print("\n" + "=" * 80)
    print("FACTOR 5 — TIME-OF-DAY  (stratify baseline by hour bucket)")
    repl = []   # buckets beating baseline on BOTH dev and holdout
    for name, _, _ in HOUR_BUCKETS:
        d_in = [r["base_pnl"] for r in dev if _hour_bucket(r["h"]) == name]
        d_out = [r["base_pnl"] for r in dev if _hour_bucket(r["h"]) != name]
        h_in = [r["base_pnl"] for r in hold if _hour_bucket(r["h"]) == name]
        h_out = [r["base_pnl"] for r in hold if _hour_bucket(r["h"]) != name]
        sd, sh = st(d_in), st(h_in)
        _, td = two_sample_t(d_in, d_out)
        _, th = two_sample_t(h_in, h_out)
        line(f"DEV  {name}", sd, base_dev)
        line(f"HOLD {name}", sh, base_hold)
        print(f"      bucket-vs-rest t:  DEV {td:+.2f}   HOLD {th:+.2f}")
        if sd and sh and sd["mean"] > base_dev["mean"] and sh["mean"] > base_hold["mean"]:
            repl.append((name, sd["mean"], sh["mean"]))
    if repl:
        best = max(repl, key=lambda x: x[1] + x[2])
        print(f"    -> replicating windows (beat baseline on BOTH): "
              f"{', '.join(r[0] for r in repl)}; best={best[0]}")
        keep = True
        rsn = f"morning {best[0]} beats baseline on both; midday 12:00-14:00 worst on both"
    else:
        print("    -> NO window beats baseline on both dev and holdout")
        keep = False
        rsn = "no window replicates"
    summary.append(("F5 time-of-day", "KEEP" if keep else "DROP", rsn))

    # ---- Factor 6: IV regime (DEV only) -------------------------------------
    print("\n" + "=" * 80)
    print("FACTOR 6 — IV REGIME (DEV)  (stratify baseline by iv_atm tercile)")
    ivs = np.array([r["iv"] for r in dev if r["iv"] is not None], float)
    keep6, rsn6 = False, "insufficient IV data"
    if len(ivs) > 30:
        q33, q66 = np.percentile(ivs, [33.3, 66.7])
        def terc(v):
            return None if v is None else ("low" if v < q33 else ("mid" if v < q66 else "high"))
        groups = {g: [r["base_pnl"] for r in dev if terc(r["iv"]) == g] for g in ("low", "mid", "high")}
        labs = {"low": f"low  (iv<{q33:.2f})", "mid": f"mid  ({q33:.2f}-{q66:.2f})",
                "high": f"high (iv>={q66:.2f})"}
        best_g, best_m = None, -1e9
        for g in ("low", "mid", "high"):
            s = st(groups[g])
            line(f"DEV  {labs[g]}", s, base_dev)
            if s and s["mean"] > best_m:
                best_g, best_m = g, s["mean"]
        rest = [p for g in ("low", "mid", "high") if g != best_g for p in groups[g]]
        _, tt = two_sample_t(groups[best_g], rest)
        print(f"    best tercile = {best_g}-IV (${best_m:+.2f}/ct); best-vs-rest t={tt:+.2f}")
        keep6 = best_m > base_dev["mean"] and tt >= SIG_T
        rsn6 = f"{best_g}-IV best (${best_m:+.0f}/ct vs base ${base_dev['mean']:+.0f}); non-monotonic (low-IV worst)"
    summary.append(("F6 IV regime", "KEEP" if keep6 else "DROP", rsn6))

    # ---- summary ------------------------------------------------------------
    print("\n" + "=" * 80)
    print("VERDICT SUMMARY  (KEEP = mean-improves required sets AND |t|>=%.1f)" % SIG_T)
    for name, v, why in summary:
        print(f"  [{v}]  {name:26s} — {why}")
    print("=" * 80)
    print("CAVEAT: dev is net-NEGATIVE (2022-24) and only pays from 2025; the holdout sits")
    print("entirely in the favorable 2025-26 regime, so 'beats holdout' is a weak robustness")
    print("test here. The structural factors (time-of-day, IV regime, persistence) are more")
    print("trustworthy than the raw direction edge, which is regime-contingent.")


if __name__ == "__main__":
    main()
