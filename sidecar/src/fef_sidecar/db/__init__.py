"""SQLite + sqlmodel persistence layer.

Stores every FEF iter for later analysis + trade tickets + daily P/L aggregates.

Schema is created via `init_db()` on app startup — no migration system in v1
because there's no existing data to preserve. Add Alembic in v1.5 when the
schema starts evolving against real session history.

Public entrypoints:
    init_db / dispose_db        - lifecycle
    get_session                 - async context manager for transactions
    SignalIterRepository        - high-level writes/reads for the FEF loop
    SignalIter, TradeTicket,
    DailySession                - model classes
    FEFDirection, FEFConfidence,
    TicketStatus                - enums
"""

from fef_sidecar.db.engine import dispose_db, get_session, init_db
from fef_sidecar.db.models import (
    DailySession,
    FEFConfidence,
    FEFDirection,
    SignalIter,
    TicketStatus,
    TradeTicket,
)
from fef_sidecar.db.repository import SignalIterRepository

__all__ = [
    "DailySession",
    "FEFConfidence",
    "FEFDirection",
    "SignalIter",
    "SignalIterRepository",
    "TicketStatus",
    "TradeTicket",
    "dispose_db",
    "get_session",
    "init_db",
]
