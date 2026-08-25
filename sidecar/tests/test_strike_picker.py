"""Strike selection for credit spreads — the 2026-08-12 regression suite.

WHAT HAPPENED. The spread auto-trader fired once (parent 927, PCS 7660/7650)
and picked a short strike at delta -0.0329 while configured for 0.15, offering
$15.50 of credit against $984.50 of risk. At that credit the $2.56 commission is
16.5% of the gross, versus ~1-1.7% at a genuine 0.15 delta. Two defects
combined:

  * strikes whose quote failed were silently skipped, so the scan stepped from
    >0.15 delta straight past four unquotable strikes to 0.033 and took it;
  * the `d < delta_max * 0.5` guard was evaluated AFTER the accept test, so a
    too-far strike was returned before the guard could ever run.

Neither module had a single test. Every test below pins one behaviour that,
had it existed, would have refused parent 927.

SCAN GRID (assumed by every ladder here, verified against `_round_to_5`):
spot 7700 with starting_offset 10 begins at 7690 and steps by `width` — so a
10-wide PCS visits 7690, 7680, 7670, 7660 ... and a 10-wide CCS starts at 7710
and visits 7710, 7720, 7730. A ladder off this grid is simply never scanned.
"""

from __future__ import annotations

import os
from dataclasses import fields as dataclass_fields
from datetime import datetime

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.engine import strike_picker
from fef_sidecar.engine.strike_picker import (
    clear_strike_cache,
    find_ccs_strike,
    find_pcs_strike,
)
from fef_sidecar.ibkr.models import Contract, OptionQuote


SPOT = 7700.0
EXPIRY = "20260812"
# Puts the PCS ladder on round tens (7690, 7680, ...) at width 10.
OFFSET = 10.0


@pytest.fixture(autouse=True)
def _clear_cache():
    """The picker memoises by (kind, symbol, expiry, rounded spot) for 60s.

    Without this, every test after the first would read the previous test's
    pick instead of exercising the scan.
    """
    clear_strike_cache()
    yield
    clear_strike_cache()


class FakeIBKR:
    """Serves a hand-built delta ladder and records which strikes were asked.

    `deltas` maps strike -> |delta|; a value of None, or a strike absent from
    the map entirely, is a strike that refuses to quote — the case the old scan
    skipped silently. `mids` overrides the default premium and `spreads`
    overrides the default bid/ask.
    """

    def __init__(self, deltas: dict[float, float | None],
                 mids: dict[float, float] | None = None,
                 spreads: dict[float, tuple[float, float]] | None = None) -> None:
        self.deltas = deltas
        self.mids = mids or {}
        self.spreads = spreads or {}
        self.asked: list[float] = []

    async def get_option_quote(self, contract: Contract) -> OptionQuote | None:
        strike = contract.strike
        self.asked.append(strike)
        d = self.deltas.get(strike)
        if d is None:
            return None
        # Default pricing: a plausible ladder where premium falls with delta.
        # Tests that care about credit override via `mids` / `spreads`.
        mid = self.mids.get(strike, max(0.05, d * 20.0))
        bid, ask = self.spreads.get(strike, (mid - 0.05, mid + 0.05))
        return OptionQuote(
            timestamp=datetime.now(),
            contract=contract,
            last=mid,
            bid=bid,
            ask=ask,
            delta=-d if contract.right == "P" else d,
        )


async def _pcs(ibkr: FakeIBKR, **kw):
    kw.setdefault("width", 10.0)
    kw.setdefault("delta_max", 0.15)
    # Pin premium_min to what state.py actually passes in production. Leaving it
    # to the module default would make these tests depend on the very
    # module-vs-state divergence that caused the incident, and would mask the
    # 927 reproduction behind an unrelated $0.70 premium test.
    kw.setdefault("premium_min", 0.0)
    return await find_pcs_strike(
        ibkr, symbol="SPX", expiry=EXPIRY, spot=SPOT, starting_offset=OFFSET, **kw
    )


# ---------------------------------------------------------------------------
# 1. NEAREST the target, not the first strike under it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_picks_strike_nearest_target_not_merely_under_it() -> None:
    """0.142 and 0.088 both satisfy a `<= 0.15` test; only 0.142 is NEAREST."""
    pick = await _pcs(FakeIBKR({7690.0: 0.142, 7680.0: 0.088, 7670.0: 0.041}))
    assert pick is not None
    assert pick.short_strike == 7690.0
    assert abs(pick.short_delta) == pytest.approx(0.142)
    assert pick.long_strike == 7680.0, "PCS wing sits BELOW the short strike"


@pytest.mark.asyncio
async def test_never_walks_above_the_delta_ceiling() -> None:
    """delta_max stays a hard RISK bound: 0.310 is 'nearer' to nothing we want.

    The operator's rule is nearest 0.15 from below; raising the ceiling would
    be a strategy change, not a bug fix.
    """
    pick = await _pcs(FakeIBKR(
        {7690.0: 0.310, 7680.0: 0.148, 7670.0: 0.090, 7660.0: 0.050}
    ))
    assert pick is not None
    assert pick.short_strike == 7680.0


@pytest.mark.asyncio
async def test_ccs_side_picks_nearest_target_too() -> None:
    """The call side shared the bug because it was copy-pasted. It now shares
    the implementation — pin it independently so it cannot diverge again."""
    pick = await find_ccs_strike(
        FakeIBKR({7710.0: 0.310, 7720.0: 0.144, 7730.0: 0.070}),
        symbol="SPX", expiry=EXPIRY, spot=SPOT,
        starting_offset=OFFSET, width=10.0, delta_max=0.15, premium_min=0.0,
    )
    assert pick is not None
    assert pick.short_strike == 7720.0
    assert pick.long_strike == 7730.0, "CCS wing sits ABOVE the short strike"


# ---------------------------------------------------------------------------
# 2. The over-far rejection — the floor must REJECT, not merely stop the walk
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rejects_the_927_pick() -> None:
    """THE REGRESSION. 0.0329 against a 0.15 target must yield NO spread.

    The old guard (`d < delta_max * 0.5: break`) ran after the accept test, so
    this exact ladder returned the 0.033 strike and sent it on to the order
    path. 'Show no spread' is the documented fallback and was the correct
    answer that day.
    """
    assert await _pcs(FakeIBKR(
        {7690.0: 0.0329, 7680.0: 0.0210, 7670.0: 0.0150}
    )) is None


@pytest.mark.asyncio
async def test_below_floor_strike_never_wins_over_an_in_band_one() -> None:
    """The below-floor strike terminates the walk but must not be the pick."""
    ibkr = FakeIBKR({7690.0: 0.090, 7680.0: 0.040, 7670.0: 0.130})
    pick = await _pcs(ibkr)
    assert pick is not None
    assert pick.short_strike == 7690.0
    assert 7670.0 not in ibkr.asked, (
        "the walk stops once delta drops below the floor — delta is monotone "
        "going OTM, so no strike beyond it can re-enter the band"
    )


@pytest.mark.asyncio
async def test_delta_floor_is_configurable() -> None:
    """Operator escape hatch: a wider band accepts what the default refuses.

    This is the knob to reach for if the default floor proves too tight on a
    coarse strike grid — not the picker's selection rule.
    """
    ladder = {7690.0: 0.060, 7680.0: 0.030}
    assert await _pcs(FakeIBKR(dict(ladder))) is None, "0.060 is below the 0.075 floor"

    clear_strike_cache()
    pick = await _pcs(FakeIBKR(dict(ladder)), delta_floor_frac=0.3)  # floor 0.045
    assert pick is not None
    assert pick.short_strike == 7690.0


# ---------------------------------------------------------------------------
# 3. The sparse-quote refusal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refuses_when_the_window_is_mostly_dark() -> None:
    """The shape of the 927 scan: >0.15, four dark strikes, then 0.033.

    Two of six strikes quoted. 'Nearest 0.15 among the two that happened to
    answer' is not a nearest-delta claim at all.

    7630 quotes so that the 7640/7630 spread is fully priceable — otherwise the
    old code also returns None, but only because the WING failed, and the test
    would pass without exercising the coverage guard at all.
    """
    assert await _pcs(FakeIBKR({
        7690.0: 0.180,   # above the ceiling
        7680.0: None, 7670.0: None, 7660.0: None, 7650.0: None,
        7640.0: 0.033,   # the strike the old code took
        7630.0: 0.030,   # ...and its priceable wing
    })) is None


@pytest.mark.asyncio
async def test_refuses_on_sparse_coverage_even_with_a_usable_candidate() -> None:
    """The coverage gate is independent of whether a good strike exists.

    7690 is a perfectly good 0.14-delta candidate whose wing (7680) prices
    cleanly — the old code takes it — but eight of the ten strikes scanned are
    dark, so we cannot claim it is the NEAREST one. Refusing here is the point:
    the guard is about what we could SEE, not about what we happened to find.
    """
    assert await _pcs(FakeIBKR({7690.0: 0.140, 7680.0: 0.100})) is None


@pytest.mark.asyncio
async def test_one_dark_strike_does_not_block_a_trade() -> None:
    """Chain noise is not a dark window. The guard must not become a second
    undesigned filter that quietly stops the bot trading."""
    pick = await _pcs(FakeIBKR(
        {7690.0: 0.140, 7680.0: 0.100, 7670.0: None, 7660.0: 0.060}
    ))
    assert pick is not None
    assert pick.short_strike == 7690.0


# ---------------------------------------------------------------------------
# 4. The credit backstop — a ratio of width, plus an absolute floor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rejects_credit_too_small_for_the_width() -> None:
    """The economics of parent 927: $0.155 net on a 10-wide = 1.55% of width,
    on which a $2.56 commission is 16.5% of the gross credit.

    `spread_premium_min` could never have caught this — it gates the SHORT
    leg's mid, not the net credit of the spread — and was 0.0 in state.py
    besides.
    """
    assert await _pcs(FakeIBKR(
        {7690.0: 0.140, 7680.0: 0.060},
        mids={7690.0: 2.00, 7680.0: 1.845},
        spreads={7690.0: (1.95, 2.05), 7680.0: (1.795, 1.895)},
    )) is None


@pytest.mark.asyncio
async def test_credit_floor_scales_with_width() -> None:
    """A RATIO floor, not a fixed dollar floor. The same $0.40 net credit is
    8% of a 5-wide (accepted) and 4% of a 10-wide (refused) — judged against
    the risk it is actually being paid to carry.

    A fixed floor is what turned `premium_min = $0.70` on a 5-wide into an
    accidental vol filter at 14% of width; a ratio cannot drift that way.
    """
    five = await _pcs(
        FakeIBKR({7690.0: 0.140, 7685.0: 0.100},
                 mids={7690.0: 1.00, 7685.0: 0.60},
                 spreads={7685.0: (0.55, 0.65)}),
        width=5.0, max_scan_strikes=2,
    )
    assert five is not None, "$0.40 on a 5-wide is 8% — clears the 5% floor"
    assert five.estimated_credit == pytest.approx(0.40)

    clear_strike_cache()
    ten = await _pcs(
        FakeIBKR({7690.0: 0.140, 7680.0: 0.100},
                 mids={7690.0: 1.00, 7680.0: 0.60},
                 spreads={7680.0: (0.55, 0.65)}),
        width=10.0, max_scan_strikes=2,
    )
    assert ten is None, "the same $0.40 on a 10-wide is 4% — below the floor"


@pytest.mark.asyncio
async def test_absolute_floor_catches_what_the_ratio_lets_through() -> None:
    """$0.06 is 6% of a 1-wide and clears the ratio, but is not a trade.

    Pins that the absolute floor is doing independent work — remove it and
    this case passes the ratio test and trades.
    """
    assert await _pcs(
        FakeIBKR({7690.0: 0.140, 7689.0: 0.135},
                 mids={7690.0: 0.50, 7689.0: 0.44},
                 spreads={7689.0: (0.39, 0.49)}),
        width=1.0, max_scan_strikes=2,
    ) is None


@pytest.mark.asyncio
async def test_a_rejected_candidate_does_not_abort_the_whole_scan() -> None:
    """A failed credit/wing check rejects the CANDIDATE, not the scan — the
    next-nearest strike still gets its turn.

    ⚠ THE ASYMMETRY THIS TEST ONCE ASSERTED WAS REVERSED 2026-08-25. It used to
    hold that a 0.00 x 1.60 quote was "unusable as a WING yet still fine as a
    short leg." That is wrong: in a credit spread you SELL the short leg, so your
    proceeds are bounded by its BID. A zero bid means nobody will pay anything —
    there is no credit to collect — and `OptionQuote.mid` falls back to `last`,
    so the number that survives is a stale print which becomes the combo LIMIT
    PRICE. The reconciliation harness measured the consequence: 80.5% of recorded
    credits reproduce from ThetaData at some minute of their own session, but at
    a median of -51.5 minutes.

    Here 7680 refuses to quote at all, so 7690 loses its wing and is rejected —
    and the scan still goes on to price 7670.
    """
    pick = await _pcs(FakeIBKR(
        {7690.0: 0.140, 7680.0: None, 7670.0: 0.115,
         7660.0: 0.100, 7650.0: 0.085, 7640.0: 0.070},
        spreads={7690.0: (2.95, 3.05), 7670.0: (1.75, 1.85), 7660.0: (1.15, 1.25),
                 7650.0: (0.55, 0.65), 7640.0: (0.25, 0.35)},
    ))
    assert pick is not None
    # 7690 is nearest the 0.15 target but its wing (7680) refuses to quote, so
    # the candidate is dropped and the scan carries on to price 7670/7660.
    assert pick.short_strike == 7670.0
    assert pick.estimated_credit == pytest.approx(0.60)


@pytest.mark.asyncio
async def test_credit_is_net_of_both_legs_not_the_short_mid() -> None:
    """Regression pin for the earlier A1/A2 fix: `estimated_credit` becomes the
    combo LIMIT PRICE, so it must be the NET of both legs."""
    pick = await _pcs(FakeIBKR(
        {7690.0: 0.140, 7680.0: 0.090, 7670.0: 0.050},
        mids={7690.0: 3.00, 7680.0: 1.00},
        spreads={7690.0: (2.95, 3.05), 7680.0: (0.95, 1.05)},
    ))
    assert pick is not None
    assert pick.short_mid == pytest.approx(3.00)
    assert pick.long_mid == pytest.approx(1.00)
    assert pick.estimated_credit == pytest.approx(2.00)


# ---------------------------------------------------------------------------
# 5. Defaults must never drift apart again
# ---------------------------------------------------------------------------

def test_module_defaults_match_state_defaults() -> None:
    """The incident began as a DIVERGENCE: strike_picker advertised
    premium_min=0.70 in its signature and its docstring while state.py passed
    0.0, so the only credit guard in the system had been switched off for the
    auto-trader's entire life. Pin the two together.
    """
    from fef_sidecar.state import AppState

    defaults = {f.name: f.default for f in dataclass_fields(AppState)}
    assert defaults["spread_delta_max"] == strike_picker.DEFAULT_DELTA_MAX
    assert defaults["spread_delta_floor_frac"] == strike_picker.DEFAULT_DELTA_FLOOR_FRAC
    assert defaults["spread_min_quote_coverage"] == strike_picker.DEFAULT_MIN_QUOTE_COVERAGE
    assert (defaults["spread_min_credit_width_ratio"]
            == strike_picker.DEFAULT_MIN_CREDIT_WIDTH_RATIO)
    assert defaults["spread_min_net_credit"] == strike_picker.DEFAULT_MIN_NET_CREDIT


# ---------------------------------------------------------------------------
# The SHORT leg is held to the same two-sided test as the wing (2026-08-25).
#
# The wing has been guarded since 2026-08-11. The short leg was not — and it is
# the LARGER term of the credit. `OptionQuote.mid` falls back to `last` when a
# side is missing or zero, so an hours-old print could set the combo limit.
#
# Measured consequence, from the backtest/live reconciliation harness: 80.5% of
# recorded credits reproduce from ThetaData at SOME minute of their own session,
# but the best match sits a median -51.5 minutes earlier, and two structures
# carried byte-identical credits while their legs re-quoted. The join was sound;
# the anchor was stale.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bid,ask,label", [
    (0.0, 3.05, "zero bid — nobody pays, and mid falls back to last"),
    (2.95, 0.0, "zero ask"),
    (3.40, 2.90, "crossed quote"),
])
@pytest.mark.asyncio
async def test_short_leg_without_a_two_sided_quote_is_never_selected(bid, ask, label):
    """7690 is NEAREST 0.15 and would win on delta alone — but it cannot be
    priced, so it counts UNQUOTABLE and the next-best strike wins instead. The
    scan is not aborted; the bad strike simply cannot be sold."""
    ladder = {7690.0: 0.150, 7680.0: 0.130, 7670.0: 0.115,
              7660.0: 0.100, 7650.0: 0.085, 7640.0: 0.070}
    # Explicit two-sided quotes: $0.60 credit at every rung, clearing the 5%
    # of width floor. The fake's default `delta * 20` ladder gives $0.30, which
    # the floor correctly refuses -- that is the guard working, not a fixture.
    good = {7690.0: (2.95, 3.05), 7680.0: (2.35, 2.45), 7670.0: (1.75, 1.85),
            7660.0: (1.15, 1.25), 7650.0: (0.55, 0.65), 7640.0: (0.25, 0.35)}
    good[7690.0] = (bid, ask)
    pick = await _pcs(FakeIBKR(ladder, spreads=good))
    assert pick is not None, f"scan aborted on {label} instead of moving on"
    assert pick.short_strike != 7690.0, f"selected an unpriceable short leg ({label})"


@pytest.mark.asyncio
async def test_short_leg_mid_is_recomputed_not_taken_from_the_model():
    """Even when `mid` is present we recompute from bid/ask: a model mid that
    disagrees with its own two-sided quote is not the tradeable price."""
    ladder = {7690.0: 0.150, 7680.0: 0.130, 7670.0: 0.115,
              7660.0: 0.100, 7650.0: 0.085, 7640.0: 0.070}
    # Explicit two-sided quotes: $0.60 credit at every rung, clearing the 5%
    # of width floor. The fake's default `delta * 20` ladder gives $0.30, which
    # the floor correctly refuses -- that is the guard working, not a fixture.
    good = {7690.0: (2.95, 3.05), 7680.0: (2.35, 2.45), 7670.0: (1.75, 1.85),
            7660.0: (1.15, 1.25), 7650.0: (0.55, 0.65), 7640.0: (0.25, 0.35)}
    pick = await _pcs(FakeIBKR(
        ladder, mids={7690.0: 99.0, 7680.0: 99.0}, spreads=good,
    ))
    assert pick is not None
    assert pick.short_strike == 7690.0
    assert pick.short_mid == pytest.approx(3.00), pick.short_mid
    assert pick.long_mid == pytest.approx(2.40), pick.long_mid
    assert pick.estimated_credit == pytest.approx(0.60), pick.estimated_credit
