"""BookieBashing authentication and session management."""

from __future__ import annotations

import logging

from playwright.async_api import BrowserContext, Page

from src.config import Config
from src.utils.browser import wait_for_content

logger = logging.getLogger(__name__)


class BBAuth:
    """Handles login and session persistence for BookieBashing."""

    def __init__(self, context: BrowserContext, config: Config | None = None):
        self.config = config or Config()
        self.context = context
        self._logged_in = False

    async def login(self) -> Page:
        """Log into BookieBashing and return the authenticated page."""
        page = await self.context.new_page()

        logger.info("Navigating to BookieBashing login page...")
        await page.goto(self.config.BB_LOGIN_URL, wait_until="networkidle")

        # BookieBashing uses WooCommerce - the login form has standard fields
        # Try to find the login form
        login_form = await page.query_selector("form.woocommerce-form-login")
        if not login_form:
            # May already be logged in - check for account content
            account_content = await page.query_selector(".woocommerce-MyAccount-content")
            if account_content:
                logger.info("Already logged in to BookieBashing")
                self._logged_in = True
                return page
            # Try alternative login form selectors
            login_form = await page.query_selector("#customer_login")

        # Fill in credentials
        username_field = await page.query_selector(
            'input[name="username"], #username, input[id="username"]'
        )
        password_field = await page.query_selector(
            'input[name="password"], #password, input[id="password"]'
        )

        if not username_field or not password_field:
            logger.error("Could not find login form fields")
            raise RuntimeError("Login form not found on BookieBashing")

        await username_field.fill(self.config.BB_USERNAME)
        await password_field.fill(self.config.BB_PASSWORD)

        # Check "remember me" if available
        remember_me = await page.query_selector(
            'input[name="rememberme"], #rememberme'
        )
        if remember_me:
            await remember_me.check()

        # Submit the form
        submit_btn = await page.query_selector(
            'button[name="login"], button[type="submit"], input[type="submit"]'
        )
        if submit_btn:
            await submit_btn.click()
        else:
            await page.keyboard.press("Enter")

        # Wait for navigation after login
        await page.wait_for_load_state("networkidle")

        # Check for login errors
        error_el = await page.query_selector(".woocommerce-error")
        if error_el:
            error_text = await error_el.inner_text()
            logger.error("Login failed: %s", error_text)
            raise RuntimeError(f"BookieBashing login failed: {error_text}")

        # Verify we're logged in
        account_content = await page.query_selector(".woocommerce-MyAccount-content")
        if account_content:
            logger.info("Successfully logged in to BookieBashing")
            self._logged_in = True
        else:
            # Some sites redirect to dashboard - check URL
            if "/my-account/" in page.url:
                logger.info("Login appears successful (on account page)")
                self._logged_in = True
            else:
                logger.warning(
                    "Login status uncertain - current URL: %s", page.url
                )
                self._logged_in = True  # Proceed optimistically

        return page

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    async def navigate_to_tool(self, page: Page, url: str) -> Page:
        """Navigate to a BB tool page, ensuring we're still authenticated."""
        await page.goto(url, wait_until="networkidle")

        # Check if we got redirected to login
        if "/my-account/" in page.url and "login" in page.url.lower():
            logger.warning("Session expired, re-logging in...")
            page = await self.login()
            await page.goto(url, wait_until="networkidle")

        return page
