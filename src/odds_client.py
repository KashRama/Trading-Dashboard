"""The Odds API client.

WHAT THIS FILE DOES:
Fetches live sportsbook moneyline odds from the-odds-api.com, de-vigs them
using the math in src/devig.py, and returns ExternalOddsSnapshot objects that
strategy.evaluate() can compare against Kalshi prices.

THE ODDS API:
- Free tier: 500 requests/month. Each sport = 1 request per fetch.
- At 5 sports polling every 60 seconds, that's 5 requests/min = 300/hr.
- We recommend not running live for more than 20 minutes at a time.
- Every call is logged to the api_call_log table so the dashboard can show
  the monthly usage counter.

DATA FLOW:
    fetch_h2h(sport) → list[RawGame]
        ↓
    to_consensus_snapshot(game) → ExternalOddsSnapshot
        ↓
    strategy.evaluate(kalshi_snap, odds_snap, now) → Signal
"""

from __future__ import annotations

from dataclasses import dataclass   
from datetime import datetime       
from typing import Optional    
import httpx          
from src import config             
from src.devig import BookQuote, consensus_from_books   
from src.strategy import ExternalOddsSnapshot           
from src.storage import ApiCallLog, session_scope       


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 timestamp string to a datetime object.

    The Odds API returns timestamps ending in 'Z' (e.g. "2026-05-03T19:10:00Z").
    Python's fromisoformat() handles '+00:00' offsets in 3.11+ but not 'Z'.
    We replace 'Z' with '+00:00' for compatibility, then strip timezone info
    to get a naive UTC datetime (consistent with the rest of the codebase).
    """
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"   
    return datetime.fromisoformat(s).replace(tzinfo=None)  


@dataclass
class RawGame:
    """Raw game data as returned by the Odds API, before de-vigging.

    This is an intermediate object: we parse the raw API JSON into RawGame,
    then pass it to to_consensus_snapshot() to get the de-vigged ExternalOddsSnapshot.
    """
    game_id: str            
    sport_key: str          
    commence_time: datetime 
    home_team: str          
    away_team: str          
    bookmakers: list[dict]  
    last_seen: datetime     


class OddsAPIClient:
    """HTTP client for the-odds-api.com.

    Fetches H2H (head-to-head = moneyline) odds for sports markets,
    de-vigs them, and returns consensus probability snapshots.

    Free tier limit: 500 requests/month. Every call is logged.
    """

    def __init__(self, api_key: Optional[str] = None):
        """Initialize the client.

        Args:
            api_key: override the API key from config (used in tests).
                     If None, reads from config.ODDS_API_KEY.

        Raises:
            RuntimeError if no API key is configured.
        """
        self.api_key = api_key or config.ODDS_API_KEY

        if not self.api_key:
            raise RuntimeError("ODDS_API_KEY missing in environment.")

        self._client = httpx.Client(timeout=15.0)   # 15-second timeout per request
        self.calls_this_session = 0                  # Counter for this logger session

    def fetch_h2h(
        self, sport: str, region: str = "us", odds_format: str = "american"
    ) -> list[RawGame]:
        """Fetch current H2H (moneyline) odds for all games in one sport.

        One call = one sport = uses 1 API quota credit.

        Args:
            sport: Odds API sport key, e.g. "basketball_nba"
            region: which sportsbooks to include ("us" = DraftKings, FanDuel, etc.)
            odds_format: "american" (e.g. -150/+130) or "decimal" (e.g. 1.67/2.30)
                         We use American because that's what our de-vig math expects.

        Returns:
            List of RawGame objects — one per upcoming game in this sport.
        """
        # Build the API URL and parameters
        url = f"{config.ODDS_API_BASE_URL}/sports/{sport}/odds"
        params = {
            "apiKey": self.api_key,      # Authentication
            "regions": region,            # "us" for US sportsbooks
            "markets": "h2h",            # Head-to-head = moneyline (not spreads or totals)
            "oddsFormat": odds_format,    # "american" for +/- format
        }

        resp = self._client.get(url, params=params)
        self.calls_this_session += 1 
        
        with session_scope() as s:
            s.add(
                ApiCallLog(
                    ts=datetime.utcnow(),
                    api="odds",                        # Which external API
                    endpoint=f"/sports/{sport}/odds",  # Which endpoint
                    sport=sport,                       # Which sport
                    cost=1,                            # Each call costs 1 quota unit
                )
            )

        resp.raise_for_status()   
        payload = resp.json()     

        now = datetime.utcnow()   
        games: list[RawGame] = []

        # Parse each game in the response into a RawGame object
        for g in payload:
            try:
                games.append(
                    RawGame(
                        game_id=g["id"],                                    # Odds API game UUID
                        sport_key=g.get("sport_key", sport),               # Usually matches input sport
                        commence_time=_parse_iso(g["commence_time"]),       # Game start time (UTC)
                        home_team=g["home_team"],                           # Full team name
                        away_team=g["away_team"],
                        bookmakers=g.get("bookmakers", []),                 # List of bookmaker odds
                        last_seen=now,                                      # When we fetched this
                    )
                )
            except (KeyError, ValueError):
                continue   # Skip malformed game entries — don't crash on bad data

        return games

    @staticmethod
    def to_consensus_snapshot(
        game: RawGame, allowed_books: Optional[list[str]] = None
    ) -> Optional[ExternalOddsSnapshot]:
        """De-vig each book's odds and compute a consensus probability snapshot.

        PROCESS:
        1. Filter to only the allowed sportsbooks (e.g. DraftKings, FanDuel, BetMGM, etc.)
        2. For each book, extract home and away American odds
        3. Convert to implied probabilities and de-vig (in src/devig.py)
        4. Take the median across all books as the consensus
        5. Return an ExternalOddsSnapshot ready for strategy.evaluate()

        Args:
            game: RawGame with raw bookmaker odds
            allowed_books: list of sportsbook keys to include (e.g. ["draftkings", "fanduel"])
                           None = include all returned books

        Returns:
            ExternalOddsSnapshot with consensus probabilities, or None if no valid quotes.
        """
        quotes: list[BookQuote] = []   # Will hold one BookQuote per valid sportsbook

        for bm in game.bookmakers:
            # Filter to only allowed sportsbooks if specified
            if allowed_books and bm.get("key") not in allowed_books:
                continue   # Skip this book — not in our allowed list

            for market in bm.get("markets", []):
                if market.get("key") != "h2h":
                    continue   # Skip non-H2H markets (spreads, totals, etc.)

                # Build a dict of {team_name: american_odds} for this market
                outcomes = {o["name"]: o["price"] for o in market.get("outcomes", [])}

                # Get home and away odds by team name
                home_odds = outcomes.get(game.home_team)
                away_odds = outcomes.get(game.away_team)

                if home_odds is None or away_odds is None:
                    continue   # Skip if either side's odds are missing

                try:
                    quotes.append(
                        BookQuote(bm["key"], int(home_odds), int(away_odds))
                    )
                except (TypeError, ValueError):
                    continue   # Skip if odds are malformed (non-numeric, etc.)

        # If no valid quotes were found, return None (strategy will skip this game)
        if len(quotes) < 1:
            return None

        # De-vig all book quotes and compute the median consensus probability
        c = consensus_from_books(quotes)   # See src/devig.py

        # Return an ExternalOddsSnapshot ready for strategy.evaluate()
        return ExternalOddsSnapshot(
            game_id=game.game_id,                  
            consensus_home_prob=c.home_prob,       
            consensus_away_prob=c.away_prob,       
            fair_home_per_book=c.per_book_home,    
            n_books=c.n_books,                     
            snapshot_ts=game.last_seen,            
        )

    def close(self) -> None:
        """Close the underlying HTTP client connection.

        Called at logger shutdown to release the connection pool cleanly.
        """
        self._client.close()
