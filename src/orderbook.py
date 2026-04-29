"""Order book data structures, market metrics, and execution simulation.

HOW KALSHI ORDER BOOKS WORK:
A Kalshi order book has two sides — bids and asks — for the YES contract.
- Bids: people willing to BUY YES (they think the home team wins)
- Asks: people willing to SELL YES (they think the home team loses)

Prices are in cents (1..99). A YES contract at 60¢ means:
- Buyers pay 60¢ and receive $1.00 if home wins (net +40¢) or $0 (net -60¢)
- Sellers receive 60¢ and pay $1.00 if home wins (net -40¢) or keep 60¢ (net +60¢)

The mid-price = (best_bid + best_ask) / 2 and is our estimate of the market's
current implied probability for the home team winning.

HOW WE SIMULATE FILLS (VWAP):
Rather than assuming we can buy at the best ask price, we "walk the book":
we take whatever is available at each price level until we have our full quantity.
This gives a Volume-Weighted Average Price (VWAP) — a realistic estimate of
what we'd actually pay if we executed the trade right now.
"""

from __future__ import annotations
from dataclasses import dataclass                  
from typing import Sequence                


@dataclass(frozen=True)
class Level:
    """One price level in an order book.

    frozen=True: immutable after creation. Order book levels shouldn't be modified —
    if prices change, a new Level is created.

    Example: Level(price_cents=52, size=100) means there are 100 contracts
    available to buy/sell at 52¢.
    """
    price_cents: int   
    size: int          


@dataclass(frozen=True)
class OrderBook:
    """A complete order book for one Kalshi YES contract market.

    Contains the top-5 price levels on each side, sorted correctly:
    - bids sorted descending (best bid first, e.g. 51, 50, 49, 48, 47)
    - asks sorted ascending (best ask first, e.g. 53, 54, 55, 56, 57)

    The spread is ask[0] - bid[0], e.g. 53 - 51 = 2¢

    frozen=True: the book is a snapshot in time — immutable once fetched.
    """
    market_ticker: str              
    bids: tuple[Level, ...]         
    asks: tuple[Level, ...]         
    ts: float = 0.0                 

    def best_bid(self) -> Level | None:
        """The highest price someone is willing to pay for YES.
        Returns None if the bid side is empty (no buyers)."""
        return self.bids[0] if self.bids else None

    def best_ask(self) -> Level | None:
        """The lowest price someone is willing to sell YES.
        Returns None if the ask side is empty (no sellers)."""
        return self.asks[0] if self.asks else None

    def mid_cents(self) -> float | None:
        """The midpoint between best bid and best ask, in cents.

        This is our best estimate of the market's current implied probability
        for the home team winning. e.g. bid=51, ask=53 → mid=52¢ → 52% implied prob.

        Returns None if either side is empty (can't compute mid without both sides).
        """
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None:
            return None
        return (b.price_cents + a.price_cents) / 2.0   # Simple average of best bid and ask

    def spread_cents(self) -> float | None:
        """The bid-ask spread in cents.

        Spread = best_ask - best_bid. This is the cost of immediately entering
        and exiting a position. A 3¢ spread means we pay 3¢ in slippage per contract
        on a round trip. Wide spreads eat into edge, which is why we filter on this.

        Returns None if either side is empty.
        """
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None:
            return None
        return float(a.price_cents - b.price_cents)   # e.g. 53 - 51 = 2¢


@dataclass(frozen=True)
class BookMetrics:
    """Pre-computed summary statistics for one order book snapshot.

    These are the numbers we store in the database for every snapshot and
    use in the strategy filter stack. Computing them all at once (rather than
    on demand) is more efficient since multiple filters use the same values.
    """
    top_bid: float | None          
    top_ask: float | None          
    mid: float | None              
    spread_cents: float | None     
    depth_3_bid: int               
    depth_3_ask: int               
    imbalance: float | None        
                                   
    total_quoted_size: int         


def compute_metrics(book: OrderBook) -> BookMetrics:
    """Compute all the summary metrics for one order book snapshot.

    This is called on every Kalshi snapshot before running the strategy filters.
    All the resulting values are stored in the database and displayed on the dashboard.
    """
    # Get the best bid and ask levels (may be None if a side is empty)
    bid = book.best_bid()
    ask = book.best_ask()
    d3_bid = sum(level.size for level in book.bids[:3])
    d3_ask = sum(level.size for level in book.asks[:3])

    # Total size across both sides — used to normalize the imbalance calculation
    total = d3_bid + d3_ask

    imbalance = (d3_bid - d3_ask) / total if total > 0 else None

    return BookMetrics(
        top_bid=float(bid.price_cents) if bid else None,     
        top_ask=float(ask.price_cents) if ask else None,     
        mid=book.mid_cents(),                                
        spread_cents=book.spread_cents(),                    
        depth_3_bid=d3_bid,
        depth_3_ask=d3_ask,
        imbalance=imbalance,
        total_quoted_size=total,
    )


@dataclass(frozen=True)
class Fill:
    """The result of simulating a trade execution against the order book.

    Represents what would happen if we tried to buy `target_qty` contracts
    right now by walking the order book levels.
    """
    avg_price_cents: float               
    filled_qty: int          
    levels_walked: int                          


def vwap_fill(levels: Sequence[Level], target_qty: int) -> Fill | None:
    """Simulate walking one side of the order book to fill a target quantity.

    VWAP = Volume-Weighted Average Price. Rather than assuming we buy all contracts
    at the best price, we walk through levels in order until we have what we need.

    Example:
        levels = [Level(52, 5), Level(53, 10), Level(54, 20)]
        target_qty = 12

        Level 52: take 5 (remaining: 7), notional: 5 × 52 = 260
        Level 53: take 7 (remaining: 0), notional: 7 × 53 = 371

        Total filled: 12
        Total notional: 260 + 371 = 631
        VWAP: 631 / 12 = 52.58¢ (worse than best ask of 52¢ due to walking)

    Args:
        levels: bid or ask levels in price order (asks: ascending, bids: descending)
        target_qty: how many contracts we want to buy

    Returns:
        Fill with VWAP price and actual filled quantity.
        Returns None if there are no levels at all (empty book side).
        Returns a partial Fill if the book doesn't have enough depth — filled_qty < target_qty.
    """
    if target_qty <= 0:
        raise ValueError("target_qty must be positive")  # Sanity guard — can't buy 0 contracts

    remaining = target_qty   
    notional = 0.0           
    filled = 0               
    n_levels = 0             

    for level in levels:
        if remaining <= 0:
            break   # We have everything we need — stop walking

        take = min(level.size, remaining)

        # Accumulate notional: price × quantity at this level
        notional += take * level.price_cents

        filled += take           # Add to filled count
        remaining -= take        # Reduce what we still need
        n_levels += 1            # We used one more price level

    if filled == 0:
        return None   # Completely empty book side — can't fill at all

    return Fill(
        avg_price_cents=notional / filled,   
        filled_qty=filled,                   
        levels_walked=n_levels,              
    )


def buy_yes_fill(book: OrderBook, qty: int) -> Fill | None:
    """Simulate buying YES contracts by lifting asks (crossing the spread).

    To buy YES: we accept the sellers' prices. We walk the ASK side of the book
    from lowest price upward, taking what we need at each level.

    Args:
        book: current order book snapshot
        qty: number of YES contracts to buy

    Returns:
        Fill with VWAP price paid, or None if no asks exist.
    """
    # We lift asks (buy from sellers) — asks are sorted ascending (cheapest first)
    return vwap_fill(book.asks, qty)


def buy_no_fill(book: OrderBook, qty: int) -> Fill | None:
    """Simulate buying NO contracts by hitting bids on the YES side.

    HOW NO CONTRACTS WORK ON KALSHI:
    There's only one order book — the YES order book. Buying NO means you're
    agreeing to sell YES to the highest bidder. On Kalshi:
    - If YES bid = 48¢, then NO ask (what you'd pay for NO) = 100 - 48 = 52¢
    - Buying NO at 52¢ and YES at 48¢ are two sides of the same trade

    So to buy NO: we walk the BID side of the YES order book and invert prices.
    Taking a YES bid of 48¢ means we're selling YES for 48¢, which means
    we're buying NO for (100 - 48) = 52¢.

    Args:
        book: current order book snapshot
        qty: number of NO contracts to buy

    Returns:
        Fill with the NO-side price (100 - YES_bid_VWAP), or None if no bids.
    """
    if not book.bids:
        return None   # No bids means no one to sell YES to — can't buy NO

    sell = vwap_fill(book.bids, qty)

    if sell is None:
        return None   # Book was empty — shouldn't happen given the check above

    return Fill(
        avg_price_cents=100.0 - sell.avg_price_cents,   # NO price = 100 - YES bid price
        filled_qty=sell.filled_qty,                     # Same quantity
        levels_walked=sell.levels_walked,               # Same number of levels crossed
    )
