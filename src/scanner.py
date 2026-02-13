"""Main scanner that orchestrates scraping and comparison."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from rich.console import Console

from src.config import Config
from src.models.betting import ValueBet
from src.scrapers.bb_bet_tracker import BBBetTrackerScraper
from src.scrapers.bb_client import BBClient
from src.scrapers.bb_daily import BBDailyScraper
from src.utils.alerts import send_alerts
from src.utils.player_stats import PlayerStatsProvider

logger = logging.getLogger(__name__)
console = Console()


def _merge_value_bets(*bet_lists: list[ValueBet]) -> list[ValueBet]:
    """Merge multiple lists of value bets, deduplicating."""
    seen = set()
    merged = []
    for bets in bet_lists:
        for bet in bets:
            key = (
                bet.bookmaker.lower(),
                bet.market_type.value,
                bet.selection.lower(),
                bet.line,
                bet.book_odds,
            )
            if key not in seen:
                seen.add(key)
                merged.append(bet)
    merged.sort(key=lambda b: b.ev_percent, reverse=True)
    return merged


class Scanner:
    """Main scanner that coordinates all scraping sources and finds value bets."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.bb_daily = BBDailyScraper(self.config)
        self.bb_bet_tracker = BBBetTrackerScraper(self.config)
        self.stats_provider = PlayerStatsProvider(self.config)

    async def run_scan(self) -> list[ValueBet]:
        """Run a single scan cycle."""
        console.print(
            f"\n[bold blue]--- Scan started at "
            f"{datetime.now().strftime('%H:%M:%S')} ---[/bold blue]"
        )

        all_bets: list[list[ValueBet]] = []

        async with BBClient(self.config) as client:
            console.print(
                f"[green]Logged in as {client.username}[/green]"
            )

            # Daily Player Stats (primary source)
            stats_msg = ""
            if self.stats_provider.is_available:
                stats_msg = " (with stats validation)"
                console.print("[dim]Scanning Daily Player Stats + real stats validation...[/dim]")
            else:
                console.print("[dim]Scanning Daily Player Stats...[/dim]")
            try:
                daily_bets = await self.bb_daily.scrape_value_bets(
                    client,
                    min_ev_percent=self.config.MIN_EV_PERCENT,
                    stats_provider=self.stats_provider,
                )
                all_bets.append(daily_bets)
                stats_count = sum(1 for b in daily_bets if b.stats_supported is not None)
                console.print(
                    f"  [green]Player Stats: {len(daily_bets)} value bets"
                    f"{f' ({stats_count} stats-validated)' if stats_count else ''}[/green]"
                )
            except Exception as e:
                logger.error("Daily Player Stats failed: %s", e)
                console.print(f"  [red]Player Stats failed: {e}[/red]")

            # Bet Tracker (supplementary)
            console.print("[dim]Scanning Bet Tracker...[/dim]")
            try:
                tracker_bets = await self.bb_bet_tracker.scrape_value_bets(
                    client,
                    stats_only=False,
                    min_ev_percent=self.config.MIN_EV_PERCENT,
                )
                all_bets.append(tracker_bets)
                console.print(
                    f"  [green]Bet Tracker: {len(tracker_bets)} value bets[/green]"
                )
            except Exception as e:
                logger.error("Bet Tracker failed: %s", e)
                console.print(f"  [red]Bet Tracker failed: {e}[/red]")

        # Merge and deduplicate
        final_bets = _merge_value_bets(*all_bets)

        # Send alerts
        await send_alerts(final_bets, self.config)

        console.print(
            f"[bold blue]--- Scan complete: "
            f"{len(final_bets)} value bets ---[/bold blue]\n"
        )
        return final_bets

    async def run_continuous(self) -> None:
        """Run scanner in a continuous loop."""
        console.print(
            f"[bold]BB Scanner starting - scanning every "
            f"{self.config.SCAN_INTERVAL_SECONDS}s[/bold]"
        )
        console.print(f"[dim]Markets: {', '.join(self.config.MARKETS)}[/dim]")
        console.print(
            f"[dim]Bookmakers: Bet365, Paddy Power, Sky Bet, "
            f"William Hill, Betfred, Betfair[/dim]"
        )
        console.print(f"[dim]Min EV: {self.config.MIN_EV_PERCENT}%[/dim]")
        if self.stats_provider.is_available:
            console.print("[dim]Stats validation: ACTIVE (API-Football)[/dim]")
        else:
            console.print("[dim]Stats validation: OFF (set API_FOOTBALL_KEY to enable)[/dim]")
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
