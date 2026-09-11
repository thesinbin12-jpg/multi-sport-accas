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


def _norm_team(name: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _match_key(leg: dict) -> str:
    """Source-independent match key: date + normalized teams."""
    ct = str(leg.get("commence_time", ""))[:10]
    return f"{ct}|{_norm_team(leg.get('home_team'))}|{_norm_team(leg.get('away_team'))}"


def _same_match(a: dict, b: dict) -> bool:
    if _match_key(a) == _match_key(b):
        return True
    if str(a.get("commence_time", ""))[:10] != str(b.get("commence_time", ""))[:10]:
        return False
    ha, aa = _norm_team(a.get("home_team")), _norm_team(a.get("away_team"))
    hb, ab = _norm_team(b.get("home_team")), _norm_team(b.get("away_team"))
    return bool(ha and hb and aa and ab) and (ha in hb or hb in ha) and (aa in ab or ab in aa)


def _merge_legs(primary: list, secondary: list) -> list:
    """Merge leg lists; secondary wins same (match, market) ties (sharper price).
    Same-match different-market legs are all kept (guarded at ticket assembly)."""
    merged = list(primary)
    for leg in secondary:
        dup = next((m for m in merged
                    if str(m.get("market", "1X2")) == str(leg.get("market", "1X2"))
                    and _same_match(m, leg)), None)
        if dup:
            merged.remove(dup)
        merged.append(leg)
    return merged


def scan_and_extract(max_credits: int | None = None, progress_cb=None, kind: str = "daily") -> list:
    window_h = config.KICKOFF_HOURS_WEEKLY if kind == "weekly" else config.KICKOFF_HOURS_DAILY
    filt = _scan_filter()
    try:
        from scanner import SCAN_CONFIG
    except ImportError:
        from worker.scanner import SCAN_CONFIG  # type: ignore
    if filt:
        soccer_keys = [k for keys in filt.values() for k in keys
                       if k in SCAN_CONFIG.get("soccer", [])]
    else:
        soccer_keys = list(SCAN_CONFIG.get("soccer", []))
    legs = []
    # 1. Free primary: Betika bookmaker API (zero quota, real prices). Non-soccer keys skip it.
    if config.BETIKA_ON and soccer_keys:
        try:
            try:
                from betika_odds import scan_betika
            except ImportError:
                from worker.betika_odds import scan_betika  # type: ignore
            btk_legs = scan_betika(hours_ahead=window_h, callback=progress_cb, log=progress_cb)
            if btk_legs:
                legs = OddsScanner().extract_legs({"soccer_betika": btk_legs},
                                                  min_odds=config.MIN_ODDS,
                                                  max_odds=config.MAX_ODDS)
        except Exception as e:
            if progress_cb:
                progress_cb(f"Betika failed ({e}), trying next source.")
    # 1b. Second free source: Smarkets exchange (sharper, wins ties on merge).
    if config.SMARKETS_ON:
        try:
            try:
                from smarkets_odds import scan_smarkets
            except ImportError:
                from worker.smarkets_odds import scan_smarkets  # type: ignore
            smk_legs = scan_smarkets(hours_ahead=window_h, callback=progress_cb, log=progress_cb)
            if smk_legs:
                smk_legs = OddsScanner().extract_legs(
                    {"soccer_smarkets": smk_legs}, min_odds=config.MIN_ODDS,
                    max_odds=config.MAX_ODDS)
                legs = _merge_legs(legs, smk_legs)
        except Exception as e:
            if progress_cb:
                progress_cb(f"Smarkets failed ({e}), continuing.")
    if legs:
        return legs
    # 2. Paid fallback: Odds API, guarded by quota floor (free get_sports call first).
    s = OddsScanner()
    try:
        s.get_sports()  # free, refreshes credits_remaining from headers
        remaining = s.credits_remaining
        if remaining is not None and remaining < config.ODDS_MIN_FLOOR:
            if progress_cb:
                progress_cb(f"Odds API guarded: {remaining} credits left (floor "
                            f"{config.ODDS_MIN_FLOOR}), skipping paid scan.")
            return legs
    except Exception:
        pass
    results = s.scan_all(sports_filter=filt,
                         max_credits=(max_credits or config.MAX_CREDITS_PER_SCAN), callback=progress_cb)
    legs = legs + s.extract_legs(results, min_odds=config.MIN_ODDS, max_odds=config.MAX_ODDS)
    return legs


def _scan_filter() -> dict | None:
    """Build {group: [keys]} from SCAN_FOCUS (groups and/or raw keys). None = all."""
    raw = (config.SCAN_FOCUS or "").strip()
    if not raw:
        return None
    try:
        from scanner import SCAN_CONFIG
    except ImportError:
        from worker.scanner import SCAN_CONFIG  # type: ignore
    want = [w.strip().lower() for w in raw.split(",") if w.strip()]
    filt: dict[str, list] = {}
    all_keys = {k for keys in SCAN_CONFIG.values() for k in keys}
    for w in want:
        if w in SCAN_CONFIG:
            filt[w] = list(SCAN_CONFIG[w])
        elif w in all_keys:
            filt.setdefault("custom", []).append(w)
    return filt or None


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
                  max_credits: int | None = None, progress_cb=None, kind: str = "daily") -> list:
    """Build accumulator ticket(s). kind=daily (4-6 legs, value zone ~2.2)
    or weekly (up to 8 legs, value zone ~3.0, bigger payout)."""
    kind = kind if kind in ("daily", "weekly") else "daily"
    weekly = (kind == "weekly")
    max_legs = max_legs or (8 if weekly else config.MAX_LEGS_PER_ACCA)
    max_legs = max(2, min(int(max_legs), 20))  # hard cap 20, never forced: rank pass can trim
    legs = scan_and_extract(max_credits=max_credits, progress_cb=progress_cb, kind=kind)

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

    # Prefer mid-odds value zone first, then fill (weekly aims higher)
    center = 3.0 if weekly else 2.2
    diverse.sort(key=lambda l: abs(float(l.get("best_odds", 2.0)) - center))
    # Learner strategy: skip cold leagues, prefer proven odds band (never breaks builds)
    try:
        from learner import get_strategy
    except ImportError:
        try:
            from worker.learner import get_strategy  # type: ignore
        except ImportError:
            get_strategy = None  # type: ignore
    if get_strategy:
        try:
            strat = get_strategy() or {}
            blocked = set(str(x).lower() for x in (strat.get("blocked_leagues") or []))
            if blocked:
                kept = [l for l in diverse if str(l.get("league", "")).lower() not in blocked]
                if kept:
                    diverse = kept
        except Exception:
            pass
    # Kickoff window: daily = near-term only, weekly = 7 days (missing times kept)
    try:
        from datetime import timedelta as _td
        window_h = config.KICKOFF_HOURS_WEEKLY if weekly else config.KICKOFF_HOURS_DAILY
        now = datetime.now(timezone.utc)
        cutoff = now + _td(hours=window_h)

        def _in_window(leg: dict) -> bool:
            ct = leg.get("commence_time", "")
            if not ct:
                return True
            try:
                dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt <= cutoff
            except Exception:
                return True

        windowed = [l for l in diverse if _in_window(l)]
        if windowed:
            diverse = windowed
    except Exception:
        pass
    # Market scout (pure code, no LLM): EVERY fixture x EVERY market it carries
    # is data-scored (Poisson 1X2 / BTTS rates / O-U from FDO form+H2H blended
    # with implied). The data names the market first; the swarm debates survivors.
    try:
        try:
            from market_scout import scout
        except ImportError:
            from worker.market_scout import scout  # type: ignore
        scouted = scout(diverse, fdo_budget=int(getattr(config, "SCOUT_FIXTURES", 24) or 24),
                        keep=max(20, max_legs * 3), progress_cb=progress_cb)
        if scouted:
            diverse = scouted
    except Exception:
        pass
    candidates = diverse[: max(24, (max_legs or 6) + 10)]

    # Analyst swarm (draw-predictor pattern, multi-market): deep verdicts on
    # finalists only. analyst.py gathers its own history (football-data.org)
    # + news (Tavily -> DuckDuckGo fallback), so the loop below skips its own
    # Tavily call + thin _ai_assess whenever a swarm verdict exists.
    if use_ai and getattr(config, "ANALYST_ON", True):
        try:
            try:
                from analyst import analyze_finalist
            except ImportError:
                from worker.analyst import analyze_finalist  # type: ignore
            import concurrent.futures as _cf
            finalists = candidates[: max(2, int(getattr(config, "ANALYST_MAX", 10) or 10))]

            def _one(ix_leg):
                ix, leg = ix_leg
                try:
                    return ix, leg, analyze_finalist(leg, progress_cb=progress_cb)
                except Exception:
                    return ix, leg, None

            with _cf.ThreadPoolExecutor(max_workers=5) as _ex:
                done = sorted(_ex.map(_one, enumerate(finalists)), key=lambda t: t[0])
            for _ix, leg, res in done:
                if res:
                    p, w, a = res
                    leg["_swarm"] = (p, w, a)
        except Exception:
            pass

    # Phase 1: assess every candidate. Selection = the scout's data pick
    # (best blended in the 1.5-7.0 band) — shared by analyst and builder, so
    # prob/odds/selection can never disagree. Out-of-band legs are dropped.
    assessed = []
    for leg in candidates:
        _enrich_with_fotmob(leg)
        pick = str(leg.get("_pick") or "").lower()
        data = (leg.get("_picks") or {}).get(pick) if pick else None
        if data:
            try:
                if not (1.5 <= float(data[2]) <= 7.0):
                    continue
            except Exception:
                continue
        if "_swarm" in leg:
            prob, why = leg["_swarm"][0], leg["_swarm"][1]
            leg["analysis"] = leg["_swarm"][2]
        elif use_ai:
            news = ""
            if os.environ.get("TAVILY_API_KEY"):
                news = _tavily_search(f"{leg.get('home_team')} vs {leg.get('away_team')} {leg.get('league')} prediction injuries")
            prob, why = _ai_assess(leg, news)
        elif data:
            prob, why = data[0], f"data model {data[0]:.3f} ({str(data[3])[:160]})"
        else:
            prob, why = _implied_prob(leg.get("best_odds", 2.0)), "implied (AI off)"
        try:
            prob = float(prob)
        except Exception:
            prob = 0.0
        assessed.append((leg, prob, why))
    # Phase 2: pick. Ceiling 20, never forced: first 6 by rank, extras only
    # when genuinely likely (prob >= 0.55). Same-match guard: one market/match.
    ceiling = min(20, max(2, int(max_legs or 20)))
    assessed.sort(key=lambda t: -t[1])
    picked = []
    for leg, prob, why in assessed:
        if len(picked) >= ceiling:
            break
        if len(picked) >= 6 and prob < 0.55:
            continue
        if any(_same_match(leg, p[0]) for p in picked):
            continue
        picked.append((leg, prob, why))
    # Market diversity: span >= 2 markets when candidates allow (swap worst
    # picked leg for the best unpicked leg of another market within 0.07 prob).
    if len(picked) >= 2 and len({p[0].get("market") for p in picked}) < 2:
        worst = min(picked, key=lambda t: t[1])
        taken = {id(p[0]) for p in picked}
        alt = None
        for leg, prob, why in assessed:
            if id(leg) in taken:
                continue
            if leg.get("market") == worst[0].get("market"):
                continue
            if prob < worst[1] - 0.07:
                continue
            if any(_same_match(leg, p[0]) for p in picked if p is not worst):
                continue
            alt = (leg, prob, why)
            break
        if alt:
            picked = [p for p in picked if p is not worst] + [alt]
    built = []
    for leg, prob, why in picked:
        # selection = scout pick (band-checked live; prices move, never force).
        outcomes = leg.get("outcomes", []) or []
        if not outcomes:
            try:
                outcomes = ((leg.get("bookmakers") or [{}])[0].get("markets") or [{}])[0].get("outcomes", []) or []
            except Exception:
                outcomes = []
        pick = str(leg.get("_pick") or "").lower()
        sel_out = next((o for o in outcomes if str(o.get("name", "")).lower() == pick), None) if pick else None
        if sel_out is not None:
            try:
                oprice = float(sel_out.get("price", 0))
            except Exception:
                oprice = 0
            if 1.5 <= oprice <= 7.0:
                selection, odds = sel_out.get("name"), oprice
            else:
                continue
        elif outcomes:
            fav = min(outcomes, key=lambda o: float(o.get("price", 999)))
            selection, odds = fav.get("name", leg.get("home_team")), float(fav.get("price", leg.get("best_odds", 2.0)))
        else:
            selection, odds = leg.get("home_team", "?"), float(leg.get("best_odds", 2.0))
        built.append({
            "sport": leg.get("sport", leg.get("sport_key", "")),
            "sport_key": leg.get("sport_key", ""),
            "league": leg.get("league", ""),
            "market": leg.get("market", "1X2"),
            "match": f"{leg.get('home_team','?')} vs {leg.get('away_team','?')}",
            "selection": selection,
            "odds": round(odds, 3),
            "probability": prob,
            "result": "pending",
            "reason": why,
            "analysis": leg.get("analysis", ""),
            "commence_time": leg.get("commence_time", ""),
            "bookmaker": leg.get("best_bookmaker", ""),
        })

    if not built:
        return []

    # Agentic final pass: LLM ranks legs, sets stake + confidence (tools ground it)
    stake = _agentic_stake(built, kind, use_ai)

    combined = round(math.prod(max(float(b["odds"]), 1.01) for b in built), 3)
    ticket = {
        "id": f"acca-{kind}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "combined_odds": combined,
        "legs": built,
        "status": "pending",
        "kind": kind,
        "stake": stake,
    }
    return [ticket]


RANK_SYSTEM = (
    "You manage a sports accumulator portfolio. Tier B (daily): steady value, "
    "stake 1-3 units. Tier A (weekly): dream ticket, tiny stake 0.5-1 unit. "
    "Higher combined odds and lower hit probability means FEWER units. "
    "Reply with exactly one JSON object, no other text."
)

RANK_PROMPT = """Ticket kind: {kind} (Tier {tier})
Legs (index, selection, league, odds, model probability, reason):
{legs}
Combined odds: {combined}x

Pick the final order (best first, drop any leg you distrust by omitting it, keep at least 2, keep at most 20 — more legs only when each one is genuinely likely). Prefer tickets spanning at least 2 markets when candidates allow. Set stake in units and confidence 0-1. Reply exactly:
{{"order": [0, 2, 1], "stake_units": 1.5, "confidence": 0.62, "stake_note": "one short sentence"}}"""


def _heuristic_stake(built: list, kind: str, why: str = "") -> dict:
    """Kelly-capped fallback when LLM is unavailable. Never stakes big."""
    try:
        try:
            from learner import log_llm_error
        except ImportError:
            from worker.learner import log_llm_error  # type: ignore
        log_llm_error("rank", "", "rank", (why or "rank LLM failed")[:200])
    except Exception:
        pass
    import math as _m
    combined = _m.prod(max(float(b["odds"]), 1.01) for b in built)
    avg_p = sum(float(b.get("probability") or 0) for b in built) / max(len(built), 1)
    # quarter-Kelly on the ticket treated as one bet, hard-capped
    b = max(combined - 1.0, 0.01)
    kelly = max((avg_p * combined - 1.0) / b, 0.0) / 4.0
    cap = 1.0 if kind == "weekly" else 3.0
    units = round(min(max(kelly * 100 / 10.0, 0.5 if kind == "weekly" else 1.0), cap) * 2) / 2
    tier = "A · dream ticket" if kind == "weekly" else "B · steady value"
    return {"units": units, "confidence": round(min(avg_p + 0.1, 0.9), 2),
            "note": f"Tier {tier}. Heuristic sizing (LLM unavailable) — small either way.", "llm": False}


def _agentic_stake(built: list, kind: str, use_ai: bool) -> dict:
    if not use_ai:
        return _heuristic_stake(built, kind)
    try:
        lines = [f"{i}. {b.get('selection')} | {b.get('league')} | odds {b.get('odds')} | p {b.get('probability')} | {str(b.get('reason',''))[:80]}"
                 for i, b in enumerate(built)]
        import math as _m
        combined = round(_m.prod(max(float(b["odds"]), 1.01) for b in built), 2)
        prompt = RANK_PROMPT.format(kind=kind, tier="A" if kind == "weekly" else "B",
                                    legs="\n".join(lines)[:3000], combined=combined)
        out = None
        for _try in range(2):
            # freshest quota first (OpenRouter), then the full chain
            _pref = "orouter" if _try == 0 else None
            out = router.analyze(prompt, system_prompt=RANK_SYSTEM, model_pref=_pref)
            _t = out[0] if isinstance(out, tuple) else None
            _e = out[2] if isinstance(out, tuple) and len(out) > 2 else None
            if _t and not _e:
                break
            import time as _tm
            _tm.sleep(3)
        text = out[0] if isinstance(out, tuple) else None
        err = out[2] if isinstance(out, tuple) and len(out) > 2 else None
        if err or not text:
            return _heuristic_stake(built, kind, str(err or "empty rank reply"))
        import re as _re, json as _js
        m = _re.search(r"\{.*\}", text, _re.DOTALL)
        if not m:
            return _heuristic_stake(built, kind)
        d = _js.loads(m.group(0))
        order = [i for i in (d.get("order") or []) if isinstance(i, int) and 0 <= i < len(built)]
        if len(order) >= 2:
            ordered = [built[i] for i in order]
            built.clear()
            built.extend(ordered)
            combined = round(_m.prod(max(float(b["odds"]), 1.01) for b in built), 3)
        units = float(d.get("stake_units", 1.0))
        cap = 1.0 if kind == "weekly" else 3.0
        units = min(max(units, 0.5), cap)
        conf = min(max(float(d.get("confidence", 0.5)), 0.05), 0.95)
        return {"units": units, "confidence": round(conf, 2),
                "note": str(d.get("stake_note", ""))[:220], "llm": True,
                "combined_odds": combined}
    except Exception:
        return _heuristic_stake(built, kind)


def build_and_save(max_legs: int | None = None, use_ai: bool = True,
                   max_credits: int | None = None, progress_cb=None, kind: str = "daily") -> list:
    """Build tickets and persist to DB. Returns ticket list."""
    import db as db_mod
    try:
        import db  # worker-local
    except ImportError:
        db = db_mod
    else:
        db = db_mod
    tickets = build_tickets(max_legs=max_legs, use_ai=use_ai, max_credits=max_credits, progress_cb=progress_cb, kind=kind)
    for t in tickets:
        db.save_ticket(t["id"], t["combined_odds"], t["legs"], t["status"], kind=t.get("kind", "daily"), stake=t.get("stake"))
    try:
        db.prune_pending(kind=tickets[0].get("kind", kind) if tickets else kind, keep=2)
    except Exception:
        pass
    return tickets


if __name__ == "__main__":
    print("AI:", "on" if os.environ.get("GROQ_API_KEY") or os.environ.get("GEMINI_API_KEY") else "off (implied probs)")
    ts = build_tickets(max_legs=3, use_ai=False)
    print(f"built {len(ts)} ticket(s) without AI")
    if ts:
        print("combined:", ts[0]["combined_odds"], "legs:", len(ts[0]["legs"]))
