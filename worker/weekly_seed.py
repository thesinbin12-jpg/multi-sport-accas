"""
weekly_seed.py — fixture-led 7-day weekly acca.

Books only price ~48h ahead, so a Monday scan can never see the weekend.
This module flips it: FIXTURES lead (FotMob 7 days + football-data.org
scheduled, both free), analysis happens early, real Betika odds attach
progressively as books price each match (per-match search via teams.py).

Flow:
  Monday weekly build -> seed_week() queues the week's ~25 (data-scored,
      spread across days, no LLM) -> price_and_append() prices Mon-Tue now.
  Nightly learn (01:00) -> price_and_append() fills Wed-Sun as priced.
Ticket grows Mon->Sun up to WEEKLY_MAX_LEGS. Frontend untouched: only
priced legs ever reach the ticket. Never raises.
"""

from datetime import datetime, timezone, timedelta

WEEK_CAP = 25
_PER_DAY_CAP = 5
_MIN_PROB = 0.55


def _cfg(name, default):
    try:
        try:
            import config as _c
        except ImportError:
            import worker.config as _c  # type: ignore
        return int(getattr(_c, name, default) or default)
    except Exception:
        return default


def current_week_id():
    try:
        today = datetime.now(timezone.utc).date()
        monday = today - timedelta(days=today.weekday())
        return monday.isoformat()
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def fixtures_7d():
    """All fixtures for the next 7 days. [{home, away, league, src, day}]"""
    out, seen = [], set()
    try:
        try:
            import fixtures as _fx
        except ImportError:
            import worker.fixtures as _fx  # type: ignore
        try:
            import teams as _tm
        except ImportError:
            import worker.teams as _tm  # type: ignore
        today = datetime.now(timezone.utc).date()
        # FotMob: any date, ~185 leagues/day, keyless. Date assigned by query day.
        for i in range(7):
            day = today + timedelta(days=i)
            try:
                for h, a, lg, src in _fx._fotmob_day(day):
                    k = (_tm.normalize(h), _tm.normalize(a))
                    if not h or not a or k in seen:
                        continue
                    seen.add(k)
                    out.append({"home": h, "away": a, "league": lg or "",
                                "src": src, "day": day.isoformat()})
            except Exception:
                continue
        # FDO scheduled: top comps with real utcDate (free 10/min, best-effort).
        try:
            try:
                from analyst import _fdo_get
            except ImportError:
                from worker.analyst import _fdo_get  # type: ignore
            d = _fdo_get("https://api.football-data.org/v4/competitions", {}) or {}
            for c in (d.get("competitions") or [])[:16]:
                try:
                    cid = c.get("id")
                    d2 = _fdo_get(
                        f"https://api.football-data.org/v4/competitions/{cid}/matches",
                        {"status": "SCHEDULED"}) or {}
                    for m in (d2.get("matches") or [])[:40]:
                        h = ((m.get("homeTeam") or {}).get("shortName")
                             or (m.get("homeTeam") or {}).get("name", ""))
                        a = ((m.get("awayTeam") or {}).get("shortName")
                             or (m.get("awayTeam") or {}).get("name", ""))
                        k = (_tm.normalize(h), _tm.normalize(a))
                        if not h or not a or k in seen:
                            continue
                        seen.add(k)
                        try:
                            _dt = datetime.fromisoformat(
                                str(m.get("utcDate", "")).replace("Z", "+00:00"))
                            _day = _dt.date().isoformat()
                        except Exception:
                            _day = today.isoformat()
                        out.append({"home": h, "away": a,
                                    "league": c.get("name") or "",
                                    "src": "fdo", "day": _day})
                except Exception:
                    continue
        except Exception:
            pass
    except Exception:
        pass
    return out


def _form_figs(form_str):
    try:
        s = str(form_str or "")
        return s.count("W") * 3 + s.count("D")
    except Exception:
        return 0


def score_fixtures(fxs, progress_cb=None, limit=90):
    """Data-score fixtures (no odds, no LLM): 30d FotMob form both sides,
    data names the market first. Returns [{...fixture, market, selection,
    prob, why}]."""
    scored = []
    try:
        try:
            from fotmob import team_form as _tf
        except ImportError:
            from worker.fotmob import team_form as _tf  # type: ignore
        # sample round-robin by day: scoring only pool[:N would see Monday
        # alone and the weekend would never queue. ~16/day x 7 days.
        _by_day: dict = {}
        for _f in (fxs or []):
            _by_day.setdefault(str(_f.get("day", "")), []).append(_f)
        _per = max(8, int(int(limit or 90) // max(1, len(_by_day))))
        pool = []
        for _d in sorted(_by_day):
            pool.extend(_by_day[_d][:_per])
        n = 0
        for fx in pool:
            try:
                h, a = fx.get("home", ""), fx.get("away", "")
                fm = _tf(h, a) or {}
                hf = fm.get(h) or fm.get(h.strip()) or {}
                af = fm.get(a) or fm.get(a.strip()) or {}
                if not hf and fm:
                    try:
                        hf = list(fm.values())[0] or {}
                        af = list(fm.values())[1] or {}
                    except Exception:
                        pass
                hp, ap = _form_figs(hf.get("form")), _form_figs(af.get("form"))
                try:
                    hgf = float(hf.get("gf", 0) or 0) / max(1, int(hf.get("gp", 0) or 0))
                    hga = float(hf.get("ga", 0) or 0) / max(1, int(hf.get("gp", 0) or 0))
                    agf = float(af.get("gf", 0) or 0) / max(1, int(af.get("gp", 0) or 0))
                    aga = float(af.get("ga", 0) or 0) / max(1, int(af.get("gp", 0) or 0))
                except Exception:
                    hgf = hga = agf = aga = 1.3
                edge = hp - ap
                xg = hgf + aga + agf + hga  # rough combined-goals expectation
                if edge >= 5:
                    market, sel = "Double chance", "1X"
                    prob = min(0.78, 0.62 + edge * 0.012)
                elif edge <= -5:
                    market, sel = "Double chance", "X2"
                    prob = min(0.78, 0.62 - edge * 0.012)
                elif hgf >= 1.6 and agf >= 1.4 and hga >= 1.0 and aga >= 1.0:
                    market, sel = "BTTS", "BTTS: Yes"
                    prob = 0.62
                elif xg <= 2.2 and hga <= 1.2 and aga <= 1.2:
                    market, sel = "O/U 2.5", "Under 2.5"
                    prob = 0.63
                elif xg >= 3.2:
                    market, sel = "O/U 2.5", "Over 2.5"
                    prob = 0.60
                else:
                    market, sel = ("Double chance", "1X" if edge >= 0 else "X2")
                    prob = 0.58
                why = (f"DATA pre-odds: {h} {hf.get('form','?')} ({hp}pts) vs "
                       f"{a} {af.get('form','?')} ({ap}pts); "
                       f"goals {hgf:.1f}/{hga:.1f} vs {agf:.1f}/{aga:.1f}.")
                if prob >= _MIN_PROB:
                    scored.append({**fx, "market": market, "selection": sel,
                                   "prob": round(prob, 4), "why": why})
                n += 1
                if progress_cb and n % 20 == 0:
                    try:
                        progress_cb(f"Weekly seed: data-scored {n}/{len(pool)} fixtures…")
                    except Exception:
                        pass
            except Exception:
                continue
    except Exception:
        pass
    scored.sort(key=lambda s: -float(s.get("prob", 0)))
    # spread across the week: no single day hogs the ticket
    kept, per_day = [], {}
    for s in scored:
        d = str(s.get("day", ""))
        if per_day.get(d, 0) >= _PER_DAY_CAP:
            continue
        per_day[d] = per_day.get(d, 0) + 1
        kept.append(s)
        if len(kept) >= WEEK_CAP:
            break
    return kept


def seed_week(progress_cb=None):
    """Queue this week's ~25 from the 7-day fixture universe. Keeps already
    priced rows; skips matches already on the open weekly ticket."""
    summary = {"week": current_week_id(), "fixtures": 0, "queued": 0}
    try:
        try:
            import db as _db
        except ImportError:
            import worker.db as _db  # type: ignore
        wid = current_week_id()
        fxs = fixtures_7d()
        summary["fixtures"] = len(fxs)
        if not fxs:
            return summary
        # don't re-queue what's priced or already filed
        try:
            existing = {(str(r.get("home", "")).lower(), str(r.get("away", "")).lower())
                        for r in (_db.get_shortlist(wid, ("priced",)) or [])}
        except Exception:
            existing = set()
        try:
            for t in (_db.get_tickets(limit=10, kind="weekly") or []):
                if (t.get("status") or "pending") not in ("pending",):
                    continue
                try:
                    for _l in (_db.get_legs(t["id"]) or []):
                        _m = str(_l.get("match", "") or "")
                        if " vs " in _m:
                            _h, _a = _m.split(" vs ", 1)
                            existing.add((_h.strip().lower(), _a.strip().lower()))
                except Exception:
                    continue
        except Exception:
            pass
        try:
            import teams as _tm
        except ImportError:
            import worker.teams as _tm  # type: ignore
        fresh = [f for f in fxs
                 if (_tm.normalize(f.get("home", "")), _tm.normalize(f.get("away", ""))) not in existing]
        short = score_fixtures(fresh, progress_cb=progress_cb)
        try:
            _db.save_shortlist(wid, short)
        except Exception:
            pass
        summary["queued"] = len(short)
        if progress_cb:
            try:
                progress_cb(f"Weekly seed: {len(short)} queued from {len(fxs)} fixtures ({wid}).")
            except Exception:
                pass
    except Exception:
        pass
    return summary


def price_and_append(ticket, progress_cb=None):
    """Price queued shortlist legs via per-match Betika search; append priced
    legs into the open weekly ticket dict (builder path saves it; learner
    path uses the returned fresh legs with db.append_ticket_legs). Marks
    rows priced / drops stale ones. Returns (fresh_legs, summary)."""
    summary = {"scanned": 0, "priced": 0, "dropped": 0}
    fresh = []
    try:
        try:
            import db as _db
        except ImportError:
            import worker.db as _db  # type: ignore
        try:
            import teams as _tm
        except ImportError:
            import worker.teams as _tm  # type: ignore
        try:
            from betika_odds import scan_betika as _scan
        except ImportError:
            from worker.betika_odds import scan_betika as _scan  # type: ignore
        wid = current_week_id()
        try:
            cap = _cfg("WEEKLY_MAX_LEGS", WEEK_CAP)
        except Exception:
            cap = WEEK_CAP
        legs = ticket.get("legs", []) or []
        if len(legs) >= cap:
            return fresh, summary
        try:
            queued = _db.get_shortlist(wid, ("queued",)) or []
        except Exception:
            queued = []
        if not queued:
            return fresh, summary
        now = datetime.now(timezone.utc)
        # only legs inside the books' pricing horizon can price
        due = []
        for q in queued:
            try:
                _d = datetime.fromisoformat(str(q.get("commence_time", ""))[:10] + "T23:59:59+00:00")
                if _d.tzinfo is None:
                    _d = _d.replace(tzinfo=timezone.utc)
                if _d < now - timedelta(hours=6):
                    try:
                        _db.set_shortlist_status(wid, q.get("home", ""), q.get("away", ""), "dropped")
                    except Exception:
                        pass
                    summary["dropped"] += 1
                    continue
                if _d <= now + timedelta(hours=50):
                    due.append(q)
            except Exception:
                continue
        if not due:
            return fresh, summary
        scanned = _scan(hours_ahead=50) or []
        summary["scanned"] = len(scanned)
        pairs = [f"{m.get('home_team', '')} vs {m.get('away_team', '')}" for m in scanned]
        on_ticket = set()
        for _l in legs:
            _m = str(_l.get("match", "") or "")
            if " vs " in _m:
                _h, _a = _m.split(" vs ", 1)
                on_ticket.add((_tm.normalize(_h), _tm.normalize(_a)))
        appended = 0
        for q in due:
            try:
                if len(legs) >= cap:
                    break
                qk = (_tm.normalize(q.get("home", "")), _tm.normalize(q.get("away", "")))
                if qk in on_ticket:
                    try:
                        _db.set_shortlist_status(wid, q.get("home", ""), q.get("away", ""), "priced")
                    except Exception:
                        pass
                    continue
                hit = _tm.resolve_pair(q.get("home", ""), q.get("away", ""), pairs)
                if not hit:
                    continue
                m = scanned[pairs.index(hit)] if hit in pairs else None
                if not m:
                    continue
                try:
                    _tm.learn(q.get("home", ""), m.get("home_team", ""))
                    _tm.learn(q.get("away", ""), m.get("away_team", ""))
                except Exception:
                    pass
                outs = []
                for _bm in m.get("bookmakers", []) or []:
                    for _mk in (_bm.get("markets", []) or []):
                        outs += (_mk.get("outcomes", []) or [])
                want_mkt = str(q.get("market", "")).lower()
                want_sel = str(q.get("selection", "")).lower()
                price = None
                for _o in outs:
                    _n = str(_o.get("name", "")).lower()
                    if want_mkt.startswith("double"):
                        if _n == want_sel:
                            price = float(_o.get("price", 0) or 0)
                            break
                    elif want_mkt.startswith("btts"):
                        if _n == want_sel:
                            price = float(_o.get("price", 0) or 0)
                            break
                    elif want_mkt.startswith("o/u"):
                        if _n == want_sel:
                            price = float(_o.get("price", 0) or 0)
                            break
                    else:
                        if _n == want_sel:
                            price = float(_o.get("price", 0) or 0)
                            break
                if not price or price < 1.5 or price > 7.0:
                    continue
                _nl = {
                    "sport": "Soccer", "sport_key": "soccer_betika",
                    "league": m.get("league", "") or q.get("league", ""),
                    "match": f"{m.get('home_team', '')} vs {m.get('away_team', '')}",
                    "market": q.get("market", ""), "selection": q.get("selection", ""),
                    "odds": round(price, 3), "probability": float(q.get("prob", 0.6)),
                    "result": "pending",
                    "analysis": str(q.get("why", "") or "")[:2000],
                    "commence_time": m.get("commence_time", ""),
                    "bookmaker": "Betika",
                }
                legs.append(_nl)
                fresh.append(_nl)
                on_ticket.add(qk)
                try:
                    _db.set_shortlist_status(wid, q.get("home", ""), q.get("away", ""), "priced")
                except Exception:
                    pass
                appended += 1
            except Exception:
                continue
        if appended:
            try:
                import math as _math
                ticket["legs"] = legs
                ticket["combined_odds"] = round(_math.prod(
                    max(float(l.get("odds", 1.0)), 1.01) for l in legs), 3)
            except Exception:
                pass
            if progress_cb:
                try:
                    progress_cb(f"Weekly fill: +{appended} priced ({len(legs)} legs, "
                                f"{ticket.get('combined_odds')}x).")
                except Exception:
                    pass
        summary["priced"] = appended
        return fresh, summary
    except Exception:
        return fresh, summary
