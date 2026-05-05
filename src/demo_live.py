"""Demo-live mode: replay synthetic data into the LIVE tables at fast-forward speed.

WHAT THIS FILE DOES:
Generates a fresh 30-game synthetic dataset anchored to wall-clock now,
then replays it into the same live tables (kalshi_snapshots, signals,
simulated_trades, pnl_timeseries) that the real logger writes to.
The dashboard's Live tab shows trades appearing in real time.

WHY THIS EXISTS:
The real --live-orders mode requires NBA/MLB games to be available on the
Kalshi sandbox AND for the strategy filters to find a tradeable edge.
Neither is guaranteed. Demo mode generates controlled synthetic data that
is engineered to guarantee signal fires, proving the full pipeline works
regardless of what the sandbox is currently offering.

KEY DIFFERENCE FROM replay.py:
- replay.py → writes to backtest_* tables, dashboard Backtest view
- demo_live.py → writes to live tables, dashboard Live view

SPEED CONTROL:
At speed=60 (default), each 60-second synthetic gap becomes 1 real second.
A 5-hour synthetic window (300 ticks × 60s) plays back in 5 real minutes.

TIMESTAMP REBASING:
The synthetic dataset is generated with timestamps anchored to datetime.utcnow().
The logger adds wall_offset = wall_now - first_synthetic_ts to shift all timestamps
to right now, so the dashboard chart shows today's date instead of the generation date.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import random

from sqlalchemy import select

from src import config
from src.execution import Portfolio
from src.orderbook import Level, OrderBook, compute_metrics
from src.storage import (
    ExternalOddsSnapshot as ExtOddsRow,
    KalshiSnapshot,
    PnLTimeseries,
    Signal as SignalRow,
    SimulatedTrade,
    SyntheticExternalOddsSnapshot,
    SyntheticKalshiSnapshot,
    init_db,
    session_scope,
)
from src.strategy import (
    ExternalOddsSnapshot,
    KalshiMarketSnapshot,
    evaluate,
)

log = logging.getLogger("kalshi-rv-demo-live")


def _odds_from_row(row: SyntheticExternalOddsSnapshot) -> ExternalOddsSnapshot:
    """Convert a synthetic odds DB row to the ExternalOddsSnapshot format evaluate() expects.
    Extracts per-book fair probs from the raw_json blob (needed for books_agreeing filter)."""
    raw = json.loads(row.raw_json) if row.raw_json else {}
    return ExternalOddsSnapshot(
        game_id=row.game_id,
        consensus_home_prob=row.consensus_home_prob,
        consensus_away_prob=row.consensus_away_prob,
        fair_home_per_book=list(raw.get("fair_home_per_book", [])),   # Per-book values
        n_books=row.n_books,
        snapshot_ts=row.ts,   # Snapshot time — used for staleness filter
    )


def _persist_trade_close(pos) -> None:
    """Update the live SimulatedTrade row when a position closes.

    Demo mode opens a SimulatedTrade row when a position opens (status="open"),
    then calls this function to patch in the exit details when it closes.
    This mirrors what the live logger does for its own positions.
    """
    with session_scope() as s:
        row = s.execute(
            select(SimulatedTrade).where(SimulatedTrade.trade_id == pos.trade_id)
        ).scalar_one_or_none()
        if row is None:
            return   # Row doesn't exist — defensive guard
        row.close_ts = pos.close_ts
        row.exit_price = pos.exit_price
        row.realized_pnl = pos.realized_pnl
        row.close_reason = pos.close_reason
        row.status = "closed"   # Mark as closed — dashboard moves it to Closed Trades table


def _book_from_json(book_json: str, ticker: str) -> OrderBook:
    """Reconstruct an OrderBook from the JSON string stored in the synthetic Kalshi row."""
    data = json.loads(book_json)   # Parse {"bids": [...], "asks": [...]}
    return OrderBook(
        market_ticker=ticker,
        bids=tuple(Level(int(l["price"]), int(l["size"])) for l in data["bids"]),
        asks=tuple(Level(int(l["price"]), int(l["size"])) for l in data["asks"]),
    )


def _tag_demo_mode() -> None:
    """Write "mode=demo" to session_metadata so the dashboard knows it's showing demo data.
    The dashboard reads this to decide which data source to use for Cash Available."""
    from src.storage import set_session_meta
    set_session_meta("mode", "demo")


def _reset_live_tables() -> None:
    """Clear all live tables before a fresh demo run.

    Demo mode is meant for demonstration — persistent live state from prior runs
    or real logger sessions would pollute the display. Clearing here gives a clean slate.
    Also prevents the Plotly chart "long diagonal" artifact when old + new data coexist.
    """
    with session_scope() as s:
        s.query(KalshiSnapshot).delete()
        s.query(SignalRow).delete()
        s.query(SimulatedTrade).delete()
        s.query(PnLTimeseries).delete()
        s.query(ExtOddsRow).delete()


def run_demo(
    speed: float = 60.0,
    max_iterations: Optional[int] = None,
    reset: bool = True,
    seed: int = 7,
) -> None:
    """Replay synthetic snapshots into live tables at speed × real time.

    Args:
        speed: simulated seconds per real second.
               60.0 = each 60s synthetic gap becomes 1 real second → 5-min run.
               600.0 = each 60s gap becomes 0.1s → 30-second run.
        max_iterations: stop after this many Kalshi ticks (used in tests)
        reset: clear live tables before starting (default True)
        seed: RNG seed for reproducible game outcomes
    """
    init_db()           
    _tag_demo_mode()   
    if reset:
        _reset_live_tables()  

    portfolio = Portfolio()         
    rng = random.Random(seed)       
    log.info(f"demo-live starting (speed={speed}x). Ctrl+C to stop.")

    # Load all synthetic data upfront — avoid per-tick DB queries during replay
    with session_scope() as s:
        odds_rows = s.execute(
            select(SyntheticExternalOddsSnapshot).order_by(SyntheticExternalOddsSnapshot.ts)
        ).scalars().all()
        kalshi_rows = s.execute(
            select(SyntheticKalshiSnapshot).order_by(SyntheticKalshiSnapshot.ts)
        ).scalars().all()
        odds_rows = list(odds_rows)       # Materialize from SQLAlchemy lazy cursor
        kalshi_rows = list(kalshi_rows)

    if not kalshi_rows:
        log.error("no synthetic data found. run: python -m scripts.generate_synthetic_history")
        return

    odds_by_game: dict[str, list[SyntheticExternalOddsSnapshot]] = {}
    for r in odds_rows:
        odds_by_game.setdefault(r.game_id, []).append(r)

    wall_now = datetime.utcnow()
    wall_offset = wall_now - kalshi_rows[0].ts   # How much to add to every synthetic timestamp

    def shift(ts: datetime) -> datetime:
        """Apply the wall-clock offset to a synthetic timestamp."""
        return ts + wall_offset

    outcomes: dict[str, bool] = {}      # game_id → did home team win?
    game_starts: dict[str, datetime] = {}   # game_id → rebased game start time
    for game_id, ods_list in odds_by_game.items():
        mean_consensus = sum(o.consensus_home_prob for o in ods_list) / len(ods_list)
        outcomes[game_id] = rng.random() < mean_consensus    # Home wins with prob = consensus
        game_starts[game_id] = shift(ods_list[0].game_start_ts)   # Shift game start to wall clock

    latest_mid: dict[str, float] = {}   # Most recent mid per market (for mark-to-market)
    last_pnl_ts: Optional[datetime] = None   # Throttle P&L sampling to once per minute
    n_trades_opened = 0
    n_trades_closed = 0
    iteration = 0
    prev_synthetic_ts: Optional[datetime] = None

    for k_row in kalshi_rows:
        iteration += 1
        if max_iterations is not None and iteration > max_iterations:
            break   # Used in tests to stop after a fixed number of ticks

        synthetic_ts = k_row.ts                 # Original synthetic timestamp
        display_ts = shift(synthetic_ts)        # Rebased to wall clock (shows today's date)

        if prev_synthetic_ts is not None:
            delta = (synthetic_ts - prev_synthetic_ts).total_seconds()
            sleep_for = max(0.0, delta / speed)   # e.g. 60s synthetic / 60x speed = 1s real
            if sleep_for > 0:
                time.sleep(min(sleep_for, 5.0))   # Cap at 5s per sleep to stay responsive
        prev_synthetic_ts = synthetic_ts   # Remember this tick's time for next iteration

        # EXIT FIRST: check if any open positions' games have started
        for ticker in list(portfolio.open_positions.keys()):
            pos = portfolio.open_positions[ticker]
            gs = game_starts.get(pos.game_id)   # Rebased game start time
            if gs is not None and display_ts >= gs:
                home_won = outcomes.get(pos.game_id, False)
                closed = portfolio.close_at_resolution(ticker, home_won, display_ts)
                if closed is not None:
                    n_trades_closed += 1
                    _persist_trade_close(closed)   # Update the DB row to "closed"

        book = _book_from_json(k_row.book_json, k_row.market_ticker)
        metrics = compute_metrics(book)
        if metrics.mid is not None:
            latest_mid[k_row.market_ticker] = metrics.mid   # Track for mark-to-market

        with session_scope() as s:
            s.add(
                KalshiSnapshot(
                    ts=display_ts,                           # Wall-clock rebased timestamp
                    market_ticker=k_row.market_ticker,
                    game_id=k_row.game_id,
                    home_team=k_row.home_team,
                    away_team=k_row.away_team,
                    sport=k_row.sport,
                    game_start_ts=shift(k_row.game_start_ts),   # Rebase game start too
                    top_bid=metrics.top_bid,
                    top_ask=metrics.top_ask,
                    mid=metrics.mid,
                    spread_cents=metrics.spread_cents,
                    depth_3_bid=metrics.depth_3_bid,
                    depth_3_ask=metrics.depth_3_ask,
                    imbalance=metrics.imbalance,
                    total_quoted_size=metrics.total_quoted_size,
                    book_json=None,          # Don't store full book in live table (saves space)
                    is_synthetic=True,       # Flag: this came from demo mode, not real Kalshi
                )
            )

        ods_list = odds_by_game.get(k_row.game_id, [])
        ods_row = None
        for r in reversed(ods_list):
            if r.ts <= synthetic_ts:
                ods_row = r
                break
        if ods_row is None:
            continue

        kalshi_snap = KalshiMarketSnapshot(
            market_ticker=k_row.market_ticker,
            game_id=k_row.game_id,
            home_team=k_row.home_team,
            away_team=k_row.away_team,
            sport=k_row.sport,
            game_start_ts=shift(k_row.game_start_ts),
            book=book,
            snapshot_ts=display_ts,
        )
        
        ods_snap = _odds_from_row(ods_row)
        ods_snap = ExternalOddsSnapshot(
            game_id=ods_snap.game_id,
            consensus_home_prob=ods_snap.consensus_home_prob,
            consensus_away_prob=ods_snap.consensus_away_prob,
            fair_home_per_book=ods_snap.fair_home_per_book,
            n_books=ods_snap.n_books,
            snapshot_ts=shift(ods_row.ts),
        )
        sig = evaluate(kalshi_snap, ods_snap, display_ts)
        if sig is None:
            continue

        with session_scope() as s:
            s.add(
                SignalRow(
                    ts=sig.ts,
                    market_ticker=sig.market_ticker,
                    game_id=sig.game_id,
                    side=sig.side,
                    edge_pp=sig.edge_pp,
                    kalshi_mid=sig.kalshi_mid,
                    consensus_prob=sig.consensus_prob,
                    n_books=sig.n_books,
                    spread_cents=sig.spread_cents,
                    top_of_book_size=sig.top_of_book_size,
                    minutes_to_game=sig.minutes_to_game,
                    passed_filters=sig.passed_filters,
                    reason=sig.reason,
                )
            )

        # Mean-reversion exit: if we hold a position and edge collapsed, close at current mid before considering opening anything new.
        existing = portfolio.open_positions.get(sig.market_ticker)
        if existing is not None and abs(sig.edge_pp) < config.MEAN_REVERSION_EXIT_PP:
            mid_p = sig.kalshi_mid / 100.0
            exit_p = mid_p if existing.side == "YES" else (1 - mid_p)
            closed = portfolio.close(sig.market_ticker, exit_p, display_ts, "mean_reversion")
            if closed is not None:
                n_trades_closed += 1
                _persist_trade_close(closed)

        if sig.passed_filters and sig.market_ticker not in portfolio.open_positions:
            pos = portfolio.open_from_signal(sig, book)
            if pos is not None:
                n_trades_opened += 1
                log.info(
                    f"opened {pos.side} x{pos.size} on {pos.market_ticker} "
                    f"@ ${pos.entry_price:.4f} (edge {pos.edge_at_entry_pp:+.2f}pp)"
                )
                with session_scope() as s:
                    s.add(
                        SimulatedTrade(
                            trade_id=pos.trade_id,
                            open_ts=pos.open_ts,
                            market_ticker=pos.market_ticker,
                            game_id=pos.game_id,
                            side=pos.side,
                            size=pos.size,
                            entry_price=pos.entry_price,
                            edge_at_entry_pp=pos.edge_at_entry_pp,
                            spread_at_entry=pos.spread_at_entry,
                            depth_3_bid_at_entry=pos.depth_3_bid_at_entry,
                            depth_3_ask_at_entry=pos.depth_3_ask_at_entry,
                            consensus_prob_at_entry=pos.consensus_prob_at_entry,
                            status="open",
                        )
                    )

        if last_pnl_ts is None or (display_ts - last_pnl_ts) >= timedelta(minutes=1):
            unreal = portfolio.total_unrealized(latest_mid)
            with session_scope() as s:
                s.add(
                    PnLTimeseries(
                        ts=display_ts,
                        realized_pnl=portfolio.realized_pnl,
                        unrealized_pnl=unreal,
                        total_pnl=portfolio.realized_pnl + unreal,
                        open_positions=len(portfolio.open_positions),
                    )
                )
            last_pnl_ts = display_ts

    final_ts = display_ts if 'display_ts' in dir() else datetime.utcnow()
    for ticker in list(portfolio.open_positions.keys()):
        pos_open = portfolio.open_positions[ticker]
        mid = latest_mid.get(ticker, 50.0) / 100.0
        exit_p = mid if pos_open.side == "YES" else (1.0 - mid)
        closed_pos = portfolio.close(ticker, exit_p, final_ts, "end_of_demo")
        if closed_pos is not None:
            n_trades_closed += 1
            _persist_trade_close(closed_pos)

    with session_scope() as s:
        s.add(PnLTimeseries(
            ts=final_ts,
            realized_pnl=portfolio.realized_pnl,
            unrealized_pnl=0.0,
            total_pnl=portfolio.realized_pnl,
            open_positions=0,
        ))

    log.info(
        f"demo-live finished: {iteration} ticks replayed, "
        f"{n_trades_opened} opened, {n_trades_closed} closed, "
        f"realized P&L ${portfolio.realized_pnl:+.2f}"
    )
