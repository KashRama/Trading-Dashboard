"""Signal generation: compare Kalshi mid to de-vigged sportsbook consensus.

THIS IS THE CORE OF THE TRADING STRATEGY.

THE IDEA:
On any given NBA/MLB game, both Kalshi and sportsbooks are trying to estimate
the probability that the home team wins. Sportsbooks employ professional
oddsmakers with large teams and significant resources. Kalshi is a newer,
thinner market. When these two estimates diverge significantly, we bet that
Kalshi will converge toward the sportsbook consensus.

HOW EDGE IS COMPUTED:
    edge_pp = (consensus_probability - kalshi_mid_probability) × 100

Positive edge: sportsbooks think home team is more likely to win than Kalshi does.
→ Kalshi is underpricing YES → BUY YES

Negative edge: sportsbooks think home team is less likely to win than Kalshi does.
→ Kalshi is overpricing YES → BUY NO (bet against home team)

THE FILTER STACK:
A raw edge alone is not enough to trade. We apply 6 additional filters to ensure:
1. The edge is large enough to be real (not noise)
2. There is enough liquidity to actually execute
3. The spread isn't so wide it eats the edge
4. Multiple books agree (not a one-book anomaly)
5. The game is in the right time window
6. Both data sources are fresh

ARCHITECTURAL COMMITMENT:
evaluate() is called identically by:
- src/logger.py (live trading loop)
- src/replay.py (backtest engine)
- src/demo_live.py (demo mode)

Same function, same filter stack, same reason strings. This means backtest
results reflect exactly what would happen in live trading.
"""

from __future__ import annotations

from dataclasses import dataclass    
from datetime import datetime        
from statistics import median        
from typing import Optional          

from src import config               
from src.devig import books_agreeing_direction   
from src.orderbook import BookMetrics, OrderBook, compute_metrics 


@dataclass(frozen=True)
class KalshiMarketSnapshot:
    """A complete snapshot of one Kalshi market at one point in time.

    frozen=True: immutable — this is a point-in-time observation, not mutable state.
    Contains both the market metadata AND the current order book.

    This is the "left side" input to evaluate() — what Kalshi thinks.
    """
    market_ticker: str      
    game_id: str            
    home_team: str          
    away_team: str          
    sport: str              
    game_start_ts: datetime 
    book: OrderBook         
    snapshot_ts: datetime   


@dataclass(frozen=True)
class ExternalOddsSnapshot:
    """De-vigged sportsbook consensus for one game at one point in time.

    frozen=True: immutable — a point-in-time observation.

    This is the "right side" input to evaluate() — what the sportsbooks think.
    Produced by OddsAPIClient.to_consensus_snapshot() after de-vigging each book.
    """
    game_id: str                      
    consensus_home_prob: float        
    consensus_away_prob: float        
    fair_home_per_book: list[float]   
                                      
    n_books: int                      
    snapshot_ts: datetime             


@dataclass(frozen=True)
class Signal:
    """The output of evaluate() — a complete description of the edge at one point in time.

    A Signal is ALWAYS returned (even when filters fail) because we want to log
    every evaluated market to the scanner — traders should see why signals didn't fire.

    The passed_filters field tells you whether this signal is tradeable.
    The reason field tells you which filter failed (or "ok" if all passed).

    frozen=True: signals are historical records — never mutate them.
    """
    market_ticker: str         
    game_id: str               
    side: str                  
    edge_pp: float             
    kalshi_mid: float          
    consensus_prob: float      
    n_books: int               
    spread_cents: float        
    top_of_book_size: int      
    minutes_to_game: float     
    metrics: BookMetrics       
    passed_filters: bool       
    reason: str                
    ts: datetime               


def _minutes_until(game_start: datetime, now: datetime) -> float:
    """Calculate how many minutes until the game starts.

    Positive = game is in the future (normal case).
    Negative = game has already started (we should not be trading this market).
    """
    return (game_start - now).total_seconds() / 60.0


def _staleness_seconds(snapshot_ts: datetime, now: datetime) -> float:
    """Calculate how many seconds old a data snapshot is.

    Used to ensure both Kalshi and odds data are recent enough to compare.
    A 5-minute-old odds quote vs. a fresh Kalshi mid is not a fair comparison.
    """
    return (now - snapshot_ts).total_seconds()


def evaluate(
    kalshi: KalshiMarketSnapshot,
    odds: Optional[ExternalOddsSnapshot],
    now: datetime,
) -> Signal | None:
    """The core strategy function. Takes a Kalshi snapshot and odds snapshot, returns a Signal.

    Called identically in live mode, backtest, and demo mode.

    RETURNS None only if the book is empty (no mid price computable) or odds are missing.
    RETURNS a Signal with passed_filters=False if any filter fails.
    RETURNS a Signal with passed_filters=True if all filters pass — this is a tradeable signal.

    The filter stack is applied in ORDER — first failure wins.
    Order chosen to reject the most common failures cheapest (edge and size first,
    staleness last since it requires timestamp arithmetic).
    """
    metrics = compute_metrics(kalshi.book)

    if metrics.mid is None or metrics.spread_cents is None:
        return None

    if odds is None:
        return None

    # --- Edge calculation ---
    kalshi_yes_prob = metrics.mid / 100.0
    consensus_yes = odds.consensus_home_prob

    # Edge in percentage points:
    edge_pp = (consensus_yes - kalshi_yes_prob) * 100.0
    side = "YES" if edge_pp > 0 else "NO"

    # Top-of-book size on the side we want to trade:
    if side == "YES":
        top_size = kalshi.book.asks[0].size if kalshi.book.asks else 0
    else:
        top_size = kalshi.book.bids[0].size if kalshi.book.bids else 0

    minutes_to_game = _minutes_until(kalshi.game_start_ts, now)
    n_agree = books_agreeing_direction(odds.fair_home_per_book, kalshi_yes_prob)

    # --- Filter stack ---
    reason = "ok"    
    passed = True    

    if abs(edge_pp) < config.EDGE_THRESHOLD_PP:        
        passed, reason = False, f"edge {edge_pp:+.2f}pp < {config.EDGE_THRESHOLD_PP}pp"

    elif top_size < config.MIN_TOP_OF_BOOK_SIZE:
        # LIQUIDITY FILTER: Not enough contracts available to fill our trade.
        passed, reason = False, f"top size {top_size} < {config.MIN_TOP_OF_BOOK_SIZE}"

    elif metrics.spread_cents > config.MAX_SPREAD_CENTS:
        # COST FILTER: Spread too wide.
        passed, reason = False, f"spread {metrics.spread_cents:.0f}c > {config.MAX_SPREAD_CENTS}c"

    elif n_agree < config.MIN_BOOKS_AGREEING:
        # OUTLIER FILTER: Not enough books agree on the edge direction.
        passed, reason = False, f"only {n_agree}/{odds.n_books} books agree"

    elif minutes_to_game < config.MIN_MINUTES_TO_GAME:
        # TIMING FILTER (too close): Game starting too soon.
        passed, reason = False, f"too close to tipoff ({minutes_to_game:.0f}min)"

    elif minutes_to_game > config.MAX_MINUTES_TO_GAME:
        # TIMING FILTER (too far): Game too far away
        passed, reason = False, f"too far from tipoff ({minutes_to_game:.0f}min)"

    else:
        # STALENESS FILTER: Only checked if all other filters pass
        kalshi_stale = _staleness_seconds(kalshi.snapshot_ts, now)
        odds_stale = _staleness_seconds(odds.snapshot_ts, now)

        if kalshi_stale > config.MAX_STALENESS_SECONDS:
            passed, reason = False, f"kalshi quote stale ({kalshi_stale:.0f}s)"
        elif odds_stale > config.MAX_STALENESS_SECONDS:
            passed, reason = False, f"odds stale ({odds_stale:.0f}s)"

    return Signal(
        market_ticker=kalshi.market_ticker,
        game_id=kalshi.game_id,
        side=side,                        
        edge_pp=edge_pp,                  
        kalshi_mid=metrics.mid,           
        consensus_prob=consensus_yes,     
        n_books=odds.n_books,             
        spread_cents=metrics.spread_cents,
        top_of_book_size=top_size,        
        minutes_to_game=minutes_to_game,  
        metrics=metrics,                  
        passed_filters=passed,            
        reason=reason,                    
        ts=now,                           
    )
