from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MarketType(Enum):
    FOULS = "fouls"
    FOULS_WON = "fouls_won"
    THROW_INS = "throw_ins"
    CORNERS = "corners"
    CARDS = "cards"
    SHOTS = "shots"
    SHOTS_ON_TARGET = "shots_on_target"
    GOALS = "goals"
    BOOKINGS = "bookings"
    OFFSIDES = "offsides"
    TACKLES = "tackles"
    PASSES = "passes"
    SAVES = "saves"
    GOAL_KICKS = "goal_kicks"


class BetDirection(Enum):
    OVER = "over"
    UNDER = "under"


@dataclass
class Match:
    home_team: str
    away_team: str
    league: str
    kick_off: datetime | None = None
    match_id: str = ""

    @property
    def display_name(self) -> str:
        return f"{self.home_team} vs {self.away_team}"


@dataclass
class OddsEntry:
    """A single odds offering from a bookmaker for a specific market."""

    bookmaker: str
    market_type: MarketType
    selection: str  # e.g. "Over 22.5 Throw Ins", "Player X 2+ Fouls"
    line: float  # e.g. 22.5
    direction: BetDirection | None = None
    odds: float = 0.0  # decimal odds
    match: Match | None = None
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def implied_probability(self) -> float:
        if self.odds <= 0:
            return 0.0
        return 1.0 / self.odds


@dataclass
class FairOdds:
    """BookieBashing's calculated fair odds for a market."""

    market_type: MarketType
    selection: str
    line: float
    direction: BetDirection | None = None
    fair_odds: float = 0.0
    match: Match | None = None
    source_tool: str = ""  # which BB tool provided this
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def fair_probability(self) -> float:
        if self.fair_odds <= 0:
            return 0.0
        return 1.0 / self.fair_odds


@dataclass
class ValueBet:
    """A detected +EV opportunity."""

    match: Match
    bookmaker: str
    market_type: MarketType
    selection: str
    line: float
    direction: BetDirection | None
    book_odds: float
    fair_odds: float
    ev_percent: float
    stats_avg: float | None = None  # Real player season average (per 90)
    stats_supported: bool | None = None  # Whether real stats support the bet
    stats_note: str = ""  # Explanation of stats validation
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def edge(self) -> float:
        """The percentage edge over the bookmaker."""
        if self.fair_odds <= 0:
            return 0.0
        return (self.book_odds / self.fair_odds - 1) * 100

    def format_alert(self) -> str:
        return (
            f"VALUE BET | {self.match.display_name}\n"
            f"  League: {self.match.league}\n"
            f"  Market: {self.selection}\n"
            f"  Bookmaker: {self.bookmaker} @ {self.book_odds:.2f}\n"
            f"  Fair Odds: {self.fair_odds:.2f}\n"
            f"  EV: {self.ev_percent:+.1f}%\n"
            f"  Edge: {self.edge:+.1f}%"
        )
