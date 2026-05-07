# Kalshi Sports Trading Algorithm — Writeup

## Approach

I open a one-sided Kalshi position based on the difference between Kalshi's mid
and the de-vigged sportsbook consensus. I have not laid off the risk on the sportsbook
side as that would require a betting account, sufficient bankroll, and
careful sizing. A "winning" trade can still resolve against us if
the favourite loses. We are betting that **consensus is a more accurate
estimate of true probability than Kalshi's mid**, and that Kalshi will
revert toward consensus before the game starts (or, failing that, that
the long-run resolution rate matches the consensus probability).

### Example with a basketball game

Kalshi LAL @ BOS: best bid 47, best ask 49 → mid 48¢ → implied YES
probability 0.48.

Three sportsbooks quote:

| Book | Home  | Away  | Implied home | Implied away | De-vigged home |
|------|-------|-------|--------------|--------------|----------------|
| DK   | -150  | +130  | 0.6000       | 0.4348       | 0.5798         |
| FD   | -145  | +125  | 0.5918       | 0.4444       | 0.5710         |
| MGM  | -148  | +128  | 0.5968       | 0.4386       | 0.5763         |

Median de-vigged home prob = **0.5763**. Edge = (0.5763 − 0.48) × 100
= **+9.6 percentage points**. All three books are above 0.48 →
3/3 agree. If top-of-book size on the ask is ≥ 500 contracts, spread
is 2¢, and tipoff is in 90 minutes, the signal fires: BUY YES, walk
the asks for 100 contracts at VWAP fill (against a $100,000 bankroll).

A pure arbitrage version would *also* lay BOS on a sportsbook at
American +130 to flatten the directional exposure. We don't.

### What each filter prevents

| Filter | Default | Prevents |
|---|---|---|
| `\|edge_pp\| ≥ 3.0` | 3pp | **Noise.** A 1pp gap between Kalshi and de-vigged consensus is well within sportsbook noise + de-vig method error. We need a wide enough gap that we're confident there's a real divergence, not a 50bp rounding artifact. |
| `top_of_book_size ≥ 500` | 500 | **Phantom edge.** A 10pp gap on a market quoting a handful of contracts on the ask is uninvestable at 100-contract sizing — the moment we trade we exhaust the level and walk into much worse prices. We need real size on the side we're crossing into. |
| `spread ≤ 3¢` | 3¢ | **Bid-ask cost.** Wide spreads mean crossing the spread already eats most of the apparent edge. A 6¢ spread = paying 3¢ over mid just to enter, which on a 3pp signal leaves zero. |
| `≥ 3 of 5 books agree` | 3/5 | **One-book outliers.** If only one book diverges from Kalshi but the other four agree with Kalshi's mid, the median was probably dragged by an off-by-a-tick bug at one shop. We want directional consensus to be broad. |
| `30min ≤ minutes_to_game ≤ 360` | [30, 360] | **Pre-game window.** Too far out and lines are stale, lineups unannounced. Too close to tipoff and Kalshi liquidity dries up while sportsbooks pull lines for last-minute scratches; signals here are dominated by who-can-pull-fastest, not by mispricing. |
| `staleness ≤ 300s` | 5min | **Stale quote.** A 10-minute-old odds snapshot vs. a 60-second-old Kalshi mid is comparing two different worlds. We need both quotes to be recent enough that "edge" is a real-time observation. |-style sizing or position re-entry, that
ratio drops a lot.

## Findings

1. I ran the bot for 20 minutes prior to the Pistons vs. Magic game on Sunday, May 3rd and found that after the game, the orders the bot placed were **slightly profitable**. However, a lot of work needs to be done to make this work with real money
2. Trading math is not easy. I spent time researching different trading methods and chose the one that made the most sense and enjoyable to me. Would love to talk about alternate approaches in the future
3. Understanding that markets are not always profitable. When I initially tested early, I expected for hundreds of trades to be made within seconds. However, this is not the case due to the limited market size and stringent algorithm to only place orders on what it finds profitable.
4. Going with the last point, demo and backtesting models are a ton of help to validate approaches. Made it a lot easier for me to see my code in action
5. A lot of the tradeoffs were considered prior to writing a single line of code. Examples include trading algorithms, market to test, and tech stack to use.