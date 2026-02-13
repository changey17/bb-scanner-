"""Scraper for BookieBashing Daily Tools - Player Stats & Calculator.

Uses the Node.js REST API at /node/rest/goals/list to fetch live football
match data including player-level stats odds from bookmakers.

Fair odds are computed using BB's own methodology:
1. Poisson reverse-engineering of expected stats from bookmaker odds
2. Optimism adjustment (from BB's config) applied to fair probability
3. Polynomial adjustment (A*x² + B*x + C, clamped to [D, E]) on the mean
4. Trimmed mean across bookmakers for consensus fair value

Auth uses custom headers extracted from the vue_config element on the page:
    X-BB-User, X-BB-Hash, X-BB-Userid, X-BB-Userlevel
"""

from __future__ import annotations

import json
import logging
import math
import re
import time

import httpx

from src.config import Config
from src.models.betting import (
    BetDirection,
    MarketType,
    Match,
    ValueBet,
)
from src.scrapers.bb_client import BBClient

logger = logging.getLogger(__name__)

# Target bookmakers in BB's playerStatsData
TARGET_BOOKMAKERS = {
    "Bet365": "Bet365",
    "Paddy Power": "Paddy Power",
    "Skybet": "Sky Bet",
    "William Hill": "William Hill",
    "Betfred": "Betfred",
    "Betfair Exchange": "Betfair",
}

# Map playerStatsData market keys to our MarketType
MARKET_MAP = {
    "overSot": (MarketType.SHOTS_ON_TARGET, BetDirection.OVER),
    "underSot": (MarketType.SHOTS_ON_TARGET, BetDirection.UNDER),
    "overShots": (MarketType.SHOTS, BetDirection.OVER),
    "underShots": (MarketType.SHOTS, BetDirection.UNDER),
    "overFouls": (MarketType.FOULS, BetDirection.OVER),
    "overFoulsWon": (MarketType.FOULS, BetDirection.OVER),
    "overTackles": (MarketType.TACKLES, BetDirection.OVER),
    "overPasses": (MarketType.PASSES, BetDirection.OVER),
    "overOffsides": (MarketType.OFFSIDES, BetDirection.OVER),
    "overPlayer_cards": (MarketType.CARDS, BetDirection.OVER),
    "overAssists": (MarketType.GOALS, BetDirection.OVER),
    "overSaves": (MarketType.GOALS, BetDirection.OVER),
    "overGoalKicks": (MarketType.GOALS, BetDirection.OVER),
}

# Map market keys to BB config stat names (for optimism/polynomial lookup)
MARKET_TO_STAT = {
    "overSot": "shots",
    "underSot": "shots",
    "overShots": "player_shots",
    "underShots": "player_shots",
    "overFouls": "fouls",
    "overFoulsWon": "foulswon",
    "overTackles": "tackles",
    "overPasses": "passes",
    "overOffsides": "shots",
    "overPlayer_cards": "player_cards",
    "overAssists": "assists",
    "overSaves": "saves",
    "overGoalKicks": "shots",
}

# Stat markets we care about (football stats focus)
STATS_MARKETS = {
    "overSot", "underSot", "overShots", "underShots",
    "overFouls", "overFoulsWon", "overTackles", "overPasses",
    "overOffsides", "overPlayer_cards",
}

BASE_URL = "https://www.bookiebashing.net/node"
DAILY_PAGE = "https://www.bookiebashing.net/tools/daily/"

# Sub percentage: player-level stats reduced by this % (bench/sub time)
DEFAULT_SUB_PERCENTAGE = 9.5


# --- Poisson math (mirrors BB's JavaScript calculations) ---

def _poisson_pmf(mean: float, k: int) -> float:
    """Poisson probability mass function: P(X = k)."""
    if mean <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-mean) * (mean ** k) / math.factorial(k)


def _poisson_cdf(mean: float, k: int) -> float:
    """P(X <= k) using Poisson CDF."""
    return sum(_poisson_pmf(mean, i) for i in range(k + 1))


def _poisson_over(mean: float, line: float) -> float:
    """Probability of over line (e.g. over 0.5 = P(X >= 1))."""
    return 1.0 - _poisson_cdf(mean, math.floor(line))


def _fair_odds_from_bookie_market(
    over_odds: float,
    over_under_odds: list[float],
    optimism: float | None = None,
) -> float:
    """Derive fair probability from a bookmaker's over/under market.

    Mirrors BB's fairOddsFromBookieMarket$1 function.
    Uses the power method when both over and under are available.
    optimism: BB's optimism setting (0-100, where lower = more pessimistic on over).
    """
    if over_odds is None or over_odds <= 1.0:
        return float("inf")

    total_implied = sum(1.0 / o for o in over_under_odds if o > 0)

    if len(over_under_odds) == 2 and all(o > 0 for o in over_under_odds):
        # Power method
        fair_prob = (2 - (total_implied - 1) * over_odds) / (2 * over_odds)

        if optimism is not None and optimism != 50:
            opt = 100 - optimism
            overround = 1 - (1 / over_under_odds[0] + 1 / over_under_odds[1])
            if opt > 50:
                margin = 1.0 / over_odds + overround - fair_prob
                fair_prob += margin * (opt - 50) / 50
            elif opt < 50:
                margin = fair_prob - 1.0 / over_odds
                fair_prob = 1.0 / over_odds + margin * opt / 50

        return 1.0 / max(fair_prob, 0.001)

    return over_odds * total_implied


def _reverse_poisson_over(fair_over_odds: float, line: int) -> float:
    """Binary search to find the Poisson mean that gives the target over odds.

    Mirrors BB's reversePoissonOver function.
    """
    if fair_over_odds <= 1.0 or fair_over_odds == float("inf"):
        return 0.0

    lo, hi = 0.0, 1000.0
    mid = 500.0

    for _ in range(100):
        prob = _poisson_over(mid, line)
        if prob <= 0:
            target = 1.0 / fair_over_odds
            if abs(0 - target) < 0.0005:
                break
            hi = mid
            mid = (lo + hi) / 2.0
            continue
        current_odds = 1.0 / prob
        if abs(current_odds - fair_over_odds) < 0.0005 or abs(hi - lo) < 0.0005:
            break
        if current_odds > fair_over_odds:
            lo = mid
        else:
            hi = mid
        mid = (lo + hi) / 2.0

    return mid


def _poisson_fair_over(mean: float, line: float) -> float:
    """Fair decimal odds for over a given line using Poisson."""
    prob = _poisson_over(mean, line)
    if prob <= 0:
        return float("inf")
    return 1.0 / prob


def _apply_polynomial(mean: float, poly_config: dict | None) -> float:
    """Apply BB's polynomial adjustment to a Poisson mean.

    BB's formula: adjustment = min(E, max(D, A*x² + B*x + C))
    Then: adjusted_mean = mean * adjustment

    Uses the 'player' polynomial for individual player stats.
    """
    if not poly_config or not isinstance(poly_config, dict):
        return mean

    player_poly = poly_config.get("player", {})
    if not player_poly:
        return mean

    a = float(player_poly.get("A", 0) or 0)
    b = float(player_poly.get("B", 0) or 0)
    c = float(player_poly.get("C", 1) or 1)
    d = float(player_poly.get("D", 0) or 0)
    e = float(player_poly.get("E", 2) or 2)

    adjustment = a * mean**2 + b * mean + c
    adjustment = min(e, max(d, adjustment))

    return mean * adjustment


def _estimate_mean_from_odds(
    over_odds: float,
    under_odds: float | None,
    handicap: float,
    optimism: float | None = None,
) -> float:
    """Estimate expected stat value from bookmaker odds using reverse Poisson."""
    line = math.floor(handicap)

    if under_odds and under_odds > 0:
        fair_over = _fair_odds_from_bookie_market(
            over_odds, [over_odds, under_odds], optimism
        )
    else:
        fair_over = over_odds

    return _reverse_poisson_over(fair_over, line)


class BBDailyScraper:
    """Scrapes player stats from BB's Daily Goals tool via Node.js API."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self._bb_config: dict | None = None

    async def _extract_vue_config(self, client: BBClient) -> dict:
        """Extract vue_config from the daily page."""
        resp = await client._client.get(DAILY_PAGE)
        match = re.search(r"id='vue_config'[^>]*>(.*?)</script>", resp.text)
        if not match:
            match = re.search(r'id="vue_config"[^>]*>(.*?)</script>', resp.text)
        if not match:
            raise RuntimeError("Could not find vue_config on daily page")
        return json.loads(match.group(1))

    async def _api_get(
        self, client: BBClient, vue_config: dict, path: str
    ) -> list | dict:
        """Make an authenticated GET to the Node API."""
        headers = {
            "X-BB-User": str(vue_config["user"]),
            "X-BB-Hash": vue_config["hash"],
            "X-BB-Userid": str(vue_config["userId"]),
            "X-BB-Userlevel": str(vue_config["userLevel"]),
        }
        url = f"{BASE_URL}{path}"
        if "?" in url:
            url += f"&t={int(time.time() * 1000)}"
        else:
            url += f"?t={int(time.time() * 1000)}"

        resp = await client._client.get(url, headers=headers)
        return resp.json()

    async def get_games(self, client: BBClient) -> tuple[list[dict], dict]:
        """Fetch all live football games and BB config from the daily goals API."""
        vue_config = await self._extract_vue_config(client)

        # Fetch BB's goals config (optimism, polynomial, etc.)
        bb_config = await self._api_get(client, vue_config, "/rest/goals/config")
        if isinstance(bb_config, dict):
            self._bb_config = bb_config
            logger.info("Loaded BB goals config (optimism, polynomials)")

        games = await self._api_get(client, vue_config, "/rest/goals/list")
        if not isinstance(games, list):
            logger.error("Unexpected goals/list response type: %s", type(games))
            return [], vue_config
        logger.info("Daily Goals: %d games loaded", len(games))
        return games, vue_config

    def _get_optimism(self, stat_name: str) -> float | None:
        """Get BB's optimism setting for a stat type."""
        if not self._bb_config:
            return None
        optimism = self._bb_config.get("optimism", {})
        val = optimism.get(stat_name)
        if val is not None and val > -1:
            return val
        return optimism.get("shots")

    def _get_polynomial(self, stat_name: str) -> dict | None:
        """Get BB's polynomial config for a stat type."""
        if not self._bb_config:
            return None
        poly = self._bb_config.get("optimismPolynomial", {})
        return poly.get(stat_name, poly.get("shots"))

    def _extract_player_value_bets(
        self,
        game: dict,
        min_ev_percent: float,
    ) -> list[ValueBet]:
        """Extract value bets from a single game's playerStatsData.

        Uses BB's own fair value methodology:
        1. For each bookmaker, derive Poisson mean using BB's optimism setting
        2. Average means across bookmakers (trimmed if 5+)
        3. Apply BB's polynomial adjustment to the consensus mean
        4. Compute fair odds from adjusted mean using Poisson
        5. Compare target bookmaker's price to fair odds

        Requires at least 2 bookmakers total to establish a fair mean.
        """
        value_bets = []
        psd = game.get("playerStatsData", {})
        if not isinstance(psd, dict):
            return value_bets

        event_name = game.get("event", "")
        competition = game.get("competition", {})
        comp_name = competition.get("name", "") if isinstance(competition, dict) else ""

        parts = event_name.split(" v ", 1)
        home = parts[0].strip() if len(parts) == 2 else event_name
        away = parts[1].strip() if len(parts) == 2 else ""
        match = Match(
            home_team=home,
            away_team=away,
            league=comp_name,
            match_id=str(game.get("id", "")),
        )

        # Step 1: Collect data from ALL bookmakers per player per market
        all_data: dict[str, dict[str, list[tuple]]] = {}
        target_entries: dict[str, dict[str, list[tuple]]] = {}

        for bookie_raw, players in psd.items():
            if not isinstance(players, dict):
                continue
            is_target = bookie_raw in TARGET_BOOKMAKERS

            for player_name, markets in players.items():
                if not isinstance(markets, dict):
                    continue

                for market_key, data in markets.items():
                    if market_key == "team" or market_key not in STATS_MARKETS:
                        continue
                    if not isinstance(data, dict):
                        continue

                    odds = data.get("odds", 0)
                    handicap = data.get("handicap", 0.5)
                    if not odds or odds <= 1.0:
                        continue

                    under_key = market_key.replace("over", "under")
                    under_odds = None
                    if under_key != market_key and under_key in markets:
                        under_data = markets[under_key]
                        if isinstance(under_data, dict):
                            under_odds = under_data.get("odds")

                    all_data.setdefault(player_name, {}).setdefault(market_key, []).append(
                        (bookie_raw, odds, handicap, under_odds)
                    )

                    if is_target:
                        display = TARGET_BOOKMAKERS[bookie_raw]
                        target_entries.setdefault(player_name, {}).setdefault(market_key, []).append(
                            (display, odds, handicap)
                        )

        # Step 2: For each player+market with target bookmaker entries
        for player_name, markets in target_entries.items():
            for market_key, targets in markets.items():
                all_entries = all_data.get(player_name, {}).get(market_key, [])
                if len(all_entries) < 2:
                    continue

                market_info = MARKET_MAP.get(market_key)
                if not market_info:
                    continue
                market_type, direction = market_info

                # Get BB's config for this stat type
                stat_name = MARKET_TO_STAT.get(market_key, "shots")
                optimism = self._get_optimism(stat_name)
                polynomial = self._get_polynomial(stat_name)

                for display_name, odds, handicap in targets:
                    # Estimate fair mean from OTHER bookmakers (exclude self)
                    means = []
                    has_under = False
                    for other_bookie, other_odds, other_hcap, other_under in all_entries:
                        if TARGET_BOOKMAKERS.get(other_bookie) == display_name:
                            continue
                        if other_under and other_under > 0:
                            has_under = True
                        est_mean = _estimate_mean_from_odds(
                            other_odds, other_under, other_hcap, optimism
                        )
                        if 0 < est_mean < 100:
                            means.append(est_mean)

                    if len(means) < 3:
                        continue

                    # Trim if 5+ means and only-over (no under markets)
                    means.sort()
                    if not has_under and len(means) > 5:
                        means = means[1:-1]
                    elif len(means) >= 5:
                        trimmed = means[1:-1]
                        means = trimmed

                    fair_mean = sum(means) / len(means)

                    # Apply BB's polynomial adjustment
                    fair_mean = _apply_polynomial(fair_mean, polynomial)

                    # Apply sub percentage reduction for player-level stats
                    sub_pct = DEFAULT_SUB_PERCENTAGE
                    if self._bb_config:
                        sub_pct = self._bb_config.get("subPercentage", sub_pct)
                    fair_mean *= (1 - sub_pct / 100)

                    fair_odds = _poisson_fair_over(fair_mean, handicap)

                    if fair_odds <= 1.0 or fair_odds == float("inf"):
                        continue

                    ev_percent = (odds / fair_odds - 1) * 100

                    if ev_percent < min_ev_percent:
                        continue

                    stat_label = market_key.replace("over", "Over ").replace("under", "Under ")
                    stat_label = stat_label.replace("Sot", "SOT").replace("Player_cards", "Cards")
                    stat_label = stat_label.replace("FoulsWon", "Fouls Won")

                    vb = ValueBet(
                        match=match,
                        bookmaker=display_name,
                        market_type=market_type,
                        selection=f"{player_name} - {stat_label} {handicap}",
                        line=handicap,
                        direction=direction,
                        book_odds=odds,
                        fair_odds=round(fair_odds, 3),
                        ev_percent=round(ev_percent, 2),
                    )
                    value_bets.append(vb)

        return value_bets

    async def scrape_value_bets(
        self,
        client: BBClient,
        min_ev_percent: float = 2.0,
        stats_provider=None,
    ) -> list[ValueBet]:
        """Scrape all player stats value bets from the Daily Goals tool.

        If stats_provider is given, validates bets against real player stats.
        """
        games, _ = await self.get_games(client)
        value_bets = []

        now_ms = time.time() * 1000
        upcoming = [g for g in games if g.get("startTime", 0) > now_ms - 7200000]
        logger.info("Processing %d upcoming games for player stats", len(upcoming))

        for game in upcoming:
            psd = game.get("playerStatsData")
            if not psd:
                continue
            bets = self._extract_player_value_bets(game, min_ev_percent)
            value_bets.extend(bets)

        # Validate against real player stats if available
        if stats_provider and stats_provider.is_available:
            await self._validate_with_stats(value_bets, stats_provider)

        value_bets.sort(key=lambda b: b.ev_percent, reverse=True)
        logger.info("Daily Player Stats: %d value bets found", len(value_bets))
        return value_bets

    async def _validate_with_stats(
        self, bets: list[ValueBet], stats_provider
    ) -> None:
        """Enrich value bets with real player stats from API-Football."""
        from src.utils.player_stats import PlayerStatsProvider

        if not isinstance(stats_provider, PlayerStatsProvider):
            return

        # Group bets by player to minimize API calls
        player_bets: dict[str, list[ValueBet]] = {}
        for bet in bets:
            # Extract player name from selection (format: "Player Name - Over SOT 0.5")
            parts = bet.selection.split(" - ", 1)
            player_name = parts[0].strip() if parts else bet.selection
            player_bets.setdefault(player_name, []).append(bet)

        validated = 0
        for player_name, player_bet_list in player_bets.items():
            if not stats_provider.is_available:
                break

            # Use first bet's match info for lookup
            first_bet = player_bet_list[0]
            league = first_bet.match.league
            team = first_bet.match.home_team  # Best guess

            stats = await stats_provider.get_player_stats(
                player_name, team, league
            )
            if not stats:
                continue

            for bet in player_bet_list:
                # Determine original market key from selection
                market_key = self._selection_to_market_key(bet.selection)
                if not market_key:
                    continue

                real_avg = stats.get_stat(market_key)
                if real_avg is not None:
                    bet.stats_avg = real_avg
                    supported, note = stats_provider.validate_bet(
                        stats, market_key, bet.line, 0
                    )
                    bet.stats_supported = supported
                    bet.stats_note = note
                    validated += 1

        logger.info("Stats validation: %d bets validated", validated)

    @staticmethod
    def _selection_to_market_key(selection: str) -> str | None:
        """Convert selection text back to BB market key for stats lookup."""
        sel = selection.lower()
        if "over sot" in sel or "over  sot" in sel:
            return "overSot"
        if "under sot" in sel or "under  sot" in sel:
            return "underSot"
        if "over shots" in sel:
            return "overShots"
        if "under shots" in sel:
            return "underShots"
        if "over fouls won" in sel:
            return "overFoulsWon"
        if "over fouls" in sel:
            return "overFouls"
        if "over tackles" in sel:
            return "overTackles"
        if "over passes" in sel:
            return "overPasses"
        if "over offsides" in sel:
            return "overOffsides"
        if "over cards" in sel:
            return "overPlayer_cards"
        return None
