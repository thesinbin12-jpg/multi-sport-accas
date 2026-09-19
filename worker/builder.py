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
    from fotmob import league_odds_to_fotmob, get_fotmob, fotmob_league_id, league_form, match_team
    _HAS_FOTMOB = True
except ImportError:
    try:
        from worker.fotmob import league_odds_to_fotmob, get_fotmob, fotmob_league_id, league_form, match_team  # type: ignore
        _HAS_FOTMOB = True
    except ImportError:
        _HAS_FOTMOB = False


def _news_search(query: str, max_results: int = 3) -> str:
    """FREE-first news context (Brave -> DDG). Tavily only when free backends
    return nothing AND a key exists (quota guard). Never raises."""
    try:
        try:
            from websearch import search as _free_search
        except ImportError:
            from worker.websearch import search as _free_search  # type: ignore
        _free = _free_search(query, max_results=max_results) or []
        if _free:
            return " | ".join(
                f"{x.get('title', '')}: {str(x.get('snippet', ''))[:160]}"
                for x in _free[:max_results] if x.get("title") or x.get("snippet"))
    except Exception:
        pass
    return _tavily_search(query, max_results)


def _tavily_search(query: str, max_results: int = 3) -> str:
    """Last-resort Tavily call (only when free backends empty). Skipped when key missing."""
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


def _all_outcomes(leg: dict) -> list:
    """Every outcome dict across leg['outcomes'] + all nested bookmaker markets."""
    seen, out = set(), []
    try:
        for o in (leg.get("outcomes", []) or []):
            _k = (str(o.get("name", "")).lower(), str(o.get("price", "")))
            if _k not in seen:
                seen.add(_k)
                out.append(o)
        for bm in leg.get("bookmakers", []) or []:
            for mk in (bm.get("markets", []) or []):
                for o in (mk.get("outcomes", []) or []):
                    _k = (str(o.get("name", "")).lower(), str(o.get("price", "")))
                    if _k not in seen:
                        seen.add(_k)
                        out.append(o)
    except Exception:
        pass
    return out


def _closest_outcome(leg: dict, price: float):
    """Outcome whose price is nearest `price` (reconstructs selection when _pick missing)."""
    try:
        cands = [(o, abs(float(o.get("price", 0)) - price)) for o in _all_outcomes(leg)]
        cands = [(o, d) for o, d in cands if o.get("price")]
        if not cands:
            return None
        return min(cands, key=lambda t: t[1])[0]
    except Exception:
        return None


def _find_outcome(leg: dict, pick: str):
    """Find outcome by name across ALL markets (not just the first). Returns outcome dict or None."""
    try:
        outs = leg.get("outcomes", []) or []
        if outs:
            so = next((o for o in outs if str(o.get("name", "")).lower() == pick), None)
            if so is not None:
                return so
        for bm in leg.get("bookmakers", []) or []:
            for mk in (bm.get("markets", []) or []):
                for o in (mk.get("outcomes", []) or []):
                    if str(o.get("name", "")).lower() == pick:
                        return o
    except Exception:
        pass
    return None


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
    """Attach FotMob table form for both teams. League resolved by Odds API
    sport_key first, then Betika-style league text (England Premier League
    -> 47) so bookmaker legs get real form too. Cached per league."""
    if not _HAS_FOTMOB:
        return leg
    try:
        lid = league_odds_to_fotmob(leg.get("sport_key", "")) or fotmob_league_id(leg.get("league", ""))
        if not lid:
            return leg
        form = league_form(lid)
        if isinstance(form, dict) and form:
            for team_key in (leg.get("home_team", ""), leg.get("away_team", "")):
                hit = match_team(form, team_key)
                if hit:
                    leg.setdefault("form", {})[team_key] = hit
    except Exception:
        pass
    return leg


_LAST_BUILD_DIAG: dict = {}


def build_tickets(max_legs: int | None = None, use_ai: bool = True,
                  max_credits: int | None = None, progress_cb=None, kind: str = "daily",
                  slips: str = "both") -> list:
    """Build accumulator ticket(s). kind=daily (4-6 legs, value zone ~2.2)
    or weekly (up to 8 legs, value zone ~3.0, bigger payout).
    slips (daily only): both | steady | dreamer — skips the unbuilt slip's
    pick/materialize/stake tail (shared scan+scout+swarm still runs)."""
    globals()["_LAST_BUILD_DIAG"] = {"kind": kind}
    kind = kind if kind in ("daily", "weekly") else "daily"
    slips = slips if slips in ("both", "steady", "dreamer") else "both"
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
        from learner import get_strategy, calibration_discount
    except ImportError:
        try:
            from worker.learner import get_strategy, calibration_discount  # type: ignore
        except ImportError:
            get_strategy = None  # type: ignore
            calibration_discount = None  # type: ignore
    if get_strategy:
        try:
            strat = get_strategy() or {}
            blocked = set(str(x).lower() for x in (strat.get("blocked_leagues") or []))
            if blocked:
                kept = [l for l in diverse if str(l.get("league", "")).lower() not in blocked]
                if kept:
                    diverse = kept
            blocked_mk = set(str(x).lower() for x in (strat.get("blocked_markets") or []))
            if blocked_mk:
                keptm = [l for l in diverse if str(l.get("market", "")).lower() not in blocked_mk]
                if keptm:
                    diverse = keptm
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
                # upcoming only: kickoff within the window ahead, at most 10min past
                return dt <= cutoff and dt >= now - _td(minutes=10)
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
                        keep=max(20, max_legs * 3), progress_cb=progress_cb, hours_ahead=window_h)
        if scouted:
            diverse = scouted
    except Exception:
        pass
    candidates = diverse[: max(24, (max_legs or 6) + 10)]
    # Finalists round-robin across markets so the swarm debates 1X2, DC,
    # BTTS, O/U and combos — never one market only.
    _cap = max(2, int(getattr(config, "ANALYST_MAX", 10) or 10))
    _by_mkt: dict = {}
    for leg in candidates:
        _by_mkt.setdefault(str(leg.get("market", "1X2")), []).append(leg)
    finalists = []
    _round = 0
    _depth = max([len(v) for v in _by_mkt.values()] or [0])
    while len(finalists) < _cap and _round < _depth:
        for _mk in sorted(_by_mkt):
            if len(finalists) >= _cap:
                break
            if _round < len(_by_mkt[_mk]):
                finalists.append(_by_mkt[_mk][_round])
        _round += 1

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
            finalists = finalists or candidates[: max(2, int(getattr(config, "ANALYST_MAX", 10) or 10))]

            def _one(ix_leg):
                ix, leg = ix_leg
                try:
                    return ix, leg, analyze_finalist(leg, progress_cb=progress_cb)
                except Exception:
                    return ix, leg, None

            with _cf.ThreadPoolExecutor(max_workers=3) as _ex:
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
            news = _news_search(f"{leg.get('home_team')} vs {leg.get('away_team')} {leg.get('league')} prediction injuries")
            prob, why = _ai_assess(leg, news)
        elif data:
            prob, why = data[0], f"data model {data[0]:.3f} ({str(data[3])[:160]})"
        else:
            prob, why = _implied_prob(leg.get("best_odds", 2.0)), "implied (AI off)"
        try:
            prob = float(prob)
        except Exception:
            prob = 0.0
        if calibration_discount:
            try:
                _f = calibration_discount(float(prob))
                if _f < 1.0:
                    prob = max(0.05, min(0.99, float(prob) * _f))
                    why = f"{why} [calib x{_f:.2f}]"
            except Exception:
                pass
        assessed.append((leg, prob, why))
    # Phase 2: pick.
    # Daily = TWO slips from one trigger: steady (~50x, best probs) +
    # dreamer (7-10 ANALYZED legs 1.5-8.0, payout via count + bet builders).
    # Weekly = single volume ticket (first 8, extras prob>=0.45 + EV>=0.95).
    # Same-match guard always: one leg per fixture on every ticket.
    if kind == "daily":
        want_steady = slips in ("both", "steady")
        want_dream = slips in ("both", "dreamer")
        value_cands = _pick_conservative(assessed) if want_steady else []
        if want_steady:
            _ensure_diversity(value_cands, assessed)
        # Dreamer: 7-10 ANALYZED legs 1.5-8.0, payout via count + bet
        # builders — never unscouted filler (see _pick_dreamer).
        dream_cands = _pick_dreamer(assessed, diverse, legs) if want_dream else []
        # draw insurance: swap 1X2 legs for book-priced DC/DNB same-match
        # siblings (lowest-margin 1X2 substitutes) whenever they price in band
        _prefer_insurance(value_cands, assessed)
        _prefer_insurance(dream_cands, assessed)
        try:
            if progress_cb:
                progress_cb(f"Daily pair: {len(value_cands)} steady candidates, {len(dream_cands)} dreamer candidates.")
        except Exception:
            pass
        picked = []
    else:
        value_cands, dream_cands = [], []
        _base, _pfloor, _evfloor = 8, 0.45, 0.95
        ceiling = min(20, max(2, int(max_legs or 20)))
        assessed.sort(key=lambda t: -t[1])
        picked = []
        for leg, prob, why in assessed:
            if len(picked) >= ceiling:
                break
            if len(picked) >= _base:
                try:
                    _o = float(leg.get("_sel_price") or leg.get("best_odds") or 0)
                except Exception:
                    _o = 0
                if prob < _pfloor or prob * _o < _evfloor - 0.01:
                    continue
            if any(_same_match(leg, p[0]) for p in picked):
                continue
            picked.append((leg, prob, why))
        _ensure_diversity(picked, assessed)
    if kind == "daily":
        # TWO-SLIP DAILY: steady (~50x) + dreamer (high odds). Same trigger,
        # same pool, two tickets. prune_pending(keep=2) keeps the newest pair.
        tickets: list = []
        v_built = _trim_to_target(_materialize(value_cands), target=50.0)
        v_comb = 0.0
        if len(v_built) >= 2:
            stake_v = _agentic_stake(v_built, kind, use_ai, progress_cb=progress_cb)
            try:
                stake_v["tier"] = "value"
            except Exception:
                pass
            v_comb = round(math.prod(max(float(b["odds"]), 1.01) for b in v_built), 3)
            _now = datetime.now(timezone.utc)
            tickets.append({
                "id": f"acca-daily-{_now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}",
                "created_at": _now.isoformat(),
                "combined_odds": v_comb,
                "legs": v_built,
                "status": "pending",
                "kind": "daily",
                "stake": stake_v,
            })
        d_built = _materialize(dream_cands, band_lo=1.5, band_hi=8.0, fallback="drop")
        d_comb = 0.0
        if len(d_built) >= 5:
            d_comb = round(math.prod(max(float(b["odds"]), 1.01) for b in d_built), 3)
            _joint = 1.0
            for b in d_built:
                _joint *= max(min(float(b["probability"]), 0.99), 0.01)
            _now2 = datetime.now(timezone.utc)
            tickets.append({
                "id": f"acca-dream-{_now2.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}",
                "created_at": _now2.isoformat(),
                "combined_odds": d_comb,
                "legs": d_built,
                "status": "pending",
                "kind": "daily",
                "stake": {"units": 0.5, "confidence": round(_joint, 4),
                           "note": "Dreamer — tiny stake, huge payout. Joint hit chance shown honestly.",
                           "llm": False, "tier": "dream", "combined_odds": d_comb},
            })
        try:
            if progress_cb:
                if v_built and len(v_built) >= 2 and len(d_built) >= 5:
                    progress_cb(f"Filed pair: steady {v_comb}x ({len(v_built)} legs) + dreamer {d_comb}x ({len(d_built)} legs).")
                elif v_built and len(v_built) >= 2:
                    progress_cb(f"Filed steady {v_comb}x ({len(v_built)} legs); no dreamer (<5 analyzed legs in 1.5-8.0).")
                elif len(d_built) >= 5:
                    progress_cb(f"Filed dreamer {d_comb}x ({len(d_built)} legs).")
                else:
                    progress_cb("Nothing filed: pool too thin for the requested slip(s).")
        except Exception:
            pass
        globals()["_LAST_BUILD_DIAG"] = {"kind": "daily",
            "steady_cands": len(value_cands), "dream_cands": len(dream_cands),
            "steady_legs": len(v_built), "dream_legs": len(d_built),
            "steady_comb": v_comb, "dream_comb": d_comb}
        return tickets
    built = _materialize(picked)
    if not built:
        return []

    # Agentic final pass: LLM ranks legs, sets stake + confidence (tools ground it)
    stake = _agentic_stake(built, kind, use_ai, progress_cb=progress_cb)

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
    tickets = [ticket]
    globals()["_LAST_BUILD_DIAG"] = {"kind": kind, "steady_legs": len(built),
        "steady_comb": combined}
    if weekly:
        # FIXTURE-LED 7-DAY WEEKLY: books price ~48h ahead, so Monday scans
        # can never see the weekend. Fixtures lead (FotMob 7d + FDO scheduled)
        # -> seed queues the week's ~25 -> real Betika odds attach per match
        # progressively (now for Mon-Tue, nightly learn fills Wed-Sun).
        try:
            try:
                from weekly_seed import seed_week, price_and_append
            except ImportError:
                from worker.weekly_seed import seed_week, price_and_append  # type: ignore
            _seed = seed_week(progress_cb=progress_cb) or {}
            _fresh, _app = price_and_append(ticket, progress_cb=progress_cb) or ([], {})
            try:
                globals()["_LAST_BUILD_DIAG"].update({
                    "seed_fixtures": (_seed or {}).get("fixtures", 0),
                    "seed_queued": (_seed or {}).get("queued", 0),
                    "append_priced": (_app or {}).get("priced", 0),
                    "steady_legs": len(ticket.get("legs", [])),
                    "steady_comb": ticket.get("combined_odds", combined)})
            except Exception:
                pass
        except Exception:
            pass
    return tickets


def _materialize(picked: list, band_lo: float = 1.5, band_hi: float = 7.0, fallback: str = "fav") -> list:
    """Turn (leg, prob, why) picks into priced built legs.
    Selection = scout pick, band-checked against the LIVE price; legs whose
    pick vanished or priced out of band are dropped (never fabricated).
    fallback 'fav' (steady tickets): no stored pick -> favourite outcome.
    fallback 'closest' (dreamer): pick vanished -> nearest-price outcome.
    The band check applies to fallbacks too (fav=min-price bug fix)."""
    built = []
    for leg, prob, why in picked:
        # file-time kickoff guard: a leg that already started (or starts
        # within 15min) is unstakeable — drop it, never file it.
        try:
            from datetime import datetime as _dtn, timezone as _tzn, timedelta as _tdn
            _ct = _dtn.fromisoformat(str(leg.get("commence_time", "")).replace("Z", "+00:00"))
            if _ct.tzinfo is None:
                _ct = _ct.replace(tzinfo=_tzn.utc)
            if _ct <= _dtn.now(_tzn.utc) + _tdn(minutes=15):
                continue
        except Exception:
            pass
        outcomes = leg.get("outcomes", []) or []
        if not outcomes:
            try:
                outcomes = []
                for _bm in leg.get("bookmakers", []) or []:
                    for _mk in (_bm.get("markets", []) or []):
                        outcomes += (_mk.get("outcomes", []) or [])
            except Exception:
                outcomes = []
        pick = str(leg.get("_pick") or "").lower()
        sel_out = _find_outcome(leg, pick) if pick else None
        if sel_out is None and not pick and fallback == "closest":
            # unscouted fallback legs carry no _pick: reconstruct the
            # in-band outcome nearest the snapshot price (band-checked below).
            try:
                _ref2 = min(band_hi, max(band_lo, float(leg.get("best_odds") or 0)))
            except Exception:
                _ref2 = 0
            sel_out = _closest_outcome(leg, _ref2) if _ref2 else None
            if sel_out is None:
                continue
        if sel_out is None and pick and fallback == "closest":
            try:
                _ref = float(leg.get("_sel_price") or leg.get("best_odds") or 0)
            except Exception:
                _ref = 0
            sel_out = _closest_outcome(leg, _ref) if _ref else None
        if sel_out is None and pick:
            continue  # pick vanished from the book (price moved) — never fabricate
        if sel_out is not None:
            try:
                oprice = float(sel_out.get("price", 0))
            except Exception:
                oprice = 0
            if band_lo - 1e-9 <= oprice <= band_hi + 1e-9:
                selection, odds = sel_out.get("name"), oprice
            else:
                continue
        elif outcomes and fallback == "fav":
            fav = min(outcomes, key=lambda o: float(o.get("price", 999)))
            try:
                _fp = float(fav.get("price", 0))
            except Exception:
                _fp = 0
            if not (band_lo - 1e-9 <= _fp <= band_hi + 1e-9):
                continue
            selection, odds = fav.get("name", leg.get("home_team")), _fp
        elif outcomes:
            continue
        else:
            try:
                _bo = float(leg.get("best_odds", 0))
            except Exception:
                _bo = 0
            if not (band_lo - 1e-9 <= _bo <= band_hi + 1e-9):
                continue
            selection, odds = leg.get("home_team", "?"), _bo
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
            "coverage": ("wide" if int(leg.get("_coverage", 1) or 1) > 1 else "single-book"),
        })
    return built


def _prefer_insurance(cands: list, pool: list, band_lo: float = 1.5, band_hi: float = 8.0) -> None:
    """Swap 1X2 legs for DC/DNB same-match siblings (draw insurance at a
    book price, lowest-margin 1X2 substitutes). Keeps the swap only when the
    insured version prices in band at no worse prob. Mutates cands in place.
    Never raises."""
    try:
        for i, (leg, prob, why) in enumerate(list(cands)):
            try:
                if str(leg.get("market", "")) != "1X2":
                    continue
                _pick = str(leg.get("_pick") or "").lower()
                _home = str(leg.get("home_team", ""))
                _away = str(leg.get("away_team", ""))
                side = None
                if _pick in ("1",) or (_home and _home.lower() in _pick):
                    side = "home"
                elif _pick in ("2",) or (_away and _away.lower() in _pick):
                    side = "away"
                if not side:
                    continue
                best = None
                for leg2, prob2, why2 in pool:
                    try:
                        if not _same_match(leg, leg2):
                            continue
                        if str(leg2.get("market", "")) not in ("Double chance", "DNB"):
                            continue
                        sel2 = str(leg2.get("_pick") or leg2.get("selection") or "").lower()
                        if side == "home":
                            ok = ("1x" in sel2 or "dnb:1" in sel2 or (_home and _home.lower() in sel2))
                        else:
                            ok = ("x2" in sel2 or "dnb:2" in sel2 or (_away and _away.lower() in sel2))
                        if not ok:
                            continue
                        o2 = float(leg2.get("_sel_price") or leg2.get("best_odds") or 0)
                        if not (band_lo - 1e-9 <= o2 <= band_hi + 1e-9):
                            continue
                        if float(prob2 or 0) < float(prob or 0) - 0.02:
                            continue
                        if best is None or float(prob2 or 0) > float(best[1] or 0):
                            best = (leg2, prob2, str(why2 or why) + " [insured: DC/DNB over 1X2]")
                    except Exception:
                        continue
                if best:
                    cands[i] = best
            except Exception:
                continue
    except Exception:
        pass


def _ensure_diversity(picked: list, pool: list) -> None:
    """Span >= 2 markets when candidates allow (swap worst picked leg for the
    best unpicked leg of another market within 0.07 prob). Mutates picked."""
    try:
        if len(picked) >= 2 and len({p[0].get("market") for p in picked}) < 2:
            worst = min(picked, key=lambda t: t[1])
            taken = {id(p[0]) for p in picked}
            alt = None
            for leg, prob, why in pool:
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
                picked.remove(worst)
                picked.append(alt)
    except Exception:
        pass


def _pick_conservative(assessed: list, cap: int = 10) -> list:
    """Steady D-slip candidates: best probability first. Trim to ~50x happens
    after live pricing (see _trim_to_target). Legs past the 6th need
    prob>=0.45 + EV>=0.95 so the tail never dilutes the ticket."""
    pool = sorted(assessed, key=lambda t: -(float(t[1] or 0.0)))
    picks = []
    for leg, prob, why in pool:
        if len(picks) >= cap:
            break
        try:
            _o = float(leg.get("_sel_price") or leg.get("best_odds") or 0)
            _pr = float(prob or 0.0)
        except Exception:
            _o, _pr = 0.0, 0.0
        # steady never takes double-margin combos (dreamer's lottery vehicle)
        try:
            if "+" in str(leg.get("market", "")):
                continue
        except Exception:
            pass
        # O/U 2.5 is efficiently priced (we hit 33%): demand a real edge
        try:
            if str(leg.get("market", "")) == "O/U 2.5" and _pr * _o < 1.0:
                continue
        except Exception:
            pass
        if len(picks) >= 6 and (_pr < 0.45 or _pr * _o < 0.94):
            continue
        if any(_same_match(leg, p[0]) for p in picks):
            continue
        picks.append((leg, prob, why))
    return picks


def _trim_to_target(built: list, target: float = 50.0, min_legs: int = 4, max_legs: int = 8) -> list:
    """Keep best-prob legs until combined reaches ~target (stops at target-5
    once min_legs held). Thin pools: dynamically adjust target/max_legs so
    we still file a meaningful ticket (4-8 legs) instead of 2-3 legs."""
    if not built:
        return []
    ordered = sorted(built, key=lambda b: -(float(b.get("probability") or 0.0)))
    # Calculate max achievable combined odds with all available legs
    max_comb = 1.0
    for b in ordered:
        try:
            max_comb *= max(float(b.get("odds") or 1.01), 1.01)
        except Exception:
            pass
    # If even all legs can't reach target, we're in a thin pool.
    # Adjust: allow more legs (up to 20 ceiling) and lower target to 70% of max.
    effective_max_legs = max_legs
    effective_target = target
    if max_comb < target:
        # Thin pool: allow up to 12 legs (was 8), target 70% of achievable
        effective_max_legs = min(12, len(ordered))
        effective_target = max_comb * 0.7
        # But never go below min_legs=4 or below 15x (floor)
        effective_target = max(effective_target, 15.0)
    kept, comb = [], 1.0
    for b in ordered:
        if len(kept) >= effective_max_legs:
            break
        if len(kept) >= min_legs and comb >= effective_target - 5.0:
            break
        kept.append(b)
        try:
            comb *= max(float(b.get("odds") or 1.01), 1.01)
        except Exception:
            pass
    return kept


def _pick_dreamer(assessed: list, candidates: list, raw_legs: list | None = None) -> list:
    """Dreamer = 7-10 ANALYZED legs where the payout comes from COUNT, not
    lottery tickets. Best-probability legs only (swarm-debated, data-modelled
    or AI-assessed — never pure-implied), per-leg odds 1.5-8.0, prob>=0.40.
    Combo/bet-builder markets (1X2&BTTS, 1X2+O/U, O/U&BTTS) rank first: one
    combo leg per fixture captures same-match correlation at a book-priced
    number. ONE leg per fixture always. No raw top-up: unscouted legs never
    file. Needs >=5 analyzed legs to file."""
    aprobs: dict = {}
    for leg, prob, why in assessed:
        try:
            aprobs[id(leg)] = (float(prob or 0.0), why)
        except Exception:
            pass
    def _analyzed(leg, why):
        try:
            _w = str(why or "")
            if _w == "implied (AI off)" or "board longshot" in _w or "undebated longshot" in _w:
                return False
            if str(leg.get("analysis", "") or "").strip():
                return True
            return bool(_w.strip())
        except Exception:
            return False
    cands = []
    for leg in candidates:
        if id(leg) in aprobs:
            pr, why = aprobs[id(leg)]
            if not _analyzed(leg, why):
                continue
        else:
            why = ""
            try:
                _pk = str(leg.get("_pick") or "").lower()
                _pd = (leg.get("_picks") or {}).get(_pk) if _pk else None
                pr = float(_pd[0]) if _pd else 0.0
                why = str(_pd[3])[:160] if _pd and len(_pd) > 3 else ""
            except Exception:
                pr = 0.0
            if not pr or not _analyzed(leg, why):
                continue
        try:
            _dp = float(leg.get("_sel_price") or leg.get("best_odds") or 0)
        except Exception:
            _dp = 0
        try:
            pr = float(pr or 0.0)
        except Exception:
            pr = 0.0
        if 1.5 <= _dp <= 8.0 and pr >= 0.40:
            try:
                if str(leg.get("market", "")) == "O/U 2.5" and pr * _dp < 1.0:
                    continue
            except Exception:
                pass
            cands.append((leg, pr, why, _dp))
    def _dream_rank(t):
        try:
            _cb = 0 if "+" in str(t[0].get("market", "")) else 1
        except Exception:
            _cb = 1
        return (_cb, -(t[1] * t[3]), -t[3])
    cands.sort(key=_dream_rank)
    picks, comb, fix = [], 1.0, {}
    for leg, pr, why, dp in cands:
        if len(picks) >= 10:
            break
        try:
            fk = _norm_team(leg.get("home_team", "")) + "|" + _norm_team(leg.get("away_team", ""))
        except Exception:
            fk = str(len(picks))
        if fix.get(fk, 0) >= 1:
            continue
        picks.append((leg, pr, why))
        fix[fk] = fix.get(fk, 0) + 1
        comb *= max(dp, 1.01)
    return picks


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


def _bankroll_cap(units: float) -> tuple:
    """Drawdown rule: bankroll below 70u (down 30% from 100u start) halves
    stakes, floor 0.5u. Returns (units, halved). Never raises."""
    try:
        try:
            import db as _db
        except ImportError:
            import worker.db as _db  # type: ignore
        if _db.get_bankroll() < 70.0:
            return max(0.5, round(float(units) / 2 * 2) / 2), True
    except Exception:
        pass
    return units, False


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
    units, _halved = _bankroll_cap(units)
    tier = "A · dream ticket" if kind == "weekly" else "B · steady value"
    return {"units": units, "confidence": round(min(avg_p + 0.1, 0.9), 2),
            "note": f"Tier {tier}. Heuristic sizing (LLM unavailable) — small either way." + (" Bankroll drawdown: stake halved." if _halved else ""), "llm": False}


def _agentic_stake(built: list, kind: str, use_ai: bool, progress_cb=None) -> dict:
    if not use_ai:
        return _heuristic_stake(built, kind)
    try:
        lines = [f"{i}. {b.get('selection')} | {b.get('league')} | odds {b.get('odds')} | p {b.get('probability')} | {str(b.get('reason',''))[:80]}"
                 for i, b in enumerate(built)]
        import math as _m
        combined = round(_m.prod(max(float(b["odds"]), 1.01) for b in built), 2)
        prompt = RANK_PROMPT.format(kind=kind, tier="A" if kind == "weekly" else "B",
                                    legs="\n".join(lines)[:3000], combined=combined)
        import time as _tm
        try:
            _jp = os.environ.get("JUDGE_PROVIDER", "gemini")
        except Exception:
            _jp = "gemini"
        # cooldown: the swarm just burst ~100 calls; let buckets refill before the
        # single most-exposed call. Judge lane (JUDGE_PROVIDER) is swarm-reserved.
        try:
            if progress_cb:
                progress_cb("Judge: cooling down 10s, then calling reserved lane…")
        except Exception:
            pass
        _tm.sleep(10)
        out = None
        for _try, _pref in enumerate([_jp, _jp, "nim", "nim", None]):
            if _try:
                _tm.sleep((0, 15, 20, 25, 30)[_try] if _try < 5 else 30)
            # honest budget gates: judge spends from its lane's quota
            try:
                try:
                    from learner import llm_left, log_llm, or_left, log_or
                except ImportError:
                    from worker.learner import llm_left, log_llm, or_left, log_or  # type: ignore
                if _pref == "orouter":
                    if or_left() <= 0:
                        continue
                    log_or()
                elif _pref == "gemini":
                    try:
                        from learner import gm_left, log_gm
                    except ImportError:
                        from worker.learner import gm_left, log_gm  # type: ignore
                    if gm_left() <= 0:
                        continue
                    log_gm()
                else:
                    if llm_left() <= 0:
                        continue
                    log_llm()
            except Exception:
                pass
            out = router.analyze(prompt, system_prompt=RANK_SYSTEM, model_pref=_pref)
            _t = out[0] if isinstance(out, tuple) else None
            _e = out[2] if isinstance(out, tuple) and len(out) > 2 else None
            if _t and not _e:
                break
            try:
                if progress_cb:
                    progress_cb(f"Judge: lane {_pref or 'chain'} busy, retrying…")
            except Exception:
                pass
        text = out[0] if isinstance(out, tuple) else None
        err = out[2] if isinstance(out, tuple) and len(out) > 2 else None
        if err or not text:
            return _heuristic_stake(built, kind, str(err or "empty rank reply"))
        try:
            try:
                from learner import log_model as _lm2
            except ImportError:
                from worker.learner import log_model as _lm2  # type: ignore
            _lm2(out[1] if isinstance(out, tuple) and len(out) > 1 else "")
        except Exception:
            pass
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
        units, _halved2 = _bankroll_cap(units)
        conf = min(max(float(d.get("confidence", 0.5)), 0.05), 0.95)
        return {"units": units, "confidence": round(conf, 2),
                "note": str(d.get("stake_note", ""))[:220] + (" Bankroll drawdown: stake halved." if _halved2 else ""), "llm": True,
                "combined_odds": combined}
    except Exception:
        return _heuristic_stake(built, kind)


def build_and_save(max_legs: int | None = None, use_ai: bool = True,
                   max_credits: int | None = None, progress_cb=None, kind: str = "daily",
                   detail: dict | None = None, slips: str = "both") -> list:
    """Build tickets and persist to DB. Returns ticket list.
    detail (optional dict) is filled with last-build diagnostics
    (candidate counts, filed legs/odds) for the public /status."""
    import db as db_mod
    try:
        import db  # worker-local
    except ImportError:
        db = db_mod
    else:
        db = db_mod
    tickets = build_tickets(max_legs=max_legs, use_ai=use_ai, max_credits=max_credits, progress_cb=progress_cb, kind=kind, slips=slips)
    for t in tickets:
        db.save_ticket(t["id"], t["combined_odds"], t["legs"], t["status"], kind=t.get("kind", "daily"), stake=t.get("stake"))
    try:
        # single-slip runs must NOT prune: keep=2 would eat yesterday's pair.
        if kind != "daily" or (slips or "both") == "both":
            db.prune_pending(kind=tickets[0].get("kind", kind) if tickets else kind, keep=2)
    except Exception:
        pass
    if detail is not None:
        try:
            detail.update(globals().get("_LAST_BUILD_DIAG") or {})
            detail["filed"] = [(t["id"], (t.get("stake") or {}).get("tier", "value"),
                                 len(t.get("legs", [])), t.get("combined_odds")) for t in tickets]
        except Exception:
            pass
    return tickets


if __name__ == "__main__":
    print("AI:", "on" if os.environ.get("GROQ_API_KEY") or os.environ.get("GEMINI_API_KEY") else "off (implied probs)")
    ts = build_tickets(max_legs=3, use_ai=False)
    print(f"built {len(ts)} ticket(s) without AI")
    if ts:
        print("combined:", ts[0]["combined_odds"], "legs:", len(ts[0]["legs"]))
