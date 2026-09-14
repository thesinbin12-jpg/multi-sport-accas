"""verifier.py — Result verification via FotMob + Odds API scores + football-data.org.
Web context via FREE search (Brave -> DDG) for undecided legs; Tavily only
as last resort when free backends return nothing (quota guard)."""
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


def record_clv_snapshot(progress_cb=None) -> dict:
    """Closing-line-value snapshot: one free Betika scan; for pending legs
    kicking off within 36h, compare filed odds vs the current same
    match/market/selection price. Filed > late = beating the close (+edge).
    Saves rows via db.save_clv. Never raises."""
    out = {"checked": 0, "recorded": 0}
    try:
        try:
            from betika_odds import scan_betika as _scan
        except ImportError:
            from worker.betika_odds import scan_betika as _scan  # type: ignore
        try:
            import teams as _tm
        except ImportError:
            import worker.teams as _tm  # type: ignore
        now = datetime.now(timezone.utc)
        scanned = _scan(hours_ahead=36) or []
        if not scanned:
            return out
        pairs = [f"{m.get('home_team', '')} vs {m.get('away_team', '')}" for m in scanned]
        tickets = [t for t in db.get_tickets(limit=50) if t.get("status") == "pending"]
        for t in tickets:
            try:
                legs = db.get_legs(t["id"])
            except Exception:
                continue
            for leg in legs:
                try:
                    if (leg.get("result") or "pending") != "pending":
                        continue
                    ct = str(leg.get("commence_time", "") or "")
                    if not ct:
                        continue
                    dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if not (now < dt <= now + timedelta(hours=36)):
                        continue
                    m = str(leg.get("match", "") or "")
                    if " vs " not in m:
                        continue
                    h, a = m.split(" vs ", 1)
                    hit = _tm.resolve_pair(h, a, pairs)
                    if not hit or hit not in pairs:
                        continue
                    sm = scanned[pairs.index(hit)]
                    want_mkt = str(leg.get("market", "")).lower()
                    want_sel = str(leg.get("selection", "")).lower()
                    # market family must match: Home Total Over 1.5 != O/U Over 1.5
                    if str(sm.get("market", "")).lower() != want_mkt:
                        continue
                    price = None
                    for _bm in sm.get("bookmakers", []) or []:
                        for _mk in (_bm.get("markets", []) or []):
                            for _o in (_mk.get("outcomes", []) or []):
                                if str(_o.get("name", "")).lower() == want_sel:
                                    # same-match guard: market family must match too
                                    price = float(_o.get("price", 0) or 0)
                                    break
                            if price:
                                break
                        if price:
                            break
                    if not price:
                        continue
                    out["checked"] += 1
                    try:
                        db.save_clv(t["id"], m, leg.get("market", ""), leg.get("selection", ""),
                                  float(leg.get("odds", 0) or 0), price)
                        out["recorded"] += 1
                    except Exception:
                        continue
                except Exception:
                    continue
        if progress_cb and out["recorded"]:
            try:
                progress_cb(f"CLV: {out['recorded']} late-price snapshots recorded.")
            except Exception:
                pass
    except Exception:
        pass
    return out


def verify_all_pending(days_from: int = 3, progress_cb=None) -> dict:
    """Settle pending tickets via selection-aware verification. Returns summary.
    Stale undecided legs get free web context (Brave -> DDG, capped 5/run;
    Tavily only when free backends are empty)."""
    db.init_schema()
    tickets = [t for t in db.get_tickets(limit=50) if t.get("status") == "pending"]
    if not tickets:
        return {"checked": 0, "won": 0, "lost": 0, "pending": 0}
    # Oldest slips first: the routes we keep failing get walked deliberately,
    # and capped web evidence goes to the longest-waiting legs.
    try:
        tickets.sort(key=lambda t: str(t.get("created_at") or ""))
    except Exception:
        pass

    won = lost = still_pending = 0
    skipped_future_all = 0
    contexts: list = []
    src_tally: dict = {}
    unres_sample: list = []
    for _i, t in enumerate(tickets):
        try:
            r = verify_ticket_with_selection(t["id"], days_from=days_from,
                                             fallback_date=t.get("created_at", "") or "")
        except Exception:
            still_pending += 1
            continue
        for k, v in (r.get("settle_sources") or {}).items():
            src_tally[k] = src_tally.get(k, 0) + v
        try:
            skipped_future_all += int(r.get("skipped_future", 0) or 0)
        except Exception:
            pass
        for m in (r.get("unresolved") or [])[:2]:
            if len(unres_sample) < 8 and m not in unres_sample:
                unres_sample.append(m)
        if r["status"] == "pending":
            still_pending += 1
            if len(contexts) < 5:
                for leg in db.get_legs(t["id"]):
                    if leg.get("result") == "pending" and len(contexts) < 5:
                        if not _kickoff_passed(leg.get("commence_time", "")):
                            continue  # upcoming match: no story yet, don't burn search on it
                        ctx = leg_context(leg.get("match", ""))
                        if ctx.get("text"):
                            contexts.append({"match": leg.get("match"),
                                             "note": ctx["text"][:200], "via": ctx["source"]})
        elif r["status"] == "won":
            won += 1
        else:
            lost += 1
        try:
            if progress_cb:
                progress_cb(f"Verify {_i + 1}/{len(tickets)}: {str(t.get('id', ''))[:26]} -> {r['status']} "
                              f"({r.get('correct', '?')}/{r.get('total', '?')})")
        except Exception:
            pass

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
    out: dict = {"checked": won + lost, "won": won, "lost": lost, "pending": still_pending,
                 "settle_sources": src_tally, "unresolved_sample": unres_sample,
                 "skipped_future": skipped_future_all}
    if contexts:
        out["contexts"] = contexts
    return out


def _kickoff_passed(ct: str) -> bool:
    """True when a leg's kickoff time is in the past (missing/NAIVE time ->
    True: better to spend one capped search than to leave a decider blind)."""
    try:
        if not ct:
            return True
        dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= datetime.now(timezone.utc)
    except Exception:
        return True


def _audit_fdo_score(home: str, away: str, score, cache: dict, days_from: int) -> None:
    """Best-effort FotMob-vs-FDO agreement audit (the verifier checking its
    own map). Shares the cached FDO call — no extra quota. Never raises."""
    try:
        key = config.FOOTBALL_DATA_ORG_KEY if hasattr(config, "FOOTBALL_DATA_ORG_KEY") else ""
        if not key:
            return
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
        ms = cache["_fd_matches"] or []
        if not ms:
            return
        try:
            from teams import resolve_pair as _tres_a
        except ImportError:
            from worker.teams import resolve_pair as _tres_a  # type: ignore
        pairs = []
        for m in ms:
            try:
                pairs.append((str(m.get("homeTeam", {}).get("name", "")),
                              str(m.get("awayTeam", {}).get("name", ""))))
            except Exception:
                continue
        bp, _ = _tres_a(home, away, pairs)
        if not bp:
            return
        for m in ms:
            try:
                if (str(m.get("homeTeam", {}).get("name", "")) != bp[0] or
                        str(m.get("awayTeam", {}).get("name", "")) != bp[1]):
                    continue
                ft = (m.get("score") or {}).get("fullTime", {}) or {}
                fdo = (_num(ft.get("home")), _num(ft.get("away")))
                if fdo[0] is None:
                    return
                try:
                    from learner import record_source_audit as _rsa
                except ImportError:
                    from worker.learner import record_source_audit as _rsa  # type: ignore
                _rsa("fotmob", "fdo", tuple(fdo) == tuple(score))
                return
            except Exception:
                continue
    except Exception:
        pass


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


def _settle_leg(selection: str, hs: int, aws: int, market: str = "") -> bool | str | None:
    """Market-aware settle from a full-time score. True=won, False=lost,
    'void'=push (DNB draw), None=unknown."""
    sel = str(selection or "").strip().lower()
    mkt = str(market or "").strip().lower()
    total = hs + aws
    home_win, draw, away_win = hs > aws, hs == aws, aws > hs
    both = hs > 0 and aws > 0
    if mkt in ("home total", "away total"):
        import re as _re4
        g = hs if mkt == "home total" else aws
        mt = _re4.match(r"(over|under)\s+(\d+(?:\.\d+)?)", sel)
        if not mt:
            return None
        try:
            line = float(mt.group(2))
        except Exception:
            return None
        return g > line if mt.group(1) == "over" else g < line
    if sel.startswith("dnb:"):
        code = sel.split(":", 1)[1].strip()
        if draw:
            return "void"
        if code == "1":
            return home_win
        if code == "2":
            return away_win
        return None
    if sel.startswith("btts:"):
        return both if "yes" in sel else (not both)
    if sel in ("yes", "no"):
        return both if sel == "yes" else (not both)
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
    Order (all free except Odds API): FotMob (keyless, ~185 leagues) ->
    Odds API scores -> football-data.org -> free web search (Brave -> DDG).
    NOTE: Betika REST is prematch-only (no results endpoint — probed
    /v1/results, /v1/matches/results, /v1/livescore, all 404; results page
    is an SPA shell), so it cannot settle. ESPN dropped per user call
    (limited league coverage; FotMob covers it)."""
    if " vs " not in match:
        return None
    home, away = [p.strip().lower() for p in match.split(" vs ", 1)]
    try:
        try:
            from learner import source_usable
        except ImportError:
            from worker.learner import source_usable  # type: ignore
        _use_fm = source_usable("scores-fotmob")
    except Exception:
        _use_fm = True
    if _use_fm:
        try:
            ref = None
            try:
                ct = (leg or {}).get("commence_time", "")
                ref = datetime.fromisoformat(str(ct).replace("Z", "+00:00")).date() if ct else None
            except Exception:
                ref = None
            fm = _fotmob_score(home, away, ref_date=ref)
            if fm:
                score, _fuzzy = fm
                try:
                    _audit_fdo_score(home, away, score, cache, days_from)
                except Exception:
                    pass
                try:
                    import logging as _lg
                    _lg.getLogger("acca").info("settle %s via FotMob%s %s", match[:60],
                                                  "-fuzzy" if _fuzzy else "", score)
                except Exception:
                    pass
                try:
                    if isinstance(leg, dict):
                        leg["_src"] = "fotmob-fuzzy" if _fuzzy else "fotmob"
                except Exception:
                    pass
                return score
        except Exception as e:
            try:
                import logging as _lg2
                _lg2.getLogger("acca").warning("fotmob skip %s: %s", match[:50], str(e)[:120])
            except Exception:
                pass
            try:
                try:
                    from learner import source_record as _sr2
                except ImportError:
                    from worker.learner import source_record as _sr2  # type: ignore
                _sr2("scores-fotmob", False, 0, str(e)[:150])
            except Exception:
                pass
    # Custom bookmaker/exchange keys have no Odds-API scores endpoint (was 2 wasted credits/leg).
    keys = [k for k in _keys_for_leg(leg or {}) if k not in ("soccer_betika", "soccer_smarkets")]
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
                    try:
                        (leg or {}).__setitem__("_src", "oddsapi") if isinstance(leg, dict) else None
                    except Exception:
                        pass
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
        try:
            from teams import resolve_pair as _tres2, learn as _tlearn2
        except ImportError:
            from worker.teams import resolve_pair as _tres2, learn as _tlearn2  # type: ignore
        _fms = cache["_fd_matches"] or []
        try:
            _pairs = [(str(m.get("homeTeam", {}).get("name", "")),
                       str(m.get("awayTeam", {}).get("name", ""))) for m in _fms]
            _bp, _sp = _tres2(home, away, _pairs)
        except Exception:
            _bp = None
        if _bp:
            for m in _fms:
                try:
                    h = str(m.get("homeTeam", {}).get("name", ""))
                    a = str(m.get("awayTeam", {}).get("name", ""))
                    if (h, a) != _bp:
                        continue
                    ft = (m.get("score") or {}).get("fullTime", {}) or {}
                    hs, aws = _num(ft.get("home")), _num(ft.get("away"))
                    if hs is not None and aws is not None:
                        try:
                            _tlearn2(home, h)
                            _tlearn2(away, a)
                        except Exception:
                            pass
                        try:
                            (leg or {}).__setitem__("_src", "fdo") if isinstance(leg, dict) else None
                        except Exception:
                            pass
                        return hs, aws
                except Exception:
                    continue
    # Last resort: own search engine (metasearch + page fetch, free, capped per run).
    try:
        _wn = cache.get("_web_n", 0)
        if _wn < 6:
            cache["_web_n"] = _wn + 1
            try:
                from websearch import enriched_search as _wsearch, settle_parse as _sparse
            except ImportError:
                _wsearch, _sparse = None, None
            if _wsearch is None:
                try:
                    from worker.websearch import enriched_search as _wsearch, settle_parse as _sparse  # type: ignore
                except ImportError:
                    _wsearch, _sparse = None, None
            if _wsearch is not None and _sparse is not None:
                _res = _wsearch(f"{home} vs {away} full time result score", max_results=4, fetch_top=1)
                _texts = [str(x.get("title", "")) + " " + str(x.get("snippet", "")) + " " + str(x.get("page", "")) for x in _res]
                _ws = _sparse(_texts, home, away)
                if _ws:
                    try:
                        if isinstance(leg, dict):
                            leg["_src"] = "web"
                    except Exception:
                        pass
                    return _ws
    except Exception:
        pass
    return None


def verify_ticket_with_selection(ticket_id: str, days_from: int = 3, fallback_date: str = "") -> dict:
    """Precise per-ticket verification using stored selection + scores.
    Slip-by-slip patience: legs whose kickoff is still in the future are
    skipped untouched (revisited on the next learn); finished legs bank
    their result immediately, but the TICKET only flips when every leg has
    decided. fallback_date (ticket created_at) anchors old legs that were
    saved without commence_time."""
    legs = db.get_legs(ticket_id)
    scanner = OddsScanner()
    cache: dict[str, list] = {}
    correct = 0
    decided = 0
    skipped_future = 0
    voided = 0
    sources: dict = {}
    unresolved: list = []
    for leg in legs:
        try:
            if not leg.get("commence_time") and fallback_date:
                leg["commence_time"] = fallback_date  # in-memory anchor only
        except Exception:
            pass
        if _kickoff_future(leg.get("commence_time", "")):
            skipped_future += 1
            unresolved.append(leg.get("match", "?"))
            continue
        score = _resolve_score(leg.get("match", ""), scanner, cache, days_from, leg)
        if score is None:
            # Ghost rule (bookmaker-style void): a leg still unsettled 7+ days
            # after kickoff stops blocking its slip — void, never guessed.
            if _leg_expired(leg, days=7):
                try:
                    db.update_leg_result(ticket_id, leg.get("match", ""), "void",
                                         "unresolved 7d+ after kickoff — void")
                except TypeError:
                    db.update_leg_result(ticket_id, leg.get("match", ""), "void")
                decided += 1
                voided += 1
            else:
                unresolved.append(leg.get("match", "?"))
            continue
        try:
            try:
                from learner import source_record as _srec
            except ImportError:
                from worker.learner import source_record as _srec  # type: ignore
            _srec("scores-" + str(leg.get("_src", "unknown")), True)
        except Exception:
            pass
        sources[leg.get("_src", "unknown")] = sources.get(leg.get("_src", "unknown"), 0) + 1
        hs, aws = score
        sel = str(leg.get("selection", ""))
        market_hit = _settle_leg(sel, hs, aws, leg.get("market", ""))
        if market_hit == "void":
            # DNB push (draw): stake back, leg counts for neither side.
            try:
                db.update_leg_result(ticket_id, leg.get("match", ""), "void",
                                     f"DNB push FT {hs}-{aws}")
            except TypeError:
                db.update_leg_result(ticket_id, leg.get("match", ""), "void")
            decided += 1
            voided += 1
            continue
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
        _side = "won" if market_hit else "lost"
        try:
            _ev = f"FT {hs}-{aws} {str(leg.get('_src', '')) or 'scores'}"
        except Exception:
            _ev = ""
        try:
            db.update_leg_result(ticket_id, leg.get("match", ""), _side, _ev)
        except TypeError:
            db.update_leg_result(ticket_id, leg.get("match", ""), _side)
        if market_hit:
            correct += 1
    total = len(legs)
    live_total = total - voided  # void legs don't count for or against
    base = {"ticket_id": ticket_id, "correct": correct, "total": live_total,
            "settle_sources": sources, "unresolved": unresolved[:8],
            "skipped_future": skipped_future, "voided": voided}
    if decided < total:
        return {**base, "status": "pending"}
    ticket_won = (correct == live_total and live_total > 0)
    db.record_verification(ticket_id, ticket_won, correct, live_total, {})
    return {**base, "status": "won" if ticket_won else "lost"}


def _leg_expired(leg: dict, days: int = 7) -> bool:
    """True when kickoff is known and `days`+ have passed. Missing time ->
    False (never void what we can't date)."""
    try:
        ct = leg.get("commence_time", "") or ""
        if not ct:
            return False
        dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() > days * 86400
    except Exception:
        return False


def _kickoff_future(ct: str) -> bool:
    """True only when a KNOWN kickoff is still ahead. Missing/unparseable ->
    False (attempt resolution; sources stay silent on unplayed matches)."""
    try:
        if not ct:
            return False
        dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt > datetime.now(timezone.utc)
    except Exception:
        return False


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


def _norm_name(s: str) -> str:
    """Normalize a club name: fold accents (Atlético->Atletico), saint->st,
    drop parentheticals, kill periods. Fixes real misses from user tickets."""
    try:
        import unicodedata as _ud
        t = _ud.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode("ascii")
        t = t.lower().strip()
        t = __import__("re").sub(r"\s*\([^)]*\)", "", t)
        t = t.replace(".", " ")
        t = __import__("re").sub(r"\bsaint\b", "st", t)
        return __import__("re").sub(r"\s+", " ", t).strip()
    except Exception:
        return str(s or "").lower().strip()


_STOPWORDS = {"fc", "ac", "sc", "cf", "cd", "ud", "ss", "us", "as",
              "fk", "sk", "bk", "ifk", "united", "city", "town", "rovers",
              "wanderers", "athletic", "sporting", "real", "club", "de",
              "del", "la", "le", "les", "al", "el", "das", "dos", "the",
              "w", "women", "ladies", "ii", "iii", "iv", "u21", "u23",
              "u19", "reserves", "reserve", "youth", "b"}


def _sig(name: str) -> set:
    """Significant tokens of a club name (stopwords dropped)."""
    try:
        return {w for w in __import__("re").split(r"[\s\-']+", _norm_name(name))
                if w and w not in _STOPWORDS}
    except Exception:
        return set()


def _names_match(a: str, b: str) -> bool:
    """Fuzzy team-name match on NORMALIZED names (accents/saint/periods folded).
    Either contains the other (handles 'Genoa CFC' vs 'Genoa')."""
    a, b = _norm_name(a), _norm_name(b)
    return bool(a and b) and (a == b or a in b or b in a)


def _fotmob_score(home: str, away: str, ref_date=None, span: int = 2):
    """((hs, aws), fuzzy?) or None — resolved through the agentic teams layer
    (every solved name is learned). Home and away resolve independently over
    the day's pool and must land on the SAME fixture; ambiguous days are
    skipped. Never guesses a scoreline."""
    try:
        try:
            from fotmob import _fm_day
        except ImportError:
            from worker.fotmob import _fm_day  # type: ignore
        try:
            from teams import resolve_pair as _tresolve, learn as _tlearn
        except ImportError:
            from worker.teams import resolve_pair as _tresolve, learn as _tlearn  # type: ignore
        from datetime import timedelta as _td, datetime as _dt, timezone as _tz
        base = ref_date or _dt.now(_tz.utc).date()
        if isinstance(base, str):
            base = _dt.fromisoformat(base[:10]).date()
        today = _dt.now(_tz.utc).date()
        # Two anchors: the leg's own date first (exact window), then today
        # (old legs saved without commence_time fall back to ticket-created,
        # but their matches play days AFTER the build — today catches those).
        bases = [base] + ([today] if today != base else [])
        for _base in bases:
            for d in range(-span, 1):
                try:
                    pool = _fm_day(_base + _td(days=d)) or {}
                except Exception:
                    continue
                if not pool:
                    continue
                try:
                    pair, ps = _tresolve(home, away, list(pool.keys()))
                except Exception:
                    continue
                if not pair or pair not in pool:
                    continue
                try:
                    _tlearn(home, pair[0])
                    _tlearn(away, pair[1])
                except Exception:
                    pass
                return pool[pair], (ps < 0.95)
    except Exception:
        pass
    return None


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
    """DuckDuckGo lite fallback (POST, works from datacenter IPs). NOTE: the
    old html.duckduckgo.com GET endpoint now returns a 202 bot-challenge, so
    this uses lite.duckduckgo.com instead. Tavily stays out of this path."""
    try:
        import re as _re
        import requests
        r = requests.post("https://lite.duckduckgo.com/lite/", data={"q": query},
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if r.status_code != 200:
            return ""
        html = r.text
        titles = _re.findall(r"class=['\"]result-link['\"][^>]*>(.*?)</a", html)[:max_results]
        snips = _re.findall(r"class=['\"]result-snippet['\"][^>]*>(.*?)</td", html)[:max_results]
        clean = lambda s: _re.sub(r"<[^>]+>", "", s).strip()[:160]
        parts = []
        for i, t in enumerate(titles):
            s = clean(snips[i]) if i < len(snips) else ""
            parts.append(f"{clean(t)}: {s}" if s else clean(t))
        return " | ".join(parts)
    except Exception:
        return ""


def web_search(query: str, max_results: int = 3) -> dict:
    """FREE-first search (Brave -> DDG via websearch stack). Tavily only as
    last resort when free backends return nothing (quota guard: Tavily at
    ~20% month). Returns {text, source}."""
    try:
        try:
            from websearch import search as _free_search
        except ImportError:
            from worker.websearch import search as _free_search  # type: ignore
        _free = _free_search(query, max_results=max_results) or []
        if _free:
            _txt = " | ".join(
                f"{x.get('title', '')}: {str(x.get('snippet', ''))[:160]}"
                for x in _free[:max_results] if x.get("title") or x.get("snippet"))
            if _txt:
                _src = (_free[0].get("source") or "free").lower()
                return {"text": _txt, "source": "brave" if "brave" in _src else "duckduckgo" if "ddg" in _src else _src}
    except Exception:
        pass
    # Tavily is LAST resort (paid quota): direct DDG runs before it.
    text = _ddg_search(query, max_results)
    if text:
        return {"text": text, "source": "duckduckgo"}
    text = _tavily_search(query, max_results)
    return {"text": text, "source": "tavily" if text else "none"}


def leg_context(match: str, max_age_days: int = 2) -> dict:
    """Web context for a still-undecided leg (postponed? walkover?).
    Bounded: caller caps calls per run."""
    res = web_search(f"{match} match result postponed cancelled", max_results=2)
    res["match"] = match
    return res


if __name__ == "__main__":
    print(verify_all_pending())
