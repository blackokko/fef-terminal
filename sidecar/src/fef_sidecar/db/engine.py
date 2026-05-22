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
    """Create all tables + run lightweight ALTER TABLE migrations.

    Idempotent — safe to call on every startup. SQLModel's create_all only
    creates MISSING tables; it does NOT add columns to existing tables when
    a model gains a new field. So when we add columns over time, we need to
    explicitly ADD COLUMN them here.
    """
    # Import models so SQLModel.metadata picks them up.
    from fef_sidecar.db import models  # noqa: F401
    from sqlalchemy import text

    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

        # Enable WAL mode for better concurrent read/write performance
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")

        # ---- Forward-only schema migrations ----
        # Each entry: (table, column, sqlite_type_sql)
        # Add new rows here whenever a model gains a column. Wrapped in
        # try/except so a missing table (fresh install before create_all
        # ran in some weird order) doesn't crash startup.
        migrations: list[tuple[str, str, str]] = [
            ("signal_iter", "provider_mode", "TEXT"),
            ("daily_session", "updated_at", "DATETIME"),
            # LiveOrder columns for exit-strategy + SPX-target watcher + spreads
            ("live_order", "exit_strategy", "TEXT"),
            ("live_order", "spx_target_t1", "REAL"),
            ("live_order", "spx_target_t2", "REAL"),
            ("live_order", "spx_target_t3", "REAL"),
            ("live_order", "spx_target_stop", "REAL"),
            ("live_order", "spx_target_t1_qty", "INTEGER"),
            ("live_order", "spx_target_t2_qty", "INTEGER"),
            ("live_order", "spx_target_t3_qty", "INTEGER"),
            ("live_order", "spx_target_t1_fired", "INTEGER DEFAULT 0"),
            ("live_order", "spx_target_t2_fired", "INTEGER DEFAULT 0"),
            ("live_order", "spx_target_t3_fired", "INTEGER DEFAULT 0"),
            ("live_order", "spx_target_t1_at_spx", "REAL"),
            ("live_order", "spx_target_t2_at_spx", "REAL"),
            ("live_order", "spx_target_t3_at_spx", "REAL"),
            ("tournament_run", "regime_distribution", "JSON"),
            ("tournament_run", "dominant_regime", "TEXT DEFAULT 'UNKNOWN'"),
            # IBKR execution persistence — table created via create_all, no
            # columns to migrate. Listed here as a marker for the model add.
            ("live_order", "spread_kind", "TEXT"),
            ("live_order", "spread_short_strike", "REAL"),
            ("live_order", "spread_long_strike", "REAL"),
            ("live_order", "spread_initial_credit", "REAL"),
            ("live_order", "dollar_loss_cap", "REAL"),
            ("live_order", "dynamic_stop_level", "REAL"),
            ("live_order", "be_ladder_max_step_R", "REAL DEFAULT 0.0"),
            ("live_order", "entry_spx_price", "REAL"),
            ("daily_review", "by_direction", "JSON"),
            ("daily_review", "by_regime", "JSON"),
            ("daily_review", "by_wall_distance", "JSON"),
            ("daily_review", "by_fe_ratio", "JSON"),
            ("daily_review", "by_block_reason", "JSON"),
            ("daily_review", "actual_trades_taken", "INTEGER DEFAULT 0"),
            ("daily_review", "actual_wins", "INTEGER DEFAULT 0"),
            ("daily_review", "actual_losses", "INTEGER DEFAULT 0"),
            ("daily_review", "actual_total_R", "REAL DEFAULT 0.0"),
            ("audit_log", "block_reason_code", "TEXT"),
            ("daily_review", "dedup_actionable", "INTEGER DEFAULT 0"),
            ("daily_review", "dedup_wins", "INTEGER DEFAULT 0"),
            ("daily_review", "dedup_stops", "INTEGER DEFAULT 0"),
            ("daily_review", "dedup_timeouts", "INTEGER DEFAULT 0"),
            ("daily_review", "dedup_win_rate_pct", "REAL DEFAULT 0.0"),
            ("daily_review", "dedup_expectancy_r", "REAL DEFAULT 0.0"),
            ("daily_review", "sharpe_ratio", "REAL"),
            ("daily_review", "sortino_ratio", "REAL"),
            ("daily_review", "max_drawdown_R", "REAL"),
            ("daily_review", "profit_factor", "REAL"),
            ("daily_review", "ulcer_index", "REAL"),
            # MLModelRun gains regression metrics + on-disk model path
            ("ml_model_run", "label_kind", "TEXT DEFAULT 'r_multiple'"),
            ("ml_model_run", "cv_mae", "REAL"),
            ("ml_model_run", "cv_r2", "REAL"),
            ("ml_model_run", "cv_n_folds", "INTEGER"),
            ("ml_model_run", "model_path", "TEXT"),
            # IBKR commission capture (2026-05-21). Paper accounts report
            # $0; live accounts report real fees. NULL on pre-fix rows.
            ("ibkr_execution", "commission", "REAL"),
            ("ibkr_execution", "realized_pnl", "REAL"),
            # Engine v2 version tag — existing rows backfill as 1 (v1)
            ("signal_iter", "engine_version", "INTEGER DEFAULT 1"),
            ("live_order", "engine_version", "INTEGER DEFAULT 1"),
            # Higher-order Greek wall captures (CHARM + VANNA exposures)
            ("signal_iter", "top_cex_walls", "JSON"),
            ("signal_iter", "top_vex_walls", "JSON"),
            ("flow_snapshot", "top_cex_walls", "JSON"),
            ("flow_snapshot", "top_vex_walls", "JSON"),
            # T2/T3 lookahead tracking — lets B+C+D scale-out evaluate properly
            ("signal_lookahead_outcome", "would_have_hit_t2", "INTEGER DEFAULT 0"),
            ("signal_lookahead_outcome", "would_have_hit_t3", "INTEGER DEFAULT 0"),
            # SignalLookaheadOutcome — net-new table, created via create_all,
            # listed here as a marker so the migration log shows it.
            # Expiration-bucketed GEX/DEX (exposure_by_expiration pipeline)
            ("signal_iter", "top_gex_by_expiry", "JSON"),
            ("signal_iter", "top_dex_by_expiry", "JSON"),
            ("signal_iter", "zero_dte_gex_net_m", "REAL"),
            ("signal_iter", "zero_dte_dex_net_m", "REAL"),
            ("flow_snapshot", "top_gex_by_expiry", "JSON"),
            ("flow_snapshot", "top_dex_by_expiry", "JSON"),
            ("flow_snapshot", "zero_dte_gex_net_m", "REAL"),
            ("flow_snapshot", "zero_dte_dex_net_m", "REAL"),
            # Order-flow summary (order_flow pipeline)
            ("signal_iter", "recent_orders", "JSON"),
            ("signal_iter", "recent_whale_count_30m", "INTEGER DEFAULT 0"),
            ("signal_iter", "recent_golden_sweep_count_30m", "INTEGER DEFAULT 0"),
            ("flow_snapshot", "recent_orders", "JSON"),
            ("flow_snapshot", "recent_whale_count_30m", "INTEGER DEFAULT 0"),
            ("flow_snapshot", "recent_golden_sweep_count_30m", "INTEGER DEFAULT 0"),
        ]
        for table, column, type_sql in migrations:
            try:
                # SQLite-specific PRAGMA to introspect columns
                res = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
                existing = {row[1] for row in res.fetchall()}
                if column not in existing:
                    await conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {column} {type_sql}"
                    )
                    import logging
                    logging.getLogger(__name__).info(
                        "Migrated: ALTER TABLE %s ADD COLUMN %s %s",
                        table, column, type_sql,
                    )
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    "Migration check %s.%s failed (likely fresh install): %s",
                    table, column, e,
                )


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
