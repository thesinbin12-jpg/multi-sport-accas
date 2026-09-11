"""
analyst.py — Multi-persona analyst swarm for acca legs (draw-predictor pattern).

Funnel: builder sends only shortlisted finalists (~12 legs). Each finalist gets:
  history (football-data.org recent form + H2H, best-effort) +
  news (Tavily first, DuckDuckGo fallback) +
  9 persona verdicts -> synthesizer -> PROB + written reasoning + full analysis.

Same LLM provider as everything else (AIRouter: Groq primary, Gemini fallback).
Any failure degrades gracefully to implied probability — builds never break.
"""

import os
import re
import time
from datetime import datetime, timezone

try:
    from ai_router import router as _router
except ImportError:
    from worker.ai_router import router as _router  # type: ignore

try:
    import config as _config
except ImportError:
    from worker import config as _config  # type: ignore

import requests as _rq

_FDO_BASE = "https://api.football-data.org/v4"
_DDG_BASE = "https://lite.duckduckgo.com/lite/"

PERSONAS = [
    ("form", "The Form Guide",
     "You study recent form only. Weigh last-5 results, home/away splits, goals scored/conceded. "
     "Cite specific numbers from the history given. If history is thin, say so and abstain toward 0.5."),
    ("h2h", "The Head-to-Head Historian",
     "You study past meetings between these exact teams. Cite scores and patterns (repeat winners, "
     "draw frequency, BTTS frequency). If no meetings are given, abstain toward 0.5 and say so."),
    ("news", "The Team News Scout",
     "You read team news only: injuries, suspensions,likely lineups, new signings, manager quotes. "
     "Name the actual players/issues from the news given. If the news says nothing useful, abstain toward 0.5."),
    ("tactics", "The Tactician",
     "You study styles: possession vs counter, press intensity, set-piece threat, pace in attack, "
     "defensive line height. Reason about how the styles collide for THIS market. No generic phrases."),
    ("motivation", "The Motivation Reader",
     "You read stakes: title race, Europe places, relegation, derby rivalry, cup rotation, dead rubber. "
     "Who needs it more and why? If neither side has special motivation, say so."),
    ("league", "The League Context Expert",
     "You know league baselines: how often this league produces home wins, draws, BTTS, over 2.5. "
     "Judge whether the bookmaker price is generous or stingy versus baseline. Cite the baseline used."),
    ("market", "The Market Skeptic",
     "You trust prices, not stories. Convert the odds to implied probability, compare with the other "
     "personas' direction, and flag when the market disagrees with the narrative (the market is often right)."),
    ("devil", "The Devil's Advocate",
     "Your job is to kill this bet. List the 2-3 most realistic ways it loses, with specifics "
     "(which player, which pattern, which stat). Never agree by default."),
    ("outside", "The Outside Factors Watcher",
     "You watch weather, pitch, travel/fatigue (3 games in 7 days?), referee card/penalty trends, kickoff time. "
     "Only cite factors actually present in the data. If none, abstain toward 0.5."),
]

SYNTH_SYSTEM = (
    "You are the chief analyst of a betting syndicate. Nine specialists gave verdicts. "
    "Weigh them: form + h2h + news carry most weight; devil's advocate must move you unless rebutted; "
    "abstentions (0.5 with no evidence) carry almost no weight. "
    "Base probability starts at the statistical base given, adjusted by evidence — never by gut. "
    "Reply with exactly three lines:\n"
    "PROB=<0-1 number>\nWHY=<2-4 sentences citing specific teams, players, numbers — no generic filler>\n"
    "DETAIL=<one short paragraph per specialist view actually used, semicolon-separated>"
)


def _implied(odds):
    try:
        o = float(odds)
        return round(1.0 / o, 4) if o > 1.0 else 0.5
    except Exception:
        return 0.5


def get_history(home, away, league="", timeout=15):
    """Recent form + H2H via football-data.org (free 10/min). Best-effort, '' on failure."""
    key = getattr(_config, "FOOTBALL_DATA_ORG_KEY", "") or os.environ.get("FOOTBALL_DATA_ORG_KEY", "")
    if not key:
        return ""
    try:
        h = {"X-Auth-Token": key}
        # find team ids
        ids = {}
        for team in (home, away):
            r = _rq.get(f"{_FDO_BASE}/teams", params={"name": team}, headers=h, timeout=timeout)
            if r.status_code != 200:
                continue
            teams = (r.json().get("teams") or [])
            if teams:
                ids[team] = teams[0].get("id")
        if not ids:
            return ""
        tid = ids.get(home) or ids.get(away)
        r = _rq.get(f"{_FDO_BASE}/teams/{tid}/matches",
                    params={"status": "FINISHED", "limit": 6}, headers=h, timeout=timeout)
        if r.status_code != 200:
            return ""
        lines = []
        for m in (r.json().get("matches") or [])[:6]:
            sc = (m.get("score") or {}).get("fullTime") or {}
            lines.append(f"{m.get('homeTeam', {}).get('shortName') or m.get('homeTeam', {}).get('name')} "
                         f"{sc.get('home', '?')}-{sc.get('away', '?')} "
                         f"{m.get('awayTeam', {}).get('shortName') or m.get('awayTeam', {}).get('name')} "
                         f"({m.get('competition', {}).get('name', '')})")
        out = f"Recent finished matches involving these clubs: {'; '.join(lines)}." if lines else ""
        # H2H via finished matches of home team filtered to meetings with away team
        h2h = [l for l in lines if away.split()[-1].lower() in l.lower() or home.split()[-1].lower() in l.lower()]
        if len(lines) >= 2 and not h2h:
            out += " No past meetings found in this sample."
        time.sleep(6)  # respect 10 req/min free tier
        return out
    except Exception:
        return ""


def get_news(home, away, league="", timeout=20):
    """Team news: Tavily first, DuckDuckGo fallback (draw-predictor pattern)."""
    q = f"{home} vs {away} {league} prediction team news injuries"
    # Tavily
    tkey = os.environ.get("TAVILY_API_KEY", "")
    if tkey:
        try:
            r = _rq.post("https://api.tavily.com/search",
                         json={"api_key": tkey, "query": q, "max_results": 5,
                               "include_answer": True}, timeout=timeout)
            if r.status_code == 200:
                d = r.json()
                bits = []
                if d.get("answer"):
                    bits.append(str(d["answer"])[:600])
                for res in (d.get("results") or [])[:4]:
                    bits.append(f"{res.get('title', '')}: {str(res.get('content', ''))[:300]}")
                if bits:
                    return " | ".join(bits)[:2000]
        except Exception:
            pass
    # DuckDuckGo fallback (free, unlimited)
    try:
        r = _rq.post(_DDG_BASE, data={"q": q + " injuries lineup"}, timeout=timeout,
                     headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            texts = re.findall(r'class="result-snippet"[^>]*>(.*?)</', r.text)
            clean = [re.sub(r"<.*?>", "", t).strip() for t in texts[:5]]
            clean = [t for t in clean if t]
            if clean:
                return "DDG: " + " | ".join(clean)[:1500]
    except Exception:
        pass
    return ""


def _ask(prompt, system="", max_chars=1200, tries=1):
    for attempt in range(max(1, tries)):
        try:
            text, _model, err, _el = _router.analyze(prompt, system_prompt=system)
            if err or not text:
                time.sleep(3)
                continue
            return str(text)[:max_chars]
        except Exception:
            time.sleep(3)
    return None


def _parse_persona(text):
    if not text:
        return 0.5, "abstained (no response)"
    m = re.search(r"SCORE\s*=\s*(0?\.\d+|1(?:\.0)?|0|1)", text)
    score = float(m.group(1)) if m else 0.5
    score = max(0.01, min(0.99, score))
    n = re.search(r"NOTE\s*=\s*(.+)", text, re.DOTALL)
    note = n.group(1).strip()[:300] if n else text.strip()[:300]
    return score, note


def analyze_finalist(leg, progress_cb=None):
    """Full swarm on ONE shortlisted leg. Returns (prob, why, analysis). Never raises."""
    def _msg(m):
        if progress_cb:
            try:
                progress_cb(m)
            except Exception:
                pass

    home = leg.get("home_team", "?")
    away = leg.get("away_team", "?")
    league = leg.get("league", "?")
    market = leg.get("market", "1X2")
    outcomes = leg.get("bookmakers", [{}])[0].get("markets", [{}])[0].get("outcomes", []) if leg.get("bookmakers") else []
    try:
        fav = min(outcomes, key=lambda o: float(o.get("price", 999)))
        selection, odds = fav.get("name"), float(fav.get("price"))
    except Exception:
        selection, odds = leg.get("home_team", "?"), float(leg.get("best_odds", 2.0) or 2.0)
    base = _implied(odds)

    _msg(f"Analyst: {home} vs {away} ({selection}) — gathering history + news…")
    history = get_history(home, away, league)
    news = get_news(home, away, league)
    if not history:
        history = "No recent-form data available (coverage gap)."
    if not news:
        news = "No fresh team news found."

    brief = (f"Match: {home} vs {away}\nLeague: {league}\nMarket: {market}\n"
             f"Selection: {selection} @ {odds} (implied {base})\n"
             f"History: {history[:900]}\nNews: {news[:1200]}")

    verdicts = []
    for pid, pname, psys in PERSONAS:
        t = _ask(f"{brief}\n\nReply exactly:\nSCORE=<0-1 selection win probability>\nNOTE=<one-two sentences with specifics>",
                  system=f"You are {pname}. {psys}",
                  max_chars=600)
        s, n = _parse_persona(t)
        verdicts.append((pname, s, n))
    scored = " | ".join(f"{n}={s:.2f} ({note[:120]})" for n, s, note in verdicts)

    _msg(f"Analyst: {home} vs {away} — synthesizing {len(verdicts)} verdicts…")
    synth = _ask(f"Selection: {selection} @ {odds}. Statistical base probability: {base}.\n"
                  f"Specialist verdicts: {scored}\nHistory: {history[:600]}\nNews: {news[:800]}",
                  system=SYNTH_SYSTEM,
                  max_chars=1500, tries=3)
    prob, why, detail = base, f"implied {base} (synthesizer unavailable)", ""
    if synth:
        m = re.search(r"PROB\s*=\s*(0?\.\d+|1(?:\.0)?|0|1)", synth)
        if m:
            prob = max(0.01, min(0.99, float(m.group(1))))
        w = re.search(r"WHY\s*=\s*(.+?)(?:\nDETAIL\s*=|\Z)", synth, re.DOTALL)
        if w:
            why = w.group(1).strip()[:600]
        d = re.search(r"DETAIL\s*=\s*(.+)", synth, re.DOTALL)
        if d:
            detail = d.group(1).strip()[:1500]
    if not detail:
        detail = "; ".join(f"{n}: {note[:150]}" for n, _s, note in verdicts)[:1500]
    scores = "|".join(f"{n}={s:.2f}" for n, s, _note in verdicts)
    detail = f"{detail} || SCORES: {scores}"[:1800]
    return prob, why, detail
