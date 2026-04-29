"""De-vigging logic.

WHAT IS A VIG?
Sportsbooks don't offer fair odds — they build a profit margin called the "vig"
(short for vigorish) or "overround" into the lines. This means the implied
probabilities they quote always sum to MORE than 100%.

Example: -110 / -110 (a coin-flip game):
  Implied home: 110 / (110 + 100) = 52.38%
  Implied away: 110 / (110 + 100) = 52.38%
  Sum = 104.76%  ← the extra 4.76% is the vig (the book's edge)

To get a "fair" probability, we need to remove this vig. That's de-vigging.

MULTIPLICATIVE METHOD (what this file implements):
Divide each implied probability by the sum of all implied probabilities:
    fair_p_home = implied_p_home / (implied_p_home + implied_p_away)
    fair_p_away = implied_p_away / (implied_p_home + implied_p_away)

This proportionally removes the overround from each side.

WHY MEDIAN ACROSS BOOKS?
We compute fair probability per book, then take the MEDIAN across books.
Median is more robust to one outlier book posting a bad line than the mean would be.
"""

from __future__ import annotations

from dataclasses import dataclass    
from statistics import median       
from typing import Sequence        


def american_to_implied(odds: int) -> float:
    """Convert American moneyline odds to implied probability (with vig still included).

    American odds conventions:
    - Positive odds (e.g. +130): underdog. Bet $100 to win $130.
      Implied probability = 100 / (odds + 100)
      Example: +130 → 100 / 230 = 43.5%

    - Negative odds (e.g. -150): favorite. Bet $150 to win $100.
      Implied probability = |odds| / (|odds| + 100)
      Example: -150 → 150 / 250 = 60.0%

    These implied probabilities still include the vig — they sum to > 100%
    across both sides. That's removed by devig_pair().
    """
    if odds == 0:
        raise ValueError("Odds cannot be zero")  

    if odds > 0:
        return 100.0 / (odds + 100.0)
    else:
        return (-odds) / ((-odds) + 100.0)


def devig_pair(home_implied: float, away_implied: float) -> tuple[float, float]:
    """Remove the vig from a two-outcome market using the multiplicative method.

    Takes the raw implied probabilities (which sum to > 1.0 due to vig)
    and normalizes them so they sum to exactly 1.0.

    Args:
        home_implied: raw implied probability for home team (e.g. 0.60)
        away_implied: raw implied probability for away team (e.g. 0.4348)

    Returns:
        (fair_home, fair_away) — tuple of probabilities that sum to 1.0

    Example:
        home_implied = 0.6000, away_implied = 0.4348
        total = 1.0348 (the 3.48% vig)
        fair_home = 0.6000 / 1.0348 = 0.5799
        fair_away = 0.4348 / 1.0348 = 0.4201
    """
    total = home_implied + away_implied   # Sum > 1.0 because of the vig

    if total <= 0:
        raise ValueError("Implied probabilities must sum to >0")  # Sanity guard

    return home_implied / total, away_implied / total


@dataclass(frozen=True)
class BookQuote:
    """Raw American odds quote from one sportsbook for one game.

    frozen=True makes this immutable — once created, the values can't be changed.
    This prevents accidental mutation of quote data downstream.
    """
    book: str        # Sportsbook identifier, e.g. "draftkings"
    home_odds: int   # American odds for home team, e.g. -150
    away_odds: int   # American odds for away team, e.g. +130


@dataclass(frozen=True)
class Consensus:
    """De-vigged consensus probability across multiple sportsbooks.

    This is the output of consensus_from_books() — a single agreed-upon
    probability estimate for the game based on multiple books' lines.
    """
    home_prob: float           # Final consensus probability for home team winning (0..1)
    away_prob: float           # Final consensus probability for away team winning (0..1)
    n_books: int               # How many books were used to compute this consensus
    per_book_home: list[float]
                        

def consensus_from_books(quotes: Sequence[BookQuote]) -> Consensus:
    """Compute a de-vigged consensus probability from multiple sportsbook quotes.

    Process:
    1. For each book: convert American odds → implied probs → de-vig → fair probability
    2. Collect the per-book fair home probability in a list
    3. Take the median of all books' fair home probabilities (robust to outliers)
    4. Take the median of all books' fair away probabilities
    5. Re-normalize the medians (median(home) + median(away) may not equal exactly 1.0)

    Args:
        quotes: list of BookQuote objects, one per sportsbook

    Returns:
        Consensus with home_prob + away_prob ≈ 1.0
    """
    if not quotes:
        raise ValueError("Need at least one book quote")  # Can't compute median of nothing

    # Step 1: De-vig each book individually and store the per-book fair probabilities
    fair_home_per_book: list[float] = []  # One entry per book
    fair_away_per_book: list[float] = []  # One entry per book

    for q in quotes:
        h_imp = american_to_implied(q.home_odds)
        a_imp = american_to_implied(q.away_odds)

        fh, fa = devig_pair(h_imp, a_imp)

        fair_home_per_book.append(fh)
        fair_away_per_book.append(fa)

    # Step 2: Take the median across all books (robust to one outlier book)
    med_home = median(fair_home_per_book)
    med_away = median(fair_away_per_book)

    # Step 3: Re-normalize
    s = med_home + med_away

    return Consensus(
        home_prob=med_home / s,         # Final fair probability for home team
        away_prob=med_away / s,         # Final fair probability for away team
        n_books=len(quotes),            # How many books contributed
        per_book_home=fair_home_per_book,  
    )


def books_agreeing_direction(
    fair_home_per_book: Sequence[float], kalshi_home_prob: float
) -> int:
    """Count how many individual sportsbooks agree with the direction of the consensus edge.

    PURPOSE:
    Even if the median consensus says YES is underpriced on Kalshi, we want to
    make sure this isn't driven by one outlier book. This function checks each
    book individually: does it also think Kalshi is underpriced in the same direction?

    LOGIC:
    1. Determine the direction the consensus points (is consensus > kalshi or < kalshi?)
    2. For each book individually, check if its fair probability points the same direction
    3. Count how many books agree

    Args:
        fair_home_per_book: list of per-book de-vigged home probabilities
        kalshi_home_prob: the Kalshi mid price expressed as a probability (0..1)
                          e.g. if Kalshi mid is 48¢, this is 0.48

    Returns:
        Number of books that agree with the edge direction (used against MIN_BOOKS_AGREEING)

    Example:
        Kalshi mid = 0.48 (48¢)
        Consensus = 0.57 (positive edge — consensus thinks YES is underpriced)
        Book 1: 0.58 > 0.48 → agrees
        Book 2: 0.55 > 0.48 → agrees
        Book 3: 0.60 > 0.48 → agrees
        Result: 3 (all three agree)
    """
    consensus_home = median(fair_home_per_book)

    sign_consensus = 1 if consensus_home > kalshi_home_prob else -1

    n = 0 

    for p in fair_home_per_book:
        # Does this individual book point the same direction as the overall consensus?
        sign_book = 1 if p > kalshi_home_prob else -1

        if sign_book == sign_consensus:
            n += 1  # This book agrees with the consensus direction

    return n  # Returns a number in [0, len(fair_home_per_book)]
