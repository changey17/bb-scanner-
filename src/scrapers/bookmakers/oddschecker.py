"""OddsChecker scraper for aggregated bookmaker odds.

OddsChecker provides a comparison view of odds across all major UK bookmakers.
This is more reliable than scraping individual bookie sites directly, since
bookmakers like Bet365 have aggressive anti-bot measures.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from playwright.async_api import Page

from src.config import Config
from src.models.betting import (
    BetDirection,
    MarketType,
    Match,
    OddsEntry,
)

logger = logging.getLogger(__name__)

ODDSCHECKER_FOOTBALL_URL = "https://www.oddschecker.com/football"

# OddsChecker bookmaker column identifiers
BOOKIE_MAP = {
    "B3": "Bet365",
    "PP": "Paddy Power",
    "WH": "William Hill",
    "BF": "Betfred",
    "LD": "Ladbrokes",
    "SK": "SkyBet",
    "CR": "Coral",
    "BV": "BetVictor",
    "UN": "Unibet",
    "BW": "Betway",
    "FB": "Betfair",
    "88": "888sport",
    "BY": "Boylesports",
    "BD": "Betdaq",
    "VC": "BetVictor",
    "FR": "Betfred",
}


class OddsCheckerScraper:
    """Scrapes OddsChecker for football stats odds across bookmakers."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()

    async def scrape_match_stats_odds(
        self, page: Page, match_url: str
    ) -> list[OddsEntry]:
        """Scrape odds for stats markets from a specific match page on OddsChecker."""
        odds_list: list[OddsEntry] = []

        try:
            await page.goto(match_url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # Look for stats/specials tabs
            stats_tabs = await page.query_selector_all(
                "a[href*='corner'], a[href*='card'], a[href*='foul'], "
                "a[href*='throw'], a[href*='shot'], a[href*='booking'], "
                "[class*='tab']:has-text('Stats'), [class*='tab']:has-text('Specials')"
            )

            for tab in stats_tabs:
                text = (await tab.inner_text()).strip().lower()
                href = await tab.get_attribute("href") or ""

                # Determine market type
                market_type = None
                if "corner" in text or "corner" in href:
                    market_type = MarketType.CORNERS
                elif "card" in text or "card" in href or "booking" in text:
                    market_type = MarketType.CARDS
                elif "foul" in text or "foul" in href:
                    market_type = MarketType.FOULS
                elif "throw" in text or "throw" in href:
                    market_type = MarketType.THROW_INS
                elif "shot" in text or "shot" in href:
                    market_type = MarketType.SHOTS

                if not market_type:
                    continue

                # Click through to market page
                if href and href.startswith("http"):
                    await page.goto(href, wait_until="networkidle")
                else:
                    await tab.click()
                    await page.wait_for_timeout(2000)

                # Scrape odds from this market page
                market_odds = await self._scrape_odds_table(page, market_type)
                odds_list.extend(market_odds)

        except Exception as e:
            logger.error("OddsChecker scrape failed for %s: %s", match_url, e)

        return odds_list

    async def scrape_football_matches(self, page: Page) -> list[dict]:
        """Get list of football matches from OddsChecker."""
        matches = []
        try:
            await page.goto(ODDSCHECKER_FOOTBALL_URL, wait_until="networkidle")
            await page.wait_for_timeout(2000)

            # Find match links
            match_links = await page.query_selector_all(
                "a[href*='/football/'][class*='match'], "
                "a[href*='/football/'][class*='event'], "
                "[class*='match-link'] a, [class*='event-link'] a"
            )

            for link in match_links:
                text = (await link.inner_text()).strip()
                href = await link.get_attribute("href") or ""
                if text and href:
                    matches.append({
                        "name": text,
                        "url": href if href.startswith("http") else f"https://www.oddschecker.com{href}",
                    })

        except Exception as e:
            logger.error("Failed to get OddsChecker football matches: %s", e)

        return matches

    async def _scrape_odds_table(
        self, page: Page, market_type: MarketType
    ) -> list[OddsEntry]:
        """Scrape the odds comparison table on OddsChecker."""
        odds_list: list[OddsEntry] = []

        # OddsChecker uses a table with bookmaker columns
        # Header row has bookmaker identifiers
        table = await page.query_selector(
            "table[class*='odds'], table[class*='price'], .odds-table, #odds-table"
        )
        if not table:
            # Try the main content area
            table = await page.query_selector("table")
            if not table:
                return odds_list

        # Get bookmaker headers
        header_row = await table.query_selector("tr.eventTableHeader, tr:first-child, thead tr")
        bookmaker_columns: list[str] = []
        if header_row:
            header_cells = await header_row.query_selector_all("td, th")
            for cell in header_cells:
                # OddsChecker uses data attributes or specific classes for bookmakers
                data_bk = await cell.get_attribute("data-bk") or ""
                class_name = await cell.get_attribute("class") or ""
                bk_id = data_bk or ""

                if not bk_id:
                    # Try to extract from class
                    for key in BOOKIE_MAP:
                        if key.lower() in class_name.lower():
                            bk_id = key
                            break

                bookmaker_columns.append(BOOKIE_MAP.get(bk_id, bk_id))

        # Get data rows
        data_rows = await table.query_selector_all(
            "tr.diff-row, tr.eventTableRow, tr[class*='odds-row'], tbody tr"
        )

        for row in data_rows:
            cells = await row.query_selector_all("td")
            if not cells:
                continue

            # First cell is typically the selection name
            selection_el = cells[0]
            selection = (await selection_el.inner_text()).strip()
            if not selection:
                continue

            direction = None
            if "over" in selection.lower():
                direction = BetDirection.OVER
            elif "under" in selection.lower():
                direction = BetDirection.UNDER

            line_m = re.search(r"(\d+\.?\d*)", selection)
            line = float(line_m.group(1)) if line_m else 0.0

            # Parse odds from each bookmaker column
            for i, cell in enumerate(cells[1:], 1):
                odds_text = (await cell.inner_text()).strip()
                data_odds = await cell.get_attribute("data-odig") or ""

                # Parse fractional or decimal odds
                odds_val = self._parse_odds_value(odds_text or data_odds)
                if odds_val <= 1.0:
                    continue

                bookie_name = bookmaker_columns[i] if i < len(bookmaker_columns) else f"bookie_{i}"
                if not bookie_name or bookie_name == bookie_name:
                    # Try data attribute
                    data_bk = await cell.get_attribute("data-bk") or ""
                    if data_bk in BOOKIE_MAP:
                        bookie_name = BOOKIE_MAP[data_bk]

                odds_list.append(
                    OddsEntry(
                        bookmaker=bookie_name,
                        market_type=market_type,
                        selection=selection,
                        line=line,
                        direction=direction,
                        odds=odds_val,
                    )
                )

        return odds_list

    def _parse_odds_value(self, text: str) -> float:
        """Parse odds from text (handles fractional and decimal formats)."""
        text = text.strip()
        if not text:
            return 0.0

        # Try decimal
        try:
            val = float(text)
            return val
        except ValueError:
            pass

        # Try fractional (e.g., "5/2")
        frac_m = re.match(r"(\d+)/(\d+)", text)
        if frac_m:
            num = int(frac_m.group(1))
            den = int(frac_m.group(2))
            if den > 0:
                return (num / den) + 1.0

        return 0.0
