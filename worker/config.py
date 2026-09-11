"""config.py — Central config, all values from env vars. NEVER hardcode keys."""
import os


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# API keys (never hardcode — always env)
ODDS_API_KEY = _get("ODDS_API_KEY")
TAVILY_API_KEY = _get("TAVILY_API_KEY")
GROQ_API_KEY = _get("GROQ_API_KEY")
GEMINI_API_KEY = _get("GEMINI_API_KEY")

# Neon Postgres connection string
DATABASE_URL = _get("DATABASE_URL")

# football-data.org (free tier: soccer scores fallback, 10 req/min)
FOOTBALL_DATA_ORG_KEY = _get("FOOTBALL_DATA_ORG_KEY")

# Worker settings
# Odds sources: Betika bookmaker API is primary (free, keyless, zero quota,
# real stakeable prices). Smarkets exchange is secondary (sharp, wins ties).
# The Odds API (paid quota) is fallback only, guarded by ODDS_MIN_FLOOR.
BETIKA_ON = _get("BETIKA_ON", "1") == "1"
# Analyst swarm (draw-predictor 15-agent pattern, adapted multi-market):
# deep multi-persona verdicts on shortlisted finalists only (history FDO + news Tavily->DDG).
ANALYST_ON = _get("ANALYST_ON", "1") == "1"
ANALYST_MAX = int(_get("ANALYST_MAX", "10"))
SMARKETS_ON = _get("SMARKETS_ON", "1") == "1"  # Smarkets exchange as 2nd free source
ODDS_MIN_FLOOR = int(_get("ODDS_MIN_CREDITS_FLOOR", "50"))
# Scan focus: comma-separated groups (soccer,basketball) and/or sport keys.
# Empty = all groups. Narrow this to stretch the Odds API monthly quota.
SCAN_FOCUS = _get("SCAN_FOCUS", "")
MAX_CREDITS_PER_SCAN = int(_get("MAX_CREDITS_PER_SCAN", "400"))
MAX_LEGS_PER_ACCA = int(_get("MAX_LEGS_PER_ACCA", "20"))  # ceiling 20, never forced: quality floor (0.55 past 6 legs) + rank trim decide
SCOUT_FIXTURES = int(_get("SCOUT_FIXTURES", "24"))  # fixtures given FDO history per build (10 req/min free tier)
MIN_ODDS = float(_get("MIN_ODDS", "1.5"))
MAX_ODDS = float(_get("MAX_ODDS", "7.0"))
# Kickoff windows: daily slips only near-term fixtures, weekly up to 7 days out
KICKOFF_HOURS_DAILY = int(_get("KICKOFF_HOURS_DAILY", "48"))
KICKOFF_HOURS_WEEKLY = int(_get("KICKOFF_HOURS_WEEKLY", "168"))
PORT = int(_get("PORT", "8000"))

# Frontend -> worker URL (used by Vercel API routes)
WORKER_URL = _get("WORKER_URL", _get("NEXT_PUBLIC_WORKER_URL", "http://localhost:8000"))


def as_dict() -> dict:
    """Safe dict for /health diagnostics — never leaks key values."""
    def masked(v: str) -> str:
        return "set" if v else "missing"
    return {
        "ODDS_API_KEY": masked(ODDS_API_KEY),
        "TAVILY_API_KEY": masked(TAVILY_API_KEY),
        "GROQ_API_KEY": masked(GROQ_API_KEY),
        "GEMINI_API_KEY": masked(GEMINI_API_KEY),
        "FOOTBALL_DATA_ORG_KEY": masked(FOOTBALL_DATA_ORG_KEY),
        "DATABASE_URL": masked(DATABASE_URL),
        "MAX_LEGS_PER_ACCA": MAX_LEGS_PER_ACCA,
        "MIN_ODDS": MIN_ODDS,
        "MAX_ODDS": MAX_ODDS,
    }


if __name__ == "__main__":
    print(as_dict())
