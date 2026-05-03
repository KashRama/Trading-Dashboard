"""Live data logger.

Long-running loop that:
  - polls Kalshi sandbox order books on KALSHI_POLL_SECONDS
  - polls The Odds API on ODDS_POLL_SECONDS (cached between fetches)
  - runs Strategy.evaluate per (market, latest odds) and writes signals
  - opens/closes simulated positions via the same Portfolio used by replay
  - writes pnl_timeseries snapshots
"""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src import config
from src.execution import Portfolio
from src.kalshi_client import KalshiClient
from src.live_orders import close_live, execute_live
from src.odds_client import OddsAPIClient
from src.storage import set_session_meta
from src.storage import (
    ExternalOddsSnapshot as ExtOddsRow,
    KalshiSnapshot,
    PnLTimeseries,
    Signal as SignalRow,
    SimulatedTrade,
    init_db,
    session_scope,
)
from src.strategy import (
    ExternalOddsSnapshot,
    KalshiMarketSnapshot,
    evaluate,
)

log = logging.getLogger("kalshi-rv-logger")


@dataclass
class _CachedOdds:
    snapshot: ExternalOddsSnapshot
    home_team: str
    away_team: str
    sport: str
    game_start_ts: datetime


def _normalize_team(name: str) -> str:
    """Light normalization to map sportsbook team names to short tickers.

    Kalshi market tickers typically embed three-letter abbreviations; The
    Odds API uses full team names. In production we'd maintain a real
    lookup table; here we match by suffix tokens as a reasonable v1.
    """
    return name.upper().split()[-1] if name else ""


class LiveLogger:
    def __init__(
        self,
        live_orders: bool = False,
        relax_filters: bool = False,
        reset: bool = True,
    ) -> None:
        init_db()
        self.live_orders = live_orders
        self.relax_filters = relax_filters
        self.kalshi = KalshiClient()
        try:
            self.odds = OddsAPIClient()
            self.have_odds = True
        except RuntimeError as e:
            log.warning(f"odds client disabled: {e}")
            self.odds = None
            self.have_odds = False

        if reset:
            self._reset_live_tables()

        self.portfolio = Portfolio()
        self._stop = False
        self._odds_cache: dict[str, _CachedOdds] = {}
        self._last_odds_fetch: Optional[datetime] = None
        self.odds_calls_total = 0

        if live_orders:
            log.info("LIVE ORDERS mode active — real orders will be placed on Kalshi sandbox")
        if relax_filters:
            log.info("RELAX FILTERS active — thresholds overridden for sandbox testing")

        # Record mode and starting balance so the dashboard knows which
        # data source to use for Cash Available and Realized P&L.
        mode = "live_orders" if live_orders else "live"
        set_session_meta("mode", mode)
        if live_orders or True:
            try:
                initial_cents = self.kalshi.get_balance().get("balance", 0)
                set_session_meta("initial_balance_cents", str(initial_cents))
                log.info(f"starting balance: ${initial_cents / 100:.2f}")
            except Exception as e:
                log.warning(f"could not fetch initial balance: {e}")

        signal.signal(signal.SIGINT, self._handle_sigint)
        signal.signal(signal.SIGTERM, self._handle_sigint)

    @staticmethod
    def _reset_live_tables() -> None:
        """Wipe live tables so a fresh session doesn't mix with stale demo data."""
        from src.storage import (
            ExternalOddsSnapshot,
            KalshiSnapshot,
            PnLTimeseries,
            Signal as SignalRow,
            SimulatedTrade,
        )
        with session_scope() as s:
            for Model in (KalshiSnapshot, SignalRow, SimulatedTrade, PnLTimeseries, ExternalOddsSnapshot):
                s.query(Model).delete()
        log.info("live tables cleared for fresh session")

    def _handle_sigint(self, signum, frame) -> None:
        log.info("shutdown signal received, draining...")
        self._stop = True

    # ---- odds -----------------------------------------------------------
    def _refresh_odds(self) -> None:
        if not self.have_odds:
            return
        self._odds_cache.clear()
        for sport in config.SPORTS:
            try:
                games = self.odds.fetch_h2h(sport)
            except Exception as e:
                log.warning(f"odds fetch failed for {sport}: {e}")
                continue
            self.odds_calls_total += 1
            for g in games:
                snap = OddsAPIClient.to_consensus_snapshot(
                    g, allowed_books=config.SPORTSBOOKS
                ) 
                if snap is None:
                    continue
                key = self._team_pair_key(g.home_team, g.away_team)
                self._odds_cache[key] = _CachedOdds(
                    snapshot=snap,
                    home_team=g.home_team,
                    away_team=g.away_team,
                    sport=g.sport_key,
                    game_start_ts=g.commence_time,
                )
                # Persist the snapshot for historical reference.
                with session_scope() as s:
                    s.add(
                        ExtOddsRow(
                            ts=snap.snapshot_ts,
                            game_id=g.game_id,
                            home_team=g.home_team,
                            away_team=g.away_team,
                            sport=g.sport_key,
                            game_start_ts=g.commence_time,
                            consensus_home_prob=snap.consensus_home_prob,
                            consensus_away_prob=snap.consensus_away_prob,
                            n_books=snap.n_books,
                            raw_json=None,
                            is_synthetic=False,
                        )
                    )
        self._last_odds_fetch = datetime.utcnow()

    @staticmethod
    def _team_pair_key(home: str, away: str) -> str:
        return f"{_normalize_team(home)}|{_normalize_team(away)}"

    # ---- kalshi side ----------------------------------------------------
    def _fetch_active_sport_markets(self) -> list[dict]:
        """Pull active Kalshi markets matching any KALSHI_SPORTS_PREFIXES."""
        try:
            resp = self.kalshi.list_markets(status="open", limit=200)
        except Exception as e:
            log.warning(f"kalshi list_markets failed: {e}")
            return []
        prefixes = config.KALSHI_SPORTS_PREFIXES
        return [
            m for m in resp.get("markets", [])
            if m.get("ticker", "").startswith(prefixes)
        ]

    @staticmethod
    def _force_signal(
        kalshi_snap: "KalshiMarketSnapshot",
        odds_snap: "ExternalOddsSnapshot",
        now: datetime,
    ) -> Optional["Signal"]:
        """Build a passing Signal regardless of thresholds.

        Used in --relax-filters mode to prove order-placement
        """
        from src.orderbook import compute_metrics
        from src.strategy import Signal as Sig
        metrics = compute_metrics(kalshi_snap.book)
        if metrics.mid is None:
            return None
        kalshi_prob = metrics.mid / 100.0
        edge_pp = (odds_snap.consensus_home_prob - kalshi_prob) * 100.0
        side = "YES" if edge_pp >= 0 else "NO"
        top_size = (
            kalshi_snap.book.asks[0].size if side == "YES" and kalshi_snap.book.asks
            else kalshi_snap.book.bids[0].size if side == "NO" and kalshi_snap.book.bids
            else 0
        )
        minutes = (kalshi_snap.game_start_ts - now).total_seconds() / 60.0
        return Sig(
            market_ticker=kalshi_snap.market_ticker,
            game_id=kalshi_snap.game_id,
            side=side,
            edge_pp=edge_pp,
            kalshi_mid=metrics.mid,
            consensus_prob=odds_snap.consensus_home_prob,
            n_books=odds_snap.n_books,
            spread_cents=metrics.spread_cents or 99.0,
            top_of_book_size=top_size,
            minutes_to_game=minutes,
            metrics=metrics,
            passed_filters=True,
            reason="relax_filters",
            ts=now,
        )

    @staticmethod
    def _sport_for_ticker(ticker: str) -> str:
        if ticker.startswith("KXNBA"):
            return "basketball_nba"
        if ticker.startswith("KXMLB"):
            return "baseball_mlb"
        if ticker.startswith("KXNCAABBGAME"):
            return "basketball_ncaab"
        return "unknown"

    # ---- close logic -------------------------------------------------------
    def _check_exits(self, now: datetime, book_by_ticker: dict) -> None:
        """Check open positions for exit conditions and close them.

        Two exit triggers (mirrors the backtest logic):
          1. Mean-reversion: edge collapsed below MEAN_REVERSION_EXIT_PP.
          2. Game start passed: market should be settling; close at mid if
             a quote is still available, otherwise hold for Kalshi resolution.
        """
        for ticker in list(self.portfolio.open_positions.keys()):
            pos = self.portfolio.open_positions.get(ticker)
            if pos is None:
                continue
            book = book_by_ticker.get(ticker)

            # --- trigger 1: game start passed ---
            if pos.game_id:
                game_start = self._game_start_for_pos(pos, now)
                if game_start and now >= game_start:
                    reason = "game_start"
                    exit_price = (book.mid_cents() / 100.0) if book and book.mid_cents() else None
                    if exit_price is None:
                        log.info(f"game started for {ticker}, no quote, holding for settlement")
                        continue
                    self._close_position(pos, book, exit_price, now, reason)
                    continue

            # --- trigger 2: mean-reversion ---
            ods_cached = self._odds_cache.get(
                self._team_pair_key(pos.game_id or "", pos.game_id or "")
            )
            if book and book.mid_cents() is not None:
                kalshi_prob = book.mid_cents() / 100.0
                if ods_cached:
                    consensus = ods_cached.snapshot.consensus_home_prob
                    edge = (consensus - kalshi_prob) * 100.0
                    if abs(edge) < config.MEAN_REVERSION_EXIT_PP:
                        mid_p = book.mid_cents() / 100.0
                        exit_p = mid_p if pos.side == "YES" else 1.0 - mid_p
                        self._close_position(pos, book, exit_p, now, "mean_reversion")

    def _game_start_for_pos(self, pos, now: datetime):
        """Look up the game start time from the positions table or odds cache."""
        from src.storage import KalshiSnapshot
        with session_scope() as s:
            row = (
                s.query(KalshiSnapshot)
                .filter_by(market_ticker=pos.market_ticker)
                .order_by(KalshiSnapshot.ts.desc())
                .first()
            )
            if row and row.game_start_ts:
                return row.game_start_ts
        return None

    def _close_position(self, pos, book, exit_price: float, now: datetime, reason: str) -> None:
        """Execute a position close: sell order (live) or simulated close."""
        ticker = pos.market_ticker

        if self.live_orders and book:
            report = close_live(self.kalshi, book, pos.side, pos.size)
            if report.order_status in ("failed",):
                log.warning(f"close failed for {ticker}: {report.message}")
                return
            exit_price = report.avg_price_dollars
            log.info(
                f"LIVE close {pos.side} x{pos.size} on {ticker} "
                f"@ ${exit_price:.4f} ({reason}) order={report.order_id}"
            )
        else:
            log.info(f"simulated close {pos.side} on {ticker} @ ${exit_price:.4f} ({reason})")

        closed = self.portfolio.close(ticker, exit_price, now, reason)
        if closed is None:
            return

        # Patch the DB row.
        with session_scope() as s:
            row = s.query(SimulatedTrade).filter_by(trade_id=closed.trade_id).first()
            if row:
                row.close_ts = closed.close_ts
                row.exit_price = closed.exit_price
                row.realized_pnl = closed.realized_pnl
                row.close_reason = closed.close_reason
                row.status = "closed"

    # ---- main loop ------------------------------------------------------
    def run_once(self) -> None:
        now = datetime.utcnow()

        # Refresh odds if stale.
        if self.have_odds and (
            self._last_odds_fetch is None
            or (now - self._last_odds_fetch).total_seconds() >= config.ODDS_POLL_SECONDS
        ):
            self._refresh_odds()

        markets = self._fetch_active_sport_markets()
        if not markets:
            log.info("no NBA/MLB sandbox markets available this cycle")

        latest_mid: dict[str, float] = {}

        # Fetch all order books first so exit checks can reference them.
        book_by_ticker: dict = {}
        for m in markets:
            try:
                book_by_ticker[m["ticker"]] = self.kalshi.fetch_orderbook(m["ticker"])
            except Exception as e:
                log.warning(f"orderbook fetch failed for {m['ticker']}: {e}")

        # Check for exits before looking for new entries.
        if self.portfolio.open_positions:
            self._check_exits(now, book_by_ticker)

        for m in markets:
            ticker = m["ticker"]
            book = book_by_ticker.get(ticker)
            if book is None:
                continue

            from src.orderbook import compute_metrics
            metrics = compute_metrics(book)
            if metrics.mid is not None:
                latest_mid[ticker] = metrics.mid

            home_team = m.get("yes_sub_title") or m.get("home_team") or ""
            away_team = m.get("no_sub_title") or m.get("away_team") or ""
            game_start_ts = self._parse_close_time(m.get("close_time"))

            # Persist snapshot.
            with session_scope() as s:
                s.add(
                    KalshiSnapshot(
                        ts=now,
                        market_ticker=ticker,
                        game_id=ticker,
                        home_team=home_team,
                        away_team=away_team,
                        sport=self._sport_for_ticker(ticker),
                        game_start_ts=game_start_ts,
                        top_bid=metrics.top_bid,
                        top_ask=metrics.top_ask,
                        mid=metrics.mid,
                        spread_cents=metrics.spread_cents,
                        depth_3_bid=metrics.depth_3_bid,
                        depth_3_ask=metrics.depth_3_ask,
                        imbalance=metrics.imbalance,
                        total_quoted_size=metrics.total_quoted_size,
                        book_json=None,
                        is_synthetic=False,
                    )
                )

            ods_cached = self._odds_cache.get(self._team_pair_key(home_team, away_team))
            if ods_cached is None and not self.relax_filters:
                continue

            if ods_cached is not None:
                odds_snap = ods_cached.snapshot
                game_start = ods_cached.game_start_ts
            else:
                from src.strategy import ExternalOddsSnapshot as EOS
                odds_snap = EOS(
                    game_id=ticker,
                    consensus_home_prob=0.50,
                    consensus_away_prob=0.50,
                    fair_home_per_book=[0.50, 0.50, 0.50],
                    n_books=3,
                    snapshot_ts=now,
                )
                game_start = game_start_ts or (now + __import__("datetime").timedelta(hours=2))

            kalshi_snap = KalshiMarketSnapshot(
                market_ticker=ticker,
                game_id=ticker,
                home_team=home_team,
                away_team=away_team,
                sport=self._sport_for_ticker(ticker),
                game_start_ts=game_start,
                book=book,
                snapshot_ts=now,
            )

            if self.relax_filters:
                sig = self._force_signal(kalshi_snap, odds_snap, now)
            else:
                sig = evaluate(kalshi_snap, odds_snap, now)
            if sig is None:
                continue
            with session_scope() as s:
                s.add(
                    SignalRow(
                        ts=sig.ts,
                        market_ticker=sig.market_ticker,
                        game_id=sig.game_id,
                        side=sig.side,
                        edge_pp=sig.edge_pp,
                        kalshi_mid=sig.kalshi_mid,
                        consensus_prob=sig.consensus_prob,
                        n_books=sig.n_books,
                        spread_cents=sig.spread_cents,
                        top_of_book_size=sig.top_of_book_size,
                        minutes_to_game=sig.minutes_to_game,
                        passed_filters=sig.passed_filters,
                        reason=sig.reason,
                    )
                )
            if sig.passed_filters and sig.market_ticker not in self.portfolio.open_positions:
                if self.live_orders:
                    # --- Real order on Kalshi sandbox ----------------------
                    report = execute_live(
                        self.kalshi, book, sig.side, config.TRADE_SIZE_CONTRACTS
                    )
                    if report.filled_qty == 0 or report.order_status in ("canceled", "failed"):
                        log.warning(
                            f"live order did not fill for {sig.market_ticker}: {report.message}"
                        )
                    else:
                        # Force the portfolio to use the actual fill price.
                        pos = self.portfolio.open_from_signal(sig, book)
                        if pos is not None:
                            pos = self.portfolio.open_positions[sig.market_ticker]
                            pos.entry_price = report.avg_price_dollars
                            log.info(
                                f"LIVE order filled: {pos.side} x{report.filled_qty} "
                                f"on {pos.market_ticker} @ ${report.avg_price_dollars:.4f} "
                                f"(edge {pos.edge_at_entry_pp:+.2f}pp) "
                                f"order_id={report.order_id}"
                            )
                            with session_scope() as s:
                                s.add(
                                    SimulatedTrade(
                                        trade_id=pos.trade_id,
                                        open_ts=pos.open_ts,
                                        market_ticker=pos.market_ticker,
                                        game_id=pos.game_id,
                                        side=pos.side,
                                        size=report.filled_qty,
                                        entry_price=report.avg_price_dollars,
                                        edge_at_entry_pp=pos.edge_at_entry_pp,
                                        spread_at_entry=pos.spread_at_entry,
                                        depth_3_bid_at_entry=pos.depth_3_bid_at_entry,
                                        depth_3_ask_at_entry=pos.depth_3_ask_at_entry,
                                        consensus_prob_at_entry=pos.consensus_prob_at_entry,
                                        status="open",
                                        kalshi_order_id=report.order_id,
                                        order_status=report.order_status,
                                    )
                                )
                else:
                    # --- Simulated execution --------------------------------
                    pos = self.portfolio.open_from_signal(sig, book)
                    if pos is not None:
                        log.info(
                            f"opened {pos.side} x{pos.size} on {pos.market_ticker} "
                            f"@ ${pos.entry_price:.4f} (edge {pos.edge_at_entry_pp:+.2f}pp)"
                        )
                        with session_scope() as s:
                            s.add(
                                SimulatedTrade(
                                    trade_id=pos.trade_id,
                                    open_ts=pos.open_ts,
                                    market_ticker=pos.market_ticker,
                                    game_id=pos.game_id,
                                    side=pos.side,
                                    size=pos.size,
                                    entry_price=pos.entry_price,
                                    edge_at_entry_pp=pos.edge_at_entry_pp,
                                    spread_at_entry=pos.spread_at_entry,
                                    depth_3_bid_at_entry=pos.depth_3_bid_at_entry,
                                    depth_3_ask_at_entry=pos.depth_3_ask_at_entry,
                                    consensus_prob_at_entry=pos.consensus_prob_at_entry,
                                    status="open",
                                    order_status="simulated",
                                )
                            )

        if self.live_orders:
            try:
                bal_resp = self.kalshi.get_balance()
                k_pv = bal_resp.get("portfolio_value", 0) / 100.0
                pos_resp = self.kalshi.get_positions()
                mps = pos_resp.get("market_positions", [])
                realized = sum(float(p.get("realized_pnl_dollars", 0)) for p in mps)
                cost_basis = sum(float(p.get("market_exposure_dollars", 0)) for p in mps)
                unrealized = k_pv - cost_basis
                with session_scope() as s:
                    s.add(PnLTimeseries(
                        ts=now,
                        realized_pnl=realized,
                        unrealized_pnl=unrealized,
                        total_pnl=realized + unrealized,
                        open_positions=len(mps),
                    ))
                log.debug(
                    f"PnL (Kalshi): realized=${realized:+.2f} "
                    f"unrealized=${unrealized:+.2f} "
                    f"total=${realized + unrealized:+.2f}"
                )
            except Exception as e:
                log.warning(f"Kalshi P&L fetch failed, using local Portfolio: {e}")
                unreal = self.portfolio.total_unrealized(latest_mid)
                with session_scope() as s:
                    s.add(PnLTimeseries(
                        ts=now,
                        realized_pnl=self.portfolio.realized_pnl,
                        unrealized_pnl=unreal,
                        total_pnl=self.portfolio.realized_pnl + unreal,
                        open_positions=len(self.portfolio.open_positions),
                    ))
        else:
            unreal = self.portfolio.total_unrealized(latest_mid)
            with session_scope() as s:
                s.add(PnLTimeseries(
                    ts=now,
                    realized_pnl=self.portfolio.realized_pnl,
                    unrealized_pnl=unreal,
                    total_pnl=self.portfolio.realized_pnl + unreal,
                    open_positions=len(self.portfolio.open_positions),
                ))

    def run_forever(self) -> None:
        log.info("logger starting")
        while not self._stop:
            t0 = time.time()
            try:
                self.run_once()
            except Exception as e:
                log.exception(f"loop iteration crashed: {e}")
            elapsed = time.time() - t0
            sleep_for = max(1.0, config.KALSHI_POLL_SECONDS - elapsed)
            for _ in range(int(sleep_for)):
                if self._stop:
                    break
                time.sleep(1)
        log.info(f"logger stopped. odds api calls: {self.odds_calls_total}")
        self.kalshi.close()
        if self.odds:
            self.odds.close()

    @staticmethod
    def _parse_close_time(s: Optional[str]) -> Optional[datetime]:
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s).replace(tzinfo=None)
        except ValueError:
            return None


def main(live_orders: bool = False) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    LiveLogger(live_orders=live_orders).run_forever()
