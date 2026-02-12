"""Browser management using Playwright."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from src.config import Config

logger = logging.getLogger(__name__)


class BrowserManager:
    """Manages a persistent browser session for scraping."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def start(self) -> BrowserContext:
        """Launch browser and create a context."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        self._context = await self._browser.new_context(
            user_agent=self.config.USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            java_script_enabled=True,
        )
        # Mask webdriver detection
        await self._context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """
        )
        logger.info("Browser started (headless=%s)", self.config.HEADLESS)
        return self._context

    async def new_page(self) -> Page:
        """Open a new page in the current context."""
        if not self._context:
            await self.start()
        return await self._context.new_page()

    async def stop(self) -> None:
        """Close browser and cleanup."""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser stopped")

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[BrowserContext, None]:
        """Context manager for a browser session."""
        try:
            ctx = await self.start()
            yield ctx
        finally:
            await self.stop()


async def wait_for_content(page: Page, selector: str, timeout: int = 30000) -> None:
    """Wait for a selector to appear, with retry logic."""
    try:
        await page.wait_for_selector(selector, timeout=timeout)
    except Exception:
        logger.warning("Timeout waiting for selector: %s", selector)
        raise


async def safe_get_text(page: Page, selector: str) -> str:
    """Safely extract text from an element."""
    try:
        el = await page.query_selector(selector)
        if el:
            return (await el.inner_text()).strip()
    except Exception:
        pass
    return ""


async def safe_get_all_text(page: Page, selector: str) -> list[str]:
    """Extract text from all matching elements."""
    try:
        elements = await page.query_selector_all(selector)
        return [((await el.inner_text()).strip()) for el in elements]
    except Exception:
        return []
