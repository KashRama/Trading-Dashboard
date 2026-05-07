# Kalshi Sports — Relative-Value Trading Bot

## What I built

I built a **relative-value trading bot** that compares Kalshi pregame moneyline mids
to de-vigged sportsbook consensus on NBA and MLB games, runs a stack of
liquidity / agreement / staleness filters, and executes trades (simulated or
live sandbox) by crossing the Kalshi order book. The dashboard shows a real-time
P&L chart, open positions, closed trade log, live market scanner, and a
one-click backtest. This is all backed by a synthetic historical dataset so the
pipeline demonstrates end-to-end trades without waiting for live data.

## The Hypothesis

Kalshi pregame moneyline mids on NBA and MLB games diverge from
de-vigged median sportsbook consensus by more than three percentage
points often enough, at sufficient depth, with enough cross-book
agreement is bounded to generate a positive expected value at fixed sizing.

More details can be found in [WRITEUP.md](WRITEUP.md)

---

## How to Run

### Prerequisites

**1. Check that Python 3 is installed**
```bash
python3 --version
```
If you get `command not found`, download Python 3 from https://www.python.org/downloads/ then re-run the check.

**2. Open the project**

After unzipping, you should see a folder called `Trading-Dashboard`. Open a terminal and navigate into it:
```bash
cd path/to/Trading-Dashboard
```
Replace `path/to/Trading-Dashboard` with the actual path. On a Mac you can drag the folder into Terminal after typing `cd ` to auto-fill the path.

**3. Install dependencies**
```bash
pip3 install -e .
```
This installs all required packages. Only needs to be done once.

**4. Generate synthetic history (one time setup, ~1 second):**
```bash
python3 -m scripts.generate_synthetic_history
```
You should see a confirmation line. This only needs to be run once. It is used for the backtesting mode found later in this file.

**5. Minimum Kalshi balance - Only required for modes 2 and 3**

Ensure you have at least $1,000 in your Kalshi sandbox environment. This platform assumes that minimum balance.

**6. Logs**

There are a ton of terminal logs for the commands you will run below. If something is not displaying on the dashboard, odds are that the algorithm cannot find anything to trade. The logs will display all calls being made even if the dashboard does not.

---

### First Mode — Demo (no API keys, ~5 minutes)

This runs 30 fully synthetic trades through the algorithm so you can see the dashboard in action without any real data or accounts. I created this to ensure that when the Kalshi and external markets are not favorable for the algorithm depending on when this is ran, you can still see the effects of what would happen in a favorable market.

**Terminal 1 — Start the dashboard:**
```bash
python3 -m streamlit run scripts/run_dashboard.py
```
It will print a local URL like `http://localhost:8501` — open that in your browser. You should see trades populating in real time under the **Live** tab.

**Terminal 2 — Start the demo:**
```bash
python3 -m scripts.run_logger --demo
```
Leave this running. It will print trade activity as it goes and automatically exit after ~5 minutes. You can also end this demo early by killing the terminal.

**To stop:** "kill" each terminal.

---

### Second Mode — Live orders against Kalshi sandbox (requires API keys)

This runs my algorithm against real Kalshi sandbox order books and real sportsbook odds, placing actual limit orders on the sandbox. To make a viable bot, this algorithm may not execute any trades if it does not find a market where the algorithm passes. If it does not execute trades within 20 minutes, assume that the bot has not found any trades worth betting on. This case is the reasoning behind creating the first and third modes.

**Step 1 — Kalshi Sandbox API ID and key**

The assumption is that you have an API ID, key, and a sandbox env with sufficient funds.

**Step 2 — Get an Odds API key**

Go to https://the-odds-api.com and sign up for a free account. It will prompt you to enter your first name and an email. It will then send the key to your email.

**Step 3 — Add your credentials to the project**

Open the file `.env` in the `Trading-Dashboard` folder (create it if it doesn't exist) and add:
```
KALSHI_API_KEY_ID="your-key-id-here"
KALSHI_API_PRIVATE_KEY_PATH="your-private-key-here"
ODDS_API_KEY="your-odds-api-key-here"
```
Save the file.

**Step 4 — In terminal 1, start the dashboard**
```bash
python3 -m streamlit run scripts/run_dashboard.py
```
Open `http://localhost:8501` or whatever it shows you in the logs in your browser.

**Step 5 — In terminal 2, start the live logger**
```bash
python3 -m scripts.run_logger --live-orders
```
This polls Kalshi and the odds API every minute. Look at the terminal logs to see the calls being made to both APIs. When the strategy finds a trade, it places a real limit order on the sandbox. Leave it running to see trades be made. As stated before, this does not always guarantee a trade will be made.

Disclaimer - The odds API free tier only allows for 500 free api requests per month, so please do not run this for more than 20 minutes (20 minutes * 5 calls per minute = 100 calls)

**To stop:** "kill" in each terminal.

**Switching from demo to live:** If you were previously running demo mode, the dashboard Live tab will already show demo data. After starting the live logger, the Live tab will show live data going forward since demo and live data coexist in the same tables.
The logger will reset the live tables on first run.

---

### Third Mode — Live orders, filters are bypassed

**Terminal 1 — Start the dashboard:**
```bash
python3 -m streamlit run scripts/run_dashboard.py
```
It will print a local URL like `http://localhost:8501` — open that in your browser. You should see trades populating in real time under the **Live** tab.

**Terminal 2 — Start the live logger with reduced filters:**
```bash
python3 -m scripts.run_logger --live-orders --relax-filters
```

**To stop:** "kill" each terminal.

Same as the second mode but skips the strategy's filter requirements (edge threshold, spread, liquidity, book agreement). It essentially places an order on every market it sees. Use this only to verify that the order-placement plumbing is working when there are no natural signals in the sandbox. I created this as a POC to ensure that Kalshi calls are being made. Due to the less stringent algorithm to place orders, the likelihood of orders being placed goes up dramatically. Only leave this running for 20 minutes maximum (another 100 calls to the odds api)

---

### Fourth Mode — Backtesting on synthetic data

The backtest replays 14 days of synthetic Kalshi + sportsbook data through the strategy and shows you the resulting trades, P&L curve, and signal log — all without any API calls. It runs entirely in the dashboard. When this is run, all trades will show in the closed table since it is displaying the outcome of the trades

**Step 1 — Open the dashboard:**
```bash
python3 -m streamlit run scripts/run_dashboard.py
```
Open `http://localhost:8501` or whatever your localhost port is from previously in your browser.

**Step 2 — Run the backtest from the sidebar:**
1. At the top of the page, switch the radio button from **Live** to **Backtest**
2. If the left sidebar is not open, click the button in the top left to open it. In the left sidebar, you will see a set of parameter sliders — these are the algorithm's filter thresholds (edge %, spread, liquidity, etc.)
3. Adjust any sliders you want to test, or leave them at defaults
4. Click **"Run Backtest"**
5. You can also choose to include real Kalshi historical data (checkbox underneath parameters)

The backtest will show the same P&L chart, open/closed trade tables, and signal log as live mode, but sourced from the synthetic replay instead of real data. A yellow banner appears to indicate you are viewing backtest output.

**Changing parameters and re-running:**
You can adjust the sliders and click **"Run Backtest"** as many times as you want. Each run overwrites the previous backtest results. No terminal restart needed — it all happens inside the dashboard.

**Parameter options (sidebar sliders):**

| Slider | What it controls |
|---|---|
| Edge threshold (pp) | Minimum edge in percentage points between Kalshi mid and sportsbook consensus to open a trade |
| Max spread (¢) | Maximum bid–ask spread allowed — wider spreads eat into edge |
| Min top-of-book size | Minimum contracts available at best bid/ask — filters out illiquid markets |
| Min books agreeing | How many sportsbooks must agree on the direction of the edge |
| Game window (min) | How many minutes before game start the algorithm will still consider trading |

---

## Deep dive into each run mode

This section provides a more in depth explanation of each mode. I included this so you could get more of an insight into my code without actually looking through code.

A lot of this information is repeated from before so skip to the configuration section if you don't want to read about the "how"

### `--demo` — Synthetic live demo, no API keys

```bash
python3 -m scripts.run_logger --demo
```

This will generate a fresh 30-game synthetic dataset (15 NBA + 15 MLB) starting at the current time and replays it into the live tables at 60× speed. One trade
fires within the first few seconds; all 30 games produce at least one signal.
The full run completes in 5 minutes of real time and then will automatically kill the terminal.

I created this version because depending on the live odds and games available, the algorithm may not always find a favorable trade to buy contracts on. This mode allows me to show the pure functionality of the dashboard running against fake data that is curated to guarantee that the data passes the algorithm's requirements

**What it's running against:** 100% synthetic data. No network calls. No API
keys required.

**What the code is doing:**
1. Calls `generate_demo()` in `src/synthetic_data.py` to create 30 games ×
   300 ticks of calibrated order book + odds data, timestamped to right now.
2. Calls `run_demo()` in `src/demo_live.py`, which walks every Kalshi tick,
   evaluates the strategy, opens positions, closes on mean-reversion or game
   start, and writes rows to the same `kalshi_snapshots`, `signals`,
   `simulated_trades`, and `pnl_timeseries` tables the real logger uses.
3. At end of demo, force-closes any remaining open positions so the
   dashboard shows a clean final state.

**Flags:**
| Flag | Default | Meaning |
|---|---|---|
| `--speed N` | `60` | Simulated seconds per real second. `60` = 5-min run. `600` = 30-sec run. |
| `--no-reset` | off | Keep prior demo data instead of clearing before run. |

---

### `--live-orders` — Real sandbox order placement

```bash
python3 -m scripts.run_logger --live-orders
```

Polls Kalshi sandbox order books and live sportsbook odds on a continuous loop. When the strategy fires, it places a **real limit order on the Kalshi sandbox**, polls for fill (up to 20s), and cancels if unfilled. The execution report (fill price, quantity, order ID) is persisted and shown on the dashboard.

**What it's running against:** 

Kalshi sandbox for market data + order execution; the-odds-api.com for consensus. Money is fake (sandbox), but the order flow is real.

**What the code is doing:**
1. `src/logger.py` polls `KalshiClient` for markets + order books every 60s.
   `src/odds_client.py` fetches H2H moneylines every 5 min and de-vigs them
   into consensus probabilities (`src/devig.py`).
2. `src/strategy.py` runs the filter stack and emits a `Signal`.
3. `src/live_orders.py` → `execute_live()`: places a taker limit order
   (best ask for YES buys; `100 - best_bid` for NO buys), polls
   `/portfolio/orders/{id}` every 1.5s, treats HTTP 404 as "executed"
   (Kalshi removes filled orders from the active endpoint immediately),
   cancels and reports if still resting after 20s.
4. Falls back to simulated fill if the orders endpoint is unavailable.

---

### `--live-orders --relax-filters` — Live orders, bypass strategy

```bash
python3 -m scripts.run_logger --live-orders --relax-filters
```

Same as `--live-orders`, but the strategy filter stack (edge threshold, spread
limit, liquidity minimums, book agreement) is bypassed. Places a sandbox order
on every market tick regardless of whether a signal fires.

**Use with caution:** This will open a position on every market seen on every
poll cycle. Intended for verifying order-placement plumbing when no natural
signals exist in the sandbox.

---

### Backtest — Replay synthetic history through the strategy

Run from the dashboard sidebar:
1. Open `python3 -m streamlit run scripts/run_dashboard.py`
2. In the left sidebar, adjust the parameter sliders (edge threshold, spread
   limit, top-of-book size, etc.)
3. Click **"Run Backtest"**
4. Toggle the **Live / Backtest** radio at the top of the page to view results

**What it's running against:** The 14-day synthetic Kalshi + odds dataset
generated by `python3 -m scripts.generate_synthetic_history`. No network calls.

**What the code is doing:**
`src/replay.py` → `run_backtest()`:
1. Walks every `SyntheticKalshiSnapshot` in chronological order.
2. Joins each snapshot with the most recent prior odds snapshot for that game.
3. Calls the same `strategy.evaluate()` used by `--live-orders`.
4. Feeds passing signals to the same `Portfolio` execution path.
5. Closes positions on mean-reversion (edge collapses below threshold) or
   game start (market settles), whichever comes first.
6. Force-closes any positions still open at the end of the replay window.
7. Writes results to `backtest_signals`, `backtest_trades`, `backtest_pnl_timeseries`.

The dashboard shows a yellow banner when viewing backtest output. All backtest
sliders update on re-run — no restart needed.

---

## Configuration (`src/config.py`)

These parameters govern every mode — demo, live orders, and backtest. They are the same filter thresholds, sizing rules, and timing windows regardless of which mode is running.

- **Backtest:** the sidebar sliders let you adjust these at runtime without editing any code. Each time you click "Run Backtest" it uses whatever the sliders are set to.
- **Demo and Live:** these values are fixed at whatever is in `src/config.py`. To change them for live or demo runs, open that file and edit the values directly, then restart the logger.

| Parameter | Default | Description |
|---|---|---|
| `BANKROLL` | `$1,000` | Starting cash. Shown in dashboard "Cash Available" card. Overwritten if pulled directly from Kalshi|
| `TRADE_SIZE_CONTRACTS` | `10` | Contracts per trade (fixed sizing). ~$5–9 per trade at typical prices. |
| `MAX_POSITION_PER_MARKET` | `100` | Max contracts in any single market. |
| `MIN_EDGE_PP` | `3.0` | Minimum edge in percentage points to open a position. |
| `MAX_SPREAD_CENTS` | `3` | Maximum bid–ask spread in cents to trade. |
| `MIN_TOP_OF_BOOK` | `50` | Minimum contracts available at best bid/ask. |
| `MIN_BOOKS_AGREEING` | `3` | Minimum sportsbooks agreeing on edge direction. |
| `GAME_WINDOW_MIN_MINUTES` | `30` | Don't trade if game starts in < 30 minutes. |
| `GAME_WINDOW_MAX_MINUTES` | `360` | Don't trade if game starts in > 6 hours. |
| `MAX_ODDS_STALENESS_SECONDS` | `120` | Reject odds snapshots older than 2 minutes. |
| `MEAN_REVERSION_EXIT_PP` | `1.0` | Close position when edge drops below 1pp. |
| `FILL_TIMEOUT_SECONDS` | `20` | Sandbox order fill timeout before cancel. |

---

## Filter stack

A signal passes all of the following to open a position:

1. **Edge** ≥ `MIN_EDGE_PP` — consensus probability minus Kalshi mid exceeds threshold
2. **Spread** ≤ `MAX_SPREAD_CENTS` — bid–ask spread is tight enough to cross profitably
3. **Top-of-book size** ≥ `MIN_TOP_OF_BOOK` — sufficient depth on the trade side
4. **Book agreement** ≥ `MIN_BOOKS_AGREEING` — at least N sportsbooks agree on direction
5. **Game window** — game starts in [30 min, 6 hours]
6. **Odds freshness** ≤ `MAX_ODDS_STALENESS_SECONDS`
7. **No existing position** in this market (one position per market)

The dashboard scanner shows every evaluated signal with the reason it passed
or failed. `--relax-filters` bypasses items 1–4.

---

## Dashboard

```bash
python3 -m streamlit run scripts/run_dashboard.py
```

Auto-refreshes every 2 seconds in live view. Key panels:

- **Metric cards** — Portfolio total, Cash Available, Cost Basis (open
  positions), Unrealized P&L, Realized P&L
- **P&L chart** — 30-minute rolling window with Total P&L and Realized P&L
  lines. Anchored to $0 at window start so the chart always shows a line.
- **Open Positions** — live mark-to-market with entry price, current price,
  cost per contract, total cost, current value, and delta (P&L on position)
- **Closed Trades** — settled and manually exited positions with entry/exit
  prices and realized P&L
- **Live / Backtest radio** — switches between live logger data and the most
  recent backtest run
- **Sidebar** — backtest parameter sliders, "Run Backtest" button, Odds API
  call counter

---

## Synthetic historical data

The Odds API free tier has no historical endpoint, so the backtest includes
a **synthetic** 14-day dataset of ~50 NBA/MLB games generated by
`src/synthetic_data.py` from a calibrated random-walk model. It is not real
market data. It exists to demonstrate the strategy/execution/replay pipeline
end-to-end. The dashboard shows a yellow banner when you are viewing backtest
output. Live mode is empty until you start the logger.

The synthetic-data tables (`synthetic_kalshi_snapshots`,
`synthetic_external_odds_snapshots`) are clearly named and every row carries
`is_synthetic=True`. Backtest output lands in `backtest_signals` /
`backtest_trades` / `backtest_pnl_timeseries`, mirrored cleanly from the live
tables.

Demo mode (`--demo`) generates a separate shorter dataset (30 games, 300
ticks) into the same synthetic tables, overwriting prior demo data on each
run unless `--no-reset` is passed.

---

## Assumptions and simplifications

- **Sandbox only.** All Kalshi traffic goes to `demo-api.kalshi.co`.
  Production endpoints are not used.
- **No fees.** Kalshi sandbox is free. Production fee modelling needs to be included.
- **Synthetic historical sportsbook data**, calibrated to be realistic.
- **Fixed sizing.** 10 contracts per trade against a $1,000 bankroll.
- **One position per market at a time.** No re-entry, no scaling in.
- **Two-outcome de-vig only** (multiplicative method). Power and Shin
  methods noted in writeup as future work.
- **Team-name matching needs to be updated in prod.** Production needs a real
  Kalshi ticker to external-game-id lookup table

---

## What I'd do next with more time

- More API calls - Right now, I am restricted by money to make calls every millisecond and therefore make calls every minute. This will obviously result in smaller profits
- Paid Odds API tier - Hand in hand with previous point to be able to make more calls
- Better de-vigging algorithm; the current model is not extremely profitable
- Hedge open positions on the sportsbook side to reduce losses.
- Expand the same algorithm to different markets (weather, politics, etc).
- A proper market-metadata table linking Kalshi event tickers to external
  game IDs instead of last-token name matching.
- Latency budget + line-movement model. Low latency is the key to profitable trading. Definitely need to improve this in the future.
- For the graph, I want to include options to view over different spans of time.

---

## Tech stack

| Layer | Technology | Purpose |
|---|---|---|
| Language | Python 3.X | Core application |
| Dashboard | Streamlit | Real-time UI, auto-refresh, sidebar controls |
| Charts | Plotly | Interactive P&L chart |
| Database | SQLite | Local trade/signal storage, live and backtest table mirrors |
| Data | Pandas + NumPy | Data manipulation, P&L calculations |
| HTTP client | HTTPX | Async-capable REST calls to Kalshi and Odds API |
| External APIs 1 | Kalshi sandbox REST API | Order books, placement, portfolio data |
| External APIs 2 | The Odds API | Live sportsbook moneylines for consensus |

---

## Project layout

```
src/
  config.py            tunable parameters (bankroll, filters, sizing)
  storage.py           SQLAlchemy models — live and backtest table mirrors
  devig.py             American odds → de-vigged consensus
  orderbook.py         book metrics + VWAP fill simulation
  strategy.py          evaluate(kalshi, odds, now) -> Signal | None
  execution.py         Portfolio: open/close, mark-to-market, settle
  pnl.py               P&L summarization
  replay.py            backtest engine — replays history through evaluate()
  demo_live.py         demo mode replay into live tables
  synthetic_data.py    14-day history + 5-minute demo dataset generator
  kalshi_client.py     sandbox-only REST client, RSA-PSS signing
  kalshi_history.py    candlestick fetcher for the optional real-data toggle
  odds_client.py       Odds API client + de-vig adapter
  logger.py            live data logger (poll loop)
  live_orders.py       real sandbox order placement + fill polling
scripts/
  generate_synthetic_history.py   one time dataset generation
  run_logger.py        all run modes (--demo, --live-orders, --relax-filters)
  run_dashboard.py     Streamlit dashboard
  test_trade.py        end-to-end sandbox order integration test
tests/
  fixtures/            cached real API responses + documented-schema sample
  test_devig.py        de-vig math
  test_orderbook.py    book metrics + fills
  test_strategy.py     filter stack
  test_replay.py       end-to-end backtest smoke test
  test_odds_client.py  parser against fixture
```

---

## Architectural Simplification

`Strategy.evaluate(kalshi_snapshot, external_snapshot, now)` is called by
both the live logger (`src/logger.py`) and the backtest engine
(`src/replay.py`). Same function, same filter stack.
But, different data sources with identical logic. Demo mode (`src/demo_live.py`) uses
the same path. The dashboard reads from `*` or `backtest_*` tables based on
the view toggle but renders identically.