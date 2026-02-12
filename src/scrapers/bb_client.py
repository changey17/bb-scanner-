"""BookieBashing API client.

Direct HTTP client for BB's custom PHP API at /app/auth.php.
Replaces browser-based scraping with fast API calls.

Authentication flow:
1. Login via XOO Easy Login AJAX action to get WordPress cookies
2. Fetch an authenticated page to extract session_data (token, key, hash)
3. Use session_data credentials to call /app/auth.php with JSON requests

Request format:
    POST /app/auth.php
    Content-Type: application/x-www-form-urlencoded

    data={"auth":{"user_key":"...","session_token":"...","hash":"..."},
          "requests":[{"data_name":"...","method":"restGet","tab":"...","filters":"...","system":"..."}]}
"""

from __future__ import annotations

import json
import logging
import re
import time

import httpx

from src.config import Config

logger = logging.getLogger(__name__)

AUTH_URL = "https://www.bookiebashing.net/app/auth.php"
LOGIN_PAGE = "https://www.bookiebashing.net/my-account/"
AJAX_URL = "https://www.bookiebashing.net/wp-admin/admin-ajax.php"
# Use bet tracker page to get session tokens (it loads the full app JS)
SESSION_PAGE = "https://www.bookiebashing.net/trackers/bet-tracker/"


class BBClient:
    """HTTP client for BookieBashing's internal API."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self._client: httpx.AsyncClient | None = None
        self._session_token: str = ""
        self._user_key: str = ""
        self._hash: str = ""
        self._uid: int = 0
        self._username: str = ""
        self._bookmakers: list[dict] = []

    async def start(self) -> None:
        """Create HTTP client and authenticate."""
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            headers={"User-Agent": self.config.USER_AGENT},
        )
        await self._login()
        await self._extract_session()
        logger.info(
            "BB API client ready (user=%s, uid=%d)", self._username, self._uid
        )

    async def stop(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    async def _login(self) -> None:
        """Login via XOO Easy Login AJAX."""
        # Get login page for nonce
        resp = await self._client.get(LOGIN_PAGE)
        nonce_match = re.search(
            r'xoo_el_localize.*?"nonce"\s*:\s*"([^"]+)"', resp.text, re.DOTALL
        )
        xoo_nonce = nonce_match.group(1) if nonce_match else ""

        # Submit AJAX login
        login_resp = await self._client.post(
            AJAX_URL,
            data={
                "action": "xoo_el_form_action",
                "_xoo_el_form": "login",
                "xoo-el-username": self.config.BB_USERNAME,
                "xoo-el-password": self.config.BB_PASSWORD,
                "xoo-el-rememberme": "forever",
                "xoo_el_redirect": "/my-account/",
                "_xoo_el_nonce": xoo_nonce,
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        result = login_resp.json()
        if result.get("error", 1) != 0:
            raise RuntimeError(
                f"BB login failed: {result.get('notice', 'unknown error')}"
            )

        logger.info("Logged into BookieBashing")

    async def _extract_session(self) -> None:
        """Extract session tokens from an authenticated page."""
        resp = await self._client.get(SESSION_PAGE)
        text = resp.text

        # Extract session_data JSON
        session_match = re.search(r"var session_data = '(\{[^']+\})'", text)
        if not session_match:
            raise RuntimeError("Could not find session_data on page")

        session = json.loads(session_match.group(1))
        self._session_token = session["token"]
        self._user_key = session["key"]
        self._hash = session["hash"]
        self._username = session.get("username", "")

        # Extract user ID
        id_match = re.search(r'"id"\s*:\s*(\d+)', text)
        if id_match:
            self._uid = int(id_match.group(1))

    async def api_call(self, requests: list[dict]) -> dict:
        """Make an authenticated API call to /app/auth.php.

        Args:
            requests: List of request dicts with keys:
                - data_name: Label for returned data
                - method: API method (restGet, getCachedData, restCreate, etc.)
                - tab: Table/endpoint name (for restGet)
                - filters: Filter string (for restGet)
                - system: Subsystem name (bet, coupon, user, data, etc.)

        Returns:
            Response dict with data keyed by data_name.
        """
        payload = {
            "auth": {
                "user_key": self._user_key,
                "session_token": self._session_token,
                "hash": self._hash,
            },
            "requests": requests,
        }

        resp = await self._client.post(
            AUTH_URL,
            data={"data": json.dumps(payload)},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        result = resp.json()

        if result.get("fcode", 0) > 0:
            logger.warning("API error: %s", result.get("message", ""))

        return result

    async def get_records(
        self,
        data_name: str,
        tab: str,
        system: str,
        filters: str = "",
        method: str = "restGet",
    ) -> list[dict]:
        """Convenience: make a restGet call and return the records list."""
        result = await self.api_call([
            {
                "data_name": data_name,
                "method": method,
                "tab": tab,
                "filters": filters,
                "system": system,
            }
        ])

        raw = result.get(data_name)
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and "records" in parsed:
                    return parsed["records"]
                return parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                return []
        if isinstance(raw, dict) and "records" in raw:
            return raw["records"]
        return []

    async def get_cached_data(self, dataname: str) -> list | dict | None:
        """Fetch cached data (e.g., bookmaker list)."""
        result = await self.api_call([
            {"data_name": dataname, "method": "getCachedData", "dataname": dataname}
        ])
        raw = result.get(dataname)
        if isinstance(raw, str) and raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
        return raw

    async def get_bookmakers(self) -> list[dict]:
        """Get the full bookmaker list (cached)."""
        if not self._bookmakers:
            data = await self.get_cached_data("bookList")
            if isinstance(data, list):
                self._bookmakers = data
        return self._bookmakers

    def get_bookmaker_name(self, book_id: int) -> str:
        """Look up bookmaker name by ID."""
        for b in self._bookmakers:
            if b.get("id") == book_id:
                return b.get("name", str(book_id))
        return str(book_id)

    @property
    def uid(self) -> int:
        return self._uid

    @property
    def username(self) -> str:
        return self._username
