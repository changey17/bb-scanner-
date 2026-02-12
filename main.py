#!/usr/bin/env python3
"""BB Scanner - Bookmaker odds scanner against BookieBashing fair odds.

Scrapes football stats markets (fouls, throw-ins, corners, cards, etc.)
from major UK bookmakers and compares against BookieBashing's fair odds
to identify +EV betting opportunities.
"""

import argparse
import asyncio
import logging
import sys

from rich.console import Console
from rich.logging import RichHandler

from src.config import Config
from src.scanner import Scanner

console = Console()


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_time=True, show_path=False)],
    )
    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BB Scanner - Find +EV football stats bets",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                    # Run continuous scanner
  python main.py --once             # Run a single scan
  python main.py --min-ev 5         # Only show bets with 5%+ EV
  python main.py --no-headless      # Show the browser window
  python main.py --markets fouls,corners  # Only scan specific markets
        """,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan and exit",
    )
    parser.add_argument(
        "--min-ev",
        type=float,
        default=None,
        help="Minimum EV percentage to display (default: from .env or 2.0)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Scan interval in seconds (default: from .env or 120)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show the browser window (useful for debugging)",
    )
    parser.add_argument(
        "--markets",
        type=str,
        default=None,
        help="Comma-separated list of markets to scan (e.g., fouls,corners,cards)",
    )
    parser.add_argument(
        "--bookmakers",
        type=str,
        default=None,
        help="Comma-separated list of bookmakers to check",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose/debug logging",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    config = Config()

    # Override config with CLI args
    if args.min_ev is not None:
        config.MIN_EV_PERCENT = args.min_ev
    if args.interval is not None:
        config.SCAN_INTERVAL_SECONDS = args.interval
    if args.no_headless:
        config.HEADLESS = False
    if args.markets:
        config.MARKETS = args.markets.split(",")
    if args.bookmakers:
        config.BOOKMAKERS = args.bookmakers.split(",")

    # Validate credentials
    if not config.BB_USERNAME or not config.BB_PASSWORD:
        console.print(
            "[red]Error: BookieBashing credentials not configured.[/red]\n"
            "Set BB_USERNAME and BB_PASSWORD in your .env file.\n"
            "See .env.example for reference."
        )
        sys.exit(1)

    # Display banner
    console.print(
        "[bold cyan]"
        "╔══════════════════════════════════════╗\n"
        "║        BB Scanner v1.0               ║\n"
        "║  Football Stats Value Bet Finder     ║\n"
        "╚══════════════════════════════════════╝"
        "[/bold cyan]"
    )

    scanner = Scanner(config)

    if args.once:
        asyncio.run(scanner.run_scan())
    else:
        asyncio.run(scanner.run_continuous())


if __name__ == "__main__":
    main()
