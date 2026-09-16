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
import html
import threading
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
_FDO_LOCK = threading.Lock()


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
    with _FDO_LOCK:
        wait = 6.0 - (time.time() - _FDO_LAST[0])
        if wait > 0:
            time.sleep(wait)
        try:
            r = _rq.get(url, params=params, headers={"X-Auth-Token": key}, timeout=timeout)
            _FDO_LAST[0] = time.time()
        except Exception:
            _FDO_LAST[0] = time.time()
            return None
    try:
        if r.status_code != 200:
            return None
        d = r.json()
        _FDO_CACHE[ck] = d
        return d
    except Exception:
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
        best, best_score = None, 0.0
        for variant in _name_variants(team):
            d = _fdo_get(f"{_FDO_BASE}/teams", {"name": variant})
            for cand in ((d or {}).get("teams") or [])[:5]:
                s = _name_score(team, cand.get("name", ""))
                s += 0.1 if str(cand.get("name", "")).lower().startswith(str(team or "").lower()[:4]) else 0.0
                if s > best_score:
                    best, best_score = cand, s
            if best_score >= 0.85:
                break
        if not best or best_score < 0.45:
            return out
        tid = best.get("id")
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


_STOPWORDS = {"fc", "ac", "sc", "fk", "ifk", "sk", "bk", "as", "ss", "us", "cd", "ud",
              "cf", "afc", "united", "city", "town", "rovers", "wanderers", "athletic",
              "sporting", "real", "club", "de", "la", "le", "les", "al", "el", "fc-"}


def _name_variants(team):
    toks = re.split(r"[\s.\-']+", str(team or ""))
    toks = [t for t in toks if t]
    variants = []
    full = " ".join(toks)
    if full:
        variants.append(full)
    stripped = [t for t in toks if t.lower() not in _STOPWORDS]
    if stripped and " ".join(stripped) != full:
        variants.append(" ".join(stripped))
    if len(stripped) > 1:
        variants.append(stripped[-1])
    elif toks:
        variants.append(toks[-1])
    seen, out = set(), []
    for v in variants:
        k = v.lower()
        if k and k not in seen and len(k) >= 3:
            seen.add(k)
            out.append(v)
    return out[:3]


def _lastw(name):
    parts = str(name or "").split()
    return parts[-1].lower() if parts else ""


def _stem(toks):
    out = []
    for t in toks:
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def _name_score(query, candidate):
    import difflib as _d
    q = _stem([t for t in re.split(r"[\s.\-']+", str(query or "").lower()) if t not in _STOPWORDS])
    c = _stem([t for t in re.split(r"[\s.\-']+", str(candidate or "").lower()) if t not in _STOPWORDS])
    if not q or not c:
        return 0.0
    overlap = len(set(q) & set(c)) / max(len(set(q)), 1)
    seq = _d.SequenceMatcher(None, " ".join(q), " ".join(c)).ratio()
    return 0.6 * overlap + 0.4 * seq


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


_RSS_FEEDS = [
    "https://www.skysports.com/rss/12040",
    "https://www.theguardian.com/football/rss",
]
_RSS_CACHE: dict = {}


def _rss_items():
    """Sky + Guardian football headlines, fetched once per UTC date. Free, keyless."""
    import xml.etree.ElementTree as _ET
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if day in _RSS_CACHE:
        return _RSS_CACHE[day]
    items = []
    for url in _RSS_FEEDS:
        try:
            r = _rq.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if r.status_code != 200 or not r.text.strip():
                continue
            root = _ET.fromstring(r.text)
            for it in root.findall(".//item")[:40]:
                title = (it.findtext("title") or "").strip()
                desc = re.sub(r"<.*?>", "", it.findtext("description") or "").strip()
                if title:
                    items.append((title, desc[:300]))
        except Exception:
            continue
    _RSS_CACHE[day] = items
    return items


def _rss_for(home, away, limit=3):
    """Headlines naming either club (stemmed last-word match)."""
    ht = set(_stem([_lastw(home)])) - {''}
    at = set(_stem([_lastw(away)])) - {''}
    got = []
    for title, desc in _rss_items():
        blob = set(_stem(re.split(r"[\s.\-']+", title.lower())))
        if (ht and ht & blob) or (at and at & blob):
            got.append(f"{title}" + (f": {desc[:150]}" if desc else ""))
            if len(got) >= limit:
                break
    return got


def _brave_search(query, timeout=20, limit=5):
    """Brave web search via curl_cffi (free, keyless, TLS-impersonated).
    Returns ['title: snippet', ...]. Empty on any failure. Brave 429s fast
    probing, so 2s pacing keeps build-time calls gentle."""
    try:
        time.sleep(2)
        try:
            from curl_cffi import requests as _cr
        except ImportError:
            return []
        s = _cr.Session(impersonate="chrome120")
        r = s.get("https://search.brave.com/search", params={"q": query}, timeout=timeout)
        if r.status_code != 200 or "data-type=\"web\"" not in r.text:
            return []
        out = []
        for b in r.text.split('data-type="web"')[1:]:
            tm = re.search(r'title="([^"]{10,160})"', b)
            dm = re.search(r'class="[^"]*snippet-description[^"]*"[^>]*>(.*?)</', b, re.DOTALL)
            title = html.unescape(tm.group(1)).strip() if tm else ""
            desc = html.unescape(re.sub(r"<[^>]+>", " ", dm.group(1))) if dm else ""
            desc = re.sub(r"\s+", " ", desc).strip()[:250]
            if title:
                out.append(title + (": " + desc if desc else ""))
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def get_news(home, away, league="", timeout=20):
    """Team news, free stack: RSS match -> Brave search -> DuckDuckGo; Tavily only when all thin."""
    q = f"{home} vs {away} {league} prediction team news injuries"
    rss_bits = []
    try:
        rss_bits = _rss_for(home, away)
    except Exception:
        pass
    brave_bits = []
    try:
        brave_bits = ["Brave: " + b for b in _brave_search(f"{home} vs {away} injuries lineup")]
    except Exception:
        pass
    ddg_bits = []
    try:
        r = _rq.post(_DDG_BASE, data={"q": q + " injuries lineup"}, timeout=timeout,
                     headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            texts = re.findall(r"class=['\"]result-snippet['\"][^>]*>(.*?)</", r.text)
            clean = [re.sub(r"<.*?>", "", t).strip() for t in texts[:5]]
            ddg_bits = [t for t in clean if t]
    except Exception:
        pass
    if rss_bits:
        ddg_bits = ["RSS: " + b for b in rss_bits] + brave_bits + ddg_bits
    elif brave_bits:
        ddg_bits = brave_bits + ddg_bits
    if len(ddg_bits) >= 2:
        return " | ".join(ddg_bits)[:1500]
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
        return " | ".join(ddg_bits)[:1500]
    return ""


def _learner_context(league=""):
    """Evening-learner feedback for the morning analyst: strategy, lessons,
    persona trust, painful post-mortems. Read live each build (cheap DB reads)."""
    try:
        try:
            from learner import get_strategy, recent_loss_notes, _last_debrief
        except ImportError:
            from worker.learner import get_strategy, recent_loss_notes, _last_debrief  # type: ignore
        try:
            try:
                from learner import calibration_line as _cline
            except ImportError:
                from worker.learner import calibration_line as _cline  # type: ignore
            _cl = _cline() or ""
        except Exception:
            _cl = ""
        strat = get_strategy() or {}
        notes = recent_loss_notes(league, 2) if league else []
        prev = _last_debrief() or {}
        lessons = []
        try:
            lessons = ((prev.get("summary") or {}).get("decision") or {}).get("lessons") or []
        except Exception:
            pass
        bits = []
        if strat.get("blocked_leagues"):
            bits.append("AVOID leagues: " + ", ".join(str(x)[:40] for x in strat["blocked_leagues"][:6]))
        if strat.get("preferred_band"):
            bits.append("prefer odds band " + str(strat["preferred_band"]))
        pw = strat.get("persona_weights") or {}
        if pw:
            bits.append("persona trust: " + ", ".join("%s=%s" % (k, v) for k, v in list(pw.items())[:9]))
        if lessons:
            bits.append("lessons: " + " | ".join(str(x)[:140] for x in lessons[:3]))
        if _cl:
            bits.append("SELF-KNOWLEDGE: " + _cl[:300])
        for ln in notes:
            bits.append("PAINFUL LESSON (%s %s): %s" % (ln.get("match"), ln.get("selection"), str(ln.get("why"))[:160]))
        return ("Learner feedback: " + " | ".join(bits))[:1200] if bits else ""
    except Exception:
        return ""


def _sim_line(home, away, selection, market, odds):
    try:
        try:
            from market_scout import _with_me, _team_stats, _h2h
        except ImportError:
            from worker.market_scout import _with_me, _team_stats, _h2h  # type: ignore
        try:
            from simulator import simulate, describe
        except ImportError:
            from worker.simulator import simulate, describe  # type: ignore
        hs = _with_me(team_recent_struct(home), home)
        aws = _with_me(team_recent_struct(away), away)
        hst, ast = _team_stats(hs), _team_stats(aws)
        sample = hst["gp"] + ast["gp"]
        if sample >= 2:
            exp_h = max(0.15, ((hst["gf_avg"] + ast["ga_avg"]) / 2) * 1.15)
            exp_a = max(0.10, ((ast["gf_avg"] + hst["ga_avg"]) / 2) * 0.95)
        else:
            exp_h, exp_a = 1.35, 1.15
        p_btts = (hst["scored_frac"] * ast["scored_frac"]) if sample else 0.5
        meetings = _h2h(hs, aws, home, away)
        tilt = 0.0
        if meetings:
            hw = sum(1 for m in meetings if m["hs"] > m["aws"])
            aw = sum(1 for m in meetings if m["aws"] > m["hs"])
            tilt = max(-1.0, min(1.0, (hw - aw) / max(1, len(meetings))))
        try:
            imp = 1.0 / float(odds) if float(odds) > 1 else 0.5
        except Exception:
            imp = 0.5
        imp = min(max(imp, 0.05), 0.9)
        try:
            # side-aware market triple (old code used the SELECTION price as the
            # HOME mass even for away picks, tilting every away leg homeward)
            _s = str(selection or "").lower()
            _h, _a = str(home or "").lower(), str(away or "").lower()
            _d0 = min(0.33, imp * 0.55)
            if _s == _h:
                _mkt = (imp, _d0, max(0.05, 1 - imp - _d0))
            elif _s == _a:
                _mkt = (max(0.05, 1 - imp - _d0), _d0, imp)
            elif _s == "draw":
                _r = max(0.05, (1 - imp) / 2)
                _mkt = (_r, imp, max(0.05, 1 - imp - _r))
            else:
                _mkt = (0.42, 0.28, 0.30)
        except Exception:
            _mkt = (imp, min(0.35, imp * 0.6), max(0.05, 1 - imp - min(0.35, imp * 0.6)))
        try:
            # md5, not hash(): str-hash is salted per process (old seed wandered)
            _seed = int(__import__("hashlib").md5(f"{home}|{away}".encode()).hexdigest(), 16) % 100000
        except Exception:
            _seed = 7
        sim = simulate(exp_h, exp_a, p_btts, n=2000, seed=_seed,
                       h2h_tilt=tilt, market_mix=0.2,
                       mkt_h=_mkt[0], mkt_d=_mkt[1], mkt_a=_mkt[2])
        return describe(sim, selection, market, home, away)
    except Exception:
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
_LAST_PREF = [None]
_RPM: dict = {}
_RPM_LOCK = threading.Lock()
_RPM_LIMITS = {"nim": 35, "groq": 18, "gemini": 15, "orouter": 8, None: 12}


def _rpm_wait(pref):
    """Per-provider per-minute throttle shared across threads. Never raises."""
    try:
        limit = _RPM_LIMITS.get(pref, 15)
        with _RPM_LOCK:
            now = time.time()
            b = _RPM.get(pref)
            if not b or now - b[0] >= 60:
                _RPM[pref] = [now, 1]
                return
            if b[1] >= limit:
                time.sleep(max(0.0, 60 - (now - b[0])))
                _RPM[pref] = [time.time(), 1]
            else:
                b[1] += 1
    except Exception:
        pass


def _rotation(exclude=None):
    """Providers keyed (None = full chain). NVIDIA NIM primary (40 RPM, tested).
    Groq workhorse next; OpenRouter precious (50/day): overflow + synth prefer.
    JUDGE_PROVIDER (default gemini) is reserved for the stake judge: the swarm
    never touches it, so the judge always has a fresh, unthrottled lane."""
    try:
        avail = []
        if _router.has_provider("nim"):
            avail.append("nim")
        avail += [p for p in ("groq", "gemini") if _router.has_provider(p)]
        try:
            from learner import or_left
        except ImportError:
            from worker.learner import or_left  # type: ignore
        if _router.has_provider("orouter") and or_left() > 0:
            avail = avail + ["orouter"]
        if exclude is None:
            try:
                exclude = os.environ.get("JUDGE_PROVIDER", "gemini")
            except Exception:
                exclude = "gemini"
        if exclude and len(avail) > 1:
            avail = [p for p in avail if p != exclude] or avail
        return avail or [None]
    except Exception:
        return [None]


def _ask(prompt, system="", max_chars=1200, tries=1, gated=True, stage="swarm", exclude=None, prefer=None):
    """One task, ALL providers together: attempts cycle through every keyed
    provider, so a single dead provider never sinks the task."""
    if gated:
        try:
            try:
                from learner import llm_left, log_llm, or_left, log_or
            except ImportError:
                from worker.learner import llm_left, log_llm, or_left, log_or  # type: ignore
            _ASK_N[0] += 1
            provs = _rotation(exclude)
            pref = provs[_ASK_N[0] % len(provs)]
            if pref == "orouter":
                if or_left() <= 0:
                    return None
                log_or()
            elif pref == "gemini":
                try:
                    from learner import gm_left, log_gm
                except ImportError:
                    from worker.learner import gm_left, log_gm  # type: ignore
                if gm_left() <= 0:
                    return None
                log_gm()
            else:
                if llm_left() <= 0:
                    return None
                log_llm()
        except Exception:
            _ASK_N[0] += 1
            provs = _rotation(exclude)
            pref = provs[_ASK_N[0] % len(provs)]
    else:
        _ASK_N[0] += 1
        provs = _rotation(exclude)
        pref = provs[_ASK_N[0] % len(provs)]
    _rpm_wait(pref)
    try:
        _start_idx = provs.index(pref) if pref in provs else 0
    except Exception:
        _start_idx = 0
    order = provs[_start_idx:] + provs[:_start_idx] if provs else [None]
    if prefer and prefer in provs:
        order = [prefer] + [p for p in order if p != prefer]
    last_model, last_err = "", ""
    # 2026-09-16: FULL ROTATION. Old loop did range(max(1,tries)) with
    # tries=1 at every call site -> ONE lane attempt; a dead preferred lane
    # (NIM outage, 5x "All models failed" 08:06-08:12) sank the call before
    # Groq/Gemini/OpenRouter ever ran. Now every provider gets one shot in
    # rotation order until one answers; healthy first-lane calls unchanged.
    for attempt in range(len(order) * max(1, tries)):
        pref = order[attempt % len(order)]
        _LAST_PREF[0] = pref
        _rpm_wait(pref)
        try:
            text, _model, err, _el = _router.analyze(prompt, system_prompt=system, model_pref=pref)
            last_model, last_err = str(_model or ""), str(err or "")
            if err or not text:
                if "429" in last_err or "rate" in last_err.lower() or "limit" in last_err.lower():
                    time.sleep(20)
                else:
                    time.sleep(8)
                continue
            try:
                try:
                    from learner import log_model as _lm
                except ImportError:
                    from worker.learner import log_model as _lm  # type: ignore
                _lm(last_model)
            except Exception:
                pass
            return str(text)[:max_chars]
        except Exception as e:
            last_err = str(e)[:200]
            time.sleep(8)
    try:
        try:
            from learner import log_llm_error
        except ImportError:
            from worker.learner import log_llm_error  # type: ignore
        log_llm_error(str(pref or "chain"), last_model, stage, last_err)
    except Exception:
        pass
    return None


BATCHES = [
    ("evidence", ("form", "h2h", "news")),
    ("context", ("tactics", "motivation", "league")),
    ("challenge", ("market", "devil", "outside")),
]

_PID2SYS = {}


def _ask_batch(pids, brief):
    """One LLM call, three persona verdicts. Returns [(pname, score, note)]."""
    global _PID2SYS
    if not _PID2SYS:
        _PID2SYS = {pid: (pn, ps) for pid, pn, ps in PERSONAS}
    parts = []
    for pid in pids:
        pn, ps = _PID2SYS.get(pid, (pid, ""))
        parts.append(f"[{pn}]\nRole: {ps}")
    t = _ask(f"{brief}\n\nYou are a panel of three specialists. Give EACH verdict separately in this exact shape:\n"
              + "\n".join(f"[{_PID2SYS.get(pid, (pid, ''))[0]}]\nSCORE=<0-1>\nNOTE=<one-two sentences with specifics>" for pid in pids)
              + "\n\nSpecialist briefs:\n" + "\n".join(parts),
              system="You are a betting analysis panel. Be specific, cite numbers and names, never generic filler.",
              max_chars=1800)
    out = []
    if t:
        for pid in pids:
            pn = _PID2SYS.get(pid, (pid, ""))[0]
            m = re.search(r"\[" + re.escape(pn) + r"\](.*?)(?=\[.+\]|\Z)", t, re.DOTALL)
            out.append((pn,) + _parse_persona(m.group(1) if m else ""))
    if len(out) < len(pids):
        have = {n for n, _, _ in out}
        for pid in pids:
            pn = _PID2SYS.get(pid, (pid, ""))[0]
            if pn not in have:
                out.append((pn, 0.5, "abstained (batch miss)"))
    return out


def _parse_persona(text):
    if not text:
        return 0.5, "abstained (no response)"
    m = re.search(r"SCORE\s*=\s*(0?\.\d+|1(?:\.0)?|0|1)", text)
    score = float(m.group(1)) if m else 0.5
    score = max(0.01, min(0.99, score))
    n = re.search(r"NOTE\s*=\s*(.+)", text, re.DOTALL)
    note = n.group(1).strip()[:300] if n else text.strip()[:300]
    return score, note


def _fm_line(leg: dict) -> str:
    """One-line FotMob recent-form for the brief (builder-attached). '' when absent."""
    try:
        fm = leg.get("form") or {}
        if not isinstance(fm, dict) or not fm:
            return ""
        bits = []
        for tm, st in fm.items():
            try:
                if isinstance(st, dict) and (st.get("gp") or 0) > 0:
                    bits.append(f"{tm}: last {st.get('gp')} {st.get('w', 0)}W-{st.get('d', 0)}D-"
                                f"{st.get('l', 0)}L (GF {st.get('gf', 0)}/GA {st.get('ga', 0)}), "
                                f"form {st.get('form', '?')}")
            except Exception:
                continue
        return "Recent form (FotMob results): " + (" | ".join(bits)[:450] if bits else "unavailable")
    except Exception:
        return ""


def _team_line(home: str, away: str) -> str:
    """Club involvement records (won rate when the club is on the slip).
    '' when neither club has history yet."""
    try:
        try:
            from learner import get_team_records as _gtr
        except ImportError:
            from worker.learner import get_team_records as _gtr  # type: ignore
        recs = _gtr(home, away) or {}
        if not recs:
            return ""
        bits = []
        for tm, st in recs.items():
            try:
                bits.append(f"{tm}: legs with them involved won {st.get('rate', 0):.0%} (n={st.get('n', 0)})")
            except Exception:
                continue
        return "Club history: " + (" | ".join(bits)[:300] if bits else "none yet")
    except Exception:
        return ""


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
    sim_text = _sim_line(home, away, selection, market, odds)
    if not history:
        history = "No recent-form data available (coverage gap)."
    if not news:
        news = "No fresh team news found."

    brief = (f"Match: {home} vs {away}\nLeague: {league}\nMarket: {market}\n"
             f"Selection: {selection} @ {odds} (implied {base})\n"
             f"Data scout (pure-code models, no LLM): {(leg.get('_data') or (0, 0, ''))[2] if isinstance(leg.get('_data'), tuple) else ''}\n"
             f"{sim_text}\n"
             f"{_fm_line(leg)}\n"
             f"{_team_line(home, away)}\n"
             f"{_learner_context(league)}\n"
             f"History: {history[:900]}\nNews: {news[:1200]}")

    verdicts = []
    for _bname, pids in BATCHES:
        time.sleep(1)  # gentle pacing; threads give the real speedup
        try:
            verdicts.extend(_ask_batch(pids, brief))
        except Exception:
            continue
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
                  f"{sim_text}\n"
                  f"Specialist verdicts: {scored}\nHistory: {history[:600]}\nNews: {news[:800]}",
                  system=SYNTH_SYSTEM,
                  max_chars=1500, tries=3, gated=False, stage="synth", prefer="nim")
    if not synth:
        # layered fallback: one full-chain single verdict before implied
        last = _ask(f"{brief}\n\nReply with exactly two lines:\nPROB=<0-1 selection win probability>\nWHY=<2-4 sentences citing specific teams, players, numbers>",
                    system="You are a senior betting analyst. Be specific, cite numbers and names.",
                    max_chars=600, tries=2, gated=False, stage="single", exclude=_LAST_PREF[0])
        if last:
            synth = last
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
    if sim_text:
        detail = f"{sim_text} || {detail}"[:2200]
    return prob, why, detail
