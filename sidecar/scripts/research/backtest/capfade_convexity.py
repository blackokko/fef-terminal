"""CAPITULATION-FADE -> OTM CALL convexity monetization  (READ-ONLY research spike).

QUESTION (from the operator): confluence_expectancy found ONE suggestive edge --
CAPFADE_UP -> CALLS in elevated vol (+4..+8pt on the independent IBKR ruler, sharpest on the
~10-min snapback, DEV clustered-t ~+2.1, HOLD directionally-positive but under-powered).
Does it MONETIZE in REAL OPTION DOLLARS as a cheap-OTM-call convexity play?  i.e. does the
snapback + option convexity (gamma) beat the bid/ask spread + theta?  And does MORE-OTM
(cheaper / more convex) beat ATM on the snapback?

SIGNAL (reused verbatim, NOT re-derived): the CAPFADE_UP geometry from confluence_expectancy
  (sharp down-leg >=15pt over 5min + velocity-stall + >=3pt bounce off the short-window low +
  within 6pt of the 30-min extension low), gated by  VIX>=25  OR  VIX9D>VIX backwardation
  (prior-day closes, causal SOD).  This is exactly the union of confluence's CAPFADE_UP@VIX>=25
  and CAPFADE_UP@backwrd9 -- the two suggestive cuts.  Cache: conf_cache_full.pkl.  Episodes are
  the first bar of each consecutive-condition run (confluence_expectancy.episodes), 09:45-15:00 ET.

OPTION FILLS (reused from convexity_regime_exit): option_quote_history (ThetaData SPXW 1-min NBBO,
  one 0DTE expiry/session, 40 strikes @5pt).  NO-LOOKAHEAD: the signal is confirmed by bar i's
  CLOSE, so we fill at the first quote at/after t0+FILL_LAG_MS (=t0+60s, the bar close).  BUY at
  ASK, SELL at BID (real spread in).  right=C (long call).  Skip if the strike/minute quote is
  missing/bad or ask<$0.10 (counted).

STRIKE LADDER (the convexity test): ATM, +10, +20, +30 pt OTM (higher strikes = cheaper = more
  convex).  Strike centered on the ruler spot at the fill.

EXITS (each reported): (a) fixed +10min hold (the sharpest snapback horizon); (b) managed
  scale-out that banks the snapback (sell 1/3 @+50%, 1/3 @+100%, breakeven-stop after the first
  rung, trail the runner 35% off peak, hard cap 15:45 ET) [convexity_regime_exit.exit_scaleout];
  (c) +100% target / -60% stop bracket, 15:45 ET time-cap [convexity_regime_exit.exit_bracket].

METRICS -- the FULL distribution (convexity/expectancy, NOT hit-rate).  Per (strike x exit),
  POOLED and DEV(2022-01-03..2025-08-12) vs HOLDOUT(2025-08-13..2026-06-26) separately:
  mean & MEDIAN net $/contract and net %, win%, tail (%>+50/+100/+200%), worst loss,
  session-clustered t (unit=session), N (trades + sessions).

GREEK DECOMPOSITION (reused from pnl_decomposition): split the +10min realized $ into
  DIRECTION(delta+gamma) / THETA / VEGA / SPREAD to quantify whether OTM convexity (gamma) pays
  and how much theta+spread cost.  BSM greeks (validated ~IBKR by pnl_decomposition); real IBKR
  greeks substituted where the 2026-05-27+ overlap exists.

Run:  .venv/Scripts/python.exe -m scripts.research.backtest.capfade_convexity
Engine/live code untouched.  CSVs -> scratchpad.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import sys
from collections import defaultdict

import numpy as np

from . import confluence_expectancy as CE
from . import convexity_regime_exit as CX
from . import pnl_decomposition as PD
from . import trend_breakout_entry as TB
from .data import Session
from .greeks import greeks, implied_vol
from .holdout import HOLDOUT_START

sys.stdout.reconfigure(encoding="utf-8")

SCRATCH = CE.SCRATCH
FILL_LAG_MS = 60_000            # no-lookahead: fill at the bar close (t0+60s)
MULT = 100.0
MIN_ASK = 0.10                  # skip degenerate near-zero fills (counted)
HOLD10_MS = 10 * 60_000         # exit (a): fixed +10min snapback horizon
SO_R1, SO_R2, SO_GIVE, SO_CAP_H = 1.5, 2.0, 0.35, 15.75   # exit (b) scale-out
BR_TGT, BR_STOP = 2.0, 0.40     # exit (c): +100% target / -60% stop
CAP_H = 15.75                   # 15:45 ET hard cap (never hold 0DTE to close)
OTM_LADDER = [("ATM", 0.0), ("+10", 10.0), ("+20", 20.0), ("+30", 30.0)]

DEV_START, DEV_END = "2022-01-03", "2025-08-12"
HOLD_START, HOLD_END = "2025-08-13", "2026-06-26"


# ============================================================ signal (reused from CE)
def cap_geo(f):
    """CAPFADE_UP geometry, verbatim from confluence_expectancy.conditions().cap (references CE's
    pre-registered constants so it stays byte-identical)."""
    return (f["down_leg"] and f["rng_s"] >= CE.MIN_MOVE and f["vel"] is not None
            and f["vel"] >= -CE.VEL_STALL and f["off_low"] >= CE.BOUNCE_PT
            and f["ext_from_low"] <= CE.EXT_NEAR)


def gate(vprev):
    """VIX>=25 OR VIX9D>VIX backwardation (union of the two suggestive cuts)."""
    if not vprev:
        return False
    vix, v9 = vprev.get("vix"), vprev.get("vix9d")
    backwardation = (v9 is not None and vix is not None and v9 > vix)
    return (vix is not None and vix >= 25.0) or backwardation


def et_cap_ms(t0):
    """epoch-ms of 15:45 ET on the trade's date (the hard time cap for (c))."""
    d = dt.datetime.fromtimestamp(t0 / 1000, CE.ET)
    cap = d.replace(hour=15, minute=45, second=0, microsecond=0)
    return int(cap.timestamp() * 1000)


# ============================================================ decomposition (reused from PD)
def decompose_h(sess: Session, tf: int, right: str, strike: float, hold_ms: int, real_g=None):
    """pnl_decomposition.decompose, generalized to an arbitrary hold horizon (default use = the
    +10min snapback).  Splits realized $ into DIRECTION(delta+gamma)/THETA/VEGA/SPREAD/RESID."""
    em = sess.minute_at_or_after(tf)
    if em is None or em - tf > 90_000:
        return None
    qe = sess.quote(em, strike, right)
    if not PD._valid_q(qe):
        return None
    bid0, ask0, _ = qe
    mid0 = (bid0 + ask0) / 2.0
    if ask0 < MIN_ASK:
        return None
    S0 = sess.spot(em)
    if S0 is None:
        return None
    T0 = PD.mins_to_1600(em) / PD.YEAR_MIN
    if T0 <= 0:
        return None
    iv0 = implied_vol(mid0, S0, strike, T0, right, r=0.0)
    if iv0 is None:
        return None
    g = real_g if real_g is not None else greeks(S0, strike, T0, iv0, right, r=0.0)
    if g is None:
        return None
    xm = sess.minute_at_or_after(em + hold_ms) or sess.minute_at_or_before(em + hold_ms)
    if xm is None or xm <= em:
        return None
    qx = sess.quote(xm, strike, right)
    if not PD._valid_q(qx):
        return None
    bid1, ask1, _ = qx
    mid1 = (bid1 + ask1) / 2.0
    S1 = sess.spot(xm)
    if S1 is None:
        return None
    T1 = max(0.0, PD.mins_to_1600(xm) / PD.YEAR_MIN)
    iv1 = implied_vol(mid1, S1, strike, T1, right, r=0.0) if T1 > 0 else None
    dS = S1 - S0
    dt_days = (T0 - T1) * 365.0
    d_iv_volpts = ((iv1 - iv0) * 100.0) if iv1 is not None else 0.0
    delta_d = g["delta"] * dS * 100.0
    gamma_d = 0.5 * g["gamma"] * dS * dS * 100.0
    theta_d = g["theta"] * dt_days * 100.0
    vega_d = g["vega"] * d_iv_volpts * 100.0
    mid2mid = (mid1 - mid0) * 100.0
    resid = mid2mid - (delta_d + gamma_d + theta_d + vega_d)
    realized = (bid1 - ask0) * 100.0
    spread = mid2mid - realized
    return dict(realized=realized, mid2mid=mid2mid, spread=spread,
                direction=delta_d + gamma_d, delta=delta_d, gamma=gamma_d,
                theta=theta_d, vega=vega_d, resid=resid, dS=dS, hold_min=(xm - em) / 60000.0,
                entry_px=ask0, iv0=iv0, used_real=(real_g is not None),
                ret=(bid1 / ask0 - 1.0))


# ============================================================ stats
def stats(recs, dkey, pkey):
    """Full-distribution summary for one ($ key, % key) over recs.  None if empty."""
    rr = [r for r in recs if r.get(dkey) is not None]
    if not rr:
        return None
    d = np.array([r[dkey] for r in rr], float)
    p = np.array([r[pkey] for r in rr], float)
    tcl, ns = CX.clustered_t(rr, dkey)
    return dict(
        n=len(rr), nsess=ns,
        mean_d=float(d.mean()), med_d=float(np.median(d)),
        mean_p=100.0 * float(p.mean()), med_p=100.0 * float(np.median(p)),
        win=100.0 * float(np.mean(d > 0)),
        t50=100.0 * float(np.mean(p > 0.5)), t100=100.0 * float(np.mean(p > 1.0)),
        t200=100.0 * float(np.mean(p > 2.0)),
        worst_p=100.0 * float(p.min()), worst_d=float(d.min()), tcl=tcl)


def _spx_snapback(recs, key):
    a = np.array([r[key] for r in recs if r.get(key) is not None], float)
    if len(a) == 0:
        return None
    by = defaultdict(list)
    for r in recs:
        if r.get(key) is not None:
            by[r["date"]].append(r[key])
    sm = np.array([np.mean(v) for v in by.values()], float)
    t = float(sm.mean() / (sm.std(ddof=1) / np.sqrt(len(sm)))) if len(sm) > 1 and sm.std(ddof=1) > 0 else float("nan")
    return dict(n=len(a), mean=float(a.mean()), med=float(np.median(a)), t=t, nsess=len(by))


# ============================================================ build trades
def build():
    cache = CE.build_full_cache()
    prior = CE.load_vix_prior()
    con = CX.sqlite3.connect(f"file:{CX.DB}?mode=ro", uri=True)
    real = PD.load_real_greeks(con)
    print(f"  real-greek snapshots (for decomposition overlay): {len(real)}", flush=True)

    trades = []           # one row per (episode x strike rule) that filled
    episodes_all = []     # one row per episode (for SPX-ruler snapback sanity + counts)
    skip = defaultdict(lambda: defaultdict(int))   # rule -> reason -> count
    n_ep_dev = n_ep_hold = 0

    for si, sess in enumerate(cache):
        d = sess["date"]
        if d < DEV_START or d > HOLD_END:
            continue
        vprev = prior(d)
        if not gate(vprev):
            # gate is a session-level (prior-VIX) condition; skip whole session if it fails
            continue
        split = "DEV" if d < HOLDOUT_START else "HOLD"
        rt, rp, feats = CE.session_features(sess)
        rt = np.asarray(rt, np.int64)
        rp = np.asarray(rp, float)
        eps = CE.episodes(rt, rp, feats, +1, lambda f: cap_geo(f) and gate(vprev))
        if not eps:
            continue
        by_kr, strikes = CX.load_option_session(con, d)
        osess = Session(con, d)     # for greek decomposition (parity spot)
        for e in eps:
            e.update(date=d, split=split)
            episodes_all.append(e)
            if split == "DEV":
                n_ep_dev += 1
            else:
                n_ep_hold += 1
            t0 = int(e["ts"])
            tf = t0 + FILL_LAG_MS
            s_fill = TB.real_at(rt, rp, tf)
            hz_c = et_cap_ms(t0) - tf                      # bracket time-cap horizon to 15:45 ET
            for rule, off in OTM_LADDER:
                rec = dict(date=d, split=split, rule=rule, t0=t0, s_fill=s_fill,
                           strike=None, a0=None, vix=(vprev or {}).get("vix"),
                           opt10=None, ret10=None, mfe=None, opt_so=None, ret_so=None,
                           opt_br=None, ret_br=None, br_reason=None)
                if by_kr is None or strikes is None or len(strikes) == 0 or s_fill is None:
                    skip[rule]["nostrike"] += 1
                    trades.append(rec); continue
                target = s_fill + off                       # CALL: OTM = higher strike
                k = float(strikes[int(np.argmin(np.abs(strikes - target)))])
                # require the chosen strike to actually be near the requested offset (<=3pt)
                if abs(k - target) > 3.0:
                    skip[rule]["nostrike"] += 1
                    trades.append(rec); continue
                arr = by_kr.get((k, "C"))
                if arr is None:
                    skip[rule]["nostrike"] += 1
                    trades.append(rec); continue
                ts, bd, ak, qq = arr
                j0 = CX._nearest_idx(ts, tf)
                if j0 is None or not CX._valid(bd[j0], ak[j0], qq[j0]):
                    skip[rule]["badquote"] += 1
                    trades.append(rec); continue
                a0 = float(ak[j0])
                if a0 < MIN_ASK:
                    skip[rule]["minask"] += 1
                    trades.append(rec); continue
                rec["strike"] = k; rec["a0"] = a0
                # (a) fixed +10min hold
                xh, peak = CX.exit_hold(ts, bd, qq, j0, a0, tf, HOLD10_MS)
                if xh is not None:
                    rec["opt10"] = (xh - a0) * MULT
                    rec["ret10"] = xh / a0 - 1.0
                    rec["mfe"] = peak / a0 - 1.0
                # (b) managed scale-out
                xs = CX.exit_scaleout(ts, bd, qq, j0, a0, tf, SO_R1, SO_R2, SO_GIVE, SO_CAP_H)
                rec["opt_so"] = (xs - a0) * MULT
                rec["ret_so"] = xs / a0 - 1.0
                # (c) +100% target / -60% stop, 15:45 cap
                xb, why = CX.exit_bracket(ts, bd, qq, j0, a0, tf, BR_TGT, BR_STOP, hz_c)
                if xb is not None:
                    rec["opt_br"] = (xb - a0) * MULT
                    rec["ret_br"] = xb / a0 - 1.0
                    rec["br_reason"] = why
                # greek decomposition over the +10min snapback (real greeks where available)
                key = (d, int(round(tf / 60000)), k, "C")
                dec = decompose_h(osess, tf, "C", k, HOLD10_MS, real_g=real.get(key))
                if dec is not None:
                    rec["dec"] = dec
                skip[rule]["ok"] += 1
                trades.append(rec)
        if (si + 1) % 200 == 0:
            print(f"  ...scanned {si+1}/{len(cache)} sessions "
                  f"(episodes so far DEV={n_ep_dev} HOLD={n_ep_hold})", flush=True)

    con.close()
    print(f"\n  CAPFADE_UP@(VIX>=25 or backwrd) episodes: DEV={n_ep_dev}  HOLDOUT={n_ep_hold}  "
          f"(total {n_ep_dev + n_ep_hold})")
    return trades, episodes_all, skip


# ============================================================ report
def fmt_row(lbl, s):
    if s is None:
        return f"    {lbl:26s} {'(no trades)':>12s}"
    return (f"    {lbl:26s} {s['n']:4d}/{s['nsess']:<4d} "
            f"{s['mean_d']:+8.1f} {s['med_d']:+7.1f} | {s['mean_p']:+7.1f} {s['med_p']:+7.1f} "
            f"{s['win']:4.0f}% | {s['t50']:4.0f} {s['t100']:4.0f} {s['t200']:4.0f} | "
            f"{s['worst_p']:+6.0f}% {s['tcl']:+5.2f}")


def report_exit(trades, exit_lbl, dkey, pkey):
    print("=" * 122)
    print(f"EXIT {exit_lbl}   net $/contract & net %  (buy ask / sell bid; MULT=$100/pt)")
    print(f"    {'strike x split':26s} {'N/sess':>9s} "
          f"{'mean$':>8s} {'med$':>7s} | {'mean%':>7s} {'med%':>7s} {'win':>5s} | "
          f"{'>50':>4s} {'>100':>4s} {'>200':>4s} | {'worst':>7s} {'tcl':>5s}")
    for rule, _ in OTM_LADDER:
        for split in ("DEV", "HOLD", "POOL"):
            sub = [t for t in trades if t["rule"] == rule and t.get(dkey) is not None
                   and (split == "POOL" or t["split"] == split)]
            s = stats(sub, dkey, pkey)
            print(fmt_row(f"{rule:4s} {split}", s))
        print()


def decomp_table(trades):
    print("=" * 122)
    print("GREEK DECOMPOSITION over the +10min snapback  (mean $/contract; realized = DIR+THETA+VEGA+RESID-SPREAD)")
    print(f"    {'strike x split':20s} {'n':>4s} {'realized':>9s} | {'DIR(d+g)':>9s} {'delta':>7s} "
          f"{'gamma':>7s} | {'THETA':>7s} {'VEGA':>7s} {'RESID':>7s} {'SPREAD':>7s} | {'meandS':>7s}")
    for rule, _ in OTM_LADDER:
        for split in ("DEV", "HOLD", "POOL"):
            rr = [t["dec"] for t in trades if t["rule"] == rule and "dec" in t
                  and (split == "POOL" or t["split"] == split)]
            if not rr:
                print(f"    {rule+' '+split:20s} {'(none)':>4s}")
                continue
            g = lambda k: float(np.mean([r[k] for r in rr]))
            print(f"    {rule+' '+split:20s} {len(rr):4d} {g('realized'):+9.1f} | "
                  f"{g('direction'):+9.1f} {g('delta'):+7.1f} {g('gamma'):+7.1f} | "
                  f"{g('theta'):+7.1f} {g('vega'):+7.1f} {g('resid'):+7.1f} {-g('spread'):+7.1f} | "
                  f"{g('dS'):+7.2f}")
        print()


def examples(trades):
    print("=" * 122)
    print("EXAMPLE TRADES (line-by-line, $/contract; +10min snapback decomposition)")
    print(f"  {'case':30s} {'date':11s} {'K/rule':10s} {'dS':>6s} {'entry$':>7s} {'real$':>7s} | "
          f"{'DIR':>6s} {'THETA':>6s} {'VEGA':>6s} {'SPRD':>6s}")
    filled = [t for t in trades if "dec" in t and t.get("ret10") is not None]
    picks = []
    otm_win = [t for t in filled if t["rule"] in ("+20", "+30") and t["ret10"] > 1.0]
    atm_win = [t for t in filled if t["rule"] == "ATM" and t["ret10"] > 0.3]
    loser = [t for t in filled if t["ret10"] < -0.4]
    flat = sorted(filled, key=lambda t: abs(t["dec"]["dS"]))
    if otm_win:
        picks.append(("OTM WINNER (convexity/gamma)", max(otm_win, key=lambda t: t["ret10"])))
    if atm_win:
        picks.append(("ATM WINNER (snapback caught)", max(atm_win, key=lambda t: t["ret10"])))
    if loser:
        picks.append(("LOSER (reversed / theta+spread)", min(loser, key=lambda t: t["ret10"])))
    if flat:
        picks.append(("FLAT spot (pure decay+spread)", flat[0]))
    for lbl, t in picks:
        de = t["dec"]
        print(f"  {lbl:30s} {t['date']:11s} {str(t['strike'])+'/'+t['rule']:10s} "
              f"{de['dS']:+6.1f} {de['entry_px']*100:7.0f} {de['realized']:+7.0f} | "
              f"{de['direction']:+6.0f} {de['theta']:+6.0f} {de['vega']:+6.0f} {-de['spread']:+6.0f}   "
              f"(ret10 {t['ret10']*100:+.0f}%)")


def dump_csv(trades):
    path = os.path.join(SCRATCH, "capfade_convexity_trades.csv")
    cols = ["date", "split", "rule", "t0", "s_fill", "strike", "a0", "vix",
            "opt10", "ret10", "mfe", "opt_so", "ret_so", "opt_br", "ret_br", "br_reason"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            w.writerow(t)
    print(f"\n  per-trade CSV -> {path}  ({len(trades)} rows)")

    spath = os.path.join(SCRATCH, "capfade_convexity_summary.csv")
    with open(spath, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["exit", "rule", "split", "n", "nsess", "mean_d", "med_d", "mean_pct",
                    "med_pct", "win", "p50", "p100", "p200", "worst_pct", "tcl"])
        for exit_lbl, dkey, pkey in (("+10min", "opt10", "ret10"),
                                     ("scaleout", "opt_so", "ret_so"),
                                     ("bracket", "opt_br", "ret_br")):
            for rule, _ in OTM_LADDER:
                for split in ("DEV", "HOLD", "POOL"):
                    sub = [t for t in trades if t["rule"] == rule and t.get(dkey) is not None
                           and (split == "POOL" or t["split"] == split)]
                    s = stats(sub, dkey, pkey)
                    if s is None:
                        continue
                    w.writerow([exit_lbl, rule, split, s["n"], s["nsess"],
                                f"{s['mean_d']:.2f}", f"{s['med_d']:.2f}", f"{s['mean_p']:.2f}",
                                f"{s['med_p']:.2f}", f"{s['win']:.1f}", f"{s['t50']:.1f}",
                                f"{s['t100']:.1f}", f"{s['t200']:.1f}", f"{s['worst_p']:.1f}",
                                f"{s['tcl']:.3f}"])
    print(f"  summary CSV   -> {spath}")


# ============================================================ main
def main():
    print("=" * 122)
    print("CAPITULATION-FADE -> OTM CALL convexity monetization  (real 0DTE option $, no-lookahead t0+60s fills)")
    print(f"  signal = CAPFADE_UP geometry gated by VIX>=25 OR VIX9D>VIX backwardation (prior-close, causal)")
    print(f"  DEV {DEV_START}..{DEV_END}   |   HOLDOUT {HOLD_START}..{HOLD_END}")
    print("=" * 122)

    trades, eps, skip = build()

    # ---- SPX-ruler snapback sanity (the edge we are trying to monetize) ----
    print("\n" + "=" * 122)
    print("SPX-RULER SNAPBACK SANITY  (independent IBKR ruler, signed fav in the CALL direction; the edge to beat)")
    for split in ("DEV", "HOLD"):
        rr = [e for e in eps if e["split"] == split]
        s10 = _spx_snapback(rr, "fav10"); s30 = _spx_snapback(rr, "fav30")
        if s10:
            print(f"  {split:5s}  +10min: mean {s10['mean']:+.2f}pt med {s10['med']:+.2f} "
                  f"t={s10['t']:+.2f} (n={s10['n']}/{s10['nsess']}sess)   "
                  f"+30min: mean {s30['mean']:+.2f}pt med {s30['med']:+.2f} t={s30['t']:+.2f}")

    # ---- fill / skip accounting ----
    print("\n  option-fill accounting per strike rule (ok / skipped):")
    print(f"    {'rule':5s} {'ok':>5s} {'nostrike':>9s} {'badquote':>9s} {'minask':>7s}")
    for rule, _ in OTM_LADDER:
        s = skip[rule]
        print(f"    {rule:5s} {s['ok']:5d} {s['nostrike']:9d} {s['badquote']:9d} {s['minask']:7d}")

    # ---- main tables: strike x exit, DEV vs HOLD vs POOL ----
    print("\n")
    report_exit(trades, "(a) FIXED +10min HOLD", "opt10", "ret10")
    report_exit(trades, "(b) SCALE-OUT (1/3@+50%, 1/3@+100%, trail 35%, cap 15:45)", "opt_so", "ret_so")
    report_exit(trades, "(c) BRACKET +100% target / -60% stop (cap 15:45)", "opt_br", "ret_br")

    # ---- decomposition + examples ----
    decomp_table(trades)
    examples(trades)

    dump_csv(trades)
    print("\nDONE.")


if __name__ == "__main__":
    main()
