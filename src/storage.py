"""SQLAlchemy ORM models and database session management.

DATABASE DESIGN — LIVE / BACKTEST MIRRORS:
Every table comes in a pair: one for live data and one for backtest data.
This is implemented using Python mixins — a mixin defines all the columns once,
and two classes inherit from it (one live, one backtest). This keeps schemas
in sync automatically and avoids code duplication.

Example:
    _TradeMixin defines all trade columns
    SimulatedTrade(Base, _TradeMixin) → writes to "simulated_trades" table
    BacktestTrade(Base, _TradeMixin) → writes to "backtest_trades" table

The dashboard switches between these table pairs based on the Live/Backtest toggle.

TABLE OVERVIEW:
    kalshi_snapshots             Live order book snapshots (one per market per poll)
    synthetic_kalshi_snapshots   Same schema, holds backtest/demo synthetic data
    external_odds_snapshots      Live sportsbook consensus snapshots
    synthetic_external_*         Same schema, holds backtest/demo synthetic odds
    signals / backtest_signals   Every evaluated signal (passed and failed)
    simulated_trades             Live trades (open and closed)
    backtest_trades              Backtest trades (all closed)
    pnl_timeseries               Live P&L curve samples (one per poll cycle)
    backtest_pnl_timeseries      Backtest P&L curve samples
    session_metadata             Key/value store for current session info
    api_call_log                 Tracks every external API call (for quota monitoring)

WHY SQLITE?
Zero infrastructure — no server to run, no connection string to manage.
SQLAlchemy's abstraction means switching to Postgres is a one-line change.
At 60-second poll cycles, SQLite's single-writer limitation is never a bottleneck.
"""

from __future__ import annotations

from contextlib import contextmanager   # For the session_scope() context manager
from datetime import datetime           # Timestamp type used throughout
from typing import Iterator             # Type hint for the generator in session_scope

# SQLAlchemy column types and engine utilities
from sqlalchemy import (
    Boolean,        # True/False columns (e.g. is_synthetic, passed_filters)
    Column,         # Defines a database column on a model
    DateTime,       # Datetime columns for timestamps
    Float,          # Floating point columns (prices, probabilities, P&L)
    Integer,        # Integer columns (IDs, counts, sizes)
    String,         # Variable-length text columns (tickers, statuses)
    Text,           # Long text columns (JSON blobs, reason strings)
    create_engine,  # Creates the database connection
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker  # ORM base class and session

from src.config import DB_URL, DATA_DIR  # Database file path from config


class Base(DeclarativeBase):
    """SQLAlchemy declarative base class.

    All ORM model classes inherit from this. SQLAlchemy uses it to track
    all models and create their tables with Base.metadata.create_all().
    """
    pass


# =============================================================================
# MIXIN CLASSES — Define columns once, reuse across live and synthetic tables
# =============================================================================

class _KalshiSnapshotMixin:
    """Columns shared by both kalshi_snapshots and synthetic_kalshi_snapshots.

    One row = one market at one point in time. Contains all the order book
    metrics needed for the strategy filter stack and the dashboard display.
    """
    # Auto-incrementing primary key — unique identifier for each row
    id = Column(Integer, primary_key=True, autoincrement=True)

    # When this snapshot was taken — indexed for fast time-range queries
    ts = Column(DateTime, index=True, nullable=False)

    # Kalshi ticker, e.g. "KXNBA-26MAY031950BOSKNY-BOS" — indexed for market lookups
    market_ticker = Column(String, index=True, nullable=False)

    # Internal game identifier for joining with odds data
    game_id = Column(String, index=True, nullable=True)

    # Team names extracted from the Kalshi ticker or market metadata
    home_team = Column(String, nullable=True)
    away_team = Column(String, nullable=True)

    # Sport key, e.g. "basketball_nba", "baseball_mlb"
    sport = Column(String, nullable=True)

    # When the game starts — used for the time-window filter (30min to 6hr)
    game_start_ts = Column(DateTime, nullable=True)

    # Order book metrics computed by compute_metrics() in orderbook.py
    top_bid = Column(Float, nullable=True)          # Best bid price in cents
    top_ask = Column(Float, nullable=True)          # Best ask price in cents
    mid = Column(Float, nullable=True)              # (top_bid + top_ask) / 2
    spread_cents = Column(Float, nullable=True)     # top_ask - top_bid
    depth_3_bid = Column(Integer, nullable=True)    # Total contracts across top 3 bid levels
    depth_3_ask = Column(Integer, nullable=True)    # Total contracts across top 3 ask levels
    imbalance = Column(Float, nullable=True)        # (bid_depth - ask_depth) / total — market pressure indicator
    total_quoted_size = Column(Integer, nullable=True)  # Total depth_3_bid + depth_3_ask

    # Full order book as JSON (top 5 levels on each side) — stored for replay
    # NULL in live mode to save space; populated in synthetic mode for backtest replay
    book_json = Column(Text, nullable=True)

    # True for rows generated by synthetic_data.py or demo mode
    # False for rows fetched from the real Kalshi API
    is_synthetic = Column(Boolean, default=False, nullable=False)


class KalshiSnapshot(_KalshiSnapshotMixin, Base):
    """Live Kalshi order book snapshots. Written by src/logger.py every poll cycle."""
    __tablename__ = "kalshi_snapshots"


class SyntheticKalshiSnapshot(_KalshiSnapshotMixin, Base):
    """Synthetic Kalshi snapshots for backtest and demo. Written by src/synthetic_data.py."""
    __tablename__ = "synthetic_kalshi_snapshots"


class _OddsSnapshotMixin:
    """Columns shared by both external_odds_snapshots and synthetic_external_odds_snapshots.

    One row = one game's sportsbook consensus at one point in time.
    Contains the de-vigged probability and how many books contributed.
    """
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, index=True, nullable=False)       # When odds were fetched

    # Game identifier — matches game_id in KalshiSnapshot for joining
    game_id = Column(String, index=True, nullable=False)

    home_team = Column(String, nullable=True)
    away_team = Column(String, nullable=True)
    sport = Column(String, nullable=True)
    game_start_ts = Column(DateTime, nullable=True)

    # De-vigged consensus probabilities (computed by devig.py)
    consensus_home_prob = Column(Float, nullable=True)   # Median fair home prob (0..1)
    consensus_away_prob = Column(Float, nullable=True)   # Median fair away prob (0..1)

    # How many books contributed to this consensus
    n_books = Column(Integer, nullable=True)

    # Full raw JSON from the API call (includes per-book odds and fair probs)
    # Stored for debugging and replaying exact inputs
    raw_json = Column(Text, nullable=True)

    # True for synthetic data, False for real Odds API data
    is_synthetic = Column(Boolean, default=False, nullable=False)


class ExternalOddsSnapshot(_OddsSnapshotMixin, Base):
    """Live sportsbook consensus snapshots. Written by src/logger.py every odds fetch."""
    __tablename__ = "external_odds_snapshots"


class SyntheticExternalOddsSnapshot(_OddsSnapshotMixin, Base):
    """Synthetic odds snapshots for backtest and demo. Written by src/synthetic_data.py."""
    __tablename__ = "synthetic_external_odds_snapshots"


class _SignalMixin:
    """Columns shared by both signals and backtest_signals.

    One row = one call to strategy.evaluate() for one market at one time.
    EVERY evaluated market is recorded — both passed and failed signals.
    This powers the Live Scanner on the dashboard.
    """
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, index=True, nullable=False)      # When evaluated
    market_ticker = Column(String, index=True, nullable=False)
    game_id = Column(String, index=True, nullable=True)

    # "YES" or "NO" — which direction the edge points
    side = Column(String, nullable=False)

    # The raw edge in percentage points (can be negative)
    edge_pp = Column(Float, nullable=False)

    # The Kalshi mid price in cents at evaluation time (e.g. 48.0)
    kalshi_mid = Column(Float, nullable=False)

    # The sportsbook consensus probability at evaluation time (e.g. 0.57)
    consensus_prob = Column(Float, nullable=False)

    # How many books contributed to the consensus
    n_books = Column(Integer, nullable=True)

    # Market conditions at evaluation time (for the filter stack)
    spread_cents = Column(Float, nullable=True)
    top_of_book_size = Column(Integer, nullable=True)
    minutes_to_game = Column(Float, nullable=True)

    # Whether all strategy filters passed
    passed_filters = Column(Boolean, default=False, nullable=False)

    # "ok" if passed, or the failing filter's description (e.g. "edge 2.1pp < 3.0pp")
    reason = Column(String, nullable=True)


class Signal(_SignalMixin, Base):
    """Live signals. Written every time strategy.evaluate() runs in the live logger."""
    __tablename__ = "signals"


class BacktestSignal(_SignalMixin, Base):
    """Backtest signals. Written by src/replay.py during a backtest run."""
    __tablename__ = "backtest_signals"


class _TradeMixin:
    """Columns shared by simulated_trades and backtest_trades.

    One row = one complete trade (open through close).
    Written at open time, updated at close time.
    """
    id = Column(Integer, primary_key=True, autoincrement=True)

    # Unique trade identifier (12-char hex from UUID4)
    trade_id = Column(String, unique=True, nullable=False)

    # Timestamps — close_ts is NULL while the trade is open
    open_ts = Column(DateTime, index=True, nullable=False)
    close_ts = Column(DateTime, nullable=True)

    market_ticker = Column(String, index=True, nullable=False)
    game_id = Column(String, index=True, nullable=True)

    # "YES" or "NO"
    side = Column(String, nullable=False)

    # Number of contracts
    size = Column(Integer, nullable=False)

    # Entry price in dollars per contract (e.g. 0.52). VWAP fill price.
    entry_price = Column(Float, nullable=False)

    # Exit price in dollars per contract. NULL until closed.
    exit_price = Column(Float, nullable=True)

    # Edge in percentage points at the time of entry (for post-trade analysis)
    edge_at_entry_pp = Column(Float, nullable=False)

    # Market conditions at entry — stored for post-trade analysis
    spread_at_entry = Column(Float, nullable=True)
    depth_3_bid_at_entry = Column(Integer, nullable=True)
    depth_3_ask_at_entry = Column(Integer, nullable=True)
    consensus_prob_at_entry = Column(Float, nullable=True)

    # "open" until the position is closed, then "closed"
    status = Column(String, default="open", nullable=False)

    # Realized P&L in dollars — NULL until closed. (exit - entry) × size
    realized_pnl = Column(Float, nullable=True)

    # Why the position was closed: "resolution", "mean_reversion", "game_start",
    # "end_of_backtest", "end_of_demo"
    close_reason = Column(String, nullable=True)

    # Only populated when --live-orders is active (real order was placed)
    kalshi_order_id = Column(String, nullable=True)    # Kalshi's order ID
    order_status = Column(String, nullable=True)        # "executed", "canceled", "simulated", "failed"


class SimulatedTrade(_TradeMixin, Base):
    """Live trades. Written by src/logger.py at open and close time."""
    __tablename__ = "simulated_trades"


class BacktestTrade(_TradeMixin, Base):
    """Backtest trades. Written by src/replay.py at the end of a backtest run."""
    __tablename__ = "backtest_trades"


class _PnLMixin:
    """Columns shared by pnl_timeseries and backtest_pnl_timeseries.

    One row = one P&L sample at one point in time.
    The dashboard chart is built from these rows — it's a time series of portfolio P&L.
    Sampled approximately once per minute of replay/live time.
    """
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, index=True, nullable=False)    # Sample timestamp

    # Realized P&L: sum of (exit - entry) × size for all closed trades so far
    realized_pnl = Column(Float, nullable=False)

    # Unrealized P&L: mark-to-market on all currently open positions
    unrealized_pnl = Column(Float, nullable=False)

    # Total P&L: realized + unrealized — the chart's primary metric
    total_pnl = Column(Float, nullable=False)

    # How many positions were open at sample time
    open_positions = Column(Integer, nullable=False)


class PnLTimeseries(_PnLMixin, Base):
    """Live P&L samples. Written every poll cycle by src/logger.py."""
    __tablename__ = "pnl_timeseries"


class BacktestPnLTimeseries(_PnLMixin, Base):
    """Backtest P&L samples. Written by src/replay.py during a backtest run."""
    __tablename__ = "backtest_pnl_timeseries"


class SessionMetadata(Base):
    """Key/value store for the current logger session.

    Used by the dashboard to determine:
    1. What mode the logger is running in (demo, live, live_orders)
    2. What the starting Kalshi balance was (for Cash Available display)

    Keys written by the logger:
        "mode"                   "live_orders" | "live" | "demo"
        "initial_balance_cents"  Kalshi balance in cents at session start
    """
    __tablename__ = "session_metadata"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, nullable=False)                     # When the key was written/updated
    key = Column(String, unique=True, nullable=False, index=True)   # Metadata key
    value = Column(String, nullable=True)                     # Metadata value (always stored as string)


class ApiCallLog(Base):
    """One row per external API call — tracks quota usage.

    The dashboard sidebar reads this table to show the Odds API monthly
    usage counter (free tier = 500 calls/month).
    Written by src/odds_client.py every time fetch_h2h() is called.
    """
    __tablename__ = "api_call_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, index=True, nullable=False)    # When the call was made
    api = Column(String, index=True, nullable=False)     # Which API: "odds" or "kalshi"
    endpoint = Column(String, nullable=True)             # Which endpoint was called
    sport = Column(String, nullable=True)                # Which sport (for odds calls)
    cost = Column(Integer, default=1, nullable=False)    # How many API quota credits this used


# =============================================================================
# DATABASE ENGINE AND SESSION MANAGEMENT
# =============================================================================

# Ensure the data directory exists before trying to create the database file in it
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Create the SQLAlchemy engine — this is the connection to the SQLite file
# future=True enables SQLAlchemy 2.0 style API
_engine = create_engine(DB_URL, future=True)

# Session factory — call _SessionLocal() to get a new database session
# expire_on_commit=False means objects don't expire after commit (safe for our pattern)
_SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)

# Schema migrations: columns added after the initial schema deployment.
# Each tuple is (table_name, column_name, sql_type).
# _run_migrations() tries to ADD each column; if it already exists, the error is swallowed.
# This is a simple but effective migration strategy for a single-developer SQLite project.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("session_metadata", "id", "INTEGER"),       # Added id column to session_metadata
    ("simulated_trades", "kalshi_order_id", "VARCHAR"),   # Added for --live-orders tracking
    ("simulated_trades", "order_status", "VARCHAR"),      # Added for --live-orders tracking
    ("backtest_trades", "kalshi_order_id", "VARCHAR"),    # Mirror column on backtest table
    ("backtest_trades", "order_status", "VARCHAR"),       # Mirror column on backtest table
    ("api_call_log", "api", "VARCHAR"),           # Added "api" field to distinguish call types
]


def init_db() -> None:
    """Initialize the database: create all tables and run any pending migrations.

    Called at startup by every run mode (logger, demo, backtest, dashboard).
    Safe to call multiple times — create_all() is idempotent (skips existing tables).
    """
    Base.metadata.create_all(_engine)   # Creates any tables that don't yet exist
    _run_migrations()                    # Adds any new columns to existing tables


def _run_migrations() -> None:
    """Apply any schema changes that aren't handled by create_all().

    create_all() only creates NEW tables — it doesn't add columns to existing ones.
    This function handles ALTER TABLE ADD COLUMN for columns added in later development.

    Strategy: try to add each column; if it raises an error (column already exists),
    roll back and continue. SQLite raises an error for duplicate column names.
    """
    with _engine.connect() as conn:
        for table, col, col_type in _MIGRATIONS:
            try:
                # Attempt to add the column — succeeds if it's new
                conn.execute(
                    __import__("sqlalchemy").text(
                        f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
                    )
                )
                conn.commit()   # Commit the schema change
            except Exception:
                # Column already exists — this is the expected case after first run
                # Roll back the failed transaction so the connection stays clean
                conn.rollback()


def get_engine():
    """Return the SQLAlchemy engine. Used by the dashboard for direct queries."""
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager that provides a database session with automatic commit/rollback.

    Usage:
        with session_scope() as s:
            s.add(SomeModel(...))
            # commit happens automatically when the block exits

    If any exception is raised inside the block:
        - The session is rolled back (no partial writes)
        - The exception propagates normally

    This ensures no half-written data can corrupt the database.
    Every database write in the entire codebase uses this pattern.
    """
    session = _SessionLocal()   # Create a new session for this operation
    try:
        yield session           # Hand the session to the caller
        session.commit()        # Commit all changes if no exception was raised
    except Exception:
        session.rollback()      # Undo everything if something went wrong
        raise                   # Re-raise the exception so the caller sees it
    finally:
        session.close()         # Always close the session to return the connection to the pool


def utcnow() -> datetime:
    """Return the current UTC time. Centralized to make mocking easier in tests."""
    return datetime.utcnow()


def set_session_meta(key: str, value: str) -> None:
    """Write or update a key/value pair in the session_metadata table.

    Used by the logger to record:
    - mode: "live_orders", "live", or "demo"
    - initial_balance_cents: the Kalshi balance at session start

    The dashboard reads these values to determine what data source to display
    and what starting balance to use for Cash Available calculations.
    """
    with session_scope() as s:
        # Check if this key already exists (upsert pattern)
        existing = s.query(SessionMetadata).filter_by(key=key).first()
        if existing:
            # Update existing record
            existing.value = value
            existing.ts = utcnow()
        else:
            # Insert new record
            s.add(SessionMetadata(ts=utcnow(), key=key, value=value))


def get_session_meta(key: str) -> str | None:
    """Read a value from the session_metadata table by key.

    Returns None if the key doesn't exist (e.g., no logger session has run yet).
    """
    with session_scope() as s:
        row = s.query(SessionMetadata).filter_by(key=key).first()
        return row.value if row else None   # Return the value or None if key not found
