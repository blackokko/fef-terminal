"""Coherent mock data — a single PriceTrack + MockClock all subsystems
sync to, so chart bars / ticks /
microstructure all agree on the current SPX price at every moment."""

from fef_sidecar.mock.clock import MockClock
from fef_sidecar.mock.price_track import PriceTrack, get_track

__all__ = ["MockClock", "PriceTrack", "get_track"]
