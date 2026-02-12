"""Base class for bookmaker scrapers."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from playwright.async_api import Page

from src.models.betting import OddsEntry

logger = logging.getLogger(__name__)


class BookmakerScraper(ABC):
    """Base class for individual bookmaker scrapers."""

    name: str = "unknown"

    @abstractmethod
    async def scrape_football_stats(self, page: Page) -> list[OddsEntry]:
        """Scrape football stats markets from this bookmaker.

        Returns a list of OddsEntry objects with odds for
        fouls, throw-ins, corners, cards, etc.
        """
        ...

    async def navigate_to_football(self, page: Page, url: str) -> None:
        """Navigate to a bookmaker's football section."""
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            logger.warning("Failed to navigate to %s: %s", url, e)
            raise
