"""Async SQLite engine + session factory.

Default DB lives at `~/.fef-terminal/signal_log.db`. Override with
`FEF_DB_URL` env var (e.g. `sqlite+aiosqlite:///:memory:` for tests).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

DEFAULT_DB_DIR = Path.home() / ".fef-terminal"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "signal_log.db"


def _default_url() -> str:
    return f"sqlite+aiosqlite:///{DEFAULT_DB_PATH.as_posix()}"


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_engine():
    global _engine, _session_factory
    if _engine is None:
        url = os.environ.get("FEF_DB_URL", _default_url())
        if url.startswith("sqlite+aiosqlite:///") and ":memory:" not in url:
            DEFAULT_DB_DIR.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(url, echo=False, future=True)
        _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    return _engine


async def init_db() -> None:
    """Create all tables. Idempotent — safe to call on every startup."""
    # Import models so SQLModel.metadata picks them up.
    from fef_sidecar.db import models  # noqa: F401

    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def dispose_db() -> None:
    """Close the engine. Called on app shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession with automatic commit/rollback."""
    _get_engine()
    assert _session_factory is not None
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
