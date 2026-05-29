"""Probe QuantData official API (api.quantdata.us/v1) to capture request
schemas + response shapes for the tools the sidecar needs. Read-only.
Writes full JSON responses to scripts/maintenance/_qd_probe/<tool>.json and
prints a compact structure summary. Iterates: on a 400 validation error it
prints the missing/!invalid fields so we can refine the body."""
import json
import os
import sys

import httpx

from fef_sidecar.quantdata.config import load_config

KEY = load_config().auth_token  # qd_ key from ~/.quantdata-mcp/config.json (never hardcode)
BASE = "https://api.quantdata.us/v1"
OUT = os.path.join(os.path.dirname(__file__), "_qd_probe")
os.makedirs(OUT, exist_ok=True)
SD = "2026-05-28"
TICK = {"ticker": "SPX"}

# (tool, body) — bodies are best-guesses; validation errors refine them.
EXP = {"ticker": "SPX", "expirationDate": SD}
PROBES = [
    ("exposure-by-strike", {"sessionDate": SD, "filter": TICK, "greekMode": "GAMMA", "representationMode": "PER_ONE_PERCENT_MOVE"}),
    ("exposure-by-expiration", {"sessionDate": SD, "filter": TICK, "greekMode": "GAMMA", "representationMode": "PER_ONE_PERCENT_MOVE"}),
    ("net-flow", {"sessionDate": SD, "filter": TICK, "dataMode": "NET_PREMIUM"}),
    ("max-pain", {"sessionDate": SD, "filter": EXP}),
    ("contract-trade-side-statistics", {"sessionDate": SD, "filter": TICK}),
    ("contract-statistics", {"sessionDate": SD, "filter": TICK}),
    ("order-flow-consolidated", {"sessionDate": SD, "filter": TICK}),
    ("iv-rank", {"sessionDate": SD, "filter": TICK}),
]


def shape(v, depth=0):
    """Compact structure: types + first element of lists + dict keys."""
    pad = "  " * depth
    if isinstance(v, dict):
        out = []
        for k, vv in list(v.items())[:25]:
            out.append(f"{pad}{k}: {shape(vv, depth + 1).lstrip()}")
        return "\n" + "\n".join(out)
    if isinstance(v, list):
        n = len(v)
        if n == 0:
            return "[] (empty)"
        return f"[{n} items] first=" + shape(v[0], depth + 1).lstrip()
    if isinstance(v, float):
        return f"float({v})"
    if isinstance(v, bool):
        return f"bool({v})"
    if isinstance(v, int):
        return f"int({v})"
    if isinstance(v, str):
        return f"str({v[:40]!r})"
    return f"{type(v).__name__}"


def main():
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    with httpx.Client(base_url=BASE, headers=headers, timeout=30) as c:
        for tool, body in PROBES:
            try:
                r = c.post(f"/options/tool/{tool}", json=body)
            except Exception as e:
                print(f"\n##### {tool}  body={body}\n  EXC {e!r}")
                continue
            label = f"{tool}" + ("" if "representationMode" not in body and "dataMode" not in body
                                 else f" [{body.get('representationMode') or body.get('dataMode')}]")
            print(f"\n##### {label}  HTTP {r.status_code}")
            try:
                j = r.json()
            except Exception:
                print("  non-JSON:", r.text[:200]); continue
            if r.status_code >= 400:
                # show validation errors verbatim — they reveal required fields/enums
                print("  ", json.dumps(j)[:600])
                continue
            fn = os.path.join(OUT, f"{tool}.json")
            with open(fn, "w") as f:
                json.dump(j, f, indent=2)
            print(f"  saved -> {fn}")
            print("  shape:", shape(j)[:1400])


if __name__ == "__main__":
    main()
