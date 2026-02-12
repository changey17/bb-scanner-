"""Scraper for BookieBashing Coupons Tracker.

The Coupons Tracker monitors shop coupons for value, pricing up markets
not available on exchanges (e.g., fouls, throw-ins, goal minutes).
This is a primary source for football stats markets.
"""

from __future__ import annotations

import json
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

logger = logging.getLogger(__name__)

# Football stats market keywords that appear in coupon names
STATS_KEYWORDS = [
    "foul", "throw", "corner", "card", "booking", "shot",
    "offside", "goal kick", "free kick",
]


class BBCouponsScraper:
    """Scrapes the BookieBashing Coupons Tracker for football stats value bets."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()

    async def scrape(self, page: Page) -> tuple[list[FairOdds], list[OddsEntry]]:
        """Scrape coupons tracker for football stats markets.

        Returns (fair_odds_list, bookmaker_odds_list).
        """
        logger.info("Navigating to Coupons Tracker...")
        await page.goto(self.config.BB_COUPONS_TRACKER_URL, wait_until="networkidle")
        await page.wait_for_timeout(5000)  # JS-heavy, give it time

        fair_odds_list: list[FairOdds] = []
        book_odds_list: list[OddsEntry] = []

        # Intercept API responses that contain odds data
        # BB tools often fetch data via XHR/fetch calls
        captured_data = await self._capture_api_data(page)
        if captured_data:
            self._parse_api_data(captured_data, fair_odds_list, book_odds_list)

        # Also try DOM scraping
        await self._scrape_dom(page, fair_odds_list, book_odds_list)

        # Filter to only football stats markets
        fair_odds_list = [
            fo for fo in fair_odds_list
            if fo.market_type in {
                MarketType.FOULS, MarketType.THROW_INS, MarketType.CORNERS,
                MarketType.CARDS, MarketType.BOOKINGS, MarketType.SHOTS,
                MarketType.SHOTS_ON_TARGET, MarketType.OFFSIDES,
            }
        ]
        book_odds_list = [
            bo for bo in book_odds_list
            if bo.market_type in {
                MarketType.FOULS, MarketType.THROW_INS, MarketType.CORNERS,
                MarketType.CARDS, MarketType.BOOKINGS, MarketType.SHOTS,
                MarketType.SHOTS_ON_TARGET, MarketType.OFFSIDES,
            }
        ]

        logger.info(
            "Coupons Tracker: %d fair odds, %d bookmaker odds (stats markets)",
            len(fair_odds_list),
            len(book_odds_list),
        )
        return fair_odds_list, book_odds_list

    async def _capture_api_data(self, page: Page) -> list[dict]:
        """Try to capture data from API calls the page makes."""
        captured = []

        try:
            # Evaluate page for existing loaded data
            data = await page.evaluate(
                """
                () => {
                    const results = [];

                    // Check for data in common JS patterns
                    const vars = [
                        window.couponsData,
                        window.trackerData,
                        window.couponBets,
                        window.__TRACKER_DATA__,
                    ];
                    for (const v of vars) {
                        if (v) results.push(v);
                    }

                    // Check for data rendered in the DOM as JSON
                    const dataEls = document.querySelectorAll(
                        '[data-bets], [data-coupons], [data-tracker]'
                    );
                    for (const el of dataEls) {
                        for (const attr of el.attributes) {
                            if (attr.name.startsWith('data-') && attr.value.startsWith('{')) {
                                try {
                                    results.push(JSON.parse(attr.value));
                                } catch {}
                            }
                        }
                    }

                    // Look for React/Vue data stores
                    const appEl = document.querySelector('#app, #root, [data-reactroot]');
                    if (appEl && appEl.__vue__) {
                        const vm = appEl.__vue__;
                        if (vm.$data) results.push(vm.$data);
                    }

                    return results;
                }
                """
            )
            if data:
                captured.extend(data if isinstance(data, list) else [data])
        except Exception as e:
            logger.debug("API data capture failed: %s", e)

        return captured

    async def _scrape_dom(
        self,
        page: Page,
        fair_odds_list: list[FairOdds],
        book_odds_list: list[OddsEntry],
    ) -> None:
        """Scrape odds data directly from the DOM."""
        # Look for tracker tables
        tables = await page.query_selector_all("table")
        for table in tables:
            await self._parse_tracker_table(table, fair_odds_list, book_odds_list)

        # Look for card/row-based layouts
        rows = await page.query_selector_all(
            "[class*='coupon'], [class*='tracker-row'], [class*='bet-row'], "
            "[class*='value-bet'], [class*='ev-row']"
        )
        for row in rows:
            await self._parse_coupon_row(row, fair_odds_list, book_odds_list)

    async def _parse_tracker_table(
        self,
        table,
        fair_odds_list: list[FairOdds],
        book_odds_list: list[OddsEntry],
    ) -> None:
        """Parse a tracker results table."""
        headers = []
        rows = await table.query_selector_all("tr")

        for row in rows:
            ths = await row.query_selector_all("th")
            if ths:
                headers = [(await th.inner_text()).strip() for th in ths]
                continue

            tds = await row.query_selector_all("td")
            if not tds:
                continue

            cells = [(await td.inner_text()).strip() for td in tds]
            if len(cells) < 3:
                continue

            # Try to parse this row
            self._parse_row_data(headers, cells, fair_odds_list, book_odds_list)

    def _parse_row_data(
        self,
        headers: list[str],
        cells: list[str],
        fair_odds_list: list[FairOdds],
        book_odds_list: list[OddsEntry],
    ) -> None:
        """Parse a data row from coupons tracker."""
        # Identify columns by header text
        match_col = -1
        market_col = -1
        selection_col = -1
        fair_col = -1
        ev_col = -1
        bookie_col = -1
        odds_col = -1

        for i, h in enumerate(headers):
            hl = h.lower()
            if any(w in hl for w in ["match", "game", "fixture", "event"]):
                match_col = i
            elif any(w in hl for w in ["market", "type"]):
                market_col = i
            elif any(w in hl for w in ["selection", "bet", "pick"]):
                selection_col = i
            elif any(w in hl for w in ["fair", "true"]):
                fair_col = i
            elif "ev" in hl:
                ev_col = i
            elif any(w in hl for w in ["book", "bookie", "layer"]):
                bookie_col = i
            elif any(w in hl for w in ["odds", "price"]):
                odds_col = i

        # Build selection text from available data
        selection_text = ""
        if selection_col >= 0 and selection_col < len(cells):
            selection_text = cells[selection_col]
        elif market_col >= 0 and market_col < len(cells):
            selection_text = cells[market_col]
        else:
            # Use first cell as fallback
            selection_text = cells[0]

        # Determine market type
        market_type = self._detect_market_type(selection_text, cells)
        if not market_type:
            return  # Skip non-stats markets

        direction = self._detect_direction(selection_text)
        line = self._extract_line(selection_text)

        # Extract match info
        match = None
        if match_col >= 0 and match_col < len(cells):
            match = self._parse_match(cells[match_col])

        # Fair odds
        if fair_col >= 0 and fair_col < len(cells):
            try:
                fair_val = float(re.search(r"(\d+\.?\d*)", cells[fair_col]).group(1))
                if fair_val > 1.0:
                    fair_odds_list.append(
                        FairOdds(
                            market_type=market_type,
                            selection=selection_text,
                            line=line,
                            direction=direction,
                            fair_odds=fair_val,
                            match=match,
                            source_tool="coupons_tracker",
                        )
                    )
            except (ValueError, AttributeError):
                pass

        # Bookmaker odds
        bookie_name = ""
        if bookie_col >= 0 and bookie_col < len(cells):
            bookie_name = cells[bookie_col]
        if odds_col >= 0 and odds_col < len(cells):
            try:
                odds_val = float(re.search(r"(\d+\.?\d*)", cells[odds_col]).group(1))
                if odds_val > 1.0:
                    book_odds_list.append(
                        OddsEntry(
                            bookmaker=bookie_name or "unknown",
                            market_type=market_type,
                            selection=selection_text,
                            line=line,
                            direction=direction,
                            odds=odds_val,
                            match=match,
                        )
                    )
            except (ValueError, AttributeError):
                pass

    def _detect_market_type(self, selection: str, all_cells: list[str]) -> MarketType | None:
        """Detect market type from selection text and row data."""
        combined = " ".join([selection] + all_cells).lower()

        market_checks = [
            (["foul"], MarketType.FOULS),
            (["throw in", "throw-in", "throwin"], MarketType.THROW_INS),
            (["corner"], MarketType.CORNERS),
            (["card", "booking", "yellow", "red card"], MarketType.CARDS),
            (["shot on target", "shots on target", "sot"], MarketType.SHOTS_ON_TARGET),
            (["shot", "shots"], MarketType.SHOTS),
            (["offside"], MarketType.OFFSIDES),
            (["goal"], MarketType.GOALS),
        ]

        for keywords, mtype in market_checks:
            if any(kw in combined for kw in keywords):
                return mtype
        return None

    def _detect_direction(self, text: str) -> BetDirection | None:
        text_lower = text.lower()
        if "over" in text_lower:
            return BetDirection.OVER
        if "under" in text_lower:
            return BetDirection.UNDER
        return None

    def _extract_line(self, text: str) -> float:
        match = re.search(r"(\d+\.?\d*)", text)
        return float(match.group(1)) if match else 0.0

    def _parse_match(self, text: str) -> Match | None:
        """Parse match info from text like 'Arsenal vs Chelsea'."""
        separators = [" vs ", " v ", " - ", " @ "]
        for sep in separators:
            if sep in text.lower():
                idx = text.lower().index(sep)
                home = text[:idx].strip()
                away = text[idx + len(sep):].strip()
                if home and away:
                    return Match(home_team=home, away_team=away, league="")
        return None

    def _parse_api_data(
        self,
        data_list: list[dict],
        fair_odds_list: list[FairOdds],
        book_odds_list: list[OddsEntry],
    ) -> None:
        """Parse API response data into odds entries."""
        for data in data_list:
            if isinstance(data, dict):
                bets = data.get("bets", data.get("coupons", data.get("results", [])))
                if isinstance(bets, list):
                    for bet in bets:
                        self._parse_api_bet(bet, fair_odds_list, book_odds_list)
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        self._parse_api_bet(item, fair_odds_list, book_odds_list)

    def _parse_api_bet(
        self,
        bet: dict,
        fair_odds_list: list[FairOdds],
        book_odds_list: list[OddsEntry],
    ) -> None:
        """Parse a single bet from API data."""
        selection = bet.get("selection", bet.get("market", bet.get("name", "")))
        market_type = self._detect_market_type(str(selection), [str(v) for v in bet.values()])
        if not market_type:
            return

        direction = self._detect_direction(str(selection))
        line = self._extract_line(str(selection))

        # Match info
        match = None
        match_str = bet.get("match", bet.get("event", bet.get("fixture", "")))
        if match_str:
            match = self._parse_match(str(match_str))

        # Fair odds
        fair = bet.get("fair_odds", bet.get("fairOdds", bet.get("true_odds")))
        if fair:
            try:
                fair_odds_list.append(
                    FairOdds(
                        market_type=market_type,
                        selection=str(selection),
                        line=line,
                        direction=direction,
                        fair_odds=float(fair),
                        match=match,
                        source_tool="coupons_tracker",
                    )
                )
            except (ValueError, TypeError):
                pass

        # Bookmaker odds
        bookie = bet.get("bookmaker", bet.get("bookie", ""))
        odds = bet.get("odds", bet.get("price", bet.get("back_odds")))
        if bookie and odds:
            try:
                book_odds_list.append(
                    OddsEntry(
                        bookmaker=str(bookie),
                        market_type=market_type,
                        selection=str(selection),
                        line=line,
                        direction=direction,
                        odds=float(odds),
                        match=match,
                    )
                )
            except (ValueError, TypeError):
                pass
