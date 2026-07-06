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
        # busy_timeout (seconds): make concurrent writers WAIT for the write
        # lock instead of failing immediately. Critical — multiple async loops
        # (engine + poll + watcher + captures) write to one SQLite file; with
        # the default no-wait behavior, lock collisions raised "database is
        # locked" and hung the loops (2026-05-28 regression).
        _connect_args = {"timeout": 30} if url.startswith("sqlite") else {}
        _engine = create_async_engine(url, echo=False, future=True, connect_args=_connect_args)
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
            # fixed_dollar watcher-managed $ targets (2026-06-30)
            ("live_order", "dollar_target_t1", "REAL"),
            ("live_order", "dollar_target_t2", "REAL"),
            ("live_order", "dollar_target_t3", "REAL"),
            ("live_order", "dollar_target_stop", "REAL"),
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
            # LiveOrder.parent_id has been on the model since inception; the
            # migration entry is here as a no-op safety net for any old DB
            # where the create_all path missed it.
            ("live_order", "parent_id", "INTEGER REFERENCES live_order(id)"),
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
            # Audit priority #2 — within-bar order probability for T1 vs stop
            ("signal_lookahead_outcome", "t1_hit_probability", "REAL DEFAULT 0.0"),
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
            # Shadow-exit row usability (2026-07-06 premium-gap fix): rows
            # whose premiums couldn't be established are marked unusable
            # instead of contributing $0 to the A/B aggregate.
            ("shadow_exit_outcome", "status", "TEXT DEFAULT 'ok'"),
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

        # ---- Index migrations (CREATE INDEX IF NOT EXISTS) ----
        # SQLModel/create_all already provisions the column indexes declared
        # via Field(index=True). This list captures composite unique indexes
        # that the model can't express via Field(...) alone — required for
        # idempotent inserts (insert-and-swallow-IntegrityError pattern).
        index_migrations: list[tuple[str, str]] = [
            # historical_option_chain_point: dedup re-captures of the same
            # contract at the same minute (multiple capture loops, restarts).
            (
                "idx_chain_pt_unique",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_chain_pt_unique "
                "ON historical_option_chain_point "
                "(timestamp_ms, strike, right, expiry)",
            ),
            # exposure_chain_point: dedup re-captures of the same strike+greek
            # at the same instant (multiple poll iterations, backfill reruns).
            # Required for the insert-and-swallow-IntegrityError idempotency.
            (
                "idx_exposure_chain_pt_unique",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_exposure_chain_pt_unique "
                "ON exposure_chain_point "
                "(timestamp_ms, greek_type, strike)",
            ),
            # oi_chain_point: dedup re-captures of the same strike at the same
            # instant (live poll + backfill reruns). Required for the
            # insert-and-swallow-IntegrityError idempotency.
            (
                "idx_oi_chain_pt_unique",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_oi_chain_pt_unique "
                "ON oi_chain_point "
                "(timestamp_ms, strike)",
            ),
            # aggression_detail_point: one row per poll, dedup re-captures of
            # the same (instant, symbol).
            (
                "idx_aggression_detail_pt_unique",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_aggression_detail_pt_unique "
                "ON aggression_detail_point "
                "(timestamp_ms, symbol)",
            ),
            # flow_series_point: one row per (minute, symbol) — dedup across
            # poll upserts + backfill reruns of the full intraday series.
            (
                "idx_flow_series_pt_unique",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_flow_series_pt_unique "
                "ON flow_series_point "
                "(timestamp_ms, symbol)",
            ),
            # es_vwap_point: one row per (bar, symbol).
            (
                "idx_es_vwap_pt_unique",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_es_vwap_pt_unique "
                "ON es_vwap_point "
                "(timestamp_ms, symbol)",
            ),
            # shadow_forward_signal: one row per (signal, FROZEN version, instant)
            # — include definition_version so re-frozen specs (v1, v2, ...) coexist
            # for audit instead of colliding. Drops the old version-less index.
            (
                "drop_old_shadow_fwd_idx",
                "DROP INDEX IF EXISTS idx_shadow_fwd_signal_unique",
            ),
            (
                "idx_shadow_fwd_signal_uniq2",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_fwd_signal_uniq2 "
                "ON shadow_forward_signal "
                "(signal_name, definition_version, timestamp_ms)",
            ),
            # option_quote_history: one NBBO row per (contract, instant) — dedup
            # re-ingests of the same ThetaData pull.
            (
                "idx_option_quote_hist_unique",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_option_quote_hist_unique "
                "ON option_quote_history "
                "(symbol, expiry, strike, right, timestamp_ms)",
            ),
        ]
        for index_name, sql in index_migrations:
            try:
                await conn.exec_driver_sql(sql)
                import logging
                logging.getLogger(__name__).info(
                    "Index migration applied: %s", index_name,
                )
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    "Index migration %s failed (table may not exist yet): %s",
                    index_name, e,
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
