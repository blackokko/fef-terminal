"""GEX (gamma exposure) regime classifier.

LONG_GAMMA (suppressive)  — dealers long gamma; hedging dampens moves (mean-revert).
SHORT_GAMMA (amplifying)  — dealers short gamma; hedging amplifies moves (momentum).
TRANSITION                — spot within a band of the gamma-flip level.

SIGN CONVENTION (load-bearing — verified 2026-05-29 against the live official API):
  net_m > 0 at a strike = DEALER LONG gamma (call-dominant, suppressive).
  Total net_gex > 0 with spot ABOVE the flip => LONG_GAMMA.
  Reference-day check: 5/29 (quiet, SPX range 35.8) classified LONG_GAMMA with the
  gamma flip 13pts below spot — i.e. low realized vol <-> long/suppressive gamma,
  as the mechanism predicts. See verify_sign_convention().

  WARNING: the pre-2026-05-28 exposure_chain_point rows were backfilled from the
  OLD dashboard API and have a DIFFERENT structure (huge isolated put concentration
  at spot -> absurd far-OTM flips). They are NOT comparable to new-API rows and must
  be re-backfilled through the official API before use. classify() is only trustworthy
  on official-API ('live_poll' / new 'backfill') data.

gamma_flip: the strike where the cumulative net-gamma profile (summed low->high
strikes) crosses zero, interpolated, taken nearest spot.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

DEFAULT_TRANSITION_BAND = 5.0  # SPX pts around the flip

LONG_GAMMA = "LONG_GAMMA"
SHORT_GAMMA = "SHORT_GAMMA"
TRANSITION = "TRANSITION"
UNKNOWN = "UNKNOWN"


@dataclass
class RegimeResult:
    regime: str
    net_gex_m: float
    gamma_flip: float | None
    spot_vs_flip: float | None
    spot: float
    n_strikes: int


def classify(
    rows: list[tuple[float, float]],
    spot: float,
    *,
    transition_band: float = DEFAULT_TRANSITION_BAND,
) -> RegimeResult:
    """Classify a single GAMMA exposure snapshot.

    rows: (strike, net_m) pairs — net_m signed dealer gamma in millions
          (positive = dealer long gamma). Order-independent (sorted here).
    spot: underlying price at capture.
    """
    rows = sorted(rows, key=lambda r: r[0])
    if not rows:
        return RegimeResult(UNKNOWN, 0.0, None, None, spot, 0)
    net_gex = sum(nm for _, nm in rows)

    # gamma flip: cumulative net-gamma zero crossing, interpolated, nearest spot
    cum = 0.0
    prev_k = prev_cum = None
    crossings: list[float] = []
    for k, nm in rows:
        new_cum = cum + nm
        if prev_cum is not None and (
            (prev_cum < 0 <= new_cum) or (prev_cum > 0 >= new_cum)
        ):
            if new_cum != prev_cum:
                frac = (0.0 - prev_cum) / (new_cum - prev_cum)
                crossings.append(prev_k + frac * (k - prev_k))
            else:
                crossings.append(k)
        prev_k, prev_cum, cum = k, new_cum, new_cum

    flip = min(crossings, key=lambda s: abs(s - spot)) if crossings else None

    if flip is None:
        regime = LONG_GAMMA if net_gex > 0 else SHORT_GAMMA
        return RegimeResult(regime, net_gex, None, None, spot, len(rows))

    spot_vs_flip = spot - flip
    if abs(spot_vs_flip) <= transition_band:
        regime = TRANSITION
    elif spot > flip and net_gex > 0:
        regime = LONG_GAMMA
    elif spot < flip or net_gex < 0:
        regime = SHORT_GAMMA
    else:  # spot > flip but net_gex <= 0 — amplifying overrides
        regime = SHORT_GAMMA
    return RegimeResult(regime, net_gex, flip, spot_vs_flip, spot, len(rows))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _db() -> str:
    return os.path.expanduser("~/.fef-terminal/signal_log.db")


def load_snapshot(c: sqlite3.Connection, session_date: str, timestamp_ms: int) -> RegimeResult | None:
    rows = c.execute(
        "SELECT strike, net_m, spot_at_capture FROM exposure_chain_point "
        "WHERE greek_type='GAMMA' AND session_date=? AND timestamp_ms=? ORDER BY strike",
        (session_date, timestamp_ms),
    ).fetchall()
    if not rows:
        return None
    spot = rows[0][2]
    return classify([(r[0], r[1]) for r in rows], spot)


def regime_at(c: sqlite3.Connection, session_date: str, at_ms: int) -> RegimeResult | None:
    """Regime from the latest GAMMA snapshot at/before at_ms in the session.
    For single-EOD sessions this returns the one EOD snapshot (coarse — the
    caller must treat single-snapshot sessions as a weak, session-level label)."""
    ts = c.execute(
        "SELECT MAX(timestamp_ms) FROM exposure_chain_point "
        "WHERE greek_type='GAMMA' AND session_date=? AND timestamp_ms<=?",
        (session_date, at_ms),
    ).fetchone()[0]
    if ts is None:
        ts = c.execute(
            "SELECT MIN(timestamp_ms) FROM exposure_chain_point "
            "WHERE greek_type='GAMMA' AND session_date=?", (session_date,),
        ).fetchone()[0]
    if ts is None:
        return None
    return load_snapshot(c, session_date, ts)


def session_snapshot_counts(c: sqlite3.Connection) -> dict[str, int]:
    """Per-session GAMMA snapshot count — to split intraday vs single-EOD."""
    return {
        sd: n for sd, n in c.execute(
            "SELECT session_date, COUNT(DISTINCT timestamp_ms) FROM exposure_chain_point "
            "WHERE greek_type='GAMMA' GROUP BY session_date ORDER BY session_date"
        ).fetchall()
    }


def verify_sign_convention(c: sqlite3.Connection, *, min_snapshots: int = 5) -> dict:
    """Reference-day check: among INTRADAY (new-API) sessions, the quietest
    session (smallest SPX RTH range) should classify LONG_GAMMA at EOD. Returns
    a dict with the verdict so callers can refuse to run on a flipped sign."""
    counts = session_snapshot_counts(c)
    intraday = [sd for sd, n in counts.items() if n >= min_snapshots]
    out: dict = {"intraday_sessions": intraday, "checks": []}
    best = None
    for sd in intraday:
        rng = c.execute(
            "SELECT MAX(high)-MIN(low) FROM historical_bar WHERE symbol='SPX' "
            "AND bar_size='1 min' AND date(timestamp_ms/1000,'unixepoch','-4 hours')=?",
            (sd,),
        ).fetchone()[0]
        ts = c.execute("SELECT MAX(timestamp_ms) FROM exposure_chain_point "
                       "WHERE greek_type='GAMMA' AND session_date=?", (sd,)).fetchone()[0]
        res = load_snapshot(c, sd, ts)
        if res is None or rng is None:
            continue
        out["checks"].append({"session": sd, "spx_range": rng, "regime": res.regime,
                              "net_gex_m": res.net_gex_m, "flip": res.gamma_flip})
        if best is None or rng < best[1]:
            best = (sd, rng, res)
    if best:
        sd, rng, res = best
        out["quietest_session"] = sd
        out["quietest_regime"] = res.regime
        out["sign_ok"] = res.regime in (LONG_GAMMA, TRANSITION)
        out["note"] = (f"Quietest intraday session {sd} (range {rng:.1f}) -> {res.regime}. "
                       f"Expected LONG_GAMMA/TRANSITION if sign is correct.")
    else:
        out["sign_ok"] = None
        out["note"] = "No intraday sessions to verify against."
    return out


if __name__ == "__main__":
    c = sqlite3.connect(_db())
    v = verify_sign_convention(c)
    print("SIGN-CONVENTION VERIFICATION")
    for ch in v["checks"]:
        print(f"  {ch['session']}: range={ch['spx_range']:.1f}  regime={ch['regime']}  "
              f"net_gex={ch['net_gex_m']:+.0f}M  flip={ch['flip']}")
    print(f"  -> {v['note']}")
    print(f"  sign_ok = {v.get('sign_ok')}")
