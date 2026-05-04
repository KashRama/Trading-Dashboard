"""Backtest engine: replay synthetic history through strategy.evaluate().

WHAT THIS FILE DOES:
Replays the 14-day synthetic dataset through the exact same strategy and
execution code used in live trading. Produces trade records, P&L curves,
and signal logs that the dashboard displays under the Backtest view.

ARCHITECTURAL COMMITMENT:
strategy.evaluate() is called identically here and in src/logger.py.
Same function, same filter stack, same Portfolio execution path.
Different data source (synthetic tables vs live API), identical logic.
This means the backtest is a true simulation of what live trading would do.

KEY DESIGN DECISIONS:

1. EXIT BEFORE ENTRY EVERY TICK:
   On each Kalshi snapshot, we first check if any open positions should close
   (game started, mean reversion), THEN look for new positions to open.
   This prevents opening a trade and immediately exiting it on the same tick.

2. MEAN REVERSION RUNS ON EVERY SIGNAL:
   Not just passing ones. When edge collapses below threshold, passed_filters=False.
   If mean-reversion only ran inside "if passed_filters:", positions would never
   get mean-reversion exits.

3. FORCE-CLOSE AT END:
   Any position still open when synthetic history ends gets closed at the last
   known mid. Ensures every position has a realized P&L and the dashboard
   shows 0 open trades after backtest.

4. ALL WRITES IN ONE SESSION:
   The entire backtest runs inside one session_scope() block. All DB writes
   commit at the end. Much faster than opening a new session per write."""

from __future__ import annotations

import json     
import random   
from dataclasses import dataclass          
from datetime import datetime, timedelta   
from typing import Optional              

from sqlalchemy import select 

from src import config          
from src.execution import Portfolio           
from src.orderbook import Level, OrderBook    
from src.storage import (
    BacktestPnLTimeseries,              
    BacktestSignal,                     
    BacktestTrade,                      
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


@dataclass
class BacktestResult:
    """Summary returned by run_backtest() to the dashboard after each run."""
    n_kalshi_snapshots: int      
    n_signals_evaluated: int     
    n_signals_passed: int        
    n_trades_opened: int         
    n_trades_closed: int         
    realized_pnl: float          
    unrealized_pnl: float        
    total_pnl: float             
    hit_rate: Optional[float]    
    best_trade: Optional[float]  
    worst_trade: Optional[float] 


def _book_from_json(book_json: str, ticker: str, ts: datetime) -> OrderBook:
    """Reconstruct an OrderBook from the JSON string stored in the synthetic row.
    The synthetic generator stores the full book as JSON so we can replay exact conditions."""
    data = json.loads(book_json)   # Parse JSON to dict with "bids" and "asks" lists
    bids = tuple(Level(int(l["price"]), int(l["size"])) for l in data["bids"])
    asks = tuple(Level(int(l["price"]), int(l["size"])) for l in data["asks"])
    return OrderBook(
        market_ticker=ticker,
        bids=bids,
        asks=asks,
        ts=ts.timestamp(),   # Store as Unix float timestamp
    )


def _odds_from_row(row: SyntheticExternalOddsSnapshot) -> ExternalOddsSnapshot:
    """Convert a synthetic odds DB row to the ExternalOddsSnapshot format evaluate() needs."""
    raw = json.loads(row.raw_json) if row.raw_json else {}   # Parse JSON blob if present
    fair_per_book = raw.get("fair_home_per_book", [])  
    return ExternalOddsSnapshot(
        game_id=row.game_id,
        consensus_home_prob=row.consensus_home_prob,   
        consensus_away_prob=row.consensus_away_prob,
        fair_home_per_book=list(fair_per_book),        
        n_books=row.n_books,
        snapshot_ts=row.ts,                            
    )


def _kalshi_snapshot(row: SyntheticKalshiSnapshot) -> KalshiMarketSnapshot:
    """Convert a synthetic Kalshi DB row to the KalshiMarketSnapshot format evaluate() expects."""
    book = _book_from_json(row.book_json, row.market_ticker, row.ts)   # Rebuild OrderBook from JSON
    return KalshiMarketSnapshot(
        market_ticker=row.market_ticker,
        game_id=row.game_id,
        home_team=row.home_team,
        away_team=row.away_team,
        sport=row.sport,
        game_start_ts=row.game_start_ts,   
        book=book,
        snapshot_ts=row.ts,                
    )


def _build_odds_index(session) -> dict[str, list[SyntheticExternalOddsSnapshot]]:
    """Pre-load all synthetic odds into a dict {game_id: [rows sorted by time]}.
    Avoids 18,000 individual DB queries during replay — we look up in-memory instead."""
    rows = session.execute(
        select(SyntheticExternalOddsSnapshot).order_by(SyntheticExternalOddsSnapshot.ts)
    ).scalars().all()
    by_game: dict[str, list[SyntheticExternalOddsSnapshot]] = {}
    for r in rows:
        by_game.setdefault(r.game_id, []).append(r)   # Group by game, maintaining chronological order
    return by_game


def _latest_odds_at_or_before(
    odds_list: list[SyntheticExternalOddsSnapshot], ts: datetime
) -> Optional[SyntheticExternalOddsSnapshot]:
    """Find the most recent odds row available at or before time ts.
    Scans backwards (from most recent) — stops at first row with ts <= target.
    Simulates what data we'd actually have had at each moment during live trading."""
    for r in reversed(odds_list):   # Scan from most recent to oldest
        if r.ts <= ts:
            return r   # This is the latest odds we'd have had at time ts
    return None   # No odds data existed yet for this game at this time


def run_backtest(
    seed: int = 7,
    include_real_kalshi: bool = False,
) -> BacktestResult:
    """Replay synthetic history through the strategy and execution layer.
    Called by the dashboard's Run Backtest button. Clears prior results first."""
    init_db()
    rng = random.Random(seed)   # Seeded for reproducibility — same seed = same game outcomes

    # Counters for the BacktestResult summary
    n_signals_eval = 0
    n_signals_passed = 0
    n_opened = 0
    n_closed = 0

    portfolio = Portfolio()   # Fresh in-memory portfolio for this backtest run

    # ALL DB operations in one session — much faster than per-write sessions
    with session_scope() as session:
        # Clear any prior backtest results so each run is a clean slate
        session.query(BacktestSignal).delete()
        session.query(BacktestTrade).delete()
        session.query(BacktestPnLTimeseries).delete()

        # Pre-load all synthetic odds data grouped by game (avoids per-tick queries)
        odds_by_game = _build_odds_index(session)

        # Load all Kalshi ticks in chronological order — these drive the replay
        kalshi_rows = session.execute(
            select(SyntheticKalshiSnapshot).order_by(SyntheticKalshiSnapshot.ts)
        ).scalars().all()

        outcomes: dict[str, bool] = {}
        for game_id, ods in odds_by_game.items():
            mean_consensus = sum(o.consensus_home_prob for o in ods) / len(ods)
            outcomes[game_id] = rng.random() < mean_consensus   # Probabilistic outcome

        latest_mid: dict[str, float] = {}    # Most recent mid price per market (for mark-to-market)
        last_pnl_sample: Optional[datetime] = None   # Throttle: max one P&L sample per minute

        for k_row in kalshi_rows:
            ts = k_row.ts   # Synthetic timestamp for this tick
            k_snap = _kalshi_snapshot(k_row)   # Convert DB row to KalshiMarketSnapshot

            # Update the most recent known mid for this market
            latest_mid[k_row.market_ticker] = k_row.mid or latest_mid.get(
                k_row.market_ticker, 50.0   # Fallback to 50¢ if mid is None
            )

            for ticker in list(portfolio.open_positions.keys()):
                pos = portfolio.open_positions[ticker]
                if ts >= _game_start_for_ticker(pos.game_id, odds_by_game):
                    # Game has started — settle this position at the simulated outcome
                    home_won = outcomes.get(pos.game_id, False)
                    pos = portfolio.close_at_resolution(ticker, home_won, ts)
                    if pos is not None:
                        n_closed += 1

            # Find the most recent odds available at or before this tick
            ods_list = odds_by_game.get(k_row.game_id, [])
            ods_row = _latest_odds_at_or_before(ods_list, ts)
            ods = _odds_from_row(ods_row) if ods_row else None

            # THE CORE STRATEGY CALL — identical to what the live logger calls
            sig = evaluate(k_snap, ods, ts)
            if sig is None:
                continue   # Skip: empty book or no odds data
            n_signals_eval += 1

            # Log every signal (passed and failed) to the backtest scanner table
            session.add(
                BacktestSignal(
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

            existing = portfolio.open_positions.get(sig.market_ticker)
            if existing is not None and abs(sig.edge_pp) < config.MEAN_REVERSION_EXIT_PP:
                # Compute exit price in the position's native space
                exit_price = (sig.kalshi_mid / 100.0) if existing.side == "YES" else (
                    1 - sig.kalshi_mid / 100.0   # NO exit price = 1 - YES mid
                )
                closed = portfolio.close(sig.market_ticker, exit_price, ts, "mean_reversion")
                if closed is not None:
                    n_closed += 1

            if sig.passed_filters:
                n_signals_passed += 1
                if sig.market_ticker not in portfolio.open_positions:   # One position per market
                    new_pos = portfolio.open_from_signal(sig, k_snap.book)
                    if new_pos is not None:
                        n_opened += 1

            if last_pnl_sample is None or (ts - last_pnl_sample) >= timedelta(minutes=1):
                unreal = portfolio.total_unrealized(latest_mid)   # Mark open positions to mid
                session.add(
                    BacktestPnLTimeseries(
                        ts=ts,
                        realized_pnl=portfolio.realized_pnl,
                        unrealized_pnl=unreal,
                        total_pnl=portfolio.realized_pnl + unreal,
                        open_positions=len(portfolio.open_positions),
                    )
                )
                last_pnl_sample = ts

        final_ts = kalshi_rows[-1].ts if kalshi_rows else datetime.utcnow()
        for ticker in list(portfolio.open_positions.keys()):
            pos = portfolio.open_positions[ticker]
            mid_frac = latest_mid.get(ticker, 50.0) / 100.0   # Last known mid in dollars
            exit_p = mid_frac if pos.side == "YES" else (1.0 - mid_frac)   # Side-appropriate price
            closed = portfolio.close(ticker, exit_p, final_ts, "end_of_backtest")
            if closed is not None:
                n_closed += 1

        session.add(
            BacktestPnLTimeseries(
                ts=final_ts,
                realized_pnl=portfolio.realized_pnl,
                unrealized_pnl=0.0,           # All positions closed — no unrealized
                total_pnl=portfolio.realized_pnl,
                open_positions=0,
            )
        )

        # Persist all closed trades — written here at the end for performance
        for pos in portfolio.closed_positions:
            session.add(
                BacktestTrade(
                    trade_id=pos.trade_id,
                    open_ts=pos.open_ts,
                    close_ts=pos.close_ts,
                    market_ticker=pos.market_ticker,
                    game_id=pos.game_id,
                    side=pos.side,
                    size=pos.size,
                    entry_price=pos.entry_price,
                    exit_price=pos.exit_price,
                    edge_at_entry_pp=pos.edge_at_entry_pp,
                    spread_at_entry=pos.spread_at_entry,
                    depth_3_bid_at_entry=pos.depth_3_bid_at_entry,
                    depth_3_ask_at_entry=pos.depth_3_ask_at_entry,
                    consensus_prob_at_entry=pos.consensus_prob_at_entry,
                    status="closed",           # All backtest trades are always closed
                    realized_pnl=pos.realized_pnl,
                    close_reason=pos.close_reason,
                )
            )
        # session.commit() happens automatically when the with block exits

    # Compute hit rate from all closed positions
    closed_pnls = [p.realized_pnl for p in portfolio.closed_positions if p.realized_pnl is not None]
    wins = sum(1 for p in closed_pnls if p > 0)   # Count winning trades
    hit_rate = wins / len(closed_pnls) if closed_pnls else None   # Fraction that were profitable

    return BacktestResult(
        n_kalshi_snapshots=len(kalshi_rows),
        n_signals_evaluated=n_signals_eval,
        n_signals_passed=n_signals_passed,
        n_trades_opened=n_opened,
        n_trades_closed=n_closed,
        realized_pnl=portfolio.realized_pnl,
        unrealized_pnl=0.0,                    # Always 0 after force-close
        total_pnl=portfolio.realized_pnl,      # Same as realized since all closed
        hit_rate=hit_rate,
        best_trade=max(closed_pnls) if closed_pnls else None,
        worst_trade=min(closed_pnls) if closed_pnls else None,
    )


def _game_start_for_ticker(
    game_id: str,
    odds_by_game: dict[str, list[SyntheticExternalOddsSnapshot]],
) -> datetime:
    """Look up the game start time for a game from the pre-loaded odds index.

    Returns datetime.max if no odds data exists — a far-future sentinel that
    prevents any position from being treated as "game started" (safe default).
    """
    rows = odds_by_game.get(game_id, [])
    if rows:
        return rows[0].game_start_ts   # First odds row has the game start (rows sorted by ts)
    return datetime.max   # Sentinel: no game start = never trigger the resolution exit
