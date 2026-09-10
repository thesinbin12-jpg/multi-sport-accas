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
MAX_CREDITS_PER_SCAN = int(_get("MAX_CREDITS_PER_SCAN", "400"))
MAX_LEGS_PER_ACCA = int(_get("MAX_LEGS_PER_ACCA", "6"))
MIN_ODDS = float(_get("MIN_ODDS", "1.4"))
MAX_ODDS = float(_get("MAX_ODDS", "25.0"))
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
