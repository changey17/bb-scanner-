import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # BookieBashing
    BB_USERNAME: str = os.getenv("BB_USERNAME", "")
    BB_PASSWORD: str = os.getenv("BB_PASSWORD", "")
    BB_BASE_URL: str = "https://www.bookiebashing.net"
    BB_LOGIN_URL: str = "https://www.bookiebashing.net/my-account/"
    BB_TOOLS_URL: str = "https://www.bookiebashing.net/tools/"

    # Tool URLs
    BB_BETBUILDER_URL: str = "https://www.bookiebashing.net/tools/betbuilder/"
    BB_GAME_CENTRE_URL: str = "https://www.bookiebashing.net/tools/game-centre/"
    BB_COUPONS_TRACKER_URL: str = "https://www.bookiebashing.net/trackers/coupons-tracker/"
    BB_BET_TRACKER_URL: str = "https://www.bookiebashing.net/trackers/bet-tracker/"
    BB_DETAILED_GAMES_URL: str = "https://www.bookiebashing.net/tools/detailed-games/"
    BB_XSOT_URL: str = "https://www.bookiebashing.net/tools/xsot/"
    BB_MATCH_XG_URL: str = "https://www.bookiebashing.net/tools/match-xg/"

    # Scan settings
    SCAN_INTERVAL_SECONDS: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "120"))
    MIN_EV_PERCENT: float = float(os.getenv("MIN_EV_PERCENT", "2.0"))
    HEADLESS: bool = os.getenv("HEADLESS", "true").lower() == "true"

    # Alerts
    DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Markets
    MARKETS: list[str] = os.getenv("MARKETS", "fouls,throw_ins,corners,cards,shots").split(",")

    # Bookmakers
    BOOKMAKERS: list[str] = os.getenv(
        "BOOKMAKERS", "bet365,paddypower,williamhill,skybet,betfair,betfred"
    ).split(",")

    # Player stats validation (optional - API-Football free tier: 100 req/day)
    API_FOOTBALL_KEY: str = os.getenv("API_FOOTBALL_KEY", "")

    # Browser settings
    USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
