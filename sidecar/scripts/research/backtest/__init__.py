"""Shared historical option backtest harness (fill-truth executor + strategy families).

Single source of execution truth for every strategy variant: real bid/ask from
option_quote_history, entry=ask+slip, exit=bid-slip, full lifecycle. Built once so
the Workflow fan-out compares variants on identical fill assumptions.
"""
