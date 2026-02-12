"""Scraper for BookieBashing Bet Tracker.

The Bet Tracker is the main tracker covering multiple sports and bet types.
It surfaces +EV bets by comparing bookmaker prices to fair odds.
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
    ValueBet,
)

logger = logging.getLogger(__name__)

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
}


def _match_market_type(text: str) -> MarketType | None:
    """Match text against stats market patterns."""
    text_lower = text.lower()
    for market_type, patterns in STATS_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text_lower):
                return market_type
    return None


class BBBetTrackerScraper:
    """Scrapes the BookieBashing Bet Tracker for football stats +EV bets."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()

    async def scrape(self, page: Page) -> list[ValueBet]:
        """Scrape the bet tracker for football stats value bets.

        The bet tracker already calculates EV, so we can extract
        pre-computed value bets directly.
        """
        logger.info("Navigating to Bet Tracker...")
        await page.goto(self.config.BB_BET_TRACKER_URL, wait_until="networkidle")
        await page.wait_for_timeout(5000)

        value_bets: list[ValueBet] = []

        # Try to apply filters for football stats markets if the UI allows
        await self._apply_filters(page)

        # Strategy 1: Extract from API/JS data
        js_bets = await self._extract_js_data(page)
        value_bets.extend(js_bets)

        # Strategy 2: Scrape DOM tables
        dom_bets = await self._scrape_dom_tables(page)
        value_bets.extend(dom_bets)

        # Strategy 3: Scrape card-based layouts
        card_bets = await self._scrape_cards(page)
        value_bets.extend(card_bets)

        # De-duplicate by selection + bookmaker
        seen = set()
        unique_bets = []
        for bet in value_bets:
            key = (bet.selection, bet.bookmaker, bet.book_odds)
            if key not in seen:
                seen.add(key)
                unique_bets.append(bet)

        # Filter to stats markets only
        unique_bets = [
            b for b in unique_bets
            if b.market_type in {
                MarketType.FOULS, MarketType.THROW_INS, MarketType.CORNERS,
                MarketType.CARDS, MarketType.BOOKINGS, MarketType.SHOTS,
                MarketType.SHOTS_ON_TARGET, MarketType.OFFSIDES,
            }
        ]

        logger.info("Bet Tracker: found %d football stats value bets", len(unique_bets))
        return unique_bets

    async def _apply_filters(self, page: Page) -> None:
        """Try to apply filters to show football stats markets."""
        try:
            # Look for sport filter
            sport_filter = await page.query_selector(
                "select[class*='sport'], [class*='sport-filter'], "
                "[data-filter='sport'], button:has-text('Football')"
            )
            if sport_filter:
                tag = await sport_filter.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    await sport_filter.select_option(label="Football")
                else:
                    await sport_filter.click()
                await page.wait_for_timeout(2000)

            # Look for market type filter
            market_filter = await page.query_selector(
                "select[class*='market'], [class*='market-filter'], "
                "[data-filter='market']"
            )
            if market_filter:
                tag = await market_filter.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    # Try to select stats-related options
                    options = await market_filter.query_selector_all("option")
                    for opt in options:
                        text = (await opt.inner_text()).lower()
                        if any(kw in text for kw in ["stat", "foul", "corner", "card", "throw"]):
                            value = await opt.get_attribute("value")
                            await market_filter.select_option(value=value)
                            break

            await page.wait_for_timeout(2000)
        except Exception as e:
            logger.debug("Could not apply filters: %s", e)

    async def _extract_js_data(self, page: Page) -> list[ValueBet]:
        """Extract value bets from JavaScript data on the page."""
        bets = []
        try:
            data = await page.evaluate(
                """
                () => {
                    const sources = [
                        window.trackerData,
                        window.betTrackerData,
                        window.valueBets,
                        window.__TRACKER__,
                    ];
                    for (const s of sources) {
                        if (s) return JSON.stringify(s);
                    }

                    // Try to find in script tags
                    const scripts = document.querySelectorAll('script:not([src])');
                    for (const s of scripts) {
                        const t = s.textContent || '';
                        if (t.includes('tracker') && t.includes('odds')) {
                            const m = t.match(/(?:var|let|const)\\s+\\w+\\s*=\\s*(\\{[\\s\\S]*?\\});/);
                            if (m) return m[1];
                            const m2 = t.match(/(?:var|let|const)\\s+\\w+\\s*=\\s*(\\[[\\s\\S]*?\\]);/);
                            if (m2) return m2[1];
                        }
                    }
                    return null;
                }
                """
            )

            if data:
                parsed = json.loads(data)
                items = parsed if isinstance(parsed, list) else parsed.get("bets", parsed.get("data", []))
                for item in items:
                    bet = self._parse_js_bet(item)
                    if bet:
                        bets.append(bet)

        except Exception as e:
            logger.debug("JS extraction from bet tracker failed: %s", e)
        return bets

    def _parse_js_bet(self, item: dict) -> ValueBet | None:
        """Parse a value bet from a JS data object."""
        if not isinstance(item, dict):
            return None

        selection = str(item.get("selection", item.get("market", item.get("bet", ""))))
        market_type = _match_market_type(selection)
        if not market_type:
            return None

        # Extract odds
        book_odds = 0.0
        fair_odds = 0.0
        ev = 0.0

        for key in ["odds", "price", "back_odds", "bookOdds"]:
            if key in item:
                try:
                    book_odds = float(item[key])
                    break
                except (ValueError, TypeError):
                    pass

        for key in ["fair_odds", "fairOdds", "true_odds", "trueOdds"]:
            if key in item:
                try:
                    fair_odds = float(item[key])
                    break
                except (ValueError, TypeError):
                    pass

        for key in ["ev", "EV", "ev_percent", "edge"]:
            if key in item:
                try:
                    ev = float(str(item[key]).replace("%", ""))
                    break
                except (ValueError, TypeError):
                    pass

        if book_odds <= 1.0 or fair_odds <= 1.0:
            return None

        if ev == 0.0 and fair_odds > 0:
            ev = (book_odds / fair_odds - 1) * 100

        # Parse match
        match_str = str(item.get("match", item.get("event", item.get("fixture", ""))))
        match = self._parse_match(match_str) or Match(
            home_team="Unknown", away_team="Unknown", league=""
        )
        match.league = str(item.get("league", item.get("competition", "")))

        direction = None
        sel_lower = selection.lower()
        if "over" in sel_lower:
            direction = BetDirection.OVER
        elif "under" in sel_lower:
            direction = BetDirection.UNDER

        line_match = re.search(r"(\d+\.?\d*)", selection)
        line = float(line_match.group(1)) if line_match else 0.0

        bookmaker = str(item.get("bookmaker", item.get("bookie", "unknown")))

        return ValueBet(
            match=match,
            bookmaker=bookmaker,
            market_type=market_type,
            selection=selection,
            line=line,
            direction=direction,
            book_odds=book_odds,
            fair_odds=fair_odds,
            ev_percent=ev,
        )

    async def _scrape_dom_tables(self, page: Page) -> list[ValueBet]:
        """Scrape value bets from DOM tables."""
        bets = []
        tables = await page.query_selector_all("table")

        for table in tables:
            rows = await table.query_selector_all("tr")
            headers = []

            for row in rows:
                ths = await row.query_selector_all("th")
                if ths:
                    headers = [(await th.inner_text()).strip() for th in ths]
                    continue

                tds = await row.query_selector_all("td")
                if not tds or len(tds) < 3:
                    continue

                cells = [(await td.inner_text()).strip() for td in tds]
                bet = self._parse_table_row(headers, cells)
                if bet:
                    bets.append(bet)

        return bets

    def _parse_table_row(self, headers: list[str], cells: list[str]) -> ValueBet | None:
        """Parse a table row into a ValueBet."""
        # Map headers to column indices
        col_map = {}
        for i, h in enumerate(headers):
            hl = h.lower()
            if any(w in hl for w in ["match", "event", "fixture", "game"]):
                col_map["match"] = i
            elif any(w in hl for w in ["market", "selection", "bet"]):
                col_map["selection"] = i
            elif any(w in hl for w in ["fair", "true"]):
                col_map["fair"] = i
            elif any(w in hl for w in ["book", "bookie"]):
                col_map["bookie"] = i
            elif any(w in hl for w in ["odds", "price"]) and "fair" not in hl:
                col_map["odds"] = i
            elif "ev" in hl:
                col_map["ev"] = i
            elif any(w in hl for w in ["league", "comp"]):
                col_map["league"] = i

        # Determine selection text
        selection = ""
        if "selection" in col_map and col_map["selection"] < len(cells):
            selection = cells[col_map["selection"]]
        else:
            selection = cells[0]

        market_type = _match_market_type(selection + " " + " ".join(cells))
        if not market_type:
            return None

        # Extract values
        def _get_float(key: str) -> float:
            if key in col_map and col_map[key] < len(cells):
                m = re.search(r"(\d+\.?\d*)", cells[col_map[key]])
                if m:
                    return float(m.group(1))
            return 0.0

        book_odds = _get_float("odds")
        fair_odds = _get_float("fair")
        ev = _get_float("ev")

        if book_odds <= 1.0 or fair_odds <= 1.0:
            return None

        if ev == 0.0 and fair_odds > 0:
            ev = (book_odds / fair_odds - 1) * 100

        match_text = cells[col_map["match"]] if "match" in col_map and col_map["match"] < len(cells) else ""
        match = self._parse_match(match_text) or Match(home_team="Unknown", away_team="Unknown", league="")

        if "league" in col_map and col_map["league"] < len(cells):
            match.league = cells[col_map["league"]]

        bookie = cells[col_map["bookie"]] if "bookie" in col_map and col_map["bookie"] < len(cells) else "unknown"

        direction = None
        sel_lower = selection.lower()
        if "over" in sel_lower:
            direction = BetDirection.OVER
        elif "under" in sel_lower:
            direction = BetDirection.UNDER

        line_m = re.search(r"(\d+\.?\d*)", selection)
        line = float(line_m.group(1)) if line_m else 0.0

        return ValueBet(
            match=match,
            bookmaker=bookie,
            market_type=market_type,
            selection=selection,
            line=line,
            direction=direction,
            book_odds=book_odds,
            fair_odds=fair_odds,
            ev_percent=ev,
        )

    async def _scrape_cards(self, page: Page) -> list[ValueBet]:
        """Scrape value bets from card-based layout."""
        bets = []
        cards = await page.query_selector_all(
            "[class*='tracker-item'], [class*='bet-card'], [class*='value-bet']"
        )

        for card in cards:
            text = (await card.inner_text()).strip()
            if not text:
                continue

            market_type = _match_market_type(text)
            if not market_type:
                continue

            # Extract all numbers that look like odds (between 1.01 and 100)
            numbers = re.findall(r"\b(\d+\.\d+)\b", text)
            odds_candidates = [float(n) for n in numbers if 1.01 < float(n) < 1000]

            if len(odds_candidates) < 2:
                continue

            # Heuristic: first odds-like number is book odds, second is fair odds
            book_odds = odds_candidates[0]
            fair_odds = odds_candidates[1]
            ev = (book_odds / fair_odds - 1) * 100

            # Try to extract match
            lines = text.split("\n")
            match = None
            for line in lines:
                match = self._parse_match(line)
                if match:
                    break

            if not match:
                match = Match(home_team="Unknown", away_team="Unknown", league="")

            direction = None
            if "over" in text.lower():
                direction = BetDirection.OVER
            elif "under" in text.lower():
                direction = BetDirection.UNDER

            line_m = re.search(r"(?:over|under)\s+(\d+\.?\d*)", text, re.IGNORECASE)
            line = float(line_m.group(1)) if line_m else 0.0

            bets.append(
                ValueBet(
                    match=match,
                    bookmaker="unknown",
                    market_type=market_type,
                    selection=lines[0] if lines else text[:50],
                    line=line,
                    direction=direction,
                    book_odds=book_odds,
                    fair_odds=fair_odds,
                    ev_percent=ev,
                )
            )

        return bets

    def _parse_match(self, text: str) -> Match | None:
        """Parse match info from text."""
        for sep in [" vs ", " v ", " - "]:
            if sep in text.lower():
                idx = text.lower().index(sep)
                home = text[:idx].strip()
                away = text[idx + len(sep):].strip()
                if home and away and len(home) < 40 and len(away) < 40:
                    return Match(home_team=home, away_team=away, league="")
        return None
