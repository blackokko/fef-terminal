"""Probe: (1) does QuantData `net-drift` accept a per-expiry (0DTE) filter, and
(2) does 0DTE-scoping bring its scale into line with the ThetaData 0DTE aggressive
signed-flow the edge was validated on? Read-only. Run from `sidecar` with the venv:

    .venv/Scripts/python.exe -m scripts.maintenance.probe_net_drift_0dte 2026-06-26 2026-05-20
"""
import json
import sys

import httpx

from fef_sidecar.quantdata.config import load_config
from scripts.research.backtest.data import connect

KEY = load_config().auth_token
BASE = "https://api.quantdata.us/v1"
HEAD = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def fetch_net_drift(c, date, expiry=None):
    filt = {"ticker": "SPX"}
    if expiry:
        filt["expirationDate"] = expiry
    r = c.post("/options/tool/net-drift", json={"sessionDate": date, "filter": filt})
    ct = r.headers.get("content-type", "")
    return r.status_code, (r.json() if ct.startswith("application/json") else {"_text": r.text[:300]})


def cum_net_m(j):
    d = j.get("data", {})
    if not isinstance(d, dict):
        return None, 0
    tc = tp = 0.0
    n = 0
    for _ts, v in d.items():
        if not isinstance(v, dict):
            continue
        tc += float(v.get("netCallPremium") or 0)
        tp += float(v.get("netPutPremium") or 0)
        n += 1
    return (tc - tp) / 1e6, n


def theta_cum_m(con, date):
    rows = con.execute(
        "SELECT net_signed_prem FROM option_flow_minute WHERE session_date=?", (date,)
    ).fetchall()
    if not rows:
        return None
    return sum(r[0] for r in rows) / 1e6


def main():
    dates = sys.argv[1:] or ["2026-06-26"]
    con = connect()
    with httpx.Client(base_url=BASE, headers=HEAD, timeout=60) as c:
        for date in dates:
            print(f"\n=== {date} ===")
            sc_all, j_all = fetch_net_drift(c, date)
            sc_0, j_0 = fetch_net_drift(c, date, expiry=date)
            print(f"  net-drift ALL-EXPIRY:  HTTP {sc_all}", end="")
            if sc_all == 200:
                m, n = cum_net_m(j_all)
                print(f"   cum_net={m:+.1f}M  pts={n}")
            else:
                print("  ", json.dumps(j_all)[:220])
            print(f"  net-drift 0DTE-FILTER: HTTP {sc_0}", end="")
            if sc_0 == 200:
                m, n = cum_net_m(j_0)
                print(f"   cum_net={m:+.1f}M  pts={n}")
            else:
                print("  ", json.dumps(j_0)[:220])
            tm = theta_cum_m(con, date)
            print(f"  ThetaData 0DTE cum_net = {tm:+.1f}M" if tm is not None
                  else "  ThetaData: (no option_flow_minute rows for this date)")


if __name__ == "__main__":
    main()
