"""Centralized configuration. All tunable thresholds live here.

Every number that controls the algorithm's behavior is defined in this one file.
No magic numbers are scattered across the codebase — all other files import
from here. This means changing one value here propagates everywhere:
the live logger, the backtest engine, the strategy filter stack.

The backtest dashboard sliders work by temporarily overwriting these module-level
variables at runtime before calling run_backtest(), which is why this single
file is the authoritative source for all parameters.
"""

from __future__ import annotations

import os       
from pathlib import Path 

from dotenv import load_dotenv  

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"
DB_PATH = DATA_DIR / "trades.db"
DB_URL = f"sqlite:///{DB_PATH}"


# --- Strategy thresholds ---
# These are the filter stack parameters. A signal must pass all of them to open a trade.
EDGE_THRESHOLD_PP = 3.0
MIN_TOP_OF_BOOK_SIZE = 50
MAX_SPREAD_CENTS = 3
MIN_BOOKS_AGREEING = 3
MIN_MINUTES_TO_GAME = 30
MAX_MINUTES_TO_GAME = 360
MAX_STALENESS_SECONDS = 120


# --- Execution / sizing ---
# These control how large each trade is and how the portfolio is managed.
TRADE_SIZE_CONTRACTS = 10
BANKROLL = 1000
MAX_POSITION_PER_MARKET = 100
MAX_OPEN_POSITIONS = 30


# --- Exit ---
# Controls when we exit a position early (before game resolution).
MEAN_REVERSION_EXIT_PP = 1.0


# --- Logger cadence ---
KALSHI_POLL_SECONDS = 60
ODDS_POLL_SECONDS = 60


# --- Markets ---
# Which sports and sportsbooks to monitor.
SPORTS = [
    "basketball_nba",    # NBA playoffs
    "baseball_mlb",      # MLB regular season
    "icehockey_nhl",     # NHL playoffs
    "soccer_usa_mls",    # MLS regular season
    "basketball_ncaab",  # NCAAB (college basketball)
]

KALSHI_SPORTS_PREFIXES = ("KXNBA", "KXMLB", "KXNCAABBGAME", "KXNHL", "KXMLS")
SPORTSBOOKS = ["draftkings", "fanduel", "betmgm", "caesars", "pointsbet"]

# --- Environment: SANDBOX ONLY. Never point this at production. ---
KALSHI_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"


# --- Secrets (loaded from .env) ---
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID") or os.getenv("KALSHI-API-ID")
KALSHI_API_PRIVATE_KEY_INLINE = os.getenv("KALSHI-PRIVATE-KEY")
ODDS_API_KEY = os.getenv("ODDS-API-KEY")
