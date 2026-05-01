"""Simulated execution against the Kalshi order book.

WHAT THIS FILE DOES:
Manages the portfolio of open and closed positions. When a signal fires,
the Portfolio opens a position using the current order book depth (VWAP fill).
When an exit condition triggers, the Portfolio closes the position and records P&L.

KEY DESIGN DECISIONS:

1. CROSSING THE SPREAD IS NON-NEGOTIABLE:
   Every fill price comes from walking the actual order book depth (VWAP),
   never from the mid price. This is honest — if you claim you bought at mid,
   you're lying to yourself. Real trades execute at ask (or worse), not mid.

2. NO FEES:
   Kalshi sandbox is free. Production fees are a noted TODO — they
   materially affect profitability and the minimum edge threshold.

3. IN-MEMORY STATE:
   The Portfolio object lives in RAM. Positions are persisted to SQLite
   only at open and close time. This makes the backtest fast (18,000
   ticks with no mid-loop DB writes) and the live logger responsive.

4. SHARED BY LIVE AND BACKTEST:
   The same Portfolio class is used by src/logger.py (live) and
   src/replay.py (backtest). This is the architectural commitment —
   identical execution logic in both modes.
"""

from __future__ import annotations

import uuid                               
from dataclasses import dataclass, field  
from datetime import datetime            
from typing import Optional              

from src import config                   
from src.orderbook import OrderBook, buy_no_fill, buy_yes_fill  # VWAP fill simulation
from src.strategy import Signal           


@dataclass
class Position:
    """One open or closed trading position.

    This is the core data record for a trade. Created when a signal fires,
    updated when the position closes. All financial calculations for this
    trade are derived from these fields.

    NOT frozen: positions are mutable — they start as open and get
    updated (exit_price, close_ts, realized_pnl, status) when they close.
    """
    # --- Identity ---
    trade_id: str          
    market_ticker: str     
    game_id: str           

    # --- Position details at entry ---
    side: str               
    size: int               
    entry_price: float      
    open_ts: datetime       

    # --- Context recorded at entry (for analysis later) ---
    edge_at_entry_pp: float        
    spread_at_entry: float         
    depth_3_bid_at_entry: int      
    depth_3_ask_at_entry: int      
    consensus_prob_at_entry: float 

    # --- Exit fields (None while position is open, filled on close) ---
    exit_price: Optional[float] = None    
    close_ts: Optional[datetime] = None   
    realized_pnl: Optional[float] = None  
    close_reason: Optional[str] = None    
                                          
    # --- Status ---
    status: str = "open"   


@dataclass
class Portfolio:
    """In-memory portfolio: tracks all open and closed positions and realized P&L.

    This is a pure Python object — it does NOT talk to the database.
    The logger and backtest engine persist positions to SQLite separately.

    WHY IN-MEMORY?
    The backtest calls open_from_signal() and close() thousands of times.
    If each call hit the database, the backtest would be 10-100x slower.
    In-memory state is fast; database writes happen only at open/close boundaries.

    SHARED USAGE:
    - src/logger.py: creates one Portfolio per session, calls open_from_signal()
      and close() as signals fire. Portfolio lives as long as the logger runs.
    - src/replay.py: creates one Portfolio for the entire backtest replay.
    - src/demo_live.py: creates one Portfolio for the demo session.
    """
    open_positions: dict[str, Position] = field(default_factory=dict)
    closed_positions: list[Position] = field(default_factory=list)
    realized_pnl: float = 0.0

    def has_capacity(self, market_ticker: str) -> tuple[bool, str]:
        """Check whether the portfolio can accept a new position in this market.

        Two constraints:
        1. MAX_OPEN_POSITIONS: global cap on simultaneous open positions
        2. MAX_POSITION_PER_MARKET: per-market contract limit (prevents concentration)

        Returns:
            (True, "ok") if we can open a new position
            (False, reason_string) if we can't, explaining why
        """
        if len(self.open_positions) >= config.MAX_OPEN_POSITIONS:
            return False, f"max open positions ({config.MAX_OPEN_POSITIONS})"

        existing = self.open_positions.get(market_ticker)
        if existing is not None:
            if existing.size + config.TRADE_SIZE_CONTRACTS > config.MAX_POSITION_PER_MARKET:
                return False, f"max per-market ({config.MAX_POSITION_PER_MARKET})"

        return True, "ok"

    def open_from_signal(
        self, signal: Signal, book: OrderBook
    ) -> Optional[Position]:
        """Open a new position based on a passing signal.

        Steps:
        1. Check capacity (max positions, max per-market)
        2. Enforce one-position-per-market rule
        3. Simulate the VWAP fill using current order book depth
        4. Create a Position and store it in open_positions

        Args:
            signal: the passing Signal that triggered this trade
            book: the current order book (used to compute entry price via VWAP)

        Returns:
            The newly created Position, or None if capacity check fails or book is empty.
        """
        ok, _ = self.has_capacity(signal.market_ticker)
        if not ok:
            return None

        if signal.market_ticker in self.open_positions:
            return None

        qty = config.TRADE_SIZE_CONTRACTS

        if signal.side == "YES":
            # Buying YES: lift asks (accept the sellers' prices, cross the spread)
            fill = buy_yes_fill(book, qty)
        else:
            # Buying NO: hit bids (sell YES to buyers, equivalent to buying NO)
            fill = buy_no_fill(book, qty)

        if fill is None or fill.filled_qty == 0:
            return None

        entry_price = fill.avg_price_cents / 100.0

        pos = Position(
            trade_id=uuid.uuid4().hex[:12],         
            market_ticker=signal.market_ticker,
            game_id=signal.game_id,
            side=signal.side,
            size=fill.filled_qty,                   
            entry_price=entry_price,                
            open_ts=signal.ts,                      
            edge_at_entry_pp=signal.edge_pp,        
            spread_at_entry=signal.spread_cents,    
            depth_3_bid_at_entry=signal.metrics.depth_3_bid,   
            depth_3_ask_at_entry=signal.metrics.depth_3_ask,
            consensus_prob_at_entry=signal.consensus_prob,     
        )

        self.open_positions[signal.market_ticker] = pos
        return pos

    def mark_to_market(self, market_ticker: str, current_mid_cents: float) -> float:
        """Compute the unrealized P&L for one open position at the current mid price.

        Uses mid (not ask or bid) for marking — mid is the standard for mark-to-market.

        For YES positions:
            We bought YES at entry_price. Current fair value is mid.
            Unrealized P&L = (mid_dollars - entry_price) × size

        For NO positions:
            We bought NO. NO's current fair value = 1 - YES_mid.
            Unrealized P&L = ((1 - mid_dollars) - entry_price) × size

        Args:
            market_ticker: which position to mark
            current_mid_cents: current Kalshi mid price in cents (e.g. 58.0)

        Returns:
            Unrealized P&L in dollars. 0.0 if no open position for this ticker.
        """
        pos = self.open_positions.get(market_ticker)
        if pos is None:
            return 0.0  

        mid_p = current_mid_cents / 100.0

        if pos.side == "YES":
            return (mid_p - pos.entry_price) * pos.size
        else:
            return ((1 - mid_p) - pos.entry_price) * pos.size

    def total_unrealized(self, mid_cents_by_ticker: dict[str, float]) -> float:
        """Sum unrealized P&L across all open positions.

        Args:
            mid_cents_by_ticker: dict of ticker → current mid in cents.
                Positions not in this dict are skipped (no current quote available).

        Returns:
            Total unrealized P&L in dollars across all open positions.
        """
        total = 0.0
        for t in self.open_positions:
            mid = mid_cents_by_ticker.get(t)   # Look up current mid for this position
            if mid is not None:
                total += self.mark_to_market(t, mid)   # Add this position's unrealized P&L
        return total

    def close(
        self,
        market_ticker: str,
        exit_price: float,  
        ts: datetime,       
        reason: str,        
    ) -> Optional[Position]:
        """Close an open position and record the realized P&L.

        Removes from open_positions, computes P&L, appends to closed_positions,
        and updates the portfolio's running realized_pnl total.

        P&L formula (same for YES and NO — exit_price is always in the contract's native space):
            realized_pnl = (exit_price - entry_price) × size

        Example (YES):
            Bought YES at $0.52, exited at $0.58, size=10
            P&L = (0.58 - 0.52) × 10 = +$0.60

        Example (NO):
            Bought NO at $0.48 (= 100 - 52¢ bid), market moved to YES=45¢
            Exit NO price = 1 - 0.45 = $0.55
            P&L = (0.55 - 0.48) × 10 = +$0.70

        Returns:
            The closed Position, or None if no open position exists for this ticker.
        """
        pos = self.open_positions.pop(market_ticker, None)
        if pos is None:
            return None  

        # Record exit details directly on the position object
        pos.exit_price = exit_price
        pos.close_ts = ts
        pos.close_reason = reason
        pos.status = "closed"

        # Compute realized P&L — formula is identical for YES and NO
        # because exit_price is always expressed in the position's native space
        if pos.side == "YES":
            pos.realized_pnl = (exit_price - pos.entry_price) * pos.size
        else:
            pos.realized_pnl = (exit_price - pos.entry_price) * pos.size

        # Add this trade's P&L to the running portfolio total
        self.realized_pnl += pos.realized_pnl

        # Move to the closed positions list for historical reporting
        self.closed_positions.append(pos)

        return pos

    def close_at_resolution(
        self,
        market_ticker: str,
        home_won: bool,    # True = home team won (YES pays $1, NO pays $0)
        ts: datetime,
    ) -> Optional[Position]:
        """Settle a position at Kalshi market resolution (game end).

        Kalshi contracts are binary — they pay exactly $1.00 or $0.00 at resolution:
        - YES pays $1.00 if home wins, $0.00 if home loses
        - NO pays $1.00 if home loses, $0.00 if home wins

        The exit price is therefore always 0.0 (total loss) or 1.0 (total win).
        This models the actual Kalshi settlement behavior.

        Args:
            market_ticker: which market settled
            home_won: did the home team win the game?
            ts: when the market settled

        Returns:
            The closed Position, or None if no open position for this market.
        """
        pos = self.open_positions.get(market_ticker)
        if pos is None:
            return None   # No position to settle

        if pos.side == "YES":
            payout = 1.0 if home_won else 0.0   # Win $1 if home wins, lose everything otherwise
        else:
            payout = 0.0 if home_won else 1.0   # Win $1 if home loses, lose everything otherwise

        return self.close(market_ticker, payout, ts, "resolution")
