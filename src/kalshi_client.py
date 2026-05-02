"""Kalshi sandbox REST API client.

WHAT THIS FILE DOES:
Provides an authenticated HTTP client for the Kalshi sandbox API.
All Kalshi data fetching and order placement goes through this class.

KEY DESIGN DECISIONS:

1. SANDBOX GUARD:
   The constructor checks that the base URL contains "demo-api.kalshi.co"
   and REFUSES to instantiate if it doesn't. This is a hard code assertion —
   not a flag, not a config check. You cannot accidentally point this client
   at production, even if you try.

2. RSA-PSS AUTHENTICATION:
   Every API request must be cryptographically signed. Kalshi uses RSA-PSS
   (Probabilistic Signature Scheme) with SHA-256. The signature covers:
       timestamp_ms + HTTP_METHOD + full_path
   This prevents replay attacks (the timestamp is part of the message) and
   proves the request came from the key holder.

3. ORDER BOOK PARSING:
   Kalshi's API returns the order book as YES bids and NO bids (not YES asks).
   To get YES asks: invert the NO bids (NO bid at 40¢ = YES ask at 60¢).
   The parse_orderbook() method handles this inversion.

4. SETTLEMENTS VS FILLS:
   When a market resolves, Kalshi does NOT create sell fills — it settles
   through /portfolio/settlements. The dashboard's closed trades table needs
   to read from settlements (not just fills) to show positions that resolved.
"""

from __future__ import annotations

import base64   
import json     
import time     
from datetime import datetime, timezone  
from pathlib import Path                 
from typing import Optional              

import httpx     
from cryptography.hazmat.primitives import hashes, serialization      
from cryptography.hazmat.primitives.asymmetric import padding         

from src import config           # For KALSHI_BASE_URL, API key settings
from src.orderbook import Level, OrderBook   # Internal order book data structures


def _load_private_key():
    """Load the RSA private key from the environment variable.

    The private key must be provided as an inline PEM string in the KALSHI-PRIVATE-KEY
    environment variable. This is more secure than a file path for deployed environments
    and works consistently across dev and server setups.

    Returns the loaded RSA private key object from the cryptography library.
    Raises RuntimeError if no key is configured.
    """
    inline = config.KALSHI_API_PRIVATE_KEY_INLINE  

    if inline:
        return serialization.load_pem_private_key(
            inline.encode(),  
            password=None
        )

    raise RuntimeError(
        "No Kalshi private key configured. Set KALSHI-PRIVATE-KEY in .env."
    )


def _sign(private_key, msg: bytes) -> str:
    """Sign a message with the RSA private key using PSS padding.

    WHAT IS RSA-PSS?
    PSS (Probabilistic Signature Scheme) is the modern standard for RSA signatures.
    Unlike older PKCS#1 v1.5, PSS is provably secure and includes randomness
    (salt) in the signature generation, so the same message signed twice gives
    different signatures.

    The signature process:
    1. Hash the message with SHA-256
    2. Apply PSS padding (which adds salt and masks)
    3. Sign with the RSA private key
    4. Base64-encode the signature for inclusion in the HTTP header

    Args:
        private_key: RSA private key loaded by _load_private_key()
        msg: the raw bytes to sign (timestamp + method + path)

    Returns:
        Base64-encoded signature string for the KALSHI-ACCESS-SIGNATURE header
    """
    sig = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),      
            salt_length=padding.PSS.DIGEST_LENGTH,  
        ),
        hashes.SHA256(),   
    )
    return base64.b64encode(sig).decode()


class KalshiClient:
    """Authenticated REST client for the Kalshi sandbox API.

    All API communication goes through this class. Each method maps to one
    Kalshi API endpoint and handles authentication automatically.

    IMPORTANT: This client ONLY works with the Kalshi sandbox (demo-api.kalshi.co).
    The constructor refuses any other URL, making production access impossible.
    """

    def __init__(self, base_url: Optional[str] = None):
        """Initialize the client.

        Args:
            base_url: override the default base URL (used in tests). If None,
                      uses KALSHI_BASE_URL from config (sandbox only).

        Raises:
            RuntimeError if the URL isn't the Kalshi sandbox.
        """
        self.base_url = base_url or config.KALSHI_BASE_URL

       
       
        if "demo-api.kalshi.co" not in self.base_url:
            raise RuntimeError(
                f"Refusing non-sandbox URL: {self.base_url}. Sandbox only."
            )

        self._key_id = config.KALSHI_API_KEY_ID        
        self._private_key = _load_private_key()        
        self._client = httpx.Client(timeout=15.0)      

    # =========================================================================
    # AUTHENTICATION
    # =========================================================================

    def _headers(self, method: str, path: str) -> dict[str, str]:
        """Build the authentication headers required for every Kalshi API request.

        KALSHI'S AUTH SCHEME:
        1. Take the current timestamp in milliseconds
        2. Build the message: timestamp_ms + HTTP_METHOD + full_path
        3. Sign the message with the RSA private key (PSS padding)
        4. Include timestamp, key ID, and signature in request headers

        The timestamp prevents replay attacks — Kalshi rejects requests with
        timestamps too far from the server's current time.

        The full_path (not just the path suffix) includes the API version prefix
        so the signature covers exactly what the server sees.
        """
        from urllib.parse import urlparse

        base_server_path = urlparse(self.base_url).path
        signing_path = base_server_path + path
        ts_ms = str(int(time.time() * 1000))
        msg = (ts_ms + method.upper() + signing_path).encode()
        sig = _sign(self._private_key, msg)

        return {
            "KALSHI-ACCESS-KEY": self._key_id or "",   
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,          
            "KALSHI-ACCESS-SIGNATURE": sig,            
            "Accept": "application/json",              
        }

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """Make an authenticated GET request and return the parsed JSON response.

        Args:
            path: API endpoint path (e.g. "/markets")
            params: optional query string parameters (e.g. {"status": "open"})

        Returns:
            Parsed JSON response as a Python dict.

        Raises:
            httpx.HTTPStatusError if the server returns a non-2xx status code.
        """
        url = self.base_url + path                      
        headers = self._headers("GET", path)            
        resp = self._client.get(url, params=params, headers=headers)
        resp.raise_for_status()                           
        return resp.json()                                

    # =========================================================================
    # MARKET DATA ENDPOINTS
    # =========================================================================

    def list_markets(
        self,
        series_ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        status: str = "open",
        limit: int = 200,
    ) -> dict:
        """List Kalshi markets matching the given filters.

        Used by the logger to find all open NBA/MLB/etc markets each poll cycle.
        The response is filtered by KALSHI_SPORTS_PREFIXES in logger.py.

        Args:
            status: "open" for currently tradeable markets
            limit: max markets to return (200 is Kalshi's max per page)

        Returns:
            Dict with "markets" list of market objects.
        """
        params: dict = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        return self._get("/markets", params=params)

    def get_orderbook(self, ticker: str, depth: int = 5) -> dict:
        """Fetch the top-N order book for one market.

        Args:
            ticker: Kalshi market ticker
            depth: how many levels on each side to return (5 is sufficient for VWAP simulation)

        Returns:
            Raw Kalshi order book response (needs parse_orderbook() to convert to OrderBook).
        """
        return self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})

    def get_market(self, ticker: str) -> dict:
        """Fetch metadata for one market (title, close time, status, etc.)."""
        return self._get(f"/markets/{ticker}")

    # =========================================================================
    # PORTFOLIO ENDPOINTS (require authentication)
    # =========================================================================

    def get_balance(self) -> dict:
        """Fetch the current account balance and portfolio value.

        Returns:
            Dict with:
            - "balance": available cash in cents (e.g. 100000 = $1,000)
            - "portfolio_value": total portfolio value in cents (cash + position value)

        Used by the logger to initialize the starting balance and by the dashboard
        to display Cash Available.
        """
        return self._get("/portfolio/balance")

    def get_balance_dollars(self) -> float:
        """Convenience wrapper: return available cash in dollars (not cents)."""
        resp = self.get_balance()
        cents = resp.get("balance", 0)
        return cents / 100.0   # Convert cents to dollars

    def get_positions(self) -> dict:
        """Fetch all currently open market positions.

        Returns:
            Dict with "market_positions" list — one entry per open position.
            Each entry includes realized_pnl_dollars, market_exposure_dollars, etc.

        Used to compute real P&L from Kalshi's perspective (ground truth)
        rather than relying on local portfolio math.
        """
        return self._get("/portfolio/positions")

    def get_fills(self, limit: int = 100, cursor: str | None = None) -> dict:
        """Fetch one page of fill records (executed order legs).

        Fills are created when orders execute. For ENTRY orders, this works well.
        For positions that SETTLE at resolution, there are NO fills — those appear
        in get_settlements() instead. This was a key discovery.

        Args:
            limit: number of fills to return per page
            cursor: pagination cursor from previous response
        """
        params: dict = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._get("/portfolio/fills", params=params)

    def get_all_fills(self, max_pages: int = 20) -> list[dict]:
        """Paginate through all fill records and return every fill.

        Handles Kalshi's cursor-based pagination automatically.

        Args:
            max_pages: safety limit to prevent infinite loops

        Returns:
            List of all fill dicts across all pages.
        """
        all_fills: list[dict] = []
        cursor: str | None = None

        for _ in range(max_pages):
            resp = self.get_fills(limit=100, cursor=cursor)
            batch = resp.get("fills", [])
            all_fills.extend(batch)                # Add this page's fills to the running list

            cursor = resp.get("cursor", "")
            if not cursor or not batch:
                break   # No more pages — stop paginating

        return all_fills

    # =========================================================================
    # ORDER PLACEMENT (sandbox only)
    # =========================================================================

    def place_order(
        self,
        ticker: str,
        side: str,              # "yes" or "no"
        count: int,             # number of contracts
        limit_price_cents: int, # limit price in cents (1..99)
    ) -> dict:
        """Place a limit order on the Kalshi sandbox.

        TAKER ORDER STRATEGY:
        We set the limit price to the current best ask (for YES) or equivalent (for NO).
        This prices the order to cross the spread immediately — we're takers, not makers.

        The API requires the price in the "yes_price" or "no_price" field depending
        on which side we're trading.

        Args:
            ticker: the Kalshi market ticker
            side: "yes" to buy YES contracts, "no" to buy NO contracts
            count: number of contracts to buy
            limit_price_cents: max price willing to pay in cents

        Returns:
            Full order response dict from Kalshi (includes order_id, status, etc.)
        """
        side = side.lower()
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got {side!r}")

        # The price field name depends on which side we're trading
        price_key = "yes_price" if side == "yes" else "no_price"

        body = {
            "ticker": ticker,
            "side": side,         
            "action": "buy",      
            "type": "limit",      
            "count": int(count),  
            price_key: int(limit_price_cents),   # Price in integer cents (API requirement)
        }
        return self._post("/portfolio/orders", body)

    def get_order(self, order_id: str) -> dict:
        """Fetch the current status of one order.

        Used during the fill polling loop in live_orders.py.
        Returns HTTP 404 (raises an exception) if the order has been filled and
        removed from the active orders endpoint — which we interpret as "executed".
        """
        return self._get(f"/portfolio/orders/{order_id}")

    def get_settlements(self, limit: int = 200) -> list[dict]:
        """Fetch all settlement records — positions that resolved at market expiry.

        IMPORTANT: When a Kalshi market resolves (game ends), it does NOT create
        fill records. The settlement shows up ONLY here in /portfolio/settlements.
        This is why the dashboard reads both fills AND settlements for the closed
        trades table — fills for manual exits, settlements for resolved positions.

        Paginates automatically using cursor-based pagination.

        Returns:
            List of all settlement dicts across all pages.
        """
        results = []
        cursor = ""

        while True:
            params: dict = {"limit": min(limit, 200)}   # Kalshi max per page is 200
            if cursor:
                params["cursor"] = cursor

            resp = self._get("/portfolio/settlements", params=params)
            batch = resp.get("settlements", [])
            results.extend(batch)

            cursor = resp.get("cursor", "")
            if not cursor or len(batch) < params["limit"]:
                break

        return results

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a resting (unfilled) order.

        Called when an order hasn't filled within FILL_TIMEOUT_SECONDS.
        Raises an exception if the order doesn't exist or is already filled.
        """
        return self._delete(f"/portfolio/orders/{order_id}")

    # =========================================================================
    # HTTP HELPERS
    # =========================================================================

    def _post(self, path: str, body: dict) -> dict:
        """Make an authenticated POST request with a JSON body."""
        url = self.base_url + path
        headers = self._headers("POST", path)
        headers["Content-Type"] = "application/json"   # Tell server we're sending JSON
        resp = self._client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> dict:
        """Make an authenticated DELETE request (used for order cancellation)."""
        url = self.base_url + path
        headers = self._headers("DELETE", path)
        resp = self._client.delete(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

    # =========================================================================
    # HISTORICAL DATA (optional feature)
    # =========================================================================

    def get_candlesticks(
        self, ticker: str, start_ts: int, end_ts: int, period_interval: int = 1
    ) -> dict:
        """Fetch historical OHLC candlestick data for a market.

        Used by kalshi_history.py to optionally supplement the backtest with
        real Kalshi price history when it's available.

        Args:
            ticker: Kalshi market ticker
            start_ts: start of period as Unix timestamp (seconds)
            end_ts: end of period as Unix timestamp (seconds)
            period_interval: candle size in minutes (1, 60, or 1440)
        """
        return self._get(
            f"/historical/markets/{ticker}/candlesticks",
            params={
                "start_ts": int(start_ts),
                "end_ts": int(end_ts),
                "period_interval": int(period_interval),
            },
        )

    @staticmethod
    def candlesticks_to_snapshots(
        payload: dict, ticker: str, sport: str
    ) -> list[dict]:
        """Convert a candlestick response into our internal KalshiSnapshot row shape.

        Candlesticks use fractional dollar prices (e.g. 0.52) while our snapshots
        use cents (e.g. 52). This method handles the conversion.
        """
        from datetime import datetime as _dt
        rows: list[dict] = []

        for c in payload.get("candlesticks", []):
            # Extract closing prices for yes_bid and yes_ask from the candle
            yb = (c.get("yes_bid") or {}).get("close")
            ya = (c.get("yes_ask") or {}).get("close")
            if yb is None or ya is None:
                continue   # Skip candles with missing price data

            try:
                # Convert from fractional dollars (0..1) to integer cents (0..100)
                bid_cents = int(round(float(yb) * 100))
                ask_cents = int(round(float(ya) * 100))
            except (TypeError, ValueError):
                continue   # Skip malformed price data

            # Convert Unix timestamp to datetime
            ts = _dt.utcfromtimestamp(int(c["end_period_ts"]))
            mid = (bid_cents + ask_cents) / 2.0

            rows.append(
                dict(
                    ts=ts,
                    market_ticker=ticker,
                    sport=sport,
                    top_bid=float(bid_cents),
                    top_ask=float(ask_cents),
                    mid=mid,
                    spread_cents=float(ask_cents - bid_cents),
                    depth_3_bid=None,     # Candlesticks don't have depth data
                    depth_3_ask=None,
                    imbalance=None,
                    total_quoted_size=None,
                )
            )
        return rows

    # =========================================================================
    # ORDER BOOK PARSING
    # =========================================================================

    @staticmethod
    def parse_orderbook(payload: dict, ticker: str) -> OrderBook:
        """Convert Kalshi's raw API response into our internal OrderBook format.

        KALSHI'S FORMAT:
        The API returns two lists:
        - "yes" (or "yes_dollars"): YES bids [price, qty] pairs — prices in dollars (0..1)
        - "no" (or "no_dollars"): NO bids [price, qty] pairs — prices in dollars (0..1)

        OUR CONVERSION:
        We only track the YES side internally. So:
        - YES bids → our bids (convert dollars to cents directly)
        - NO bids → our asks (invert: NO bid at $0.40 = YES ask at $0.60)

        WHY INVERT NO BIDS FOR YES ASKS?
        A NO bid at 40¢ means: "I'll pay 40¢ for NO, which means I'll sell YES for 60¢"
        So (100 - NO_bid_price) gives the YES ask price.

        Args:
            payload: raw JSON response from get_orderbook()
            ticker: the market ticker (needed to construct the OrderBook)

        Returns:
            OrderBook with bids sorted descending, asks sorted ascending.
        """
        # Handle both old and new Kalshi response shapes
        ob = payload.get("orderbook_fp") or payload.get("orderbook") or {}

        # Get the raw price level data — try both naming conventions
        yes_bids_raw = ob.get("yes_dollars") or ob.get("yes") or []
        no_bids_raw = ob.get("no_dollars") or ob.get("no") or []

        def _to_cents_levels(raw, invert: bool) -> list[Level]:
            """Convert raw [price, qty] pairs to Level objects in cents.

            Args:
                raw: list of [price_in_dollars, quantity] pairs from Kalshi
                invert: True for NO bids (convert to YES ask prices by doing 1 - price)
            """
            levels: list[Level] = []
            for entry in raw:
                if not entry:
                    continue
                price = float(entry[0])   # Price in dollars (e.g. 0.52)
                qty = int(float(entry[1]))

                # Convert to cents, inverting if this is the NO side
                cents = int(round((1 - price) * 100)) if invert else int(round(price * 100))

                # Filter out invalid prices and zero-quantity levels
                if 1 <= cents <= 99 and qty > 0:
                    levels.append(Level(price_cents=cents, size=qty))
            return levels

        # YES bids: straight conversion (no inversion)
        yes_bids = _to_cents_levels(yes_bids_raw, invert=False)

        # YES asks: derived by inverting NO bids (NO bid price → YES ask price)
        yes_asks = _to_cents_levels(no_bids_raw, invert=True)

        # Sort bids descending (best bid first = highest price first)
        yes_bids.sort(key=lambda l: l.price_cents, reverse=True)

        # Sort asks ascending (best ask first = lowest price first)
        yes_asks.sort(key=lambda l: l.price_cents)

        return OrderBook(
            market_ticker=ticker,
            bids=tuple(yes_bids[:5]),   # Top 5 bid levels
            asks=tuple(yes_asks[:5]),   # Top 5 ask levels
            ts=time.time(),             # Snapshot timestamp (current time)
        )

    def fetch_orderbook(self, ticker: str) -> OrderBook:
        """Convenience method: GET the order book and parse it in one call.

        Used by the logger's main loop to get a ready-to-use OrderBook
        without manually calling parse_orderbook().
        """
        payload = self.get_orderbook(ticker, depth=5)   # Fetch top 5 levels
        return self.parse_orderbook(payload, ticker)     # Parse and return

    def close(self) -> None:
        """Close the underlying HTTP client connection.

        Called at logger shutdown to cleanly release the connection pool.
        """
        self._client.close()
