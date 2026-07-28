"""Pin the microstructure formulas to their papers (2026-07-28 adjudication).

The 2026-05-21 deep-dive audit flagged "OFI sign + microprice formula broken."
Branch-by-branch verification against Cont/Kukanov/Stoikov (2014) and the
imbalance-weighted microprice found BOTH formulas canonical — the defect was
a code COMMENT stating the reverse of the (correct) ask-side handling, plus a
zero-sided-quote hygiene hole at the feed boundary. These tests pin the
canonical behavior with worked examples so a future "fix" that flips a sign
fails loudly, and pin the new push_quote hygiene guard.
"""

from __future__ import annotations

from fef_sidecar.engine.microstructure import (
    MicrostructureState,
    QuoteUpdate,
    classify_trade,
    compute_microprice,
    compute_ofi,
    push_quote,
)


def _q(ts, bid, ask, bs, asz) -> QuoteUpdate:
    return QuoteUpdate(timestamp_ms=ts, bid=bid, ask=ask, bid_size=bs, ask_size=asz)


def _state(*quotes: QuoteUpdate) -> MicrostructureState:
    s = MicrostructureState()
    for q in quotes:
        push_quote(s, q)
    return s


# ---------------------------------------------------------------- OFI (CKS)


def test_ofi_bid_up_adds_new_bid_size():
    # Bid 100->100.25 with new size 70: buyers pressing -> +70. Ask unchanged
    # at equal size contributes 0.
    s = _state(_q(0, 100.00, 100.50, 50, 40), _q(1, 100.25, 100.50, 70, 40))
    assert compute_ofi(s) == 70.0


def test_ofi_bid_down_subtracts_prior_bid_size():
    # Bid 100.25->100.00: buyers retreating -> -prior size (-70).
    s = _state(_q(0, 100.25, 100.50, 70, 40), _q(1, 100.00, 100.50, 55, 40))
    assert compute_ofi(s) == -70.0


def test_ofi_ask_up_is_bullish_plus_prior_ask_size():
    # THE branch the old comment described backwards: ask RETREATS UP
    # (sellers pulling away) = BULLISH = +prior ask size (+40).
    s = _state(_q(0, 100.00, 100.50, 50, 40), _q(1, 100.00, 100.75, 50, 30))
    assert compute_ofi(s) == 40.0


def test_ofi_ask_down_is_bearish_minus_new_ask_size():
    # Ask PRESSES DOWN (sellers advancing) = BEARISH = -new ask size (-30).
    s = _state(_q(0, 100.00, 100.75, 50, 40), _q(1, 100.00, 100.50, 50, 30))
    assert compute_ofi(s) == -30.0


def test_ofi_size_changes_at_unchanged_prices():
    # Unchanged prices: bid size 50->80 (+30 bullish); ask size 40->90
    # (-50 bearish). Net -20.
    s = _state(_q(0, 100.00, 100.50, 50, 40), _q(1, 100.00, 100.50, 80, 90))
    assert compute_ofi(s) == -20.0


def test_ofi_worked_sequence_accumulates():
    # Three transitions, hand-computed:
    #   t0->t1: bid up (+60); ask up (+40)          => +100
    #   t1->t2: bid size 60->20 at same bid (-40);
    #           ask down (-new 25)                  => -65
    # Total: +35.
    s = _state(
        _q(0, 100.00, 100.50, 50, 40),
        _q(1, 100.25, 100.75, 60, 35),
        _q(2, 100.25, 100.50, 20, 25),
    )
    assert compute_ofi(s) == 35.0


# ------------------------------------------------------------- microprice


def test_microprice_pulls_toward_ask_when_bid_is_heavy():
    # bid 100 (size 90) / ask 101 (size 10):
    # mp = (100*10 + 101*90) / 100 = 100.90 — heavy bid pushes price UP
    # toward the thin ask (the heavy side is support, the thin side yields).
    mp = compute_microprice(_q(0, 100.0, 101.0, 90, 10))
    assert mp is not None and abs(mp - 100.90) < 1e-9


def test_microprice_pulls_toward_bid_when_ask_is_heavy():
    # Symmetric: heavy ask -> mp near the bid.
    # mp = (100*90 + 101*10) / 100 = 100.10
    mp = compute_microprice(_q(0, 100.0, 101.0, 10, 90))
    assert mp is not None and abs(mp - 100.10) < 1e-9


def test_microprice_equal_sizes_is_mid():
    mp = compute_microprice(_q(0, 100.0, 101.0, 40, 40))
    assert mp is not None and abs(mp - 100.50) < 1e-9


def test_microprice_none_on_empty_or_zero_book():
    assert compute_microprice(None) is None
    assert compute_microprice(_q(0, 100.0, 101.0, 0, 0)) is None


# ----------------------------------------------- zero-sided quote hygiene


def test_push_quote_rejects_zero_sided_quotes():
    # A 0-bid print (one-sided BidAsk tick) must not enter the window: before
    # the 2026-07-28 guard it read as a bid collapse (spurious bearish OFI)
    # and poisoned the microprice.
    s = _state(
        _q(0, 100.00, 100.50, 50, 40),
        _q(1, 0.0, 100.50, 0, 40),      # rejected
        _q(2, 100.00, 0.0, 50, 0),      # rejected
        _q(3, 100.00, 100.50, 50, 40),  # accepted, identical to t0
    )
    assert len(s.quotes) == 2
    assert compute_ofi(s) == 0.0  # no spurious collapse/jump contribution
    assert s.last_quote is not None and s.last_quote.timestamp_ms == 3


def test_push_quote_rejects_negative_sizes():
    s = _state(_q(0, 100.0, 100.5, 50, 40), _q(1, 100.0, 100.5, -5, 40))
    assert len(s.quotes) == 1


# ------------------------------------------------------------- Lee-Ready


def test_classify_trade_edges():
    assert classify_trade(100.50, 100.00, 100.50) == "BUY"    # at ask
    assert classify_trade(100.00, 100.00, 100.50) == "SELL"   # at bid
    assert classify_trade(100.25, 100.00, 100.50) == "UNKNOWN"  # inside
    assert classify_trade(100.25, None, 100.50) == "UNKNOWN"
