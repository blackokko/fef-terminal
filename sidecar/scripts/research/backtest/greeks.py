"""Black-Scholes IV inversion + Greeks from ThetaData NBBO quotes.

Given an option mid, the parity-reconstructed underlying, strike and time-to-expiry,
we back out implied vol, then compute delta/gamma/vega/theta and the second-order
charm/vanna analytically — the same per-contract greeks QuantData reports.

What this CAN'T give from NBBO quotes alone:
  - Aggregate dealer exposures (GEX/DEX, max-pain): need per-strike Open Interest
    (a separate ThetaData endpoint — not in the quote stream we ingested).
  - Net flow / premium drift: needs trade prints (time & sales), also separate.
"""
from __future__ import annotations

import math

_SQRT2PI = math.sqrt(2 * math.pi)
_YEAR_MIN = 365.0 * 24 * 60  # minutes per year (calendar convention)


def _pdf(x):
    return math.exp(-0.5 * x * x) / _SQRT2PI


def _cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S, K, T, sigma, right, r=0.0, q=0.0):
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if right == "C" else (K - S))
    srt = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / srt
    d2 = d1 - srt
    if right == "C":
        return S * math.exp(-q * T) * _cdf(d1) - K * math.exp(-r * T) * _cdf(d2)
    return K * math.exp(-r * T) * _cdf(-d2) - S * math.exp(-q * T) * _cdf(-d1)


def implied_vol(price, S, K, T, right, r=0.0, q=0.0):
    """Back out IV by bisection. Returns None when the price has no time value
    (deep ITM at/near expiry) — IV is undefined there."""
    if price is None or T <= 0:
        return None
    intrinsic = max(0.0, (S - K) if right == "C" else (K - S))
    if price <= intrinsic + 1e-4:
        return None
    lo, hi = 1e-3, 5.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        p = bs_price(S, K, T, mid, right, r, q)
        if abs(p - price) < 1e-4:
            return mid
        if p > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def greeks(S, K, T, sigma, right, r=0.0, q=0.0):
    """Full per-contract greek set from BSM. vega/vanna are per 1 vol-point;
    theta/charm are per calendar day."""
    if T <= 0 or sigma is None or sigma <= 0:
        return None
    srt = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / srt
    d2 = d1 - srt
    pdf = _pdf(d1)
    Nd1 = _cdf(d1)
    delta = math.exp(-q * T) * (Nd1 if right == "C" else Nd1 - 1)
    gamma = math.exp(-q * T) * pdf / (S * srt)
    vega = S * math.exp(-q * T) * pdf * math.sqrt(T) / 100.0
    term1 = -(S * math.exp(-q * T) * pdf * sigma) / (2 * math.sqrt(T))
    if right == "C":
        theta = (term1 - r * K * math.exp(-r * T) * _cdf(d2)
                 + q * S * math.exp(-q * T) * Nd1) / 365.0
    else:
        theta = (term1 + r * K * math.exp(-r * T) * _cdf(-d2)
                 - q * S * math.exp(-q * T) * _cdf(-d1)) / 365.0
    charm = -math.exp(-q * T) * pdf * (2 * (r - q) * T - d2 * srt) / (2 * T * srt) / 365.0
    vanna = -math.exp(-q * T) * pdf * d2 / sigma / 100.0
    return {"iv": sigma, "delta": delta, "gamma": gamma, "vega": vega,
            "theta": theta, "charm": charm, "vanna": vanna}


def quote_greeks(sess, ts, strike, right, r=0.0):
    """Convenience: IV + greeks for one option at one minute, using the session's
    parity spot and minutes-to-1600ET-expiry."""
    from datetime import datetime
    from .data import ET
    mid = sess.mid(ts, strike, right)
    S = sess.spot(ts)
    if mid is None or S is None:
        return None
    now = datetime.fromtimestamp(ts / 1000, ET)
    expiry = now.replace(hour=16, minute=0, second=0, microsecond=0)
    mins = (expiry - now).total_seconds() / 60.0
    if mins <= 0:
        return None
    T = mins / _YEAR_MIN
    iv = implied_vol(mid, S, strike, T, right, r=r)
    if iv is None:
        return {"iv": None, "S": S, "mid": mid, "mins_to_exp": round(mins)}
    g = greeks(S, strike, T, iv, right, r=r)
    g.update({"S": round(S, 1), "mid": round(mid, 2), "mins_to_exp": round(mins)})
    return g


if __name__ == "__main__":
    # Demo: IV + greeks across the chain at the open on a sample session.
    from .data import Session, connect
    con = connect()
    s = Session(con, "2022-06-15")
    ts = s.minutes[0] + 60000  # ~09:31
    atm = s.atm(ts)
    print(f"2022-06-15 ~09:31  spot~{s.spot(ts):.1f}  ATM={atm:.0f}")
    print(f"{'K':>6} {'R':>2} {'mid':>6} {'IV':>6} {'delta':>7} {'gamma':>8} {'theta':>8} {'charm':>9} {'vanna':>8}")
    for k in [atm - 30, atm - 10, atm, atm + 10, atm + 30]:
        for right in ("C", "P"):
            g = quote_greeks(s, ts, k, right)
            if not g or g.get("iv") is None:
                continue
            print(f"{k:6.0f} {right:>2} {g['mid']:6.2f} {g['iv']:6.3f} {g['delta']:7.3f} "
                  f"{g['gamma']:8.5f} {g['theta']:8.2f} {g['charm']:9.4f} {g['vanna']:8.4f}")
