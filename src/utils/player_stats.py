"""Player stats validation using API-Football.

Fetches real player season averages (shots, fouls, tackles, cards, passes, etc.)
from API-Football's free tier to validate whether a value bet is supported by
the player's actual performance history.

Free tier: 100 requests/day at https://v3.football.api-sports.io
Requires API_FOOTBALL_KEY in .env config.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

from src.config import Config

logger = logging.getLogger(__name__)

API_BASE = "https://v3.football.api-sports.io"

# Map BB competition names to API-Football league IDs
LEAGUE_IDS = {
    "Premier League": 39,
    "Championship": 40,
    "League One": 41,
    "League Two": 42,
    "La Liga": 140,
    "Serie A": 135,
    "Bundesliga": 78,
    "Ligue 1": 61,
    "Eredivisie": 88,
    "Primeira Liga": 94,
    "Scottish Premiership": 179,
    "Champions League": 2,
    "Europa League": 3,
    "Conference League": 848,
}

# Current season
CURRENT_SEASON = 2024


@dataclass
class PlayerSeasonStats:
    """Per-90 minute averages for a player's season."""

    player_name: str
    team: str
    appearances: int = 0
    minutes: int = 0
    shots_per_90: float = 0.0
    shots_on_target_per_90: float = 0.0
    fouls_committed_per_90: float = 0.0
    fouls_drawn_per_90: float = 0.0
    tackles_per_90: float = 0.0
    yellow_cards_per_90: float = 0.0
    passes_per_90: float = 0.0
    offsides_per_90: float = 0.0
    fetched_at: float = field(default_factory=time.time)

    def get_stat(self, market_key: str) -> float | None:
        """Get the relevant per-90 stat for a BB market key."""
        mapping = {
            "overSot": self.shots_on_target_per_90,
            "underSot": self.shots_on_target_per_90,
            "overShots": self.shots_per_90,
            "underShots": self.shots_per_90,
            "overFouls": self.fouls_committed_per_90,
            "overFoulsWon": self.fouls_drawn_per_90,
            "overTackles": self.tackles_per_90,
            "overPasses": self.passes_per_90,
            "overOffsides": self.offsides_per_90,
            "overPlayer_cards": self.yellow_cards_per_90,
        }
        val = mapping.get(market_key)
        return val if val and val > 0 else None


class PlayerStatsProvider:
    """Fetches and caches player season statistics from API-Football."""

    def __init__(self, config: Config):
        self.config = config
        self.api_key = config.API_FOOTBALL_KEY
        self._cache: dict[str, PlayerSeasonStats] = {}
        self._search_cache: dict[str, int | None] = {}  # name -> player_id
        self._request_count = 0
        self._daily_limit = 100

    @property
    def is_available(self) -> bool:
        """Whether stats validation is configured and has remaining requests."""
        return bool(self.api_key) and self._request_count < self._daily_limit

    async def _api_get(self, endpoint: str, params: dict) -> dict | None:
        """Make an authenticated GET request to API-Football."""
        if not self.api_key or self._request_count >= self._daily_limit:
            return None

        headers = {"x-apisports-key": self.api_key}
        url = f"{API_BASE}/{endpoint}"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                self._request_count += 1
                resp.raise_for_status()
                data = resp.json()

                # Check remaining requests from headers
                remaining = resp.headers.get("x-ratelimit-requests-remaining")
                if remaining:
                    self._daily_limit = self._request_count + int(remaining)

                return data
        except Exception as e:
            logger.warning("API-Football request failed: %s", e)
            return None

    def _normalize_name(self, name: str) -> str:
        """Normalize player name for matching."""
        return name.strip().lower().replace(".", "").replace("'", "")

    def _name_matches(self, api_name: str, bb_name: str) -> bool:
        """Check if names match (handles partial/reversed names)."""
        api = self._normalize_name(api_name)
        bb = self._normalize_name(bb_name)

        if api == bb:
            return True

        # Check last name match
        api_parts = api.split()
        bb_parts = bb.split()

        if api_parts and bb_parts:
            # Last name matches
            if api_parts[-1] == bb_parts[-1]:
                return True
            # First + last match in either order
            if len(api_parts) >= 2 and len(bb_parts) >= 2:
                if api_parts[0] == bb_parts[0] and api_parts[-1] == bb_parts[-1]:
                    return True

        return False

    async def _search_player(
        self, name: str, team: str, league_name: str
    ) -> int | None:
        """Search for a player by name and return their API-Football ID."""
        cache_key = f"{name}|{team}"
        if cache_key in self._search_cache:
            return self._search_cache[cache_key]

        league_id = None
        for comp, lid in LEAGUE_IDS.items():
            if comp.lower() in league_name.lower() or league_name.lower() in comp.lower():
                league_id = lid
                break

        # Try searching by last name (more reliable)
        last_name = name.strip().split()[-1] if name.strip() else name
        params = {"search": last_name, "season": CURRENT_SEASON}
        if league_id:
            params["league"] = league_id

        data = await self._api_get("players", params)
        if not data or not data.get("response"):
            self._search_cache[cache_key] = None
            return None

        # Find best match
        for entry in data["response"]:
            player = entry.get("player", {})
            full_name = player.get("name", "")
            first_name = player.get("firstname", "")
            last = player.get("lastname", "")

            for candidate in [full_name, f"{first_name} {last}", last]:
                if self._name_matches(candidate, name):
                    player_id = player.get("id")
                    self._search_cache[cache_key] = player_id
                    return player_id

        self._search_cache[cache_key] = None
        return None

    async def get_player_stats(
        self, name: str, team: str, league_name: str
    ) -> PlayerSeasonStats | None:
        """Get season stats for a player, using cache if available."""
        cache_key = f"{name}|{team}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            # Cache for 1 hour
            if time.time() - cached.fetched_at < 3600:
                return cached

        if not self.is_available:
            return None

        player_id = await self._search_player(name, team, league_name)
        if not player_id:
            return None

        data = await self._api_get(
            "players", {"id": player_id, "season": CURRENT_SEASON}
        )
        if not data or not data.get("response"):
            return None

        entry = data["response"][0]
        player_info = entry.get("player", {})

        # Aggregate stats across all competitions
        total_appearances = 0
        total_minutes = 0
        total_shots = 0
        total_sot = 0
        total_fouls_committed = 0
        total_fouls_drawn = 0
        total_tackles = 0
        total_yellows = 0
        total_passes = 0
        total_offsides = 0

        for stat_block in entry.get("statistics", []):
            games = stat_block.get("games", {})
            apps = games.get("appearences") or 0  # API typo is intentional
            mins = games.get("minutes") or 0
            total_appearances += apps
            total_minutes += mins

            shots = stat_block.get("shots", {})
            total_shots += shots.get("total") or 0
            total_sot += shots.get("on") or 0

            fouls = stat_block.get("fouls", {})
            total_fouls_committed += fouls.get("committed") or 0
            total_fouls_drawn += fouls.get("drawn") or 0

            tackles = stat_block.get("tackles", {})
            total_tackles += tackles.get("total") or 0

            cards = stat_block.get("cards", {})
            total_yellows += cards.get("yellow") or 0

            passes = stat_block.get("passes", {})
            total_passes += passes.get("total") or 0

            total_offsides += stat_block.get("offsides") or 0

        if total_minutes < 90:
            self._cache[cache_key] = None
            return None

        per_90 = 90.0 / total_minutes

        stats = PlayerSeasonStats(
            player_name=player_info.get("name", name),
            team=team,
            appearances=total_appearances,
            minutes=total_minutes,
            shots_per_90=round(total_shots * per_90, 2),
            shots_on_target_per_90=round(total_sot * per_90, 2),
            fouls_committed_per_90=round(total_fouls_committed * per_90, 2),
            fouls_drawn_per_90=round(total_fouls_drawn * per_90, 2),
            tackles_per_90=round(total_tackles * per_90, 2),
            yellow_cards_per_90=round(total_yellows * per_90, 2),
            passes_per_90=round(total_passes * per_90, 2),
            offsides_per_90=round(total_offsides * per_90, 2),
        )

        self._cache[cache_key] = stats
        logger.info(
            "Fetched stats for %s: %d apps, %d mins",
            name, total_appearances, total_minutes,
        )
        return stats

    def validate_bet(
        self,
        stats: PlayerSeasonStats,
        market_key: str,
        line: float,
        fair_mean: float,
    ) -> tuple[bool, str]:
        """Validate a value bet against the player's real stats.

        Returns (supported, reason) where supported is True if the player's
        real stats support the bet, and reason explains why.
        """
        real_avg = stats.get_stat(market_key)
        if real_avg is None:
            return True, "no stats data"

        # For over bets: player's real average should be close to or above
        # the implied mean from fair odds
        is_over = market_key.startswith("over")

        if is_over:
            # Real avg >= line means player regularly hits this line
            if real_avg >= line:
                return True, f"avg {real_avg:.1f} >= line {line} (strong)"
            # Real avg close to line (within 70%) is still reasonable
            elif real_avg >= line * 0.7:
                return True, f"avg {real_avg:.1f} near line {line} (ok)"
            else:
                return False, f"avg {real_avg:.1f} << line {line} (weak)"
        else:
            # Under bet: player's average should be at or below the line
            if real_avg <= line:
                return True, f"avg {real_avg:.1f} <= line {line} (strong)"
            elif real_avg <= line * 1.3:
                return True, f"avg {real_avg:.1f} near line {line} (ok)"
            else:
                return False, f"avg {real_avg:.1f} >> line {line} (weak)"
