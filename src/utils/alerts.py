"""Alert/notification system for value bets."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

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
    table.add_column("Stats", justify="center", width=18)

    for bet in sorted(bets, key=lambda b: b.ev_percent, reverse=True):
        ev_style = "bold green" if bet.ev_percent >= 5 else "bold yellow"

        # Stats validation column
        if bet.stats_supported is True:
            stats_str = f"[green]{bet.stats_avg:.1f}/90 OK[/green]"
        elif bet.stats_supported is False:
            stats_str = f"[red]{bet.stats_avg:.1f}/90 WEAK[/red]"
        elif bet.stats_avg is not None:
            stats_str = f"[dim]{bet.stats_avg:.1f}/90[/dim]"
        else:
            stats_str = "[dim]—[/dim]"

        table.add_row(
            bet.match.display_name,
            bet.match.league,
            bet.market_type.value,
            bet.selection,
            bet.bookmaker,
            f"{bet.book_odds:.2f}",
            f"{bet.fair_odds:.2f}",
            f"[{ev_style}]{bet.ev_percent:+.1f}%[/{ev_style}]",
            stats_str,
        )

    console.print(table)
    console.print(f"[bold]{len(bets)} value bet(s) found[/bold]\n")


async def send_discord_alert(bets: list[ValueBet], config: Config) -> None:
    """Send value bet alerts to Discord webhook."""
    if not config.DISCORD_WEBHOOK_URL or not bets:
        return

    embeds = []
    for bet in bets[:10]:  # Discord limits embeds
        fields = [
            {
                "name": "Market",
                "value": bet.selection,
                "inline": False,
            },
            {"name": "League", "value": bet.match.league or "—", "inline": True},
            {
                "name": "EV",
                "value": f"**{bet.ev_percent:+.1f}%**",
                "inline": True,
            },
            {
                "name": f"{bet.bookmaker} Odds (Scraped)",
                "value": f"**{bet.book_odds:.2f}**",
                "inline": True,
            },
            {
                "name": "BB Fair Odds",
                "value": f"**{bet.fair_odds:.2f}**",
                "inline": True,
            },
            {
                "name": "Edge",
                "value": f"{bet.edge:+.1f}%",
                "inline": True,
            },
        ]

        # Add kick-off time
        if bet.match.kick_off:
            now_utc = datetime.now(timezone.utc)
            delta_h = (bet.match.kick_off - now_utc).total_seconds() / 3600
            if delta_h > 0:
                ko_time = bet.match.kick_off.strftime("%H:%M UTC")
                ko_label = f"**{ko_time}**"
                if delta_h <= 24:
                    ko_label += f" (in {delta_h:.0f}h)"
                fields.append({
                    "name": "Kick Off",
                    "value": ko_label,
                    "inline": True,
                })

        # Add real stats info if available
        if bet.stats_avg is not None:
            status = "SUPPORTED" if bet.stats_supported else "WEAK"
            fields.append({
                "name": f"Player Stats ({status})",
                "value": (
                    f"Season avg: **{bet.stats_avg:.1f}/90** | {bet.stats_note}"
                ),
                "inline": False,
            })

        embeds.append(
            {
                "title": f"{bet.bookmaker} | {bet.match.display_name}",
                "color": 0x00FF00 if bet.ev_percent >= 5 else 0xFFFF00,
                "fields": fields,
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
        ko_line = ""
        if bet.match.kick_off:
            now_utc = datetime.now(timezone.utc)
            delta_h = (bet.match.kick_off - now_utc).total_seconds() / 3600
            if delta_h > 0:
                ko_time = bet.match.kick_off.strftime("%H:%M UTC")
                ko_line = f"  KO: {ko_time} ({delta_h:.0f}h)\n"
        lines.append(
            f"*{bet.match.display_name}*\n"
            f"  {bet.match.league}\n"
            f"{ko_line}"
            f"  {bet.selection}\n"
            f"  {bet.bookmaker} Odds (Scraped): `{bet.book_odds:.2f}`\n"
            f"  BB Fair Odds: `{bet.fair_odds:.2f}`\n"
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
