"""P&L summarization helpers.

This module provides a single function (summarize()) that takes a Portfolio
object and current market prices and returns a complete P&L summary dataclass.

Used by scripts that need a one-shot portfolio snapshot — for example, at the
end of a backtest or in a reporting context. The live dashboard computes most
of these numbers inline, but this module provides a clean reusable helper.
"""

from __future__ import annotations

from dataclasses import dataclass
from src.execution import Portfolio


@dataclass
class PnLSummary:
    """A complete point-in-time summary of portfolio performance.

    Contains both realized (closed trades) and unrealized (open positions)
    P&L, plus trade statistics like hit rate, best trade, worst trade.
    """
    realized: float         
    unrealized: float       
    total: float            

    open_count: int         
    closed_count: int       

    wins: int               
    losses: int             
    hit_rate: float | None  
                            

    best_trade: float | None    
    worst_trade: float | None   


def summarize(
    portfolio: Portfolio, mid_cents_by_ticker: dict[str, float]
) -> PnLSummary:
    """Compute a complete P&L summary for the current portfolio state.

    Args:
        portfolio: the Portfolio object with all open and closed positions
        mid_cents_by_ticker: dict of ticker → current mid price in cents.
            Used to mark open positions to market for unrealized P&L.

    Returns:
        PnLSummary dataclass with all performance metrics.
    """
    # Realized P&L: running total maintained by Portfolio as positions close
    realized = portfolio.realized_pnl
    unrealized = portfolio.total_unrealized(mid_cents_by_ticker)
    wins = sum(1 for p in portfolio.closed_positions if (p.realized_pnl or 0) > 0)
    losses = sum(1 for p in portfolio.closed_positions if (p.realized_pnl or 0) < 0)

    closed_count = len(portfolio.closed_positions)
    hit_rate = wins / closed_count if closed_count > 0 else None

    # Collect all non-null realized P&L values for best/worst calculation
    pnls = [p.realized_pnl for p in portfolio.closed_positions if p.realized_pnl is not None]

    # Best trade: most profitable single trade (None if no closed trades)
    best = max(pnls) if pnls else None

    # Worst trade: most losing single trade (negative number, None if no closed trades)
    worst = min(pnls) if pnls else None

    return PnLSummary(
        realized=realized,
        unrealized=unrealized,
        total=realized + unrealized,         
        open_count=len(portfolio.open_positions),
        closed_count=closed_count,
        wins=wins,
        losses=losses,
        hit_rate=hit_rate,
        best_trade=best,
        worst_trade=worst,
    )
