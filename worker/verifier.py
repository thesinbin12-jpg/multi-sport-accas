"""verifier.py — Result verification via Odds API scores + football-data.org fallback.
Web context via Tavily -> DuckDuckGo chain for undecided legs."""
from datetime import datetime, timezone, timedelta

try:
    from scanner import OddsScanner, SCAN_CONFIG
except ImportError:
    from worker.scanner import OddsScanner, SCAN_CONFIG  # type: ignore

try:
    import db
except ImportError:
    from worker import db  # type: ignore


try:
    import config
except ImportError:
    from worker import config  # type: ignore


def _all_sport_keys() -> list:
    keys = []
    for group in SCAN_CONFIG.values():
        keys.extend(group)
    return keys


def verify_all_pending(days_from: int = 3) -> dict:
    """Settle pending tickets via selection-aware verification. Returns summary.
    Stale undecided legs get web context (Tavily -> DuckDuckGo, capped 5/run)."""
    db.init_schema()
    tickets = [t for t in db.get_tickets(limit=50) if t.get("status") == "pending"]
    if not tickets:
        return {"checked": 0, "won": 0, "lost": 0, "pending": 0}

    won = lost = still_pending = 0
    contexts: list = []
    for t in tickets:
        try:
            r = verify_ticket_with_selection(t["id"], days_from=days_from)
        except Exception:
            still_pending += 1
            continue
        if r["status"] == "pending":
            still_pending += 1
            if len(contexts) < 5:
                for leg in db.get_legs(t["id"]):
                    if leg.get("result") == "pending" and len(contexts) < 5:
                        ctx = leg_context(leg.get("match", ""))
                        if ctx.get("text"):
                            contexts.append({"match": leg.get("match"),
                                             "note": ctx["text"][:200], "via": ctx["source"]})
        elif r["status"] == "won":
            won += 1
        else:
            lost += 1

    stats = db.get_accuracy_stats()
    db.record_accuracy(stats["verified_tickets"], stats["won_tickets"], notes="auto verify")
    # finalization: any ticket (pending/checked) with all legs decided gets its
    # true status — never leaves decided slips in limbo, never deletes results.
    try:
        for t in db.get_tickets(limit=50):
            if (t.get("status") or "pending") not in ("pending", "checked"):
                continue
            legs = db.get_legs(t["id"])
            if not legs or any(l.get("result") == "pending" for l in legs):
                if (t.get("status") or "") == "checked":
                    db.set_ticket_status(t["id"], "pending")
                continue
            correct = sum(1 for l in legs if l.get("result") == "won")
            ticket_won = (correct == len(legs) and len(legs) > 0)
            db.record_verification(t["id"], ticket_won, correct, len(legs), {})
    except Exception:
        pass
    out: dict = {"checked": won + lost, "won": won, "lost": lost, "pending": still_pending}
    if contexts:
        out["contexts"] = contexts
    return out


def _check_leg_result(match: str, scanner: OddsScanner, cache: dict, days_from: int) -> str:
    """Try to resolve 'Home vs Away' to a completed score. Returns won/lost/pending."""
    if " vs " not in match:
        return "pending"
    home, away = [p.strip().lower() for p in match.split(" vs ", 1)]
    for sk in _all_sport_keys():
        if sk not in cache:
            try:
                cache[sk] = scanner.get_scores(sk, days_from=days_from) or []
            except Exception:
                cache[sk] = []
        for g in cache[sk]:
            h = str(g.get("home_team", "")).lower()
            a = str(g.get("away_team", "")).lower()
            if h == home and a == away and g.get("completed"):
                scores = {s.get("name", "").lower(): s.get("score") for s in g.get("scores", []) or []}
                hs = _num(scores.get(h))
                aws = _num(scores.get(a))
                if hs is None or aws is None:
                    return "pending"
                # legs are favourite-pick; selection check needs stored selection — caller handles won/lost via scores only when draw-aware.
                # Without stored selection here we mark decided only if a winner exists; main verify uses selection below.
                return "decided"  # placeholder, refined by verify_ticket_with_selection
    return "pending"


def _settle_leg(selection: str, hs: int, aws: int) -> bool | None:
    """Market-aware settle from a full-time score. True=won, False=lost, None=unknown."""
    sel = str(selection or "").strip().lower()
    total = hs + aws
    home_win, draw, away_win = hs > aws, hs == aws, aws > hs
    both = hs > 0 and aws > 0
    if sel.startswith("btts:"):
        return both if "yes" in sel else (not both)
    if sel in ("1x", "x2", "12"):
        return {"1x": hs >= aws, "x2": aws >= hs, "12": hs != aws}[sel]
    if "&" in sel:
        # combos: "1&YES", "O2.5&YES", "1&O2.5", "X&U1.5" …
        import re as _re
        parts = [p.strip() for p in sel.split("&")]
        res = []
        for p in parts:
            if p in ("1", "x", "2"):
                res.append({"1": home_win, "x": draw, "2": away_win}[p])
            elif p == "yes":
                res.append(both)
            elif p == "no":
                res.append(not both)
            else:
                m = _re.match(r"([ou])\s*(\d+(?:\.5)?)", p)
                if not m:
                    return None
                line = float(m.group(2))
                res.append(total > line if m.group(1) == "o" else total < line)
        return all(res)
    if sel.startswith("over"):
        try:
            line = float(sel.split()[1])
            return total > line
        except Exception:
            return None
    if sel.startswith("under"):
        try:
            line = float(sel.split()[1])
            return total < line
        except Exception:
            return None
    if sel.startswith("dc:"):
        code = sel.split(":", 1)[1].strip()
        if code == "1x":
            return hs >= aws
        if code == "x2":
            return aws >= hs
        if code == "12":
            return hs != aws
        return None
    return None  # 1X2 handled by caller via winner


def _resolve_score(match: str, scanner: OddsScanner, cache: dict, days_from: int, leg: dict | None = None):
    """Return (hs, aws) full-time goals for a match, or None.
    FotMob first (free, ~185 leagues incl. obscure), Odds API, football-data fallback."""
    if " vs " not in match:
        return None
    home, away = [p.strip().lower() for p in match.split(" vs ", 1)]
    try:
        try:
            from fotmob import find_finished_score
        except ImportError:
            from worker.fotmob import find_finished_score  # type: ignore
        ref = None
        try:
            ct = (leg or {}).get("commence_time", "")
            ref = datetime.fromisoformat(str(ct).replace("Z", "+00:00")).date() if ct else None
        except Exception:
            ref = None
        fm = find_finished_score(home, away, ref_date=ref,
                                  match_fn=lambda h, a, hn, an: _names_match(h, hn) and _names_match(a, an))
        if fm:
            return fm
    except Exception:
        pass
    keys = _keys_for_leg(leg or {})
    for sk in keys:
        if sk not in cache:
            try:
                cache[sk] = scanner.get_scores(sk, days_from=days_from) or []
            except Exception:
                cache[sk] = []
        for g in cache[sk]:
            h = str(g.get("home_team", "")).lower()
            a = str(g.get("away_team", "")).lower()
            if h == home and a == away and g.get("completed"):
                scores = {str(s.get("name", "")).lower(): s.get("score") for s in g.get("scores", []) or []}
                hs, aws = _num(scores.get(h)), _num(scores.get(a))
                if hs is not None and aws is not None:
                    return hs, aws
    # Fallback: football-data.org full-time scores
    key = config.FOOTBALL_DATA_ORG_KEY if hasattr(config, "FOOTBALL_DATA_ORG_KEY") else ""
    if key:
        if "_fd_matches" not in cache:
            try:
                import requests
                today = datetime.now(timezone.utc).date()
                start = (today - timedelta(days=days_from)).isoformat()
                r = requests.get("https://api.football-data.org/v4/matches",
                                 headers={"X-Auth-Token": key},
                                 params={"status": "FINISHED", "dateFrom": start, "dateTo": today.isoformat()},
                                 timeout=20)
                cache["_fd_matches"] = r.json().get("matches", []) if r.status_code == 200 else []
            except Exception:
                cache["_fd_matches"] = []
        for m in cache["_fd_matches"]:
            try:
                h = m.get("homeTeam", {}).get("name", "")
                a = m.get("awayTeam", {}).get("name", "")
                if _names_match(h, home) and _names_match(a, away):
                    ft = (m.get("score") or {}).get("fullTime", {}) or {}
                    hs, aws = _num(ft.get("home")), _num(ft.get("away"))
                    if hs is not None and aws is not None:
                        return hs, aws
            except Exception:
                continue
    return None


def verify_ticket_with_selection(ticket_id: str, days_from: int = 3) -> dict:
    """Precise per-ticket verification using stored selection + scores."""
    legs = db.get_legs(ticket_id)
    scanner = OddsScanner()
    cache: dict[str, list] = {}
    correct = 0
    decided = 0
    for leg in legs:
        score = _resolve_score(leg.get("match", ""), scanner, cache, days_from, leg)
        if score is None:
            continue
        hs, aws = score
        sel = str(leg.get("selection", ""))
        market_hit = _settle_leg(sel, hs, aws)
        if market_hit is None:
            # 1X2: compare selection to winner
            if hs > aws:
                winner = leg.get("match", "").split(" vs ", 1)[0]
            elif aws > hs:
                winner = leg.get("match", "").split(" vs ", 1)[1]
            else:
                winner = "draw"
            s = sel.lower()
            w = winner.lower()
            market_hit = (s == w) or (w in s) or (s in w)
        decided += 1
        db.update_leg_result(ticket_id, leg.get("match", ""), "won" if market_hit else "lost")
        if market_hit:
            correct += 1
    total = len(legs)
    if decided < total:
        return {"ticket_id": ticket_id, "status": "pending", "correct": correct, "total": total}
    ticket_won = (correct == total and total > 0)
    db.record_verification(ticket_id, ticket_won, correct, total, {})
    return {"ticket_id": ticket_id, "status": "won" if ticket_won else "lost",
            "correct": correct, "total": total}


def _keys_for_leg(leg: dict) -> list:
    """Sport keys worth querying for one leg: exact key first, league match next.
    Keeps nightly score calls to ~1-3 per leg instead of ~177."""
    sk = (leg.get("sport_key") or "").strip()
    if sk:
        return [sk]
    league = (leg.get("league") or "").strip().lower()
    if league:
        hits = [k for group in SCAN_CONFIG.values() for k in group
                if k.replace("_", " ").lower() == league or league in k.replace("_", " ").lower()]
        if hits:
            return hits[:3]
    return _all_sport_keys()


def _resolve_match_winner(match: str, scanner: OddsScanner, cache: dict, days_from: int, leg: dict | None = None):
    if " vs " not in match:
        return None
    home, away = [p.strip().lower() for p in match.split(" vs ", 1)]
    keys = _keys_for_leg(leg or {})
    for sk in keys:
        if sk not in cache:
            try:
                cache[sk] = scanner.get_scores(sk, days_from=days_from) or []
            except Exception:
                cache[sk] = []
        for g in cache[sk]:
            h = str(g.get("home_team", "")).lower()
            a = str(g.get("away_team", "")).lower()
            if h == home and a == away and g.get("completed"):
                scores = {str(s.get("name", "")).lower(): s.get("score") for s in g.get("scores", []) or []}
                hs = _num(scores.get(h))
                aws = _num(scores.get(a))
                if hs is None or aws is None:
                    return None
                if hs > aws:
                    return g.get("home_team", "")
                if aws > hs:
                    return g.get("away_team", "")
                return "draw"
    # Fallback: football-data.org for soccer (free tier, finished matches)
    return _football_data_winner(home, away, cache, days_from)


def _num(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _names_match(a: str, b: str) -> bool:
    """Fuzzy team-name match: either contains the other (handles 'Genoa CFC' vs 'Genoa')."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    return bool(a and b) and (a == b or a in b or b in a)


def _football_data_winner(home: str, away: str, cache: dict, days_from: int):
    """Fallback score source for soccer via football-data.org (free tier).
    One cached call per run; silent skip when key missing."""
    key = config.FOOTBALL_DATA_ORG_KEY if hasattr(config, "FOOTBALL_DATA_ORG_KEY") else ""
    if not key:
        return None
    if "_fd_matches" not in cache:
        try:
            import requests
            today = datetime.now(timezone.utc).date()
            start = (today - timedelta(days=days_from)).isoformat()
            r = requests.get("https://api.football-data.org/v4/matches",
                             headers={"X-Auth-Token": key},
                             params={"status": "FINISHED", "dateFrom": start, "dateTo": today.isoformat()},
                             timeout=20)
            cache["_fd_matches"] = r.json().get("matches", []) if r.status_code == 200 else []
        except Exception:
            cache["_fd_matches"] = []
    for m in cache["_fd_matches"]:
        try:
            h = m.get("homeTeam", {}).get("name", "")
            a = m.get("awayTeam", {}).get("name", "")
            if _names_match(h, home) and _names_match(a, away):
                w = (m.get("score") or {}).get("winner")
                if w == "HOME_TEAM":
                    return h
                if w == "AWAY_TEAM":
                    return a
                if w == "DRAW":
                    return "draw"
        except Exception:
            continue
    return None


_UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36"}


def _tavily_search(query: str, max_results: int = 3) -> str:
    key = getattr(config, "TAVILY_API_KEY", "") if hasattr(config, "TAVILY_API_KEY") else ""
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


def _ddg_search(query: str, max_results: int = 3) -> str:
    """DuckDuckGo html fallback — used when Tavily credits run out."""
    try:
        import re as _re
        import requests
        from urllib.parse import quote_plus
        r = requests.get("https://html.duckduckgo.com/html/?q=" + quote_plus(query),
                         headers=_UA, timeout=20)
        if r.status_code != 200:
            return ""
        html = r.text
        titles = _re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, _re.DOTALL)[:max_results]
        snips = _re.findall(r'class="result__snippet"[^>]*>(.*?)</a?>', html, _re.DOTALL)[:max_results]
        clean = lambda s: _re.sub(r"<[^>]+>", "", s).strip()[:160]
        parts = []
        for i, t in enumerate(titles):
            s = clean(snips[i]) if i < len(snips) else ""
            parts.append(f"{clean(t)}: {s}" if s else clean(t))
        return " | ".join(parts)
    except Exception:
        return ""


def web_search(query: str, max_results: int = 3) -> dict:
    """Tavily first, DuckDuckGo fallback. Returns {text, source}."""
    text = _tavily_search(query, max_results)
    if text:
        return {"text": text, "source": "tavily"}
    text = _ddg_search(query, max_results)
    return {"text": text, "source": "duckduckgo" if text else "none"}


def leg_context(match: str, max_age_days: int = 2) -> dict:
    """Web context for a still-undecided leg (postponed? walkover?).
    Bounded: caller caps calls per run."""
    res = web_search(f"{match} match result postponed cancelled", max_results=2)
    res["match"] = match
    return res


if __name__ == "__main__":
    print(verify_all_pending())
