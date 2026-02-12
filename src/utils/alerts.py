"""Alert/notification system for value bets."""

from __future__ import annotations

import json
import logging
from datetime import datetime

import httpx
from rich.console import Console
from rich.table import Table

from src.config import Config
from src.models.betting import ValueBet

logger = logging.getLogger(__name__)
console = Console()


def print_value_bets(bets: list[ValueBet]) -> None:
    """Print value bets to console using rich tables."""
    if not bets:
        console.print("[dim]No value bets found in this scan.[/dim]")
        return

    table = Table(
        title=f"Value Bets Found - {datetime.now().strftime('%H:%M:%S')}",
        show_lines=True,
    )
    table.add_column("Match", style="cyan", width=30)
    table.add_column("League", style="dim", width=15)
    table.add_column("Market", style="yellow", width=20)
    table.add_column("Selection", style="white", width=25)
    table.add_column("Bookmaker", style="green", width=12)
    table.add_column("Book Odds", justify="right", style="bold green")
    table.add_column("Fair Odds", justify="right", style="bold")
    table.add_column("EV%", justify="right", style="bold magenta")

    for bet in sorted(bets, key=lambda b: b.ev_percent, reverse=True):
        ev_style = "bold green" if bet.ev_percent >= 5 else "bold yellow"
        table.add_row(
            bet.match.display_name,
            bet.match.league,
            bet.market_type.value,
            bet.selection,
            bet.bookmaker,
            f"{bet.book_odds:.2f}",
            f"{bet.fair_odds:.2f}",
            f"[{ev_style}]{bet.ev_percent:+.1f}%[/{ev_style}]",
        )

    console.print(table)
    console.print(f"[bold]{len(bets)} value bet(s) found[/bold]\n")


async def send_discord_alert(bets: list[ValueBet], config: Config) -> None:
    """Send value bet alerts to Discord webhook."""
    if not config.DISCORD_WEBHOOK_URL or not bets:
        return

    embeds = []
    for bet in bets[:10]:  # Discord limits embeds
        direction_str = f" {bet.direction.value.upper()}" if bet.direction else ""
        embeds.append(
            {
                "title": f"Value Bet: {bet.match.display_name}",
                "color": 0x00FF00 if bet.ev_percent >= 5 else 0xFFFF00,
                "fields": [
                    {"name": "League", "value": bet.match.league, "inline": True},
                    {
                        "name": "Market",
                        "value": f"{bet.market_type.value}{direction_str} {bet.line}",
                        "inline": True,
                    },
                    {"name": "Selection", "value": bet.selection, "inline": False},
                    {
                        "name": "Bookmaker",
                        "value": f"{bet.bookmaker} @ {bet.book_odds:.2f}",
                        "inline": True,
                    },
                    {
                        "name": "Fair Odds",
                        "value": f"{bet.fair_odds:.2f}",
                        "inline": True,
                    },
                    {
                        "name": "EV",
                        "value": f"{bet.ev_percent:+.1f}%",
                        "inline": True,
                    },
                ],
                "timestamp": bet.timestamp.isoformat(),
            }
        )

    payload = {"content": f"**{len(bets)} Value Bet(s) Found**", "embeds": embeds}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(config.DISCORD_WEBHOOK_URL, json=payload)
            resp.raise_for_status()
            logger.info("Discord alert sent for %d bets", len(bets))
    except Exception as e:
        logger.error("Failed to send Discord alert: %s", e)


async def send_telegram_alert(bets: list[ValueBet], config: Config) -> None:
    """Send value bet alerts to Telegram."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID or not bets:
        return

    lines = [f"*{len(bets)} Value Bet(s) Found*\n"]
    for bet in bets:
        direction_str = f" {bet.direction.value.upper()}" if bet.direction else ""
        lines.append(
            f"*{bet.match.display_name}*\n"
            f"  {bet.match.league}\n"
            f"  {bet.market_type.value}{direction_str} {bet.line}\n"
            f"  {bet.selection}\n"
            f"  {bet.bookmaker} @ `{bet.book_odds:.2f}` | Fair: `{bet.fair_odds:.2f}`\n"
            f"  EV: *{bet.ev_percent:+.1f}%*\n"
        )

    text = "\n".join(lines)
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json={
                    "chat_id": config.TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "Markdown",
                },
            )
            resp.raise_for_status()
            logger.info("Telegram alert sent for %d bets", len(bets))
    except Exception as e:
        logger.error("Failed to send Telegram alert: %s", e)


async def send_alerts(bets: list[ValueBet], config: Config) -> None:
    """Send alerts through all configured channels."""
    print_value_bets(bets)
    await send_discord_alert(bets, config)
    await send_telegram_alert(bets, config)
