"""Scraper for BookieBashing Bet Tracker via direct API.

The Bet Tracker surfaces +EV bets by comparing bookmaker prices to fair odds.

API tables (system='bet'):
    - bets: Main bet entries with EV, odds, lay price, status
    - user_selections: User's saved selections
    - selections_data: Cached selections data
"""

from __future__ import annotations

import logging
import re
import time

from src.config import Config
from src.models.betting import (
    BetDirection,
    MarketType,
    Match,
    ValueBet,
)
from src.scrapers.bb_client import BBClient

logger = logging.getLogger(__name__)

# Target bookmakers - normalised names to match against BB's bookmaker list
TARGET_BOOKMAKERS = {
    "bet365", "paddy power", "paddypower", "sky bet", "skybet",
    "betfair", "betfair sportsbook", "william hill", "williamhill",
    "betfred",
}

# Market type detection patterns
STATS_PATTERNS = {
    MarketType.FOULS: [r"foul", r"fouls?\s+(?:won|committed)"],
    MarketType.THROW_INS: [r"throw[\s-]?in"],
    MarketType.CORNERS: [r"corner"],
    MarketType.CARDS: [r"card", r"booking", r"yellow", r"red\s+card"],
    MarketType.BOOKINGS: [r"booking\s+point"],
    MarketType.SHOTS: [r"(?<!on\s)shots?\b"],
    MarketType.SHOTS_ON_TARGET: [r"shots?\s+on\s+target", r"\bsot\b"],
    MarketType.OFFSIDES: [r"offside"],
    MarketType.TACKLES: [r"tackle"],
    MarketType.PASSES: [r"pass(?:es)?"],
}


def _is_target_bookmaker(name: str) -> bool:
    """Check if bookmaker name matches one of our target bookmakers."""
    normalised = name.lower().strip()
    for target in TARGET_BOOKMAKERS:
        if target in normalised or normalised in target:
            return True
    return False


def _safe_float(val, default: float = 0.0) -> float:
    """Safely convert a value to float, returning default on failure."""
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _match_market_type(text: str) -> MarketType | None:
    text_lower = text.lower()
    for market_type, patterns in STATS_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text_lower):
                return market_type
    return None


def _format_selection(name: str, line: float, direction: BetDirection | None) -> str:
    """Format the selection text converting decimal lines to bookmaker notation.

    Converts: 'Over 0.5 Tackles' -> 'Over 1+ Tackles'
              'Under 1.5 Shots' -> 'Under 2+ Shots'
    """
    if line <= 0:
        return name

    # Convert line to display format
    if line == int(line) + 0.5:
        display = f"{int(line + 0.5)}+"
    elif line == int(line):
        display = str(int(line))
    else:
        display = str(line)

    # Replace the raw decimal line in the name with bookmaker notation
    # Try patterns like "0.5", "1.5", "2.5" etc.
    raw_line = f"{line:g}"
    if raw_line in name:
        return name.replace(raw_line, display, 1)

    return name


class BBBetTrackerScraper:
    """Scrapes bet tracker data via BB's API."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()

    async def get_active_bets(self, client: BBClient) -> list[dict]:
        """Fetch all active public bets from the tracker."""
        now = int(time.time())
        eighteen_hours = now + 64800

        # Fetch public bets (the main tracker view)
        records = await client.get_records(
            data_name="selections_public",
            tab="bets",
            system="bet",
            filters=(
                f"filter1=status,eq,1"
                f"%26filter1=is_private,eq,0"
                f"%26filter1=is_group,eq,0"
                f"%26filter1=ev,gt,0.8"
                f"%26filter1=ko_time,gt,{now}"
                f"%26filter1=ko_time,lt,{eighteen_hours}"
                f"%26exclude=bet_info,calc_data"
            ),
        )
        logger.info("Bet Tracker: %d active public bets", len(records))
        return records

    async def get_private_bets(self, client: BBClient) -> list[dict]:
        """Fetch private/personal bets."""
        records = await client.get_records(
            data_name="selections_private",
            tab="bets",
            system="bet",
            filters=(
                f"filter=is_private,eq,1"
                f"%26filter=credit,eq,{client.uid}"
            ),
        )
        return records

    async def get_shop_bets(self, client: BBClient) -> list[dict]:
        """Fetch shop tracker bets."""
        now = int(time.time())
        records = await client.get_records(
            data_name="selections",
            tab="bets",
            system="bet",
            filters=(
                f"filter1=status,eq,3"
                f"%26exclude=bet_info,calc_data"
            ),
        )
        return records

    async def scrape_value_bets(
        self,
        client: BBClient,
        stats_only: bool = False,
        min_ev_percent: float = 2.0,
    ) -> list[ValueBet]:
        """Scrape bet tracker for value bets.

        The bet tracker already pre-computes EV, so we extract it directly.
        """
        value_bets: list[ValueBet] = []
        bookmakers = await client.get_bookmakers()
        book_map = {b["id"]: b["name"] for b in bookmakers}

        # Get all available bets
        bets = await self.get_active_bets(client)
        shop_bets = await self.get_shop_bets(client)
        all_bets = bets + shop_bets

        for bet in all_bets:
            vb = self._parse_bet(bet, book_map)
            if vb is None:
                continue
            # Filter: only target bookmakers
            if not _is_target_bookmaker(vb.bookmaker):
                continue
            if stats_only and vb.market_type not in {
                MarketType.FOULS, MarketType.THROW_INS, MarketType.CORNERS,
                MarketType.CARDS, MarketType.BOOKINGS, MarketType.SHOTS,
                MarketType.SHOTS_ON_TARGET, MarketType.OFFSIDES,
                MarketType.TACKLES, MarketType.PASSES,
            }:
                continue
            if vb.ev_percent < min_ev_percent:
                continue
            value_bets.append(vb)

        value_bets.sort(key=lambda b: b.ev_percent, reverse=True)
        logger.info("Bet Tracker: %d value bets found", len(value_bets))
        return value_bets

    def _parse_bet(self, bet: dict, book_map: dict) -> ValueBet | None:
        """Parse a raw bet record into a ValueBet."""
        name = bet.get("name", "")
        ev_value = _safe_float(bet.get("ev"))
        if ev_value == 0.0:
            return None

        # Get odds
        book_odds = 0.0
        fair_odds = 0.0

        for key in ["book_odds", "odds", "price"]:
            if key in bet:
                val = _safe_float(bet[key])
                if val > 0:
                    book_odds = val
                    break

        for key in ["lay", "fair_odds", "exchange_odds"]:
            if key in bet:
                val = _safe_float(bet[key])
                if val > 0:
                    fair_odds = val
                    break

        if book_odds <= 1.0 or fair_odds <= 1.0:
            # Try to calculate from EV
            if book_odds > 1.0 and ev_value > 0:
                fair_odds = book_odds / (ev_value / 100)
            elif fair_odds > 1.0 and ev_value > 0:
                book_odds = fair_odds * (ev_value / 100)
            else:
                return None

        ev_percent = (book_odds / fair_odds - 1) * 100

        # Detect market type
        combined = f"{name} {bet.get('market', '')} {bet.get('selection', '')}"
        market_type = _match_market_type(combined) or MarketType.GOALS

        # Direction and line
        direction = None
        if "over" in combined.lower():
            direction = BetDirection.OVER
        elif "under" in combined.lower():
            direction = BetDirection.UNDER

        line_m = re.search(r"(\d+\.?\d*)", name)
        line = float(line_m.group(1)) if line_m else 0.0

        # Match info
        home = bet.get("home", "")
        away = bet.get("away", "")
        if not home and " v " in name:
            parts = name.split(" v ", 1)
            home, away = parts[0].strip(), parts[1].strip()

        # Bookmaker
        book_id = bet.get("bookid", bet.get("book_id", 0))
        bookie_name = book_map.get(book_id, str(book_id))

        # Format selection with bookmaker notation (0.5 -> 1+, 1.5 -> 2+)
        selection = _format_selection(name, line, direction)

        return ValueBet(
            match=Match(
                home_team=home or name,
                away_team=away or "",
                league=bet.get("league", bet.get("competition", "")),
            ),
            bookmaker=bookie_name,
            market_type=market_type,
            selection=selection,
            line=line,
            direction=direction,
            book_odds=book_odds,
            fair_odds=fair_odds,
            ev_percent=ev_percent,
        )
