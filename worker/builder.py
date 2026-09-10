"""builder.py — Accumulator builder: scanner + (optional) fotmob/tavily + ai_router."""
import math
import os
import uuid
from datetime import datetime, timezone

import config

# scanner / ai_router / fotmob live alongside this file inside worker/
try:
    from scanner import OddsScanner
except ImportError:
    from worker.scanner import OddsScanner  # type: ignore

try:
    from ai_router import router
except ImportError:
    from worker.ai_router import router  # type: ignore

try:
    from fotmob import league_odds_to_fotmob, get_fotmob
    _HAS_FOTMOB = True
except ImportError:
    try:
        from worker.fotmob import league_odds_to_fotmob, get_fotmob  # type: ignore
        _HAS_FOTMOB = True
    except ImportError:
        _HAS_FOTMOB = False


def _tavily_search(query: str, max_results: int = 3) -> str:
    """Optional Tavily news context. Skipped gracefully when key missing."""
    key = config.TAVILY_API_KEY
    if not key:
        return ""
    try:
        import requests
        r = requests.post("https://api.tavily.com/search",
                          headers={"Content-Type": "application/json"},
                          json={"api_key": key, "query": query, "max_results": max_results,
                                "search_depth": "basic"},
                          timeout=15)
        if r.status_code == 200:
            items = r.json().get("results", [])
            return " | ".join(f"{i.get('title','')}: {i.get('content','')[:160]}" for i in items[:max_results])
    except Exception:
        pass
    return ""


def _implied_prob(odds: float) -> float:
    if not odds or odds <= 1.0:
        return 0.0
    return round(1.0 / float(odds), 4)


def _ai_assess(leg: dict, news: str = "") -> tuple[float, str]:
    """Ask the LLM router for a probability; fall back to implied prob on failure."""
    implied = _implied_prob(leg.get("best_odds") or leg.get("odds") or 2.0)
    prompt = (
        f"Match: {leg.get('home_team','?')} vs {leg.get('away_team','?')}\n"
        f"League: {leg.get('league','?')} Sport: {leg.get('sport','?')}\n"
        f"Best odds available: {leg.get('best_odds')} ({leg.get('best_bookmaker','')})\n"
        f"Outcomes: {leg.get('outcomes', [])}\n"
        f"News: {news[:600] if news else 'none'}\n"
        "Reply with exactly two lines:\nPROB=<0-1 favourite win probability>\nWHY=<one short sentence>"
    )
    try:
        out = router.analyze(prompt, system_prompt="You price sports bets. Be terse and numeric.")
        # router.analyze returns (text, model, err, elapsed)
        text = out[0] if isinstance(out, tuple) else None
        err = out[2] if isinstance(out, tuple) and len(out) > 2 else None
        if err or not text:
            return implied, f"implied {implied} (AI unavailable)"
        import re
        m = re.search(r"PROB\s*=\s*([0-9]*\.?[0-9]+)", text)
        p = float(m.group(1)) if m else implied
        if p > 1.0:  # model gave percent
            p = p / 100.0
        p = min(max(p, 0.01), 0.99)
        why = text.strip().splitlines()[-1][:220]
        return round(p, 4), why
    except Exception as e:
        return implied, f"implied {implied} (AI error: {e})"


def scan_and_extract(max_credits: int | None = None, progress_cb=None) -> list:
    s = OddsScanner()
    results = s.scan_all(max_credits=(max_credits or config.MAX_CREDITS_PER_SCAN), callback=progress_cb)
    legs = s.extract_legs(results, min_odds=config.MIN_ODDS, max_odds=config.MAX_ODDS)
    return legs


def _enrich_with_fotmob(leg: dict) -> dict:
    if not _HAS_FOTMOB:
        return leg
    try:
        lid = league_odds_to_fotmob(leg.get("sport_key", ""))
        if not lid:
            return leg
        fm = get_fotmob()
        form = fm.extract_form_summary(lid)
        if isinstance(form, dict) and form and "error" not in form:
            for team_key in (leg.get("home_team", ""), leg.get("away_team", "")):
                if team_key in form:
                    leg.setdefault("form", {})[team_key] = form[team_key]
    except Exception:
        pass
    return leg


def build_tickets(max_legs: int | None = None, use_ai: bool = True,
                  max_credits: int | None = None, progress_cb=None) -> list:
    """Build accumulator ticket(s). Returns list of ticket dicts (also persisted by caller or main)."""
    max_legs = max_legs or config.MAX_LEGS_PER_ACCA
    legs = scan_and_extract(max_credits=max_credits, progress_cb=progress_cb)

    if not legs:
        return []

    # De-duplicate correlated legs: one leg per match id, diverse leagues preferred
    seen, diverse = set(), []
    for leg in legs:
        lid = leg.get("id")
        if lid in seen:
            continue
        seen.add(lid)
        diverse.append(leg)

    # Prefer mid-odds value zone first, then fill
    diverse.sort(key=lambda l: abs(float(l.get("best_odds", 2.0)) - 2.2))
    candidates = diverse[: max(12, max_legs * 2)]

    built = []
    for leg in candidates[:max_legs]:
        _enrich_with_fotmob(leg)
        news = ""
        if use_ai and os.environ.get("TAVILY_API_KEY"):
            news = _tavily_search(f"{leg.get('home_team')} vs {leg.get('away_team')} {leg.get('league')} prediction injuries")
        prob, why = _ai_assess(leg, news) if use_ai else (_implied_prob(leg.get("best_odds", 2.0)), "implied (AI off)")
        # pick favourite outcome = outcome with lowest price
        outcomes = leg.get("outcomes", []) or []
        if outcomes:
            fav = min(outcomes, key=lambda o: float(o.get("price", 999)))
            selection, odds = fav.get("name", leg.get("home_team")), float(fav.get("price", leg.get("best_odds", 2.0)))
        else:
            selection, odds = leg.get("home_team", "?"), float(leg.get("best_odds", 2.0))
        built.append({
            "sport": leg.get("sport", leg.get("sport_key", "")),
            "league": leg.get("league", ""),
            "match": f"{leg.get('home_team','?')} vs {leg.get('away_team','?')}",
            "selection": selection,
            "odds": round(odds, 3),
            "probability": prob,
            "result": "pending",
            "reason": why,
            "commence_time": leg.get("commence_time", ""),
            "bookmaker": leg.get("best_bookmaker", ""),
        })

    if not built:
        return []

    combined = round(math.prod(max(float(b["odds"]), 1.01) for b in built), 3)
    ticket = {
        "id": f"acca-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "combined_odds": combined,
        "legs": built,
        "status": "pending",
    }
    return [ticket]


def build_and_save(max_legs: int | None = None, use_ai: bool = True,
                   max_credits: int | None = None, progress_cb=None) -> list:
    """Build tickets and persist to DB. Returns ticket list."""
    import db as db_mod
    try:
        import db  # worker-local
    except ImportError:
        db = db_mod
    else:
        db = db_mod
    tickets = build_tickets(max_legs=max_legs, use_ai=use_ai, max_credits=max_credits, progress_cb=progress_cb)
    for t in tickets:
        db.save_ticket(t["id"], t["combined_odds"], t["legs"], t["status"])
    return tickets


if __name__ == "__main__":
    print("AI:", "on" if os.environ.get("GROQ_API_KEY") or os.environ.get("GEMINI_API_KEY") else "off (implied probs)")
    ts = build_tickets(max_legs=3, use_ai=False)
    print(f"built {len(ts)} ticket(s) without AI")
    if ts:
        print("combined:", ts[0]["combined_odds"], "legs:", len(ts[0]["legs"]))
