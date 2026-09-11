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


_FDO_CACHE: dict = {}
_FDO_LAST = [0.0]


def _fdo_key():
    return getattr(_config, "FOOTBALL_DATA_ORG_KEY", "") or os.environ.get("FOOTBALL_DATA_ORG_KEY", "")


def _fdo_get(url, params, timeout=15):
    """Cached GET with 6s spacing (FDO free tier: 10 req/min). Returns parsed JSON or None."""
    import json as _json
    ck = url + "|" + _json.dumps(params or {}, sort_keys=True)
    if ck in _FDO_CACHE:
        return _FDO_CACHE[ck]
    key = _fdo_key()
    if not key:
        return None
    wait = 6.0 - (time.time() - _FDO_LAST[0])
    if wait > 0:
        time.sleep(wait)
    try:
        r = _rq.get(url, params=params, headers={"X-Auth-Token": key}, timeout=timeout)
        _FDO_LAST[0] = time.time()
        if r.status_code != 200:
            return None
        d = r.json()
        _FDO_CACHE[ck] = d
        return d
    except Exception:
        _FDO_LAST[0] = time.time()
        return None


def team_recent_struct(team, limit=8):
    """Structured recent finished matches for a team (shared by scout + analyst).
    Read-through weekly DB cache: Monday's fetch serves all week; FDO (10/min)
    is only hit for uncached teams. Returns {'matches': [...], 'tid': id}."""
    out = {"matches": [], "tid": None}
    try:
        try:
            from learner import get_cached_form, save_cached_form
        except ImportError:
            from worker.learner import get_cached_form, save_cached_form  # type: ignore
        hit = get_cached_form(team)
        if isinstance(hit, dict) and hit.get("matches"):
            return hit
    except Exception:
        pass
    try:
        d = _fdo_get(f"{_FDO_BASE}/teams", {"name": team})
        teams = (d or {}).get("teams") or []
        if not teams:
            return out
        tid = teams[0].get("id")
        out["tid"] = tid
        d2 = _fdo_get(f"{_FDO_BASE}/teams/{tid}/matches", {"status": "FINISHED", "limit": limit})
        for m in ((d2 or {}).get("matches") or [])[:limit]:
            sc = (m.get("score") or {}).get("fullTime") or {}
            try:
                hs, aws = int(sc.get("home")), int(sc.get("away"))
            except Exception:
                continue
            out["matches"].append({
                "home": (m.get("homeTeam") or {}).get("name", ""),
                "away": (m.get("awayTeam") or {}).get("name", ""),
                "hs": hs, "aws": aws,
                "comp": (m.get("competition") or {}).get("name", ""),
            })
    except Exception:
        pass
    if out["matches"]:
        try:
            save_cached_form(team, out)
        except Exception:
            pass
    return out


def _struct_to_text(home, away, hs_struct, as_struct):
    lines = []
    seen = set()
    for m in (hs_struct.get("matches") or []) + (as_struct.get("matches") or []):
        k = (m["home"], m["away"], m["hs"], m["aws"])
        if k in seen:
            continue
        seen.add(k)
        lines.append(f"{m['home']} {m['hs']}-{m['aws']} {m['away']} ({m['comp']})")
        if len(lines) >= 10:
            break
    if not lines:
        return ""
    out = f"Recent finished matches involving these clubs: {'; '.join(lines)}."
    last = (home.split() or [""])[-1].lower()
    h2h = [l for l in lines if last and last in l.lower()]
    if len(lines) >= 2 and not h2h:
        out += " No past meetings found in this sample."
    return out


def get_history(home, away, league="", timeout=15, struct=None):
    """Recent form + H2H text via football-data.org (free 10/min). Best-effort, '' on failure.
    Pass struct=(home_struct, away_struct) to reuse scout-fetched data (no double spend)."""
    try:
        if struct:
            return _struct_to_text(home, away, struct[0], struct[1])
        if not _fdo_key():
            return ""
        return _struct_to_text(home, away, team_recent_struct(home), team_recent_struct(away))
    except Exception:
        return ""


def get_news(home, away, league="", timeout=20):
    """Team news: DuckDuckGo first (free, unlimited); Tavily only when DDG is thin."""
    q = f"{home} vs {away} {league} prediction team news injuries"
    ddg_bits = []
    try:
        r = _rq.post(_DDG_BASE, data={"q": q + " injuries lineup"}, timeout=timeout,
                     headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            texts = re.findall(r'class="result-snippet"[^>]*>(.*?)</', r.text)
            clean = [re.sub(r"<.*?>", "", t).strip() for t in texts[:5]]
            ddg_bits = [t for t in clean if t]
    except Exception:
        pass
    if len(ddg_bits) >= 2:
        return "DDG: " + " | ".join(ddg_bits)[:1500]
    # Tavily only when DDG couldn't cover it
    tkey = os.environ.get("TAVILY_API_KEY", "")
    if tkey:
        try:
            r = _rq.post("https://api.tavily.com/search",
                         json={"api_key": tkey, "query": q, "max_results": 5,
                               "include_answer": True}, timeout=timeout)
            if r.status_code == 200:
                d = r.json()
                bits = list(ddg_bits)
                if d.get("answer"):
                    bits.append("Tavily: " + str(d["answer"])[:600])
                for res in (d.get("results") or [])[:4]:
                    bits.append(f"{res.get('title', '')}: {str(res.get('content', ''))[:300]}")
                if bits:
                    return " | ".join(bits)[:2000]
        except Exception:
            pass
    if ddg_bits:
        return "DDG: " + " | ".join(ddg_bits)[:1500]
    return ""


def _persona_weights() -> dict:
    """Historical reliability from the evening learner (1.0 = average). Never raises."""
    try:
        try:
            from learner import get_strategy
        except ImportError:
            from worker.learner import get_strategy  # type: ignore
        return dict((get_strategy() or {}).get("persona_weights") or {})
    except Exception:
        return {}


_ASK_N = [0]
_PROVIDERS = [None]


def _rotation():
    """Providers actually keyed (None = full chain). Rebuilt lazily."""
    try:
        avail = [p for p in ("groq", "gemini") if _router.has_provider(p)]
        return avail or [None]
    except Exception:
        return [None]


def _ask(prompt, system="", max_chars=1200, tries=1, gated=True):
    """LLM call with daily budget gate (personas) — synthesizer passes gated=False.
    Provider rotation (Groq/Gemini alternate) spreads rate-limit load.
    When the budget is spent, personas abstain (implied/base carries the leg)."""
    if gated:
        try:
            try:
                from learner import llm_left, log_llm
            except ImportError:
                from worker.learner import llm_left, log_llm  # type: ignore
            if llm_left() <= 0:
                return None
            log_llm()
        except Exception:
            pass
    _ASK_N[0] += 1
    provs = _rotation()
    pref = provs[_ASK_N[0] % len(provs)]
    for attempt in range(max(1, tries)):
        try:
            text, _model, err, _el = _router.analyze(prompt, system_prompt=system, model_pref=pref)
            if err or not text:
                time.sleep(8)
                continue
            return str(text)[:max_chars]
        except Exception:
            time.sleep(8)
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


def analyze_finalist(leg, progress_cb=None, history_struct=None):
    """Full swarm on ONE shortlisted leg. Returns (prob, why, analysis). Never raises.
    history_struct=(home_struct, away_struct) reuses scout-fetched FDO data."""
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
    try:
        from market_scout import _leg_outcomes as _lo
    except ImportError:
        try:
            from worker.market_scout import _leg_outcomes as _lo  # type: ignore
        except ImportError:
            _lo = None  # type: ignore
    if _lo is not None:
        try:
            pairs = _lo(leg)
            outcomes = [{"name": n, "price": p} for n, p in pairs]
        except Exception:
            outcomes = []
    else:
        outcomes = leg.get("bookmakers", [{}])[0].get("markets", [{}])[0].get("outcomes", []) if leg.get("bookmakers") else []
    # SAME selection the scout picked (shared pick, never min-price).
    pick = str(leg.get("_pick") or "").lower()
    sel_out = next((o for o in outcomes if str(o.get("name", "")).lower() == pick), None) if pick else None
    if sel_out is not None:
        try:
            selection, odds = sel_out.get("name"), float(sel_out.get("price"))
        except Exception:
            selection, odds = leg.get("home_team", "?"), float(leg.get("best_odds", 2.0) or 2.0)
    else:
        try:
            fav = min(outcomes, key=lambda o: float(o.get("price", 999)))
            selection, odds = fav.get("name"), float(fav.get("price"))
        except Exception:
            selection, odds = leg.get("home_team", "?"), float(leg.get("best_odds", 2.0) or 2.0)
    base = _implied(odds)

    _msg(f"Analyst: {home} vs {away} ({selection}) — gathering history + news…")
    history = get_history(home, away, league, struct=history_struct)
    news = get_news(home, away, league)
    if not history:
        history = "No recent-form data available (coverage gap)."
    if not news:
        news = "No fresh team news found."

    brief = (f"Match: {home} vs {away}\nLeague: {league}\nMarket: {market}\n"
             f"Selection: {selection} @ {odds} (implied {base})\n"
             f"Data scout (pure-code models, no LLM): {(leg.get('_data') or (0, 0, ''))[2] if isinstance(leg.get('_data'), tuple) else ''}\n"
             f"History: {history[:900]}\nNews: {news[:1200]}")

    verdicts = []
    for pid, pname, psys in PERSONAS:
        time.sleep(2)  # RPM kindness across ~90 calls/build
        t = _ask(f"{brief}\n\nReply exactly:\nSCORE=<0-1 selection win probability>\nNOTE=<one-two sentences with specifics>",
                  system=f"You are {pname}. {psys}",
                  max_chars=600)
        s, n = _parse_persona(t)
        verdicts.append((pname, s, n))
    scored = " | ".join(f"{n}={s:.2f} ({note[:120]})" for n, s, note in verdicts)
    weights = _persona_weights()
    wline = ""
    if weights:
        wline = ("Historical reliability weights (1.0 = average, higher = trust more, "
                 "based on settled results): " +
                 ", ".join(f"{k}={v}" for k, v in weights.items()) + ". ")

    _msg(f"Analyst: {home} vs {away} — synthesizing {len(verdicts)} verdicts…")
    synth = _ask(f"Selection: {selection} @ {odds}. Statistical base probability: {base}.\n"
                  f"{wline}"
                  f"Specialist verdicts: {scored}\nHistory: {history[:600]}\nNews: {news[:800]}",
                  system=SYNTH_SYSTEM,
                  max_chars=1500, tries=3, gated=False)
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
