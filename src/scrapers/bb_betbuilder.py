"""Scraper for BookieBashing BetBuilder tool.

The BetBuilder calculates fair odds for combination bets including
cards, corners, fouls, and other football stats markets.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from playwright.async_api import Page

from src.config import Config
from src.models.betting import (
    BetDirection,
    FairOdds,
    MarketType,
    Match,
    OddsEntry,
)
from src.utils.browser import safe_get_all_text, safe_get_text

logger = logging.getLogger(__name__)

# Map BB market names to our MarketType enum
MARKET_MAP = {
    "corners": MarketType.CORNERS,
    "cards": MarketType.CARDS,
    "bookings": MarketType.BOOKINGS,
    "fouls": MarketType.FOULS,
    "throw ins": MarketType.THROW_INS,
    "throw-ins": MarketType.THROW_INS,
    "throwin": MarketType.THROW_INS,
    "shots": MarketType.SHOTS,
    "shots on target": MarketType.SHOTS_ON_TARGET,
    "offsides": MarketType.OFFSIDES,
    "goals": MarketType.GOALS,
}


def _parse_market_type(text: str) -> MarketType | None:
    """Parse a market type from text."""
    text_lower = text.lower().strip()
    for key, mtype in MARKET_MAP.items():
        if key in text_lower:
            return mtype
    return None


def _parse_direction(text: str) -> BetDirection | None:
    """Parse over/under direction from text."""
    text_lower = text.lower()
    if "over" in text_lower:
        return BetDirection.OVER
    if "under" in text_lower:
        return BetDirection.UNDER
    return None


def _parse_line(text: str) -> float:
    """Extract the numeric line from text like 'Over 22.5'."""
    match = re.search(r"(\d+\.?\d*)", text)
    if match:
        return float(match.group(1))
    return 0.0


def _parse_odds(text: str) -> float:
    """Parse decimal odds from text."""
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        # Try to find a decimal number in the text
        match = re.search(r"(\d+\.?\d+)", text)
        if match:
            return float(match.group(1))
    return 0.0


class BBBetBuilderScraper:
    """Scrapes the BookieBashing BetBuilder tool for football stats fair odds."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()

    async def scrape_games_list(self, page: Page) -> list[dict]:
        """Get list of available games from BetBuilder."""
        logger.info("Navigating to BetBuilder...")
        await page.goto(self.config.BB_BETBUILDER_URL, wait_until="networkidle")

        # Wait for the tool to load (it's JS-rendered)
        await page.wait_for_timeout(3000)

        # BetBuilder typically shows a list of games/fixtures
        # Look for game selection elements
        games = []

        # Try to find game/fixture selectors - BB uses dynamic JS rendering
        # so we need to wait for and interact with the UI
        game_elements = await page.query_selector_all(
            "[class*='game'], [class*='fixture'], [class*='match'], "
            "[data-game], [data-fixture], tr[class*='game'], "
            ".game-row, .fixture-row, .match-row"
        )

        if not game_elements:
            # Try looking for select/dropdown with games
            select_el = await page.query_selector(
                "select[class*='game'], select[class*='fixture'], "
                "select[id*='game'], select[id*='fixture']"
            )
            if select_el:
                options = await select_el.query_selector_all("option")
                for opt in options:
                    value = await opt.get_attribute("value")
                    text = (await opt.inner_text()).strip()
                    if value and text:
                        games.append({"id": value, "name": text, "element": "select"})
            else:
                # Try button/link based game selection
                links = await page.query_selector_all("a[href*='game'], button[data-game]")
                for link in links:
                    text = (await link.inner_text()).strip()
                    href = await link.get_attribute("href") or ""
                    if text:
                        games.append({"id": href, "name": text, "element": "link"})

        else:
            for el in game_elements:
                text = (await el.inner_text()).strip()
                if text:
                    games.append({"id": text, "name": text, "element": "row"})

        logger.info("Found %d games in BetBuilder", len(games))
        return games

    async def scrape_game_markets(
        self, page: Page, game_selector: dict | None = None
    ) -> tuple[list[FairOdds], list[OddsEntry]]:
        """Scrape fair odds and bookmaker odds for a specific game.

        Returns a tuple of (fair_odds_list, bookmaker_odds_list).
        """
        fair_odds_list: list[FairOdds] = []
        book_odds_list: list[OddsEntry] = []

        # If a game needs to be selected, do it
        if game_selector:
            await self._select_game(page, game_selector)
            await page.wait_for_timeout(2000)

        # Now scrape the market data from the page
        # BetBuilder shows markets in tables/grids with fair odds and bookie odds

        # Strategy 1: Look for table-based data
        tables = await page.query_selector_all("table")
        for table in tables:
            rows = await table.query_selector_all("tr")
            await self._parse_table_rows(rows, fair_odds_list, book_odds_list)

        # Strategy 2: Look for card/grid-based data
        if not fair_odds_list:
            cards = await page.query_selector_all(
                "[class*='market'], [class*='bet-row'], [class*='odds-row']"
            )
            for card in cards:
                await self._parse_market_card(card, fair_odds_list, book_odds_list)

        # Strategy 3: Extract from page JavaScript data
        if not fair_odds_list:
            await self._extract_from_js_data(page, fair_odds_list, book_odds_list)

        logger.info(
            "BetBuilder: found %d fair odds, %d bookmaker odds",
            len(fair_odds_list),
            len(book_odds_list),
        )
        return fair_odds_list, book_odds_list

    async def _select_game(self, page: Page, game_selector: dict) -> None:
        """Select a game in the BetBuilder interface."""
        if game_selector.get("element") == "select":
            select_el = await page.query_selector(
                "select[class*='game'], select[class*='fixture'], "
                "select[id*='game'], select[id*='fixture']"
            )
            if select_el:
                await select_el.select_option(value=game_selector["id"])
        elif game_selector.get("element") == "link":
            links = await page.query_selector_all("a, button")
            for link in links:
                text = (await link.inner_text()).strip()
                if text == game_selector["name"]:
                    await link.click()
                    break
        await page.wait_for_load_state("networkidle")

    async def _parse_table_rows(
        self,
        rows,
        fair_odds_list: list[FairOdds],
        book_odds_list: list[OddsEntry],
    ) -> None:
        """Parse table rows for odds data."""
        header_cells = []
        for row in rows:
            cells = await row.query_selector_all("td, th")
            cell_texts = []
            for cell in cells:
                text = (await cell.inner_text()).strip()
                cell_texts.append(text)

            if not cell_texts:
                continue

            # First row with th elements is likely the header
            ths = await row.query_selector_all("th")
            if ths:
                header_cells = cell_texts
                continue

            # Parse data rows
            if len(cell_texts) >= 3 and header_cells:
                await self._parse_odds_row(
                    header_cells, cell_texts, fair_odds_list, book_odds_list
                )

    async def _parse_odds_row(
        self,
        headers: list[str],
        cells: list[str],
        fair_odds_list: list[FairOdds],
        book_odds_list: list[OddsEntry],
    ) -> None:
        """Parse a single row of odds data."""
        selection = cells[0] if cells else ""
        market_type = _parse_market_type(selection)
        direction = _parse_direction(selection)
        line = _parse_line(selection)

        if not market_type:
            return

        for i, header in enumerate(headers):
            if i >= len(cells):
                break

            header_lower = header.lower()
            odds_val = _parse_odds(cells[i])
            if odds_val <= 1.0:
                continue

            if "fair" in header_lower or "true" in header_lower:
                fair_odds_list.append(
                    FairOdds(
                        market_type=market_type,
                        selection=selection,
                        line=line,
                        direction=direction,
                        fair_odds=odds_val,
                        source_tool="betbuilder",
                    )
                )
            elif any(
                bk in header_lower
                for bk in [
                    "bet365", "365", "paddy", "william", "hill", "betfred",
                    "ladbrokes", "lads", "skybet", "sky", "coral", "betvictor",
                    "victor", "unibet", "betway", "888",
                ]
            ):
                book_odds_list.append(
                    OddsEntry(
                        bookmaker=header.strip(),
                        market_type=market_type,
                        selection=selection,
                        line=line,
                        direction=direction,
                        odds=odds_val,
                    )
                )

    async def _parse_market_card(
        self,
        card,
        fair_odds_list: list[FairOdds],
        book_odds_list: list[OddsEntry],
    ) -> None:
        """Parse a card/div-based market display."""
        text = (await card.inner_text()).strip()
        if not text:
            return

        market_type = _parse_market_type(text)
        if not market_type:
            return

        direction = _parse_direction(text)
        line = _parse_line(text)

        # Look for odds values within the card
        odds_elements = await card.query_selector_all(
            "[class*='odds'], [class*='price'], [data-odds]"
        )
        for odds_el in odds_elements:
            odds_text = (await odds_el.inner_text()).strip()
            odds_val = _parse_odds(odds_text)
            if odds_val <= 1.0:
                continue

            class_name = await odds_el.get_attribute("class") or ""
            if "fair" in class_name.lower():
                fair_odds_list.append(
                    FairOdds(
                        market_type=market_type,
                        selection=text.split("\n")[0],
                        line=line,
                        direction=direction,
                        fair_odds=odds_val,
                        source_tool="betbuilder",
                    )
                )
            else:
                # Try to determine bookmaker from class or parent
                bookie_name = await self._determine_bookmaker(odds_el)
                book_odds_list.append(
                    OddsEntry(
                        bookmaker=bookie_name or "unknown",
                        market_type=market_type,
                        selection=text.split("\n")[0],
                        line=line,
                        direction=direction,
                        odds=odds_val,
                    )
                )

    async def _determine_bookmaker(self, element) -> str:
        """Try to determine which bookmaker an odds element belongs to."""
        # Check class names
        class_name = await element.get_attribute("class") or ""
        data_bookie = await element.get_attribute("data-bookmaker") or ""
        if data_bookie:
            return data_bookie

        class_lower = class_name.lower()
        bookmakers = {
            "bet365": "Bet365",
            "365": "Bet365",
            "paddy": "Paddy Power",
            "william": "William Hill",
            "betfred": "Betfred",
            "ladbrokes": "Ladbrokes",
            "skybet": "SkyBet",
            "coral": "Coral",
        }
        for key, name in bookmakers.items():
            if key in class_lower:
                return name

        # Check parent element
        parent = await element.query_selector("xpath=..")
        if parent:
            parent_text = (await parent.inner_text()).strip()
            for key, name in bookmakers.items():
                if key in parent_text.lower():
                    return name

        return "unknown"

    async def _extract_from_js_data(
        self,
        page: Page,
        fair_odds_list: list[FairOdds],
        book_odds_list: list[OddsEntry],
    ) -> None:
        """Try to extract odds data from JavaScript variables on the page."""
        try:
            # Many betting tools store data in JS objects
            js_data = await page.evaluate(
                """
                () => {
                    // Look for common data variable patterns
                    const possibleVars = [
                        window.betbuilderData,
                        window.gameData,
                        window.marketsData,
                        window.oddsData,
                        window.__INITIAL_DATA__,
                        window.__DATA__,
                    ];
                    for (const v of possibleVars) {
                        if (v) return JSON.stringify(v);
                    }

                    // Search for data in script tags
                    const scripts = document.querySelectorAll('script:not([src])');
                    for (const s of scripts) {
                        const text = s.textContent;
                        if (text && (text.includes('odds') || text.includes('fair'))
                            && text.includes('{')) {
                            // Try to find JSON-like data
                            const match = text.match(/(?:var|let|const)\\s+\\w+\\s*=\\s*({[^;]+})/);
                            if (match) return match[1];
                        }
                    }
                    return null;
                }
                """
            )
            if js_data:
                logger.info("Found JS data on BetBuilder page")
                # Parse the extracted data
                import json
                try:
                    data = json.loads(js_data)
                    self._parse_js_odds_data(data, fair_odds_list, book_odds_list)
                except json.JSONDecodeError:
                    logger.debug("Could not parse JS data as JSON")
        except Exception as e:
            logger.debug("JS data extraction failed: %s", e)

    def _parse_js_odds_data(
        self,
        data: dict | list,
        fair_odds_list: list[FairOdds],
        book_odds_list: list[OddsEntry],
    ) -> None:
        """Parse structured odds data from JavaScript."""
        if isinstance(data, dict):
            items = [data]
        elif isinstance(data, list):
            items = data
        else:
            return

        for item in items:
            if not isinstance(item, dict):
                continue

            # Try to extract market info
            selection = item.get("selection", item.get("name", item.get("market", "")))
            market_type = _parse_market_type(str(selection))
            if not market_type:
                continue

            direction = _parse_direction(str(selection))
            line = _parse_line(str(selection))

            # Fair odds
            fair = item.get("fair_odds", item.get("fairOdds", item.get("true_odds")))
            if fair:
                fair_odds_list.append(
                    FairOdds(
                        market_type=market_type,
                        selection=str(selection),
                        line=line,
                        direction=direction,
                        fair_odds=float(fair),
                        source_tool="betbuilder",
                    )
                )

            # Bookmaker odds
            for key, val in item.items():
                if key.lower() in [
                    "bet365", "paddypower", "williamhill", "betfred",
                    "ladbrokes", "skybet", "coral", "betvictor",
                ]:
                    try:
                        book_odds_list.append(
                            OddsEntry(
                                bookmaker=key,
                                market_type=market_type,
                                selection=str(selection),
                                line=line,
                                direction=direction,
                                odds=float(val),
                            )
                        )
                    except (ValueError, TypeError):
                        pass
