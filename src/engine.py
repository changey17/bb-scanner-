"""Comparison engine that matches bookmaker odds against BB fair odds to find +EV bets."""

from __future__ import annotations

import logging
from datetime import datetime

from src.config import Config
from src.models.betting import (
    FairOdds,
    MarketType,
    Match,
    OddsEntry,
    ValueBet,
)

logger = logging.getLogger(__name__)


class ComparisonEngine:
    """Compares bookmaker odds against BookieBashing fair odds to identify value."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()

    def find_value_bets(
        self,
        fair_odds_list: list[FairOdds],
        book_odds_list: list[OddsEntry],
        min_ev_percent: float | None = None,
    ) -> list[ValueBet]:
        """Compare book odds against fair odds and return +EV bets.

        Matching is done by market_type + selection + line + direction.
        """
        if min_ev_percent is None:
            min_ev_percent = self.config.MIN_EV_PERCENT

        value_bets: list[ValueBet] = []

        # Build a lookup from fair odds keyed by matching criteria
        fair_lookup: dict[str, FairOdds] = {}
        for fo in fair_odds_list:
            key = self._make_key(fo.market_type, fo.selection, fo.line, fo.direction)
            fair_lookup[key] = fo
            # Also store a relaxed key (market_type + line + direction only)
            relaxed_key = self._make_relaxed_key(fo.market_type, fo.line, fo.direction)
            if relaxed_key not in fair_lookup:
                fair_lookup[relaxed_key] = fo

        # Check each bookmaker odds entry against fair odds
        for bo in book_odds_list:
            if bo.odds <= 1.0:
                continue

            # Filter by configured bookmakers
            bookie_lower = bo.bookmaker.lower().replace(" ", "")
            if not any(
                cb.lower().replace(" ", "") in bookie_lower or bookie_lower in cb.lower().replace(" ", "")
                for cb in self.config.BOOKMAKERS
            ):
                continue

            # Try exact match first
            key = self._make_key(bo.market_type, bo.selection, bo.line, bo.direction)
            fair = fair_lookup.get(key)

            # Try relaxed match
            if not fair:
                relaxed_key = self._make_relaxed_key(bo.market_type, bo.line, bo.direction)
                fair = fair_lookup.get(relaxed_key)

            if not fair or fair.fair_odds <= 1.0:
                continue

            # Calculate EV
            ev_percent = (bo.odds / fair.fair_odds - 1) * 100

            if ev_percent >= min_ev_percent:
                match = bo.match or fair.match or Match(
                    home_team="Unknown", away_team="Unknown", league=""
                )

                value_bets.append(
                    ValueBet(
                        match=match,
                        bookmaker=bo.bookmaker,
                        market_type=bo.market_type,
                        selection=bo.selection,
                        line=bo.line,
                        direction=bo.direction,
                        book_odds=bo.odds,
                        fair_odds=fair.fair_odds,
                        ev_percent=ev_percent,
                    )
                )

        # Sort by EV descending
        value_bets.sort(key=lambda b: b.ev_percent, reverse=True)

        logger.info(
            "Found %d value bets above %.1f%% EV (from %d fair odds, %d book odds)",
            len(value_bets),
            min_ev_percent,
            len(fair_odds_list),
            len(book_odds_list),
        )
        return value_bets

    def _make_key(
        self,
        market_type: MarketType,
        selection: str,
        line: float,
        direction,
    ) -> str:
        """Create a matching key for exact comparison."""
        dir_str = direction.value if direction else "none"
        # Normalize selection by removing extra whitespace and lowercasing
        sel_norm = " ".join(selection.lower().split())
        return f"{market_type.value}|{sel_norm}|{line}|{dir_str}"

    def _make_relaxed_key(
        self,
        market_type: MarketType,
        line: float,
        direction,
    ) -> str:
        """Create a relaxed key matching only market type, line, and direction."""
        dir_str = direction.value if direction else "none"
        return f"{market_type.value}|{line}|{dir_str}"


def merge_value_bets(*bet_lists: list[ValueBet]) -> list[ValueBet]:
    """Merge multiple lists of value bets, deduplicating."""
    seen = set()
    merged = []

    for bets in bet_lists:
        for bet in bets:
            key = (
                bet.bookmaker.lower(),
                bet.market_type.value,
                bet.selection.lower(),
                bet.line,
                bet.book_odds,
            )
            if key not in seen:
                seen.add(key)
                merged.append(bet)

    merged.sort(key=lambda b: b.ev_percent, reverse=True)
    return merged
