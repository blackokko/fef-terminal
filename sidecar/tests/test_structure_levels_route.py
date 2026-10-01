"""GET /chart/structure_levels -- the route, with a stand-in IBKR (no network), in the style of
test_regression_channel.py.

The stand-in serves the QQQ fixture (tests/fixtures/structure_levels_qqq_1m.csv.gz) the way IBKR
would at a frozen "now": 1-minute bars for the last two sessions, 5-minute bars for 20 sessions and
30-minute bars for the rest, each WITH extended-hours junk when use_rth=False and the bar in
progress at the end, and daily bars as naive local-midnight datetimes including today's partial
bar. The route must cut all of that to regular hours, rebuild today from the 1-minute bars and
return the same level set the engine gives on clean 1-minute bars. MIDENTRY setups must match in
content (prices, status, fills, origin level); a setup whose O bar or origin lies in a session
older than the 1-minute window carries that session's 5-minute timestamps -- the engine's
documented coarse-history effect.
"""

from __future__ import annotations

import gzip
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fef_sidecar import api, et_time
from fef_sidecar.api import chart_structure_levels
from fef_sidecar.engine import structure_levels as SL
from fef_sidecar.ibkr.models import Bar

FIX = Path(__file__).parent / "fixtures"
SNAP_NOW = int(datetime(2026, 3, 12, 10, 42, tzinfo=et_time.ET).timestamp()) + 25   # mid-minute


def _fixture() -> list[dict]:
    out = []
    with gzip.open(FIX / "structure_levels_qqq_1m.csv.gz", "rt") as f:
        next(f)
        for ln in f:
            a = ln.rstrip("\n").split(",")
            out.append({"time": int(a[0]), "open": float(a[1]), "high": float(a[2]),
                        "low": float(a[3]), "close": float(a[4])})
    return out


def _et(t: int) -> datetime:
    return datetime.fromtimestamp(t, et_time.ET)


def _agg(bs: list[dict], minutes: int) -> list[dict]:
    out: list[dict] = []
    key0 = None
    for b in bs:
        et = _et(b["time"])
        mod = et.hour * 60 + et.minute - 570
        key = (et.date(), mod // minutes)
        if key != key0:
            out.append({"time": b["time"] - (mod % minutes) * 60, "open": b["open"],
                        "high": b["high"], "low": b["low"], "close": b["close"]})
            key0 = key
        else:
            c = out[-1]
            c["high"] = max(c["high"], b["high"])
            c["low"] = min(c["low"], b["low"])
            c["close"] = b["close"]
    return out


class _FixtureIBKR:
    """Answers fetch_historical_bars from the fixture as of `now_s`; records every call."""

    def __init__(self, now_s: int) -> None:
        self.now_s = now_s
        self.calls: list[tuple[str, str, str, bool]] = []
        allb = _fixture()
        # IBKR includes the bar in progress (started, not finished)
        self.bars = [b for b in allb if b["time"] <= now_s]
        self.dates = sorted({_et(b["time"]).date() for b in self.bars})

    def _window(self, k: int) -> list[dict]:
        keep = set(self.dates[-k:])
        return [b for b in self.bars if _et(b["time"]).date() in keep]

    def _eth_junk(self, minutes: int, k: int) -> list[dict]:
        """extended-hours bars at an absurd price: they must never reach a level"""
        out = []
        for d in self.dates[-k:]:
            for hh, mm in ((8, 0), (9, 0), (16, 0), (17, 30)):
                t = int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=et_time.ET).timestamp())
                if t + minutes * 60 <= self.now_s:
                    out.append({"time": t, "open": 1.0, "high": 1.5, "low": 0.5, "close": 1.0})
        return out

    async def fetch_historical_bars(self, contract, *, bar_size: str, duration: str,
                                    use_rth: bool = False, what_to_show: str = "TRADES",
                                    timeout_seconds: float = 20.0) -> list[Bar]:
        self.calls.append((contract.symbol, bar_size, duration, use_rth))
        if bar_size == "1 day":
            days: dict = {}
            for b in self.bars:
                d = _et(b["time"]).date()
                if d not in days:
                    days[d] = dict(b)
                else:
                    c = days[d]
                    c["high"] = max(c["high"], b["high"])
                    c["low"] = min(c["low"], b["low"])
                    c["close"] = b["close"]
            return [Bar(timestamp=datetime(d.year, d.month, d.day), open=v["open"], high=v["high"],
                        low=v["low"], close=v["close"]) for d, v in sorted(days.items())]
        size, k = {"1 min": (1, 2), "5 mins": (5, 20), "30 mins": (30, 63)}[bar_size]
        rows = _agg(self._window(k), size) if size > 1 else self._window(k)
        if not use_rth:
            rows = sorted(rows + self._eth_junk(size, k), key=lambda r: r["time"])
        return [Bar(timestamp=datetime.fromtimestamp(r["time"], tz=timezone.utc), open=r["open"],
                    high=r["high"], low=r["low"], close=r["close"]) for r in rows]


def _state(ibkr) -> types.SimpleNamespace:
    return types.SimpleNamespace(ibkr=ibkr, bars_cache={}, active_symbol="QQQ")


def _request(state) -> types.SimpleNamespace:
    return types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(fef=state)))


@pytest.fixture(autouse=True)
def _frozen(monkeypatch):
    monkeypatch.setattr(et_time, "_utc_now", lambda: datetime.fromtimestamp(SNAP_NOW, tz=timezone.utc))
    monkeypatch.setattr(api, "_SL_RTH_CACHE", {})


@pytest.mark.asyncio
async def test_route_payload_matches_the_engine_on_clean_bars() -> None:
    ibkr = _FixtureIBKR(SNAP_NOW)
    out = await chart_structure_levels(_request(_state(ibkr)), symbol="QQQ")
    assert out["status"] == "ok", out["status"]
    # the four fetches: history from the shared cache (ETH included), daily + today RTH-only
    assert sorted(ibkr.calls) == sorted([("QQQ", "5 mins", "20 D", False), ("QQQ", "30 mins", "3 M", False),
                                         ("QQQ", "1 day", "2 Y", True), ("QQQ", "1 min", "2 D", True)])
    # epoch SECONDS; "now" = the end of the last complete 1-minute bar (10:42 is still forming)
    assert out["asof"] == SNAP_NOW - 25 and out["now"] == SNAP_NOW
    assert out["in_session"] and out["sources"]["1m"] > 0
    assert out["path"]["resolutions"] == [1, 5, 30]
    # extended-hours junk (price ~1) never reaches a level, a group or the ATR
    assert all(r["price"] > 500 for r in out["levels"]) and out["atr"]["1D"] > 1
    # same level set (ids carry kind, tf, known_from, price) as the engine on clean 1m bars
    clean = [b for b in _fixture() if b["time"] + 60 <= SNAP_NOW]
    ref = SL.compute({"1m": clean}, SNAP_NOW, "QQQ")
    assert {(r["id"], r["state"]) for r in out["levels"]} == {(r["id"], r["state"]) for r in ref["levels"]}
    assert len(out["levels"]) > 40 and out["groups"]
    # Setups: same content. Sessions older than the 1m window run on 5m bars, so a setup whose O
    # bar / origin lies there carries 5-minute timestamps (documented coarse-history effect).
    assert len(out["setups"]) == len(ref["setups"]) >= 3

    def by_key(rows):
        return {(s["tf"], s["direction"], s["O"]): s for s in rows}

    got, exp = by_key(out["setups"]), by_key(ref["setups"])
    assert set(got) == set(exp)
    for k, e in exp.items():
        g = got[k]
        for f in ("origin", "X", "M", "deeper", "status", "level_id", "filled_at", "resolved_at", "R"):
            assert g[f] == e[f], (k, f, g[f], e[f])
        for f in ("origin_at", "qualified_at"):
            assert abs((g[f] or 0) - (e[f] or 0)) < 300, (k, f)
        assert abs(int(g["id"].rsplit("-", 1)[1]) - int(e["id"].rsplit("-", 1)[1])) < 300


@pytest.mark.asyncio
async def test_route_caches_and_drops_todays_coarse_rows() -> None:
    ibkr = _FixtureIBKR(SNAP_NOW)
    st = _state(ibkr)
    first = await chart_structure_levels(_request(st), symbol="QQQ")
    n_calls = len(ibkr.calls)
    second = await chart_structure_levels(_request(st), symbol="QQQ")
    assert len(ibkr.calls) == n_calls                 # all four served from the caches
    assert first["levels"] == second["levels"]
    # today's 5m / 30m rows (25-minute-old cache) were dropped: today is built from 1m only
    A_today = [se for se in SL.analyze({"1m": [b for b in _fixture() if b["time"] + 60 <= SNAP_NOW]},
                                       SNAP_NOW, "QQQ").sessions][-1]
    assert first["path"]["last_session"] == A_today.date.isoformat()
    from fef_sidecar.ibkr.models import Contract
    raw5 = await ibkr.fetch_historical_bars(Contract.for_symbol("QQQ"), bar_size="5 mins", duration="20 D")
    raw30 = await ibkr.fetch_historical_bars(Contract.for_symbol("QQQ"), bar_size="30 mins", duration="3 M")
    today = A_today.date
    assert any(_et(int(b.timestamp.timestamp())).date() == today for b in raw5)
    assert first["sources"]["5m"] == sum(1 for b in raw5 if _et(int(b.timestamp.timestamp())).date() != today)
    assert first["sources"]["30m"] == sum(1 for b in raw30 if _et(int(b.timestamp.timestamp())).date() != today)
    assert first["sources"]["1D"] == len(ibkr.dates) - 1      # today's partial daily bar dropped


@pytest.mark.asyncio
async def test_route_filters() -> None:
    out = await chart_structure_levels(_request(_state(_FixtureIBKR(SNAP_NOW))), symbol="qqq",
                                       taus="1H,4H,bogus", families="S,P,x", setups=False)
    assert out["status"] == "ok" and out["symbol"] == "QQQ"
    assert out["taus"] == ["1H", "4H"] and out["families"] == ["P", "S"]
    assert {r["family"] for r in out["levels"]} <= {"S", "P"}
    assert {r["tf"] for r in out["levels"] if r["family"] == "S"} <= {"1H", "4H"}
    assert out["setups"] == []


class _Empty:
    async def fetch_historical_bars(self, contract, *, bar_size, duration, use_rth=False, **kw):
        return []


class _Boom:
    async def fetch_historical_bars(self, contract, *, bar_size, duration, use_rth=False, **kw):
        raise RuntimeError("IBKR down")


@pytest.mark.asyncio
async def test_route_no_bars_and_failures_are_soft() -> None:
    out = await chart_structure_levels(_request(_state(_Empty())), symbol="QQQ")
    assert out["status"] == "no bars for QQQ" and out["levels"] == [] and out["setups"] == []
    out = await chart_structure_levels(_request(_state(_Boom())), symbol="QQQ")
    assert out["status"] == "no bars for QQQ" and out["levels"] == []
    out = await chart_structure_levels(_request(_state(_Empty())), symbol="NOPE")
    assert out["status"].startswith("unsupported symbol") and out["groups"] == []


@pytest.mark.asyncio
async def test_route_with_the_mock_provider() -> None:
    """The dev-mode mock provider (SPX-scale synthetic bars at naive local times) gives a payload."""
    from fef_sidecar.ibkr.mock import MockIBKRProvider
    for sym in ("SPX", "QQQ"):
        out = await chart_structure_levels(_request(_state(MockIBKRProvider())), symbol=sym)
        assert isinstance(out["status"], str)
        assert isinstance(out["levels"], list) and isinstance(out["setups"], list)
