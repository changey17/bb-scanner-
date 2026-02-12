"""Main scanner that orchestrates scraping and comparison."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from rich.console import Console

from src.config import Config
from src.engine import ComparisonEngine, merge_value_bets
from src.models.betting import FairOdds, OddsEntry, ValueBet
from src.scrapers.bb_auth import BBAuth
from src.scrapers.bb_bet_tracker import BBBetTrackerScraper
from src.scrapers.bb_betbuilder import BBBetBuilderScraper
from src.scrapers.bb_coupons import BBCouponsScraper
from src.scrapers.bookmakers.oddschecker import OddsCheckerScraper
from src.utils.alerts import send_alerts
from src.utils.browser import BrowserManager

logger = logging.getLogger(__name__)
console = Console()


class Scanner:
    """Main scanner that coordinates all scraping sources and finds value bets."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.browser_mgr = BrowserManager(self.config)
        self.engine = ComparisonEngine(self.config)
        self.bb_betbuilder = BBBetBuilderScraper(self.config)
        self.bb_coupons = BBCouponsScraper(self.config)
        self.bb_bet_tracker = BBBetTrackerScraper(self.config)
        self.oddschecker = OddsCheckerScraper(self.config)

    async def run_scan(self) -> list[ValueBet]:
        """Run a single scan cycle across all sources."""
        console.print(
            f"\n[bold blue]--- Scan started at {datetime.now().strftime('%H:%M:%S')} ---[/bold blue]"
        )

        all_fair_odds: list[FairOdds] = []
        all_book_odds: list[OddsEntry] = []
        all_value_bets: list[ValueBet] = []

        async with self.browser_mgr.session() as context:
            # Step 1: Log into BookieBashing
            console.print("[dim]Logging into BookieBashing...[/dim]")
            auth = BBAuth(context, self.config)
            try:
                bb_page = await auth.login()
            except RuntimeError as e:
                console.print(f"[red]Login failed: {e}[/red]")
                return []

            console.print("[green]Logged in to BookieBashing[/green]")

            # Step 2: Scrape BB tools in sequence (reusing the same page)
            # Bet Tracker - gives us pre-computed value bets
            console.print("[dim]Scanning Bet Tracker...[/dim]")
            try:
                tracker_bets = await self.bb_bet_tracker.scrape(bb_page)
                all_value_bets.extend(tracker_bets)
                console.print(
                    f"  [green]Bet Tracker: {len(tracker_bets)} value bets[/green]"
                )
            except Exception as e:
                logger.error("Bet Tracker scrape failed: %s", e)
                console.print(f"  [red]Bet Tracker failed: {e}[/red]")

            # Coupons Tracker - great for football stats markets
            console.print("[dim]Scanning Coupons Tracker...[/dim]")
            try:
                coupon_fair, coupon_book = await self.bb_coupons.scrape(bb_page)
                all_fair_odds.extend(coupon_fair)
                all_book_odds.extend(coupon_book)
                console.print(
                    f"  [green]Coupons: {len(coupon_fair)} fair odds, "
                    f"{len(coupon_book)} book odds[/green]"
                )
            except Exception as e:
                logger.error("Coupons Tracker scrape failed: %s", e)
                console.print(f"  [red]Coupons Tracker failed: {e}[/red]")

            # BetBuilder - for combination/stats markets
            console.print("[dim]Scanning BetBuilder...[/dim]")
            try:
                games = await self.bb_betbuilder.scrape_games_list(bb_page)
                for game in games[:10]:  # Limit to first 10 games
                    try:
                        bb_fair, bb_book = await self.bb_betbuilder.scrape_game_markets(
                            bb_page, game
                        )
                        all_fair_odds.extend(bb_fair)
                        all_book_odds.extend(bb_book)
                    except Exception as e:
                        logger.debug("BetBuilder game scrape failed: %s", e)

                console.print(
                    f"  [green]BetBuilder: {len(games)} games scanned[/green]"
                )
            except Exception as e:
                logger.error("BetBuilder scrape failed: %s", e)
                console.print(f"  [red]BetBuilder failed: {e}[/red]")

            # Step 3: Optional OddsChecker scrape for additional bookmaker odds
            console.print("[dim]Scanning OddsChecker...[/dim]")
            try:
                oc_page = await context.new_page()
                matches = await self.oddschecker.scrape_football_matches(oc_page)
                for match in matches[:5]:  # Limit to first 5 matches
                    try:
                        oc_odds = await self.oddschecker.scrape_match_stats_odds(
                            oc_page, match["url"]
                        )
                        all_book_odds.extend(oc_odds)
                    except Exception as e:
                        logger.debug("OddsChecker match scrape failed: %s", e)
                await oc_page.close()
                console.print(
                    f"  [green]OddsChecker: {len(matches)} matches found[/green]"
                )
            except Exception as e:
                logger.error("OddsChecker scrape failed: %s", e)
                console.print(f"  [red]OddsChecker failed: {e}[/red]")

        # Step 4: Run comparison engine
        console.print("[dim]Comparing odds...[/dim]")
        engine_bets = self.engine.find_value_bets(all_fair_odds, all_book_odds)

        # Merge all value bets
        final_bets = merge_value_bets(all_value_bets, engine_bets)

        # Step 5: Send alerts
        await send_alerts(final_bets, self.config)

        console.print(
            f"[bold blue]--- Scan complete: {len(final_bets)} value bets ---[/bold blue]\n"
        )
        return final_bets

    async def run_continuous(self) -> None:
        """Run scanner in a continuous loop."""
        console.print(
            f"[bold]BB Scanner starting - scanning every "
            f"{self.config.SCAN_INTERVAL_SECONDS}s[/bold]"
        )
        console.print(f"[dim]Markets: {', '.join(self.config.MARKETS)}[/dim]")
        console.print(f"[dim]Bookmakers: {', '.join(self.config.BOOKMAKERS)}[/dim]")
        console.print(f"[dim]Min EV: {self.config.MIN_EV_PERCENT}%[/dim]")
        console.print()

        while True:
            try:
                await self.run_scan()
            except KeyboardInterrupt:
                console.print("\n[yellow]Scanner stopped by user[/yellow]")
                break
            except Exception as e:
                logger.error("Scan cycle failed: %s", e)
                console.print(f"[red]Scan failed: {e}[/red]")

            console.print(
                f"[dim]Next scan in {self.config.SCAN_INTERVAL_SECONDS}s "
                f"(Ctrl+C to stop)[/dim]"
            )
            try:
                await asyncio.sleep(self.config.SCAN_INTERVAL_SECONDS)
            except KeyboardInterrupt:
                console.print("\n[yellow]Scanner stopped by user[/yellow]")
                break
