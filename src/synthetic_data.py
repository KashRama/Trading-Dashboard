"""Generate realistic synthetic historical datasets for backtesting and demo mode.

WHY SYNTHETIC DATA?
The Odds API free tier has no historical endpoint — we can't get past sportsbook
lines. Kalshi has historical candlestick data but no corresponding historical odds
to join against. Without both sides, we can't generate historical signals.

Synthetic data solves this by simulating what BOTH data sources would have looked
like over a 14-day period, calibrated to be realistic. It exists to prove the
strategy/execution/replay pipeline works end-to-end, not to prove the strategy
has edge. The backtest result on synthetic data is not a P&L estimate.

TWO DATASETS:
1. generate() — 14-day dataset for backtesting. 50 games, 18,000 Kalshi ticks.
   Called by scripts/generate_synthetic_history.py (one-off setup).
   Writes to synthetic_kalshi_snapshots and synthetic_external_odds_snapshots.

2. generate_demo() — 5-hour dataset for the demo mode. 30 games, 9,000 ticks.
   Called by scripts/run_logger.py --demo before run_demo().
   Also writes to the synthetic_* tables (overwriting prior demo data).

GENERATION MODEL:
- Each game has a "true_home_prob": the actual probability the home team wins.
- "consensus_p": sportsbook probability — a mean-reverting random walk near true_p.
  Represents the aggregate sportsbook wisdom, slightly noisy.
- "kalshi_p": Kalshi mid — a noisier random walk near true_p.
  Represents a thinner, less efficient market.
- "edge events": periods where kalshi_p diverges significantly from consensus_p.
  These are the signals the strategy is designed to detect.
- Order books are built around kalshi_p with realistic spreads (2-5¢) and depth.

The demo generator engineers edge events to guarantee trades:
every game has a forced divergence of 6-9pp starting at a specific tick.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from src import config
from src.storage import (
    SyntheticExternalOddsSnapshot,
    SyntheticKalshiSnapshot,
    init_db,
    session_scope,
)

# Anchor point for the 14-day backtest dataset timestamps
# Demo mode uses datetime.utcnow() instead (rebased to wall clock)
SYNTHETIC_NOW = datetime(2026, 4, 15, 12, 0, 0)

# How many days the backtest dataset spans
WINDOW_DAYS = 14

# How often synthetic Kalshi ticks are generated (mirrors config for realism)
KALSHI_CADENCE_SEC = config.KALSHI_POLL_SECONDS

# How often synthetic odds snapshots are generated
ODDS_CADENCE_SEC = config.ODDS_POLL_SECONDS

NBA_TEAMS = [
    "BOS", "LAL", "MIL", "DEN", "PHI", "MIA", "DAL", "PHX",
    "GSW", "MEM", "NYK", "BKN", "CLE", "MIN", "ATL", "SAC",
]
MLB_TEAMS = [
    "NYY", "LAD", "HOU", "ATL", "TOR", "TB", "SEA", "PHI",
    "STL", "SD", "BOS", "MIL", "MIN", "BAL", "CLE", "TEX",
    "AZ", "SF", "CIN", "CHC",
]


@dataclass
class GameSpec:
    game_id: str
    market_ticker: str
    sport: str
    home_team: str
    away_team: str
    game_start_ts: datetime
    true_home_prob: float


def _build_games(rng: random.Random) -> list[GameSpec]:
    games: list[GameSpec] = []
    start = SYNTHETIC_NOW - timedelta(days=WINDOW_DAYS // 2)
    for i in range(20):
        home, away = rng.sample(NBA_TEAMS, 2)
        offset_h = rng.uniform(0, WINDOW_DAYS * 24)
        ts = start + timedelta(hours=offset_h)
        true_p = rng.choice(
            [rng.uniform(0.45, 0.55)] * 2 + [rng.uniform(0.55, 0.78)] * 5
        )
        games.append(
            GameSpec(
                game_id=f"nba-{i:03d}",
                market_ticker=f"KXNBAGAME-{ts:%y%m%d}-{home}{away}",
                sport="basketball_nba",
                home_team=home,
                away_team=away,
                game_start_ts=ts,
                true_home_prob=round(true_p, 3),
            )
        )
    for i in range(30):
        home, away = rng.sample(MLB_TEAMS, 2)
        offset_h = rng.uniform(0, WINDOW_DAYS * 24)
        ts = start + timedelta(hours=offset_h)
        true_p = rng.choice(
            [rng.uniform(0.43, 0.57)] * 5 + [rng.uniform(0.57, 0.70)] * 2
        )
        games.append(
            GameSpec(
                game_id=f"mlb-{i:03d}",
                market_ticker=f"KXMLBGAME-{ts:%y%m%d}-{home}{away}",
                sport="baseball_mlb",
                home_team=home,
                away_team=away,
                game_start_ts=ts,
                true_home_prob=round(true_p, 3),
            )
        )
    return games


def _gen_kalshi_book(mid_cents: float, rng: random.Random) -> tuple[list, list, dict]:
    """Build a top-5 book around `mid_cents`"""
    spread = rng.choices([2, 3, 4, 5], weights=[0.45, 0.35, 0.15, 0.05])[0]
    half = spread / 2
    bid_top = max(1, min(99, round(mid_cents - half)))
    ask_top = max(bid_top + 1, min(99, round(mid_cents + half)))

    thin = rng.random() < 0.10
    top_size_range = (10, 60) if thin else (60, 200)

    bids = []
    for i in range(5):
        p = bid_top - i
        if p < 1:
            break
        size = rng.randint(*top_size_range) if i == 0 else rng.randint(30, 150)
        bids.append({"price": p, "size": size})
    asks = []
    for i in range(5):
        p = ask_top + i
        if p > 99:
            break
        size = rng.randint(*top_size_range) if i == 0 else rng.randint(30, 150)
        asks.append({"price": p, "size": size})

    metrics = {
        "top_bid": float(bid_top),
        "top_ask": float(ask_top),
        "mid": (bid_top + ask_top) / 2.0,
        "spread_cents": float(ask_top - bid_top),
        "depth_3_bid": sum(l["size"] for l in bids[:3]),
        "depth_3_ask": sum(l["size"] for l in asks[:3]),
    }
    metrics["total_quoted_size"] = metrics["depth_3_bid"] + metrics["depth_3_ask"]
    if metrics["total_quoted_size"] > 0:
        metrics["imbalance"] = (
            (metrics["depth_3_bid"] - metrics["depth_3_ask"])
            / metrics["total_quoted_size"]
        )
    else:
        metrics["imbalance"] = None
    return bids, asks, metrics


def _walk_paths(
    game: GameSpec, rng: random.Random
) -> tuple[list[tuple[datetime, float]], list[tuple[datetime, float]]]:
    """Generate aligned consensus and Kalshi-mid price paths for one game.

    Both paths are mean-reverting random walks anchored to game.true_home_prob.
    Kalshi is noisier (higher volatility) and occasionally has "edge events"
    where it diverges significantly from consensus — these are the signals.

    Returns:
        (consensus_path, kalshi_path) where each is a list of (timestamp, probability) tuples
    """
    # Both paths cover the 6-hour window before game start
    window_start = game.game_start_ts - timedelta(hours=6)
    n_kalshi = (6 * 3600) // KALSHI_CADENCE_SEC   # Total number of Kalshi ticks in 6 hours

    consensus_path: list[tuple[datetime, float]] = []
    kalshi_path: list[tuple[datetime, float]] = []

    # Both paths start near the true probability, with slight noise
    consensus_p = game.true_home_prob
    kalshi_p = game.true_home_prob + rng.gauss(0, 0.005)   # Small initial offset

    # Generate 1-3 "edge events" where Kalshi diverges from consensus
    # These simulate periods of mispricing that the strategy should detect
    events = []
    n_events = rng.randint(1, 3)
    for _ in range(n_events):
        ev_start_min = rng.randint(10, int(6 * 60) - 60)   # Start at random minute in window
        ev_dur_min = rng.randint(10, 60)                    # Last 10-60 minutes
        magnitude = rng.uniform(0.02, 0.06) * rng.choice([-1, 1])   # 2-6pp in either direction
        events.append((ev_start_min, ev_start_min + ev_dur_min, magnitude))

    for tick in range(n_kalshi):
        ts = window_start + timedelta(seconds=tick * KALSHI_CADENCE_SEC)
        minute = tick * (KALSHI_CADENCE_SEC / 60)   # Current time in minutes from window start

        # CONSENSUS PATH: only update when an odds snapshot would be taken
        # This mirrors the real cadence where odds are fetched less frequently than Kalshi
        if tick * KALSHI_CADENCE_SEC % ODDS_CADENCE_SEC == 0:
            # Mean-reverting random walk: 10% pull toward true probability per step, plus noise
            pull = (game.true_home_prob - consensus_p) * 0.10
            consensus_p = max(0.02, min(0.98, consensus_p + pull + rng.gauss(0, 0.005)))
            consensus_path.append((ts, consensus_p))

        # KALSHI PATH: updated every tick, noisier (0.008 std vs 0.005 for consensus)
        # Weaker mean-reversion (5% vs 10%) makes Kalshi less efficient than sportsbooks
        pull = (game.true_home_prob - kalshi_p) * 0.05
        kalshi_p = max(0.02, min(0.98, kalshi_p + pull + rng.gauss(0, 0.008)))

        # EDGE EVENTS: during the event window, push Kalshi away from consensus
        # This creates the detectable mispricing the strategy is designed to find
        for ev_start, ev_end, mag in events:
            if ev_start <= minute < ev_end:
                kalshi_p = max(0.02, min(0.98, kalshi_p + mag * 0.1))   # Push by 0.1 × magnitude per tick

        kalshi_path.append((ts, kalshi_p))

    return consensus_path, kalshi_path


def _add_vig(p: float, rng: random.Random) -> tuple[int, int]:
    """Convert a fair home probability to American odds with a random vig added.

    This is the INVERSE of de-vigging: takes a fair probability and adds
    a sportsbook-style overround so the output looks like real bookmaker odds.

    The generated odds are stored in the synthetic dataset and then de-vigged
    by the strategy exactly as real Odds API data would be — testing the full pipeline.

    Args:
        p: fair (de-vigged) home probability (0..1)
        rng: seeded random number generator

    Returns:
        (home_american_odds, away_american_odds) both as integers
    """
    # Random overround between 4-8% (typical sportsbook margin)
    overround = rng.uniform(0.04, 0.08)

    # Distribute the overround across both sides (inflate each implied prob slightly)
    p_h = max(0.02, min(0.98, p * (1 + overround / 2)))
    p_a = max(0.02, min(0.98, (1 - p) * (1 + overround / 2)))

    # Renormalize so the sum equals exactly 1 + overround
    s = p_h + p_a
    p_h = p_h / s * (1 + overround)
    p_a = p_a / s * (1 + overround)

    def _to_american(prob: float) -> int:
        """Convert implied probability (with vig) to American odds integer."""
        if prob >= 0.5:
            return -int(round(prob / (1 - prob) * 100))   # Favorite: negative odds
        return int(round((1 - prob) / prob * 100))          # Underdog: positive odds

    return _to_american(p_h), _to_american(p_a)


def generate_demo(seed: int = 99) -> dict:
    """Generate the compact synthetic dataset for the 5-minute demo run.

    GUARANTEES:
    - 30 games (15 NBA + 15 MLB)
    - Every game has at least one guaranteed edge event (6-9pp divergence)
    - First game's edge event fires at tick 2 (within 2 seconds of demo start)
    - All games are in the 30-360 minute window throughout (GAME_OFFSET_MIN=350)
    - Books are tight (2-3¢ spread, 80-160 top-of-book size) so liquidity filter passes

    This dataset is designed to DEMONSTRATE the pipeline, not to simulate real markets.
    Every parameter is tuned to guarantee trades fire quickly and visibly.

    At speed=60x, 300 ticks × 1 real second = 5 real minutes total runtime.
    """
    rng = random.Random(seed)
    init_db()

    # Anchor to right now so dashboard shows today's timestamps
    demo_start = datetime.utcnow().replace(microsecond=0)

    TICKS = 300           # Number of Kalshi ticks: 5 hours synthetic at 60s cadence
    GAME_OFFSET_MIN = 350 # All games start 350 min from now = always in [30, 360] window

    # Build 30 game specs: first 15 are NBA, next 15 are MLB
    games: list[GameSpec] = []
    for i in range(30):
        if i < 15:
            home, away = rng.sample(NBA_TEAMS, 2)
            sport, prefix, gid = "basketball_nba", "KXNBAGAME", f"demo-nba-{i:02d}"
        else:
            home, away = rng.sample(MLB_TEAMS, 2)
            sport, prefix, gid = "baseball_mlb", "KXMLBGAME", f"demo-mlb-{i-15:02d}"

        game_start = demo_start + timedelta(minutes=GAME_OFFSET_MIN)

        # Format ticker to match real Kalshi format: KXNBAGAME-26MAY031950BOSKNY-BOS
        # strftime("%y%b%d%H%M").upper() → "26MAY031950" (2-digit year + month name + day + time)
        ts_tag = game_start.strftime("%y%b%d%H%M").upper()

        games.append(GameSpec(
            game_id=gid,
            market_ticker=f"{prefix}-{ts_tag}{home}{away}-{home}",   # Real Kalshi ticker format
            sport=sport,
            home_team=home,
            away_team=away,
            game_start_ts=game_start,
            true_home_prob=round(rng.uniform(0.45, 0.68), 3),   # Random true probability
        ))

    n_kalshi = n_odds = 0   # Counters for the return summary

    with session_scope() as session:
        # Wipe prior demo data — each demo run starts completely fresh
        session.query(SyntheticKalshiSnapshot).delete()
        session.query(SyntheticExternalOddsSnapshot).delete()

        for i, game in enumerate(games):
            # GUARANTEED EDGE EVENT PARAMETERS:
            # ev_start: which tick the edge event begins
            # - Game 0 always starts at tick 2 (fires within 2 real seconds at speed=60x)
            # - All other games start at a random tick (so they don't all fire at once)
            ev_start = 2 if i == 0 else rng.randint(2, 150)
            ev_dur   = rng.randint(15, 25)    # How many ticks the edge event lasts (15-25 min synthetic)
            ev_end   = ev_start + ev_dur
            magnitude = rng.uniform(0.06, 0.09)   # 6-9pp divergence — well above the 3pp threshold
            direction = rng.choice([-1, 1])         # YES underpriced or NO underpriced

            consensus_p = game.true_home_prob   # Consensus starts at true probability

            for tick in range(TICKS):
                ts = demo_start + timedelta(minutes=tick)   # One tick = one minute of synthetic time

                # Consensus drifts very slightly each tick (0.002 std = very stable)
                consensus_p = max(0.30, min(0.70,
                    consensus_p + rng.gauss(0, 0.002)
                ))

                # During edge event: Kalshi diverges by magnitude from consensus
                # Outside event: Kalshi tracks consensus closely (0.004 std noise)
                if ev_start <= tick < ev_end:
                    # Edge event: Kalshi is away from consensus by direction × magnitude
                    kalshi_p = consensus_p + direction * magnitude + rng.gauss(0, 0.003)
                else:
                    # Normal: Kalshi near consensus with small noise
                    kalshi_p = consensus_p + rng.gauss(0, 0.004)
                kalshi_p = max(0.05, min(0.95, kalshi_p))   # Keep within valid probability range

                # Generate 4 sportsbook quotes with random small noise around consensus
                # Using 4 books guarantees 3/4 agreement (MIN_BOOKS_AGREEING=3 passes)
                n_books = 4
                book_quotes, fair_home_per_book = [], []
                for book in config.SPORTSBOOKS[:n_books]:
                    # Each book has slightly different line (±0.004 noise around consensus)
                    bp = max(0.05, min(0.95, consensus_p + rng.gauss(0, 0.004)))
                    ho, ao = _add_vig(bp, rng)   # Add sportsbook vig to get American odds
                    book_quotes.append({"book": book, "home_odds": ho, "away_odds": ao})
                    fair_home_per_book.append(round(bp, 4))   # De-vigged prob for each book

                # Write one odds snapshot per tick (staleness = 0 — always fresh)
                session.add(SyntheticExternalOddsSnapshot(
                    ts=ts,
                    game_id=game.game_id,
                    home_team=game.home_team,
                    away_team=game.away_team,
                    sport=game.sport,
                    game_start_ts=game.game_start_ts,
                    consensus_home_prob=consensus_p,              # The de-vigged median
                    consensus_away_prob=round(1.0 - consensus_p, 4),
                    n_books=n_books,
                    raw_json=json.dumps({
                        "books": book_quotes,                    # Raw per-book odds
                        "fair_home_per_book": fair_home_per_book, # Per-book de-vigged probs
                    }),
                    is_synthetic=True,
                ))
                n_odds += 1

                # Build the Kalshi order book around the kalshi_p mid
                mid_cents = max(5.0, min(95.0, kalshi_p * 100))   # Convert prob to cents
                spread = rng.choice([2, 2, 3])   # Tight spread (mostly 2¢) — passes MAX_SPREAD_CENTS=3
                half = spread / 2
                bid_top = max(2, min(97, round(mid_cents - half)))   # Best bid
                ask_top = max(bid_top + 1, min(98, round(mid_cents + half)))   # Best ask

                bids = [{"price": bid_top - i, "size": rng.randint(80, 160)}
                        for i in range(5) if bid_top - i >= 1]
                asks = [{"price": ask_top + i, "size": rng.randint(80, 160)}
                        for i in range(5) if ask_top + i <= 99]

                d3b = sum(l["size"] for l in bids[:3])
                d3a = sum(l["size"] for l in asks[:3])
                tqs = d3b + d3a

                session.add(SyntheticKalshiSnapshot(
                    ts=ts,
                    market_ticker=game.market_ticker,
                    game_id=game.game_id,
                    home_team=game.home_team,
                    away_team=game.away_team,
                    sport=game.sport,
                    game_start_ts=game.game_start_ts,
                    top_bid=float(bid_top),
                    top_ask=float(ask_top),
                    mid=(bid_top + ask_top) / 2.0,
                    spread_cents=float(ask_top - bid_top),
                    depth_3_bid=d3b,
                    depth_3_ask=d3a,
                    imbalance=(d3b - d3a) / tqs if tqs > 0 else None,
                    total_quoted_size=tqs,
                    book_json=json.dumps({"bids": bids, "asks": asks}),
                    is_synthetic=True,
                ))
                n_kalshi += 1

    return {
        "games": len(games),
        "kalshi_snapshots": n_kalshi,
        "odds_snapshots": n_odds,
        "demo_start": demo_start.isoformat(),
        "window_minutes": TICKS,
    }


def generate(seed: int = 42) -> dict:
    """Generate the full synthetic dataset and write to the synthetic_* tables.

    Returns a summary dict with row counts, primarily for the CLI script.
    """
    rng = random.Random(seed)
    init_db()
    games = _build_games(rng)

    n_kalshi = 0
    n_odds = 0

    with session_scope() as session:
        # Wipe any prior synthetic data so reruns are deterministic.
        session.query(SyntheticKalshiSnapshot).delete()
        session.query(SyntheticExternalOddsSnapshot).delete()

        for game in games:
            consensus_path, kalshi_path = _walk_paths(game, rng)
            n_books = rng.choice([4, 4, 5, 5])
            book_names = config.SPORTSBOOKS[:n_books]

            for ts, consensus_home in consensus_path:
                book_quotes = []
                fair_home_per_book = []
                for book in book_names:
                    book_p = max(0.02, min(0.98, consensus_home + rng.gauss(0, 0.008)))
                    home_amer, away_amer = _add_vig(book_p, rng)
                    book_quotes.append(
                        {"book": book, "home_odds": home_amer, "away_odds": away_amer}
                    )
                    fair_home_per_book.append(round(book_p, 4))

                session.add(
                    SyntheticExternalOddsSnapshot(
                        ts=ts,
                        game_id=game.game_id,
                        home_team=game.home_team,
                        away_team=game.away_team,
                        sport=game.sport,
                        game_start_ts=game.game_start_ts,
                        consensus_home_prob=consensus_home,
                        consensus_away_prob=1 - consensus_home,
                        n_books=n_books,
                        raw_json=json.dumps(
                            {
                                "books": book_quotes,
                                "fair_home_per_book": fair_home_per_book,
                            }
                        ),
                        is_synthetic=True,
                    )
                )
                n_odds += 1

            for ts, kalshi_home_p in kalshi_path:
                mid_cents = max(2, min(98, kalshi_home_p * 100))
                bids, asks, m = _gen_kalshi_book(mid_cents, rng)
                session.add(
                    SyntheticKalshiSnapshot(
                        ts=ts,
                        market_ticker=game.market_ticker,
                        game_id=game.game_id,
                        home_team=game.home_team,
                        away_team=game.away_team,
                        sport=game.sport,
                        game_start_ts=game.game_start_ts,
                        top_bid=m["top_bid"],
                        top_ask=m["top_ask"],
                        mid=m["mid"],
                        spread_cents=m["spread_cents"],
                        depth_3_bid=m["depth_3_bid"],
                        depth_3_ask=m["depth_3_ask"],
                        imbalance=m["imbalance"],
                        total_quoted_size=m["total_quoted_size"],
                        book_json=json.dumps({"bids": bids, "asks": asks}),
                        is_synthetic=True,
                    )
                )
                n_kalshi += 1

    return {
        "games": len(games),
        "kalshi_snapshots": n_kalshi,
        "odds_snapshots": n_odds,
        "synthetic_now": SYNTHETIC_NOW.isoformat(),
    }


def game_outcome(game_id: str, true_home_prob: float, rng: random.Random) -> bool:
    """Resolve a synthetic game: home wins with probability = true prob."""
    return rng.random() < true_home_prob
