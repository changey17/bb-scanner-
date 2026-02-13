"""Scraper for BookieBashing Coupons Tracker via direct API.

The Coupons Tracker monitors shop coupons for value across bookmakers.

API tables (system='coupon'):
    - coupons: List of live/past coupons with metadata
    - selections: Individual selections within coupons (odds, EV, lay)
    - coupon_types: Types of coupons (match odds, BTTS, etc.)
"""

from __future__ import annotations

import logging
import re

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

# Stats-related keywords for filtering coupons
STATS_KEYWORDS = [
    "foul", "throw", "corner", "card", "booking", "shot",
    "offside", "tackle", "free kick", "goal kick",
    "player",  # player-level stats markets
]


def _is_target_bookmaker(name: str) -> bool:
    """Check if bookmaker name matches one of our target bookmakers."""
    normalised = name.lower().strip()
    for target in TARGET_BOOKMAKERS:
        if target in normalised or normalised in target:
            return True
    return False


def _detect_market_type(text: str) -> MarketType | None:
    """Detect market type from coupon/selection name."""
    text_lower = text.lower()
    checks = [
        (["foul"], MarketType.FOULS),
        (["throw in", "throw-in", "throwin"], MarketType.THROW_INS),
        (["corner"], MarketType.CORNERS),
        (["card", "booking point", "yellow", "red card"], MarketType.CARDS),
        (["shot on target", "sot", "shots on target"], MarketType.SHOTS_ON_TARGET),
        (["shot"], MarketType.SHOTS),
        (["offside"], MarketType.OFFSIDES),
        (["tackle"], MarketType.TACKLES),
        (["pass", "passes"], MarketType.PASSES),
    ]
    for keywords, mtype in checks:
        if any(kw in text_lower for kw in keywords):
            return mtype
    return None


def _is_stats_coupon(coupon_name: str) -> bool:
    name_lower = coupon_name.lower()
    return any(kw in name_lower for kw in STATS_KEYWORDS)


def _parse_direction(text: str) -> BetDirection | None:
    text_lower = text.lower()
    if "over" in text_lower:
        return BetDirection.OVER
    if "under" in text_lower:
        return BetDirection.UNDER
    return None


def _parse_line(text: str) -> float:
    match = re.search(r"(\d+\.?\d*)", text)
    return float(match.group(1)) if match else 0.0


def _safe_float(val, default: float = 0.0) -> float:
    """Safely convert a value to float, returning default on failure."""
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


class BBCouponsScraper:
    """Scrapes coupon tracker data via BB's API."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()

    async def get_live_coupons(self, client: BBClient) -> list[dict]:
        """Fetch all live coupons."""
        records = await client.get_records(
            data_name="coupons",
            tab="coupons",
            system="coupon",
            filters="filter=live,eq,1%26filter=treble_ev,gt,1",
        )
        logger.info("Found %d live coupons", len(records))
        return records

    async def get_coupon_selections(
        self, client: BBClient, coupon_id: int
    ) -> list[dict]:
        """Fetch selections for a specific coupon."""
        return await client.get_records(
            data_name="selections",
            tab="selections",
            system="coupon",
            filters=(
                f"filter=status,eq,1"
                f"%26filter=coupon,eq,{coupon_id}"
                f"%26join=events,coupons"
                f"%26join=price_holds"
                f"%26order=ev,asc"
            ),
        )

    async def scrape_value_bets(
        self,
        client: BBClient,
        stats_only: bool = False,
        min_ev_percent: float = 2.0,
    ) -> list[ValueBet]:
        """Scrape all coupon selections and return value bets.

        Args:
            client: Authenticated BB API client
            stats_only: If True, only return football stats markets
            min_ev_percent: Minimum EV percentage above fair (e.g. 2.0 = 2% edge)
        """
        value_bets: list[ValueBet] = []
        bookmakers = await client.get_bookmakers()
        book_map = {b["id"]: b["name"] for b in bookmakers}

        coupons = await self.get_live_coupons(client)

        for coupon in coupons:
            coupon_name = coupon.get("name", "")
            coupon_id = coupon.get("id")
            book_id = coupon.get("bookid")
            bookie_name = book_map.get(book_id, str(book_id))

            # Filter: only target bookmakers
            if not _is_target_bookmaker(bookie_name):
                continue

            market_type = _detect_market_type(coupon_name)

            if stats_only and not _is_stats_coupon(coupon_name) and not market_type:
                continue

            try:
                selections = await self.get_coupon_selections(client, coupon_id)
            except Exception as e:
                logger.debug("Failed to get coupon %s selections: %s", coupon_id, e)
                continue

            for sel in selections:
                book_odds = _safe_float(sel.get("book_odds"))
                lay_price = _safe_float(sel.get("lay"))

                if book_odds <= 1.0 or lay_price <= 1.0:
                    continue

                # EV: book_odds / lay_price - 1 (lay is the fair price)
                ev_percent = (book_odds / lay_price - 1) * 100

                if ev_percent < min_ev_percent:
                    continue

                sel_name = sel.get("name", "")
                combined_text = f"{coupon_name} {sel_name}"
                direction = _parse_direction(sel_name) or _parse_direction(coupon_name)
                line = _parse_line(sel_name) or _parse_line(coupon_name)

                # Detect market from combined coupon + selection text
                sel_market = _detect_market_type(combined_text) or market_type

                event = sel.get("events") or {}
                match = Match(
                    home_team=event.get("home", sel_name),
                    away_team=event.get("away", ""),
                    league=event.get("league", ""),
                    match_id=str(sel.get("event", "")),
                )

                vb = ValueBet(
                    match=match,
                    bookmaker=bookie_name,
                    market_type=sel_market or MarketType.GOALS,
                    selection=f"{coupon_name} - {sel_name}",
                    line=line,
                    direction=direction,
                    book_odds=book_odds,
                    fair_odds=lay_price,
                    ev_percent=ev_percent,
                )
                value_bets.append(vb)

        value_bets.sort(key=lambda b: b.ev_percent, reverse=True)
        logger.info("Coupons: %d value bets found", len(value_bets))
        return value_bets
