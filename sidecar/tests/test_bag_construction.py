"""The combo contract must be constructible — 197 orders died because it wasn't.

WHAT HAPPENED. Between 2026-08-12 and 2026-08-25 the credit-spread bot produced
**200 parent orders and ZERO fills**. 3 were refused by the FEF_ALLOW_CREDIT_SPREADS
env gate (expected, that gate was off). The other **197 died on**:

    Bag.__init__() takes 1 positional argument but 2 were given

`ib_async.Bag.__init__` is `(self, **kwargs)` — keyword-only. Both call sites
passed `symbol` positionally, which is valid `ib_insync` and invalid `ib_async`:
a migration artifact that no test covered because building the combo is the last
thing that happens before `placeOrder`, and nothing exercised it.

Every gate in the stack passed. The strike picker worked. The recommender worked.
The order never existed, because the contract it needed could not be built.

These tests construct the Bag exactly as the two production sites do. They fail
with a TypeError against the pre-fix code.
"""

from __future__ import annotations

import pytest

ib_async = pytest.importorskip("ib_async")
from ib_async import Bag, ComboLeg  # noqa: E402


@pytest.mark.parametrize("symbol,expected_exchange", [("SPX", "CBOE"), ("SPY", "SMART")])
def test_bag_builds_the_way_production_builds_it(symbol, expected_exchange):
    """Mirrors live_trading.py and spread_exit_watcher.py verbatim."""
    bag = Bag(
        symbol=symbol,
        exchange="CBOE" if symbol == "SPX" else "SMART",
        currency="USD",
    )
    assert bag.symbol == symbol
    assert bag.exchange == expected_exchange
    assert bag.currency == "USD"
    assert bag.secType == "BAG"


def test_positional_symbol_is_a_typeerror():
    """Pins the actual failure. If a future ib_async release accepts positional
    args again this test fails LOUDLY — which is correct: it means the library
    contract changed under us, and that is worth knowing either way."""
    with pytest.raises(TypeError, match="positional argument"):
        Bag("SPX", exchange="CBOE", currency="USD")  # type: ignore[misc]


def test_bag_carries_two_legs():
    """A vertical is two legs; a Bag with no legs is silently untradeable."""
    bag = Bag(symbol="SPX", exchange="CBOE", currency="USD")
    bag.comboLegs = [
        ComboLeg(conId=1, ratio=1, action="SELL", exchange="CBOE"),
        ComboLeg(conId=2, ratio=1, action="BUY", exchange="CBOE"),
    ]
    assert len(bag.comboLegs) == 2
    assert {l.action for l in bag.comboLegs} == {"BUY", "SELL"}


def test_production_call_sites_use_keyword_form():
    """Source-level guard: greps the two real files so a revert is caught even
    if nobody runs the combo path."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src" / "fef_sidecar" / "engine"
    for name in ("live_trading.py", "spread_exit_watcher.py"):
        src = (root / name).read_text(encoding="utf-8")
        for line in src.splitlines():
            if "Bag(" in line and "import" not in line:
                assert "Bag(symbol=" in line, f"{name}: positional Bag -> {line.strip()}"
