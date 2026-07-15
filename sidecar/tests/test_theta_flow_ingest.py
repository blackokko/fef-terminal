"""Unit tests for engine/theta_flow_ingest — the live rebuild of the
validated option_flow_minute construction (2026-07-15 input-parity fix).

Fixture-based, NO network: these pin the construction functions (classifier,
timestamp parsing, minute aggregation, cumsum, incremental==rebuild
invariance) against the Phase-A contract documented in the module docstring.
The end-to-end proof against the stored table lives in
test_theta_flow_construction_parity.py (skipif-no-terminal).
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from fef_sidecar.engine.theta_flow_ingest import (
    QuoteBook,
    ThetaFlowSessionBuilder,
    classify,
    et_iso_to_utc_ms,
    parse_quote_obj,
    parse_trade_obj,
    theta_drift_read,
)

ET = ZoneInfo("America/New_York")


def _ms(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso).replace(tzinfo=ET).timestamp() * 1000)


# ---------------------------------------------------------------------------
# classifier — verbatim ingest_thetadata_flow.py:65-74 semantics
# ---------------------------------------------------------------------------


def test_classify_truth_table():
    bid, ask = 9.0, 10.0
    assert classify(10.0, bid, ask) == 1          # at ask -> buy
    assert classify(10.5, bid, ask) == 1          # above ask -> buy
    assert classify(9.0, bid, ask) == -1          # at bid -> sell
    assert classify(8.5, bid, ask) == -1          # below bid -> sell
    assert classify(9.6, bid, ask) == 1           # above mid (9.5) -> buy
    assert classify(9.4, bid, ask) == -1          # below mid -> sell
    assert classify(9.5, bid, ask) == 0           # exactly mid -> neutral


def test_classify_degenerate_quotes():
    assert classify(10.0, None, 10.0) == 0        # missing bid
    assert classify(10.0, 9.0, None) == 0         # missing ask
    assert classify(10.0, 10.0, 10.0) == 0        # locked (ask <= bid)
    assert classify(10.0, 11.0, 10.0) == 0        # crossed
    # bid == 0 is a VALID quote in this construction (penny options):
    assert classify(0.05, 0.0, 0.05) == 1
    assert classify(0.0, 0.0, 0.05) == -1


def test_classify_epsilon():
    # the 1e-9 tolerance on the touch comparisons
    assert classify(10.0 - 5e-10, 9.0, 10.0) == 1
    assert classify(9.0 + 5e-10, 9.0, 10.0) == -1


# ---------------------------------------------------------------------------
# timestamps — naive ET wall-clock -> UTC ms, DST-correct
# ---------------------------------------------------------------------------


def test_et_iso_to_utc_ms_edt_and_est():
    summer = et_iso_to_utc_ms("2026-06-26T09:30:00")
    assert summer == _ms("2026-06-26T09:30:00")
    # EDT: 09:30 ET == 13:30 UTC
    assert dt.datetime.fromtimestamp(summer / 1000, dt.timezone.utc).hour == 13
    winter = et_iso_to_utc_ms("2026-01-15T09:30:00")
    # EST: 09:30 ET == 14:30 UTC
    assert dt.datetime.fromtimestamp(winter / 1000, dt.timezone.utc).hour == 14


def test_et_iso_to_utc_ms_millis():
    ms = et_iso_to_utc_ms("2026-06-26T09:30:08.725")
    assert ms == _ms("2026-06-26T09:30:08.725")
    assert ms % 1000 == 725


# ---------------------------------------------------------------------------
# ndjson row parsing — live Terminal shapes captured 2026-07-15
# ---------------------------------------------------------------------------


TRADE_OBJ = {
    "symbol": "SPXW", "strike": 7315.000, "right": "CALL",
    "sequence": 1779355774, "condition": 130, "size": 2, "price": 28.40,
    "ext_condition2": 255, "ext_condition1": 255,
    "expiration": "2026-06-26", "ext_condition4": 255, "exchange": 5,
    "ext_condition3": 255, "timestamp": "2026-06-26T09:30:08.725",
}

QUOTE_OBJ = {
    "symbol": "SPXW", "ask_size": 6, "ask_condition": 50, "strike": 7315.000,
    "right": "CALL", "bid_size": 8, "ask_exchange": 5, "bid_exchange": 5,
    "ask": 47.10, "expiration": "2026-06-26", "bid": 45.50,
    "bid_condition": 50, "timestamp": "2026-06-26T15:58:00.000",
}


def test_parse_trade_obj():
    p = parse_trade_obj(TRADE_OBJ)
    assert p is not None
    tms, strike, right, price, size = p
    assert (strike, right, price, size) == (7315.0, "C", 28.40, 2)
    assert tms == _ms("2026-06-26T09:30:08.725")
    assert parse_trade_obj({**TRADE_OBJ, "right": "PUT"})[2] == "P"
    assert parse_trade_obj({k: v for k, v in TRADE_OBJ.items() if k != "price"}) is None
    assert parse_trade_obj({**TRADE_OBJ, "right": "X"}) is None


def test_parse_quote_obj():
    p = parse_quote_obj(QUOTE_OBJ)
    assert p is not None
    tms, strike, right, bid, ask = p
    assert (strike, right, bid, ask) == (7315.0, "C", 45.50, 47.10)
    assert tms == _ms("2026-06-26T15:58:00")
    # missing bid/ask default to 0.0 (quotes.py build_rows semantics)
    p2 = parse_quote_obj({k: v for k, v in QUOTE_OBJ.items() if k not in ("bid", "ask")})
    assert p2[3] == 0.0 and p2[4] == 0.0
    assert parse_quote_obj({**QUOTE_OBJ, "right": None}) is None


# ---------------------------------------------------------------------------
# QuoteBook — backtest.data.Session lookup semantics
# ---------------------------------------------------------------------------


def test_quotebook_global_minutes_and_exact_key():
    qb = QuoteBook()
    m1, m2 = _ms("2026-06-26T09:30:00"), _ms("2026-06-26T09:31:00")
    qb.add(m1, 7300.0, "C", 1.0, 1.2)
    qb.add(m2, 7305.0, "P", 2.0, 2.4)
    # global minute list: at-or-before a ts in minute 2 returns minute 2 even
    # for a contract only quoted at minute 1 -> exact-key miss -> None quote
    assert qb.minute_at_or_before(m2 + 30_000) == m2
    assert qb.quote(m2, 7300.0, "C") is None
    assert qb.quote(m1, 7300.0, "C") == (1.0, 1.2)
    assert qb.minute_at_or_before(m1 - 1) is None


# ---------------------------------------------------------------------------
# aggregation + cumsum — the construction itself
# ---------------------------------------------------------------------------


def _builder_with_quotes() -> tuple[ThetaFlowSessionBuilder, int]:
    b = ThetaFlowSessionBuilder("2026-06-26")
    m0 = _ms("2026-06-26T09:30:00")
    for i in range(5):  # quote minutes 09:30..09:34
        b.ingest_quotes([
            (m0 + i * 60_000, 7300.0, "C", 1.0, 1.2),
            (m0 + i * 60_000, 7300.0, "P", 2.0, 2.4),
        ])
    return b, m0


def test_aggregate_minute_and_net_sign_convention():
    b, m0 = _builder_with_quotes()
    b.ingest_trades([
        (m0 + 1_000, 7300.0, "C", 1.2, 2),   # call buy at ask: prem 240
        (m0 + 2_000, 7300.0, "C", 1.0, 1),   # call sell at bid: prem 100
        (m0 + 3_000, 7300.0, "P", 2.4, 1),   # put buy at ask: prem 240
        (m0 + 4_000, 7300.0, "P", 2.2, 3),   # exactly mid -> sign 0, excluded
        (m0 + 5_000, 7310.0, "C", 5.0, 1),   # no quote for strike -> excluded
    ])
    rows = b.finalize(0, force=True)
    assert len(rows) == 1
    (sd, minute, cbp, csp, pbp, psp, cbv, csv, pbv, psv, net) = rows[0]
    assert (sd, minute) == ("2026-06-26", m0)
    assert (cbp, csp, pbp, psp) == (240.0, 100.0, 240.0, 0.0)
    assert (cbv, csv, pbv, psv) == (2, 1, 1, 0)
    # net = (call_buy - call_sell) - (put_buy - put_sell); bullish > 0
    assert net == (240.0 - 100.0) - (240.0 - 0.0) == -100.0


def test_minute_bucketing_and_quote_at_or_before():
    b, m0 = _builder_with_quotes()
    m1 = m0 + 60_000
    b.ingest_trades([
        (m0 + 59_999, 7300.0, "C", 1.2, 1),  # last ms of minute 0
        (m1, 7300.0, "C", 1.2, 1),           # first ms of minute 1
    ])
    rows = {r[1]: r for r in b.finalize(0, force=True)}
    assert set(rows) == {m0, m1}
    assert rows[m0][2] == 120.0 and rows[m1][2] == 120.0


def test_cumsum_fresh_per_session_starting_at_zero():
    b, m0 = _builder_with_quotes()
    b.ingest_trades([
        (m0 + 1_000, 7300.0, "C", 1.2, 1),            # min0 net +120
        (m0 + 61_000, 7300.0, "P", 2.4, 1),           # min1 net -240
        (m0 + 121_000, 7300.0, "C", 1.2, 2),          # min2 net +240
    ])
    b.finalize(0, force=True)
    assert b.drift_cum_net_m(m0 - 1) is None          # before first minute
    assert b.drift_cum_net_m(m0) == 120.0 / 1e6
    assert b.drift_cum_net_m(m0 + 60_000) == (120.0 - 240.0) / 1e6
    assert b.drift_cum_net_m() == (120.0 - 240.0 + 240.0) / 1e6


def test_incremental_chunking_matches_full_rebuild():
    """The incremental live path and the full-tape rebuild must produce
    IDENTICAL rows — chunking/ordering invariance is what makes the boot
    rebuild + EOD self-check exact."""
    trades = []
    base, _ = _builder_with_quotes()
    m0 = _ms("2026-06-26T09:30:00")
    for i in range(4):
        trades.append((m0 + i * 60_000 + 5_000, 7300.0, "C", 1.2, i + 1))
        trades.append((m0 + i * 60_000 + 6_000, 7300.0, "P", 2.0, 1))
    # full rebuild
    full, _ = _builder_with_quotes()
    full.ingest_trades(trades)
    full.finalize(0, force=True)
    # incremental: chunks of 3 with interleaved (non-force) finalize passes.
    # `now` advances with the tape head (live wall clock >= every ingested
    # trade ts), so a minute only finalizes once all its trades are in —
    # the live loop's semantics exactly.
    inc, _ = _builder_with_quotes()
    for i in range(0, len(trades), 3):
        inc.ingest_trades(trades[i:i + 3])
        inc.finalize(inc.trade_cursor_ms)
    inc.finalize(0, force=True)
    assert inc.rows == full.rows
    assert inc.drift_cum_net_m() == full.drift_cum_net_m()


def test_finalize_gating_waits_for_complete_minute_and_quote():
    b = ThetaFlowSessionBuilder("2026-06-26")
    m0 = _ms("2026-06-26T09:30:00")
    b.ingest_trades([(m0 + 1_000, 7300.0, "C", 1.2, 1)])
    # minute not complete yet
    assert b.finalize(m0 + 30_000) == []
    # complete + lag passed, but NO quote minute yet and grace not expired
    assert b.finalize(m0 + 80_000) == []
    # quote minute arrives -> finalizes
    b.ingest_quotes([(m0, 7300.0, "C", 1.0, 1.2)])
    rows = b.finalize(m0 + 80_000)
    assert len(rows) == 1 and rows[0][2] == 120.0


def test_finalize_quote_grace_expiry_yields_unclassified_row():
    b = ThetaFlowSessionBuilder("2026-06-26")
    m0 = _ms("2026-06-26T09:30:00")
    b.ingest_trades([(m0 + 1_000, 7300.0, "C", 1.2, 1)])
    rows = b.finalize(m0 + 60_000 + 130_000)   # grace expired, no quotes
    assert len(rows) == 1
    # no quote -> sign 0 -> bucket exists but all aggregates zero
    assert rows[0][2:11] == (0.0, 0.0, 0.0, 0.0, 0, 0, 0, 0, 0.0)


def test_trade_dedupe_and_late_print_drop():
    b, m0 = _builder_with_quotes()
    t = (m0 + 1_000, 7300.0, "C", 1.2, 1)
    assert b.ingest_trades([t]) == 1
    assert b.ingest_trades([t]) == 0               # exact duplicate
    assert b.trades_deduped == 1
    b.finalize(0, force=True)
    # a late print behind an already-finalized minute is dropped (the EOD
    # reconcile pass repairs the stored row from the authoritative rebuild)
    assert b.ingest_trades([(m0 + 2_000, 7300.0, "C", 1.0, 1)]) == 0
    assert b.rows[m0][2] == 120.0                   # row unchanged


# ---------------------------------------------------------------------------
# consumer-side fail-soft read
# ---------------------------------------------------------------------------


class _S:
    pass


def test_theta_drift_read_fail_soft():
    s = _S()
    assert theta_drift_read(s) == (None, False)          # no holder
    s.theta_flow = {"drift_theta_cum_m": None, "stale": False}
    assert theta_drift_read(s) == (None, False)          # no value yet
    s.theta_flow = {"drift_theta_cum_m": 12.5, "stale": True,
                    "last_final_minute_ms": 0}
    assert theta_drift_read(s) == (12.5, False)          # stale -> not fresh
    import time
    now_min = (int(time.time() * 1000) // 60000) * 60000
    s.theta_flow = {"drift_theta_cum_m": 12.5, "stale": False,
                    "last_final_minute_ms": now_min - 60_000}
    assert theta_drift_read(s) == (12.5, True)           # fresh
    s.theta_flow = {"drift_theta_cum_m": 12.5, "stale": False,
                    "last_final_minute_ms": now_min - 30 * 60_000}
    assert theta_drift_read(s) == (12.5, False)          # too old
