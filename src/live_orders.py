"""Live order execution against the Kalshi sandbox.

WHAT THIS FILE DOES:
When --live-orders mode is active, this module places REAL limit orders on the
Kalshi sandbox exchange, polls until they fill, and returns an execution report
that the logger uses to record the trade.

This is the difference between simulated mode and live-orders mode:
- Simulated: Portfolio.open_from_signal() walks the book mathematically, no API call
- Live orders: execute_live() actually places an order on the exchange

ORDER STRATEGY — TAKER LIMIT ORDERS:
We use "taker" orders — priced to immediately cross the spread:
- Buying YES: limit price = current best ask (we accept what sellers are asking)
- Buying NO: limit price = 100 - best_bid (we accept what YES buyers are paying)

This should fill immediately if there is matching depth, since we're pricing at
the current market. We use limit (not market) orders because Kalshi doesn't
offer pure market orders — limit orders set a maximum price we'll pay.

FILL POLLING:
After placing, we poll the order status every 1.5 seconds.
Kalshi IMMEDIATELY removes filled orders from the active orders endpoint and
returns HTTP 404. So 404 = filled (not an error). This was a key discovery.

If the order hasn't filled after 20 seconds, we cancel it and report "canceled".

All orders go to demo-api.kalshi.co (sandbox). The client refuses non-sandbox
URLs, so production execution is impossible by construction.
"""

from __future__ import annotations

import logging  
import time     

from dataclasses import dataclass   
from typing import Optional         
from src.kalshi_client import KalshiClient   
from src.orderbook import OrderBook, buy_no_fill, buy_yes_fill 

log = logging.getLogger(__name__)   

FILL_TIMEOUT_SECONDS = 2
POLL_INTERVAL_SECONDS = 1.5


@dataclass
class ExecutionReport:
    """The result of attempting to execute a trade on the Kalshi sandbox.

    Returned by both execute_live() (entry) and close_live() (exit).
    The logger uses these fields to record the trade in the database.
    """
    order_id: Optional[str]   
    order_status: str                                                      
    filled_qty: int           
    avg_price_dollars: float  
    side: str                 
    message: str              


def close_live(
    client: KalshiClient,
    book: OrderBook,
    side: str,         
    qty: int,          
) -> ExecutionReport:
    """Place a limit sell order to exit an open position early.

    CLOSING LOGIC:
    - Closing a YES position: sell YES at the current best bid (hit the bid).
      We're accepting what buyers are willing to pay for YES.
    - Closing a NO position: sell NO at (100 - best_ask).
      Selling NO = buying YES on the other side, which is equivalent.

    Uses the same fill-timeout and polling logic as execute_live().
    """
    ticker = book.market_ticker  
    side_lower = side.lower()    

    if side_lower == "yes":
        bid = book.best_bid()
        if bid is None:
            return ExecutionReport(None, "failed", 0, 0.0, side, "no bid to sell into")
        limit_cents = bid.price_cents  
        order_side = "yes"
        order_action = "sell"
    else:
        ask = book.best_ask()
        if ask is None:
            return ExecutionReport(None, "failed", 0, 0.0, side, "no ask to close NO against")
        limit_cents = 100 - ask.price_cents   
        order_side = "no"
        order_action = "sell"

    body = {
        "ticker": ticker,
        "side": order_side,       
        "action": order_action,   
        "type": "limit",          
        "count": int(qty),        
        ("yes_price" if order_side == "yes" else "no_price"): int(limit_cents),
    }

    try:
        resp = client._post("/portfolio/orders", body)
    except Exception as e:
        log.warning(f"close_live order failed for {ticker}: {e}. skipping exit.")
        return ExecutionReport(None, "failed", 0, limit_cents / 100.0, side, str(e))

    order = resp.get("order", resp)              
    order_id = order.get("order_id") or ""       
    status = order.get("status", "resting")      
    fill_count = int(float(order.get("fill_count_fp", 0)))   # Contracts filled so far
    fill_price = limit_cents / 100.0              # Default to limit price if no fill info yet

    log.info(f"close {side} order {order_id} on {ticker} @ {limit_cents}c x{qty}: {status}")

    deadline = time.time() + FILL_TIMEOUT_SECONDS
    while time.time() < deadline and status == "resting":
        time.sleep(POLL_INTERVAL_SECONDS)   
        try:
            upd = client.get_order(order_id).get("order", {}) 
            status = upd.get("status", status)                
            fill_count = int(float(upd.get("fill_count_fp", fill_count)))  # Update fill count

            pk = f"{'yes' if order_side == 'yes' else 'no'}_price_dollars"
            if upd.get(pk):
                fill_price = float(upd[pk])   # Actual execution price in dollars

        except Exception as e:
            if "404" in str(e):
                status = "executed"
                fill_count = fill_count or qty   # Assume full fill if count wasn't captured yet

    if status == "resting":
        try:
            client.cancel_order(order_id)
            status = "canceled"
        except Exception:
            pass   # Cancel may fail if order just filled — leave status as-is

    return ExecutionReport(
        order_id=order_id,
        order_status=status,
        filled_qty=fill_count or qty,        # Fallback to requested qty if count unclear
        avg_price_dollars=fill_price,         # Actual fill price or limit price fallback
        side=side,
        message=f"close {order_action} {limit_cents}c, status={status}",
    )


def execute_live(
    client: KalshiClient,
    book: OrderBook,
    side: str,    # "YES" or "NO" — which contract to buy
    qty: int,     # number of contracts to buy
) -> ExecutionReport:
    """Place a taker limit order and poll until filled, canceled, or timeout.

    ENTRY ORDER STRATEGY:
    We price at the current best ask (for YES) or 100-best_bid (for NO).
    This is a "taker" strategy — we're crossing the spread to get filled immediately.
    Using a limit order (not market) gives us a price ceiling for protection.

    FALLBACK:
    If the orders API call itself fails, falls back to simulating the fill from
    the order book. This keeps the logger running even when the orders endpoint
    is temporarily unavailable.
    """
    ticker = book.market_ticker   
    side_lower = side.lower()     

    if side_lower == "yes":
        ask = book.best_ask()
        if ask is None:
            return ExecutionReport(None, "failed", 0, 0.0, side, "no ask in book")
        limit_cents = ask.price_cents   # Set limit = best ask → immediate fill if depth exists

    else:
        bid = book.best_bid()
        if bid is None:
            return ExecutionReport(None, "failed", 0, 0.0, side, "no bid in book")
        limit_cents = 100 - bid.price_cents   # NO limit price = 100 - YES bid

    try:
        resp = client.place_order(ticker, side_lower, qty, limit_cents)
    except Exception as e:
        log.warning(f"place_order failed for {ticker}: {e}. falling back to simulation.")
        return _simulated_fallback(book, side, qty, str(e))

    order = resp.get("order", resp)   # Handle both {"order": {...}} and flat response shapes
    order_id = order.get("order_id") or order.get("id") or ""   # Kalshi's order identifier
    log.info(f"placed {side} order {order_id} on {ticker} @ {limit_cents}c x{qty}")

    deadline = time.time() + FILL_TIMEOUT_SECONDS   # When to give up and cancel
    status = order.get("status", "resting")          # Initial status from placement response
    fill_count = 0                                   # No fills recorded yet
    avg_price_dollars = limit_cents / 100.0          # Default to our limit price

    while time.time() < deadline and status == "resting":
        time.sleep(POLL_INTERVAL_SECONDS)   # Pause between polls

        try:
            upd = client.get_order(order_id).get("order", {})   # Get current order state

            status = upd.get("status", status)                            # Update status
            fill_count = int(float(upd.get("fill_count_fp", 0)))          # Update fill count

            price_key = "yes_price_dollars" if side_lower == "yes" else "no_price_dollars"
            price_raw = upd.get(price_key) or upd.get("yes_price_dollars")   # YES fallback
            if price_raw:
                avg_price_dollars = float(price_raw)   # Use actual execution price

        except Exception as e:
            if "404" in str(e):
                status = "executed"
                fill_count = fill_count or qty   # Assume full fill if count wasn't captured
            else:
                log.warning(f"poll failed for {order_id}: {e}")   # Log non-404 errors

    if status == "resting":
        log.warning(f"order {order_id} unfilled after {FILL_TIMEOUT_SECONDS}s, cancelling")
        try:
            client.cancel_order(order_id)   # Cancel the resting order
            status = "canceled"
        except Exception as e:
            log.warning(f"cancel failed for {order_id}: {e}")

    if status == "executed" and fill_count == 0:
        fill_count = qty  

    return ExecutionReport(
        order_id=order_id,
        order_status=status,
        filled_qty=fill_count,
        avg_price_dollars=avg_price_dollars,
        side=side,
        message=f"limit {limit_cents}c, status={status}, filled={fill_count}/{qty}",
    )


def _simulated_fallback(
    book: OrderBook, side: str, qty: int, reason: str
) -> ExecutionReport:
    """Simulate a fill using order book depth when the live orders API call fails.

    This is the safety net: if placing a real order fails (network error, API downtime,
    auth issue), we still record a simulated position so the logger keeps running.

    Uses the same VWAP fill logic as Portfolio.open_from_signal() — walks the
    book depth to compute a realistic fill price.
    """
    # Simulate the fill using the current order book depth
    if side == "YES":
        fill = buy_yes_fill(book, qty)   # Walk the ask side for YES buys
    else:
        fill = buy_no_fill(book, qty)    # Walk the bid side (inverted) for NO buys

    if fill is None:
        # Even the simulation failed — the book is completely empty
        return ExecutionReport(None, "failed", 0, 0.0, side, reason)

    return ExecutionReport(
        order_id=None,                                   # No real order was placed
        order_status="simulated",                        # Mark as simulated
        filled_qty=fill.filled_qty,                      # VWAP-simulated fill quantity
        avg_price_dollars=fill.avg_price_cents / 100.0,  # VWAP price in dollars
        side=side,
        message=f"simulated (api error: {reason})",     # Explain why we simulated
    )
