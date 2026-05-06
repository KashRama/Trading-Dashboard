"""Entry point for the live data logger."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi RV live logger")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Replay synthetic data into the live tables (no network calls).",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=60.0,
        help="Demo-mode replay speed (synthetic seconds per wall second). Default 60.",
    )
    parser.add_argument(
        "--live-orders",
        action="store_true",
        help=(
            "Place real orders on the Kalshi sandbox when signals fire. "
            "Requires valid KALSHI_API_KEY_ID and private key in .env. "
            "Sandbox only — no production endpoints."
        ),
    )
    parser.add_argument(
        "--relax-filters",
        action="store_true",
        help=(
            "Bypass strategy thresholds (size, spread, odds-match). "
            "Use with --live-orders to prove the order pipeline fires against "
            "sandbox markets even when liquidity and market-type don't match "
            "the production strategy. Not for real trading."
        ),
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Keep existing live-table data instead of clearing on start.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.demo and args.live_orders:
        print("Error: --demo and --live-orders are mutually exclusive.")
        raise SystemExit(1)

    if args.demo:
        from src.synthetic_data import generate_demo
        from src.demo_live import run_demo
        print("Generating demo dataset (30 games, 5-minute playback)...")
        summary = generate_demo()
        print(f"  {summary['games']} games · "
              f"{summary['kalshi_snapshots']} Kalshi ticks · "
              f"{summary['odds_snapshots']} odds ticks")
        run_demo(speed=args.speed, reset=not args.no_reset)
    else:
        from src.logger import LiveLogger
        LiveLogger(
            live_orders=args.live_orders,
            relax_filters=args.relax_filters,
            reset=not args.no_reset,
        ).run_forever()


if __name__ == "__main__":
    main()
