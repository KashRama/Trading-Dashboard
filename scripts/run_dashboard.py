"""
Streamlit dashboard for the Kalshi sports relative-value bot.

Layout:
- Top bar: portfolio, cash available, value of positions, p&l, positions.
- Top row: P&L chart
- Middle row: open positions and closed trades
- Bottom row: live market scanner: every currently qualifying market with signal status.
- Bottom row: Live / Backtest toggle + Run Backtest button.
- Sidebar: parameters for Run Backtest action.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import select

from src import config
from src.replay import run_backtest
from src.storage import (
    ApiCallLog,
    BacktestPnLTimeseries,
    BacktestSignal,
    BacktestTrade,
    PnLTimeseries,
    Signal,
    SimulatedTrade,
    SyntheticKalshiSnapshot,
    SyntheticExternalOddsSnapshot,
    init_db,
    session_scope,
)

NAVY = "#0A2540"
LIGHT = "#F5F7FA"
MUTED = "#6B7C93"

# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Kalshi Sports RV",
    layout="wide",
    initial_sidebar_state="collapsed",
)
init_db()

# Minimal styling on top of base configurations
st.markdown(
    f"""
    <style>
      /* Hide only the Deploy button and rainbow decoration bar */
      .stDeployButton {{ display: none !important; }}
      div[data-testid="stDecoration"] {{ display: none !important; }}
      .block-container {{ padding-top: 2rem !important; padding-bottom: 0rem !important; padding-left: 2rem !important; padding-right: 2rem !important; max-width: 100% !important; }}
      .st-emotion-cache-1r1cntt {{ padding-bottom: 0rem !important; }}
      h1 {{ padding-top: 0rem !important; }}
      h1, h2, h3 {{ color: #ffffff; font-family: 'Lexend', sans-serif; }}
      .metric-card {{
        background: {LIGHT};
        padding: 14px 18px;
        border-radius: 8px;
        border: 1px solid #E2E8F0;
        font-family: 'Lexend', sans-serif;
      }}
      .metric-label {{ color: {MUTED}; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; font-family: 'Lexend', sans-serif; }}
      .metric-value {{ color: {NAVY}; font-size: 22px; font-weight: 600; font-family: 'Lexend', sans-serif; }}
      div[data-testid="stDataFrame"] {{ border: 1px solid #E2E8F0; border-radius: 6px; }}
      .stButton > button {{
        background: {NAVY}; color: white; border: 0; padding: 8px 16px; border-radius: 6px;
        font-family: 'Lexend', sans-serif;
      }}
      .stButton > button:hover {{ background: #0e3057; color: white; }}
      .synth-banner {{
        background: #FFF3CD; color: #856404; padding: 6px 14px; border-radius: 6px;
        font-size: 13px; border: 1px solid #FFEEBA; display: inline-block;
        font-family: 'Lexend', sans-serif;
      }}
    </style>
    """,
    unsafe_allow_html=True,
)

def _odds_calls_this_month() -> int:
    """Sum of ApiCallLog rows for api='odds' since the first of the month."""
    from datetime import date, datetime as _dt
    start = _dt(date.today().year, date.today().month, 1)
    with session_scope() as s:
        rows = s.execute(
            select(ApiCallLog).where(
                ApiCallLog.api == "odds", ApiCallLog.ts >= start
            )
        ).scalars().all()
        return sum(r.cost for r in rows)

# ---------------------------------------------------------------------------
# Sidebar — parameters + backtest control
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("## Parameters")
    edge_threshold = st.slider(
        "Edge threshold (pp)", 0.0, 10.0, float(config.EDGE_THRESHOLD_PP), 0.5
    )
    min_top_size = st.slider(
        "Min top-of-book size", 0, 200, int(config.MIN_TOP_OF_BOOK_SIZE), 10
    )
    max_spread = st.slider(
        "Max spread (cents)", 1, 10, int(config.MAX_SPREAD_CENTS), 1
    )
    min_books = st.slider("Min books agreeing", 1, 5, int(config.MIN_BOOKS_AGREEING), 1)
    min_min_to_game = st.slider(
        "Min minutes to game", 0, 360, int(config.MIN_MINUTES_TO_GAME), 5
    )
    max_min_to_game = st.slider(
        "Max minutes to game", 30, 720, int(config.MAX_MINUTES_TO_GAME), 30
    )

    st.markdown("---")
    include_real_kalshi = st.checkbox(
        "Include real Kalshi sandbox historical data",
        value=False,
        help=(
            "Pulls whatever sandbox sports history is available and merges it "
            "into the backtest. May be sparse or empty."
        ),
    )
    if st.button("Run Backtest", width='stretch'):
        # Apply slider overrides to module-level config so the strategy
        # picks them up. (Ephemeral — only for this process.)
        config.EDGE_THRESHOLD_PP = edge_threshold
        config.MIN_TOP_OF_BOOK_SIZE = min_top_size
        config.MAX_SPREAD_CENTS = max_spread
        config.MIN_BOOKS_AGREEING = min_books
        config.MIN_MINUTES_TO_GAME = min_min_to_game
        config.MAX_MINUTES_TO_GAME = max_min_to_game

        if include_real_kalshi:
            from src.kalshi_history import fetch_and_persist_history
            with st.spinner("Pulling real Kalshi sandbox history..."):
                try:
                    summary = fetch_and_persist_history(days_back=14)
                    st.info(
                        f"Real sandbox history: {summary['markets_seen']} sports markets seen, "
                        f"{summary['markets_with_data']} had candlestick data, "
                        f"{summary['rows_inserted']} rows inserted."
                    )
                except Exception as e:
                    st.warning(
                        f"Real Kalshi history fetch failed: {e}. "
                        "Falling back to synthetic-only backtest."
                    )

        with st.spinner("Replaying historical data through strategy..."):
            result = run_backtest(seed=7, include_real_kalshi=include_real_kalshi)
        st.session_state["last_backtest"] = result
        st.session_state["view"] = "Backtest"
        st.success(
            f"Done — {result.n_trades_opened} trades, "
            f"P&L ${result.total_pnl:+.2f}"
        )

    st.markdown("---")
    auto_refresh = st.checkbox(
        "Auto-refresh (Live view, every 2s)",
        value=True,
        help=(
            "When enabled in Live view, the page re-runs every 2 seconds so "
            "open positions and new trades appear without manual refresh. "
            "Required to see the demo's brief open-position windows."
        ),
    )

    # Odds API call counter
    _sidebar_odds = _odds_calls_this_month()
    _sidebar_odds_note = "demo makes no real calls" if _sidebar_odds == 0 else f"{500 - _sidebar_odds} remaining this month"
    st.caption(f"**Odds API calls:** {_sidebar_odds} / 500  \n{_sidebar_odds_note}")

    st.markdown("---")


# ---------------------------------------------------------------------------
# Top bar
# ---------------------------------------------------------------------------
if "view" not in st.session_state:
    st.session_state["view"] = "Live"

view = st.radio(
    "View",
    ["Live", "Backtest"],
    key="view",
    horizontal=True,
    label_visibility="collapsed",
)

st.title("Kalshi Sports Trading Dashboard")

if view == "Backtest":
    st.markdown(
        '<span class="synth-banner">'
        "Backtest data is SYNTHETIC unless real Kalshi sandbox history is enabled. "
        "Calibrated to demonstrate the pipeline, not to estimate live edge."
        "</span>",
        unsafe_allow_html=True,
    )


def _read_pnl_timeseries(view: str) -> pd.DataFrame:
    Model = BacktestPnLTimeseries if view == "Backtest" else PnLTimeseries
    with session_scope() as s:
        rows = s.execute(select(Model).order_by(Model.ts)).scalars().all()
        return pd.DataFrame(
            [
                {
                    "ts": r.ts,
                    "realized": r.realized_pnl,
                    "unrealized": r.unrealized_pnl,
                    "total": r.total_pnl,
                    "open_positions": r.open_positions,
                }
                for r in rows
            ]
        )


def _kalshi_live_data() -> dict:
    """Single batched fetch of all Kalshi portfolio + market data.

    Refreshes every 2 seconds.
    Returns a dict with keys: balance, portfolio_value, market_positions,
    fills, sports_markets, error.  All dollar values in dollars (not cents).
    """
    try:
        from src.kalshi_client import KalshiClient
        from src.config import KALSHI_SPORTS_PREFIXES
        client = KalshiClient()

        bal_resp = client.get_balance()
        pos_resp = client.get_positions()
        fills = client.get_all_fills()
        settlements = client.get_settlements()
        markets_resp = client.list_markets(status="open", limit=200)
        client.close()

        sports_markets = [
            m for m in markets_resp.get("markets", [])
            if m.get("ticker", "").startswith(KALSHI_SPORTS_PREFIXES)
        ]

        return {
            "balance": bal_resp.get("balance", 0) / 100.0,
            "portfolio_value": bal_resp.get("portfolio_value", 0) / 100.0,
            "market_positions": pos_resp.get("market_positions", []),
            "event_positions": pos_resp.get("event_positions", []),
            "fills": fills,
            "settlements": settlements,
            "sports_markets": sports_markets,
            "error": None,
        }
    except Exception as e:
        return {
            "balance": None, "portfolio_value": None,
            "market_positions": [], "event_positions": [],
            "fills": [], "settlements": [], "sports_markets": [], "error": str(e),
        }


def _session_mode() -> str:
    """Read the mode the logger wrote at startup: 'live_orders'|'live'|'demo'."""
    from src.storage import get_session_meta
    return get_session_meta("mode") or "demo"


def _initial_balance_dollars() -> float | None:
    """Kalshi balance (dollars) at logger startup, or None if not stored."""
    from src.storage import get_session_meta
    val = get_session_meta("initial_balance_cents")
    return float(val) / 100.0 if val else None


def _fills_to_pnl_df(fills: list[dict]) -> pd.DataFrame:
    """Build a cumulative P&L time-series from Kalshi fills.

    Each buy fill deploys cash (negative P&L contribution until the position
    settles). When a position resolves and Kalshi credits the account, the
    balance rises — captured via the balance+portfolio_value formula at 'now'.
    We plot the cost-basis line (cumulative cash deployed) over time.
    """
    if not fills:
        return pd.DataFrame(columns=["ts", "realized", "unrealized", "total"])

    from datetime import datetime as _dt
    rows = []
    for f in sorted(fills, key=lambda x: x.get("ts", 0)):
        try:
            ts_raw = f.get("created_time") or f.get("ts")
            if isinstance(ts_raw, (int, float)):
                ts = _dt.utcfromtimestamp(ts_raw)
            else:
                ts = _dt.fromisoformat(str(ts_raw).replace("Z", "+00:00")).replace(tzinfo=None)
            count = float(f.get("count_fp", 1))
            side = f.get("side", "yes")
            price = float(f.get("yes_price_dollars" if side == "yes" else "no_price_dollars", 0))
            cost = count * price
            rows.append({"ts": ts, "cost": cost})
        except Exception:
            continue

    if not rows:
        return pd.DataFrame(columns=["ts", "realized", "unrealized", "total"])

    df = pd.DataFrame(rows)
    df = df.sort_values("ts").reset_index(drop=True)
    df["cumulative_cost"] = df["cost"].cumsum()
    df["realized"] = 0.0
    df["unrealized"] = -df["cumulative_cost"]  # money deployed = paper loss until settlement
    df["total"] = df["realized"] + df["unrealized"]
    return df[["ts", "realized", "unrealized", "total"]]


def _positions_to_df(market_positions: list[dict], fills: list[dict] | None = None) -> pd.DataFrame:
    """Convert Kalshi market_positions to the trade-log DataFrame shape.

    `fills` is used to derive the actual side (YES/NO) of each position —
    the positions endpoint only returns `position_fp` without a side field.
    """
    from datetime import datetime as _dt
    side_map = _side_map_from_fills(fills or [])
    rows = []
    for p in market_positions:
        try:
            ticker = p.get("ticker", "")
            exposure = float(p.get("market_exposure_dollars", 0))
            contracts = abs(float(p.get("position_fp", 0)))
            entry = round(abs(exposure) / contracts, 4) if contracts else 0.0
            ts_raw = p.get("last_updated_ts", "")
            try:
                ts = _dt.fromisoformat(ts_raw.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                ts = None
            rows.append({
                "open_ts": ts,
                "market": _parse_kalshi_ticker(ticker),
                "ticker": ticker,
                "side": side_map.get(ticker, "YES"),
                "size": int(contracts),
                "fill": entry,
                "status": "open",
            })
        except Exception:
            continue
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["open_ts", "market", "ticker", "side", "size", "fill", "status"]
    )


def _closed_trades_df(fills: list[dict], settlements: list[dict]) -> "pd.DataFrame":
    """Build a closed-trades table from Kalshi settlements + manual sell fills.

    Settlements (market resolution) are the primary source — Kalshi never
    creates sell fills when a market settles. Sell fills cover positions the
    user manually exited before settlement. Tickers present in settlements
    take precedence; remaining sell-fill tickers are appended.
    """
    import pandas as _pd
    from collections import defaultdict as _dd
    from datetime import datetime as _dt

    rows = []
    settled_tickers: set = set()

    # ── 1. Settlement records (game resolved by Kalshi) ───────────────────
    for s in settlements:
        ticker = s.get("ticker", "")
        if not ticker:
            continue
        settled_tickers.add(ticker)

        yes_qty = float(s.get("yes_count_fp", 0))
        no_qty = float(s.get("no_count_fp", 0))
        yes_cost = float(s.get("yes_total_cost_dollars", 0))
        no_cost = float(s.get("no_total_cost_dollars", 0))
        revenue_dollars = float(s.get("revenue", 0)) / 100.0

        if yes_qty > 0:
            position_side = "YES"
            qty = yes_qty
            cost = yes_cost
        else:
            position_side = "NO"
            qty = no_qty
            cost = no_cost

        if qty == 0:
            continue

        entry = cost / qty
        exit_p = revenue_dollars / qty
        pnl = revenue_dollars - cost

        try:
            close_ts = _dt.fromisoformat(
                s["settled_time"].replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except Exception:
            close_ts = None

        rows.append({
            "closed_at": close_ts,
            "market": _parse_kalshi_ticker(ticker),
            "position": position_side,
            "contracts": int(qty),
            "entry": round(entry, 4),
            "exit": round(exit_p, 4),
            "p&l": round(pnl, 4),
            "close type": "settled",
        })

    # ── 2. Algorithmic sells ───────────────────
    by_ticker: dict = _dd(list)
    for f in fills:
        by_ticker[f.get("ticker", "")].append(f)

    for ticker, tf in by_ticker.items():
        if ticker in settled_tickers:
            continue
        buys = [f for f in tf if f.get("action") == "buy"]
        sells = [f for f in tf if f.get("action") == "sell"]
        if not sells:
            continue

        yes_bought = sum(float(f["count_fp"]) for f in buys if f.get("side") == "yes")
        no_bought = sum(float(f["count_fp"]) for f in buys if f.get("side") == "no")
        position_side = "YES" if yes_bought >= no_bought else "NO"

        qty_closed = sum(float(f["count_fp"]) for f in sells)
        price_key = "yes_price_dollars" if position_side == "YES" else "no_price_dollars"

        buy_prices = [(float(f["count_fp"]), float(f.get(price_key, 0))) for f in buys]
        total_buy_qty = sum(q for q, _ in buy_prices)
        entry = sum(q * p for q, p in buy_prices) / total_buy_qty if total_buy_qty else 0.0

        sell_prices = [(float(f["count_fp"]), float(f.get(price_key, 0))) for f in sells]
        total_sell_qty = sum(q for q, _ in sell_prices)
        exit_p = sum(q * p for q, p in sell_prices) / total_sell_qty if total_sell_qty else 0.0
        pnl = (exit_p - entry) * qty_closed

        close_ts = None
        for f in sells:
            try:
                ct = _dt.fromisoformat(f["created_time"].replace("Z", "+00:00")).replace(tzinfo=None)
                if close_ts is None or ct > close_ts:
                    close_ts = ct
            except Exception:
                pass

        rows.append({
            "closed_at": close_ts,
            "market": _parse_kalshi_ticker(ticker),
            "position": position_side,
            "contracts": int(qty_closed),
            "entry": round(entry, 4),
            "exit": round(exit_p, 4),
            "p&l": round(pnl, 4),
            "close type": "sold",
        })

    if not rows:
        return _pd.DataFrame(columns=["closed_at", "market", "position", "contracts", "entry", "exit", "p&l", "close type"])
    df = _pd.DataFrame(rows)
    df.sort_values("closed_at", ascending=False, inplace=True)
    return df.reset_index(drop=True)


def _side_map_from_fills(fills: list[dict]) -> dict[str, str]:
    """Determine the net side (YES/NO) of each position from buy fills.

    For each ticker, sum the contracts bought on YES vs NO. Whichever is
    larger is the current position direction.
    """
    from collections import defaultdict
    counts: dict[str, dict] = defaultdict(lambda: {"yes": 0.0, "no": 0.0})
    for f in fills:
        if f.get("action") != "buy":
            continue
        ticker = f.get("ticker", "")
        side = f.get("side", "yes").lower()
        count = float(f.get("count_fp", 0))
        counts[ticker][side] += count
    return {
        ticker: ("NO" if c["no"] > c["yes"] else "YES")
        for ticker, c in counts.items()
    }


def _parse_kalshi_ticker(ticker: str) -> str:
    """Convert a Kalshi ticker into a human-readable description.

    Example: KXNBASPREAD-26MAY02PHIBOS-PHI7    → NBA Spread | PHI vs BOS | PHI -7.5
    """
    import re
    try:
        parts = ticker.split("-")
        if len(parts) < 2:
            return ticker

        prefix = parts[0]
        middle = parts[1]

        for sport_key, sport_label in [
            ("KXNBA", "NBA"), ("KXMLB", "MLB"), ("KXNHL", "NHL"),
            ("KXNCAABB", "NCAAB"), ("KXMLS", "MLS"),
        ]:
            if prefix.startswith(sport_key):
                sport = sport_label
                mtype_raw = prefix[len(sport_key):]  # SPREAD, TOTAL, GAME, etc.
                break
        else:
            return ticker

        mtype = mtype_raw.rstrip("0123456789")  # strip trailing digits

        # Strip date/time prefix from middle: YYMONDD[TIME]
        # Month abbreviations are always 3 uppercase letters
        m = re.match(
            r"^\d{2}(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}(\d{4})?",
            middle,
        )
        if not m:
            return ticker
        teams_str = middle[m.end():] 

        line_part = parts[2] if len(parts) > 2 else None

        # Determine teams.
        team1, team2 = "", ""
        if line_part:
            line_team_m = re.match(r"^([A-Z]+)\d", line_part)
            if line_team_m:
                line_team = line_team_m.group(1)
                if teams_str.endswith(line_team):
                    team1 = teams_str[: -len(line_team)]
                    team2 = line_team
                elif teams_str.startswith(line_team):
                    team1 = line_team
                    team2 = teams_str[len(line_team):]
                else:
                    team1, team2 = teams_str[:3], teams_str[3:]
            elif line_part.isdigit() or re.match(r"^\d+\.?\d*$", line_part):
                team1, team2 = teams_str[:3], teams_str[3:]
            elif line_part.isalpha():
                line_team = line_part
                if teams_str.endswith(line_team):
                    team1 = teams_str[: -len(line_team)]
                    team2 = line_team
                else:
                    team1 = line_team
                    team2 = teams_str.replace(line_team, "", 1)
        else:
            team1, team2 = teams_str[:3], teams_str[3:]

        teams_label = f"{team1} vs {team2}" if team2 else team1

        # Build readable description by market type
        if mtype == "SPREAD" and line_part:
            tm = re.match(r"([A-Z]+)(\d+\.?\d*)", line_part)
            if tm:
                return f"NBA Spread | {teams_label} | {tm.group(1)} -{tm.group(2)}.5"
            return f"{sport} Spread | {teams_label}"

        elif mtype == "TOTAL" and line_part:
            return f"{sport} Total | {teams_label} | O/U {line_part}.5"

        elif mtype == "GAME" and line_part:
            return f"{sport} ML | {teams_label} | {line_part}"

        else:
            return f"{sport} {mtype} | {teams_label}"

    except Exception:
        return ticker


def _fmt_signed(val: float) -> str:
    """Format as signed dollar amount: -$1.23 or +$1.23 (sign before $)."""
    prefix = "+" if val >= 0 else "-"
    return f"{prefix}${abs(val):,.2f}"


def _local_portfolio_value() -> float:
    """Estimate current portfolio value for demo/local mode using latest KalshiSnapshot mids."""
    from src.storage import KalshiSnapshot as _KS
    with session_scope() as _s:
        open_trades = _s.execute(
            select(SimulatedTrade).where(SimulatedTrade.status == "open")
        ).scalars().all()
        if not open_trades:
            return 0.0
        total = 0.0
        for t in open_trades:
            latest = _s.execute(
                select(_KS)
                .where(_KS.market_ticker == t.market_ticker)
                .order_by(_KS.ts.desc())
                .limit(1)
            ).scalar_one_or_none()
            if latest is not None and latest.mid is not None:
                mid = latest.mid / 100.0
                cur_price = mid if t.side == "YES" else (1.0 - mid)
            else:
                cur_price = t.entry_price
            total += cur_price * t.size
    return total


def _markets_to_scanner_df(sports_markets: list[dict]) -> pd.DataFrame:
    """Build a scanner DataFrame directly from Kalshi market listing."""
    rows = []
    for m in sports_markets:
        try:
            yb = float(m.get("yes_bid_dollars") or 0)
            ya = float(m.get("yes_ask_dollars") or 0)
            if ya == 0 and yb == 0:
                continue
            mid = round((yb + ya) / 2 * 100, 1) if ya > 0 and yb > 0 else round((yb or ya) * 100, 1)
            spread = round((ya - yb) * 100, 1) if ya > 0 and yb > 0 else None
            rows.append({
                "ticker": m["ticker"],
                "market": _parse_kalshi_ticker(m["ticker"]),
                "kalshi_mid": mid,
                "spread": spread,
                "edge_pp": None,
                "reason": None,
                "passed": False,
            })
        except Exception:
            continue
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _latest_signals_by_ticker(lookback_minutes: int = 10) -> dict:
    """Return {ticker: Signal row} for the most recent signal per market in the last N minutes."""
    from datetime import datetime as _dt, timedelta as _td
    cutoff = _dt.utcnow() - _td(minutes=lookback_minutes)
    with session_scope() as s:
        rows = s.execute(
            select(Signal).where(Signal.ts >= cutoff).order_by(Signal.ts.desc())
        ).scalars().all()
    seen: dict = {}
    for r in rows:
        if r.market_ticker not in seen:
            seen[r.market_ticker] = r
    return seen


def _read_trades(view: str) -> pd.DataFrame:
    Model = BacktestTrade if view == "Backtest" else SimulatedTrade
    with session_scope() as s:
        rows = (
            s.execute(select(Model).order_by(Model.open_ts.desc()).limit(100))
            .scalars()
            .all()
        )
        return pd.DataFrame(
            [
                {
                    "open_ts": r.open_ts,
                    "close_ts": r.close_ts,
                    "market": _parse_kalshi_ticker(r.market_ticker),
                    "ticker": r.market_ticker,
                    "side": r.side,
                    "size": r.size,
                    "fill": round(r.entry_price, 4),
                    "edge_pp": round(r.edge_at_entry_pp, 2),
                    "status": r.status,
                    "exit": round(r.exit_price, 4) if r.exit_price is not None else None,
                    "pnl": round(r.realized_pnl, 2) if r.realized_pnl is not None else None,
                    "close_reason": r.close_reason,
                }
                for r in rows
            ]
        )


def _read_signals(view: str) -> pd.DataFrame:
    Model = BacktestSignal if view == "Backtest" else Signal
    with session_scope() as s:
        rows = s.execute(select(Model).order_by(Model.ts.desc()).limit(2000)).scalars().all()
        return pd.DataFrame(
            [
                {
                    "ts": r.ts,
                    "market": _parse_kalshi_ticker(r.market_ticker),
                    "game": r.game_id,
                    "side": r.side,
                    "edge_pp": round(r.edge_pp, 2),
                    "kalshi_mid": round(r.kalshi_mid, 1),
                    "consensus": round(r.consensus_prob * 100, 1),
                    "spread": r.spread_cents,
                    "top_size": r.top_of_book_size,
                    "passed": r.passed_filters,
                    "reason": r.reason,
                }
                for r in rows
            ]
        )


# ---------------------------------------------------------------------------
# Top metrics row — data source depends on view + session mode
# ---------------------------------------------------------------------------
session_mode = _session_mode() if view == "Live" else "backtest"
use_kalshi_source = session_mode in ("live_orders", "live") and view == "Live"

if use_kalshi_source:
    # ── All data from Kalshi API ─────────────────────────────────────────
    kd = _kalshi_live_data()
    kalshi_ok = kd["error"] is None and kd["balance"] is not None

    if kalshi_ok:
        balance = kd["balance"]
        pv = kd["portfolio_value"] or 0.0
        mps = [p for p in kd["market_positions"] if float(p.get("position_fp", 0)) != 0]
        fills = kd["fills"]
        initial_bal = _initial_balance_dollars()

        # 1. Cash Available
        display_cash = balance
        cash_sub = "Kalshi sandbox — live"
        cash_color = "#000000"

        # 2. Positions (cost deployed in open orders)
        cost_basis = sum(float(p.get("market_exposure_dollars", 0)) for p in mps)
        open_count = len(mps)
        deployed_sub = f"across {open_count} open position{'s' if open_count != 1 else ''}"

        # 3. Total P&L
        realized_pnl = sum(float(p.get("realized_pnl_dollars", 0)) for p in mps)
        unrealized_pnl = pv - cost_basis
        total_pnl = realized_pnl + unrealized_pnl
        pnl_label = "Total P&L"
        pnl_sub = f"realized {_fmt_signed(realized_pnl)}  |  unrealized {_fmt_signed(unrealized_pnl)}"

        # 4. P&L chart — rolling 2-hour window
        from datetime import datetime as _dtnow, timedelta as _td
        _chart_start = _dtnow.utcnow() - _td(hours=2)
        with session_scope() as _s:
            from sqlalchemy import select as _sel
            _local_rows = _s.execute(
                _sel(PnLTimeseries)
                .where(PnLTimeseries.ts >= _chart_start)
                .order_by(PnLTimeseries.ts)
            ).scalars().all()

        if _local_rows:
            pnl_df = pd.DataFrame([
                {"ts": r.ts, "realized": r.realized_pnl,
                 "unrealized": r.unrealized_pnl, "total": r.total_pnl}
                for r in _local_rows
            ])
        else:
            pnl_df = pd.DataFrame(columns=["ts", "realized", "unrealized", "total"])


        _window_start = _dtnow.utcnow() - _td(minutes=30)
        _anchor = pd.DataFrame([{
            "ts": _window_start,
            "realized": 0.0, "unrealized": 0.0, "total": 0.0,
        }])

        _now_row = pd.DataFrame([{
            "ts": _dtnow.utcnow(),
            "realized": realized_pnl,
            "unrealized": unrealized_pnl,
            "total": total_pnl,
        }])
        pnl_df = pd.concat([_anchor, pnl_df, _now_row], ignore_index=True)

        # 5. Market scanner + current mid price map for open positions
        scanner_df_kalshi = _markets_to_scanner_df(kd["sports_markets"])
        _mid_by_ticker: dict = {}
        for _m in kd["sports_markets"]:
            _yb = float(_m.get("yes_bid_dollars") or 0)
            _ya = float(_m.get("yes_ask_dollars") or 0)
            if _ya > 0 and _yb > 0:
                _mid_by_ticker[_m["ticker"]] = (_yb + _ya) / 2
            elif _yb or _ya:
                _mid_by_ticker[_m["ticker"]] = _yb or _ya
        open_df_kalshi = _positions_to_df(mps, fills)
        trades_df = open_df_kalshi
        closed_df_kalshi = _closed_trades_df(fills, kd["settlements"])
    else:
        use_kalshi_source = False

if not use_kalshi_source:
    _mid_by_ticker = {}
    # ── Local SQLite data (demo / backtest / no Kalshi connection) ──────
    trades_df = _read_trades(view)
    pnl_df = _read_pnl_timeseries(view)

    closed_mask = trades_df["status"] == "closed" if not trades_df.empty else pd.Series(dtype=bool)

    if view == "Backtest":
        local_realized = trades_df["pnl"].sum() if not trades_df.empty else 0.0
    else:
        local_realized = trades_df.loc[closed_mask, "pnl"].sum() if not trades_df.empty else 0.0

    if view == "Backtest":
        open_count = 0
        n_closed_local = len(trades_df) if not trades_df.empty else 0
        locked = 0.0
    else:
        open_count = int((trades_df["status"] == "open").sum()) if not trades_df.empty else 0
        n_closed_local = int(closed_mask.sum()) if not trades_df.empty else 0
        open_rows = trades_df[trades_df["status"] == "open"] if not trades_df.empty else pd.DataFrame()
        locked = (open_rows["fill"] * open_rows["size"]).sum() if not open_rows.empty else 0.0
    cost_basis = locked
    display_cash = config.BANKROLL - locked + local_realized
    cash_sub = "Backtest Mode - not live data"
    cash_color = "#000000"
    realized_pnl = local_realized
    unrealized_pnl = pnl_df["unrealized"].iloc[-1] if not pnl_df.empty else 0.0
    total_pnl = realized_pnl + unrealized_pnl
    pnl_label = "Total P&L"
    pnl_sub = f"realized {_fmt_signed(realized_pnl)}  |  unrealized {_fmt_signed(unrealized_pnl)}"
    open_df_kalshi = None
    closed_df_kalshi = None
    scanner_df_kalshi = None


if not use_kalshi_source:
    balance = display_cash
    pv = cost_basis

# ── Column order: Portfolio | Cash Available | Value of Positions | Total P&L | Open Count
col1, col2, col3, col4, col5 = st.columns(5)
with col1:
    # Total portfolio value = cash balance + current market value of positions
    portfolio_total = (balance + pv) if use_kalshi_source else (display_cash + cost_basis)
    portfolio_sub = "Cash Available + Value of Positions"
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">Portfolio</div>'
        f'<div class="metric-value">${portfolio_total:,.2f}</div>'
        f'<div style="color:#6B7C93;font-size:11px;margin-top:2px;">{portfolio_sub}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
with col2:
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">Cash Available</div>'
        f'<div class="metric-value" style="color:{cash_color}">${display_cash:,.2f}</div>'
        f'<div style="color:#6B7C93;font-size:11px;margin-top:2px;">{cash_sub}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
with col3:
    cur_val = pv if use_kalshi_source else (0.0 if view == "Backtest" else _local_portfolio_value())
    cur_val_color = "#1F8E3D" if cur_val > cost_basis else "#C62828" if cur_val != 0 else "#000000"
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">Value of Positions</div>'
        f'<div style="color:{cur_val_color}" class="metric-value">${cur_val:,.2f}</div>'
        f'<div style="color:#6B7C93;font-size:11px;margin-top:2px;">'
        f'Total Initial Cost: ${cost_basis:,.2f}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
with col4:
    pnl_color = "#1F8E3D" if total_pnl >= 0 else "#C62828"
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">{pnl_label}</div>'
        f'<div class="metric-value" style="color:{pnl_color}">{_fmt_signed(total_pnl)}</div>'
        f'<div style="color:#6B7C93;font-size:11px;margin-top:2px;">{pnl_sub}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
with col5:
    n_closed_metric = len(closed_df_kalshi) if closed_df_kalshi is not None else n_closed_local
    st.markdown(
        f'<div class="metric-card"><div class="metric-label">Positions</div>'
        f'<div class="metric-value">{int(open_count)} open</div>'
        f'<div style="color:{MUTED};font-size:11px;margin-top:2px;">{n_closed_metric} closed</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

st.write("")  # spacer

# ---------------------------------------------------------------------------
# P&L chart
# ---------------------------------------------------------------------------
st.subheader("Cumulative P&L")
if pnl_df.empty or len(pnl_df) < 1:
    st.info("No P&L data yet. Start the logger to see live data, or click Run Backtest.")
else:
    import numpy as _np
    from datetime import datetime as _dtnow2, timedelta as _td2
    from zoneinfo import ZoneInfo as _ZI

    _ct = _ZI("America/Chicago")
    _pnl_disp = pnl_df.copy()
    _pnl_disp["ts"] = (
        pd.to_datetime(_pnl_disp["ts"])
        .dt.tz_localize("UTC")
        .dt.tz_convert(_ct)
        .dt.tz_localize(None)
    )

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=_pnl_disp["ts"],
            y=_pnl_disp["total"],
            mode="lines+markers" if len(_pnl_disp) <= 5 else "lines",
            name="Total P&L",
            line=dict(color="white", width=2),
            marker=dict(size=6, color="white"),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=_pnl_disp["ts"],
            y=_pnl_disp["realized"],
            mode="lines",
            name="Realized",
            line=dict(color="rgba(255,255,255,0.5)", width=1, dash="dot"),
        )
    )

    # X-axis 
    _now2 = _dtnow2.now(_ct).replace(tzinfo=None)
    if use_kalshi_source and not _pnl_disp.empty:
        _x_min = min(_pnl_disp["ts"].min(), _now2 - _td2(minutes=30))
        _x_max = _now2 + _td2(minutes=5)
        _xrange = [_x_min, _x_max]
    else:
        _xrange = None

    # Y-axis
    import math as _math
    _y_vals = pd.concat([_pnl_disp["total"], _pnl_disp["realized"]]).dropna()
    if len(_y_vals) >= 2:
        _lo, _hi = float(_y_vals.min()), float(_y_vals.max())
        _margin = max(abs(_hi - _lo) * 0.15, 0.10)
        _axis_lo, _axis_hi = _lo - _margin, _hi + _margin
        _range = _axis_hi - _axis_lo if _axis_hi > _axis_lo else 1.0
        _mag = 10 ** _math.floor(_math.log10(_range / 6))
        _step = next(
            (m * _mag for m in [1, 2, 2.5, 5, 10] if _range / (m * _mag) <= 7),
            10 * _mag,
        )
        _t0 = _math.floor(_axis_lo / _step) * _step
        _tickvals = []
        _v = _t0
        while _v <= _axis_hi + _step * 1e-9:
            _tickvals.append(round(_v, 10))
            _v += _step
        _dec = 2 if _step < 1 else 0
        _ticktext = [
            f"-${abs(v):,.{_dec}f}" if v < 0 else f"${v:,.{_dec}f}"
            for v in _tickvals
        ]
        _yticks = dict(tickvals=_tickvals, ticktext=_ticktext)
    else:
        _yticks = dict(tickformat=",.2f", tickprefix="$")

    fig.update_layout(
        margin=dict(l=10, r=10, t=10, b=10),
        height=380,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="white"),
        xaxis=dict(
            gridcolor="rgba(255,255,255,0.15)",
            tickfont=dict(color="white"),
            linecolor="rgba(255,255,255,0.3)",
            range=_xrange,
        ),
        yaxis=dict(
            gridcolor="rgba(255,255,255,0.15)",
            tickfont=dict(color="white"),
            linecolor="rgba(255,255,255,0.3)",
            **_yticks,
        ),
        legend=dict(orientation="h", y=1.05, x=0, font=dict(color="white")),
    )
    st.plotly_chart(fig, width='stretch')

# ---------------------------------------------------------------------------
# Open Positions + Closed Trades
# ---------------------------------------------------------------------------
left, right = st.columns(2)

with left:
    if use_kalshi_source and open_df_kalshi is not None:
        # ── Kalshi-sourced panels ──────────────────────────────────────
        st.subheader(f"Open Positions ({len(open_df_kalshi)})")
        if open_df_kalshi.empty:
            st.caption("No open positions in Kalshi.")
        else:
            _open_disp = open_df_kalshi[["market", "side", "size", "fill", "ticker"]].copy()
            _total_num = open_df_kalshi["size"] * open_df_kalshi["fill"]
            _open_disp["total cost"] = _total_num.apply(lambda x: f"${x:.2f}")

            # Compute numeric current cost and delta for styling
            def _cur_cost_num(row):
                mid = _mid_by_ticker.get(row["ticker"])
                if mid is None:
                    return None
                cur_price = mid if row["side"] == "YES" else (1.0 - mid)
                return cur_price * row["size"]

            _cur_num = _open_disp.apply(_cur_cost_num, axis=1)
            _delta_num = _cur_num - _total_num.values

            _open_disp["current cost"] = _cur_num.apply(
                lambda x: f"${x:.2f}" if pd.notna(x) else "no quote"
            )
            _open_disp["delta"] = [
                (_fmt_signed(d) if pd.notna(c) else "N/A")
                for c, d in zip(_cur_num, _delta_num)
            ]

            _open_disp["fill"] = _open_disp["fill"].apply(lambda x: f"${x:.2f}")
            _open_disp.drop(columns=["ticker"], inplace=True)
            _open_disp.rename(columns={
                "side": "position",
                "size": "# of contracts",
                "fill": "cost per contract",
            }, inplace=True)

            def _style_delta(col):
                styles = []
                for d in _delta_num:
                    if pd.isna(d):
                        styles.append("")
                    elif d > 0:
                        styles.append("color: #1F8E3D; font-weight: 600")
                    elif d < 0:
                        styles.append("color: #C62828; font-weight: 600")
                    else:
                        styles.append("")
                return styles

            st.dataframe(
                _open_disp.style.apply(_style_delta, subset=["delta"]),
                height=min(40 + len(_open_disp) * 35, 420),
                hide_index=True,
                use_container_width=True,
            )
    else:
        # ── Local DB panels — open positions (same layout as live) ─────
        if view == "Backtest":
            open_df = pd.DataFrame()
        else:
            open_df = trades_df[trades_df["status"] == "open"].copy() if not trades_df.empty else pd.DataFrame()

        st.subheader(f"Open Positions ({len(open_df)})")
        if open_df.empty:
            st.caption("No positions currently open.")
        else:
            # Fetch latest KalshiSnapshot mids for current cost column
            from src.storage import KalshiSnapshot as _KS
            from sqlalchemy import func as _func
            _tickers = open_df["ticker"].tolist()
            _snap_mids: dict = {}
            with session_scope() as _s:
                _subq = (
                    select(_KS.market_ticker, _func.max(_KS.ts).label("max_ts"))
                    .where(_KS.market_ticker.in_(_tickers))
                    .group_by(_KS.market_ticker)
                    .subquery()
                )
                for _tk, _mid in _s.execute(
                    select(_KS.market_ticker, _KS.mid)
                    .join(_subq, (_KS.market_ticker == _subq.c.market_ticker) & (_KS.ts == _subq.c.max_ts))
                ).all():
                    if _mid is not None:
                        _snap_mids[_tk] = _mid / 100.0

            _open_disp = open_df[["market", "side", "size", "fill", "ticker"]].copy()
            _total_num = open_df["size"] * open_df["fill"]
            _open_disp["total cost"] = _total_num.apply(lambda x: f"${x:.2f}")

            def _cur_cost_num_local(row):
                mid = _snap_mids.get(row["ticker"])
                if mid is None:
                    return None
                return (mid if row["side"] == "YES" else 1.0 - mid) * row["size"]

            _cur_num = _open_disp.apply(_cur_cost_num_local, axis=1)
            _delta_num = _cur_num - _total_num.values
            _open_disp["current cost"] = _cur_num.apply(lambda x: f"${x:.2f}" if pd.notna(x) else "no quote")
            _open_disp["delta"] = [(_fmt_signed(d) if pd.notna(c) else "N/A") for c, d in zip(_cur_num, _delta_num)]
            _open_disp["fill"] = _open_disp["fill"].apply(lambda x: f"${x:.2f}")
            _open_disp.drop(columns=["ticker"], inplace=True)
            _open_disp.rename(columns={"side": "position", "size": "# of contracts", "fill": "cost per contract"}, inplace=True)

            def _style_delta_local(col):
                return ["color: #1F8E3D; font-weight: 600" if pd.notna(d) and d > 0
                        else "color: #C62828; font-weight: 600" if pd.notna(d) and d < 0
                        else "" for d in _delta_num]

            st.dataframe(
                _open_disp.style.apply(_style_delta_local, subset=["delta"]),
                height=min(40 + len(_open_disp) * 35, 420),
                hide_index=True,
                use_container_width=True,
            )

with right:
    if use_kalshi_source and closed_df_kalshi is not None:
        st.subheader(f"Closed Trades ({len(closed_df_kalshi)})")
        if closed_df_kalshi.empty:
            st.caption("No closed trades yet.")
        else:
            _closed_disp = closed_df_kalshi.copy()
            _closed_disp["entry"] = _closed_disp["entry"].apply(lambda x: f"${x:.2f}")
            _closed_disp["exit"] = _closed_disp["exit"].apply(lambda x: f"${x:.2f}")
            _closed_disp["p&l"] = _closed_disp["p&l"].apply(_fmt_signed)
            st.dataframe(
                _closed_disp,
                height=min(40 + len(_closed_disp) * 35, 420),
                hide_index=True,
                use_container_width=True,
            )
    else:
        # ── Local DB panels — closed trades (same layout as live) ──────
        if view == "Backtest":
            closed_df = trades_df.copy() if not trades_df.empty else pd.DataFrame()
        else:
            closed_df = trades_df[trades_df["status"] == "closed"].copy() if not trades_df.empty else pd.DataFrame()
        st.subheader(f"Closed Trades ({len(closed_df)})")
        if closed_df.empty:
            st.caption("No closed trades yet.")
        else:
            _cd = pd.DataFrame({
                "closed_at": closed_df["close_ts"],
                "market":    closed_df["market"],
                "position":  closed_df["side"],
                "contracts": closed_df["size"],
                "entry":     closed_df["fill"].apply(lambda x: f"${x:.2f}"),
                "exit":      closed_df["exit"].apply(lambda x: f"${x:.2f}" if pd.notna(x) else "—"),
                "p&l":       closed_df["pnl"].apply(lambda x: _fmt_signed(x) if pd.notna(x) else "—"),
                "close type": closed_df["close_reason"].fillna("settled"),
            })
            st.dataframe(
                _cd,
                height=min(40 + len(_cd) * 35, 420),
                hide_index=True,
                use_container_width=True,
            )

# ---------------------------------------------------------------------------
# Bottom row: market scanner
# ---------------------------------------------------------------------------
st.subheader("Market Scanner")

if use_kalshi_source and scanner_df_kalshi is not None and not scanner_df_kalshi.empty:
    sig_map = _latest_signals_by_ticker()
    scanner_df_kalshi = scanner_df_kalshi.copy()
    scanner_df_kalshi["edge_pp"] = scanner_df_kalshi["ticker"].map(
        lambda t: round(sig_map[t].edge_pp, 2) if t in sig_map else None
    )
    scanner_df_kalshi["status"] = scanner_df_kalshi["ticker"].map(
        lambda t: (
            ("✓ passed" if sig_map[t].passed_filters else sig_map[t].reason or "filtered")
            if t in sig_map else "no signal yet"
        )
    )

    disp = scanner_df_kalshi[["market", "kalshi_mid", "spread", "edge_pp", "status"]].rename(columns={
        "kalshi_mid": "midpoint of best bid and best ask (¢)",
        "spread": "difference between ask and bid (¢)",
        "edge_pp": "edge (pp)",
    })
    st.dataframe(disp, hide_index=True, width='stretch', height=320)
else:
    sig_df = _read_signals(view)
    if sig_df.empty:
        st.info("No signals yet. Run the logger or click Run Backtest.")
    else:
        scanner = (
            sig_df.sort_values("ts")
            .groupby("market", as_index=False)
            .tail(1)
            .sort_values("edge_pp", key=lambda s: s.abs(), ascending=False)
        )
        _scan_disp = pd.DataFrame({
            "market": scanner["market"],
            "midpoint of best bid and best ask (¢)": scanner["kalshi_mid"],
            "difference between ask and bid (¢)":   scanner["spread"],
            "edge (pp)":                             scanner["edge_pp"],
            "status": scanner.apply(
                lambda r: "✓ passed" if r["passed"] else (r["reason"] or "filtered"), axis=1
            ),
        })

        def _hl(row):
            if row["status"] == "✓ passed":
                return ["background-color: #0A2540; color: #ffffff"] * len(row)
            return [""] * len(row)

        st.dataframe(
            _scan_disp.style.apply(_hl, axis=1),
            hide_index=True,
            width='stretch',
            height=320,
        )

st.markdown("---")

# ---------------------------------------------------------------------------
# Auto-refresh tail. Only refreshes in Live view; backtest results are static.
# ---------------------------------------------------------------------------
if auto_refresh and view == "Live":
    import time
    time.sleep(2)
    st.rerun()
