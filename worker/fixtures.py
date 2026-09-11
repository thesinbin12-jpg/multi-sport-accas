"""
fixtures.py — Popular-fixture universe from football-data.org + FotMob (both keyless-ish).

FDO free tier covers the top competitions; FotMob lists ~185 leagues/day with no auth.
Betika/Smarkets carry the ODDS; this module answers "is this fixture popular?"
so the scout boosts (not forces) prominent matches. Cached per UTC date in-process.
"""

import re
import time
from datetime import datetime, timezone, timedelta

import requests as _rq

_FOTMOB_TM = "https://www.fotmob.com/api/data/matches?date=%Y%m%d"
_FOTMOB_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
              "Chrome/120.0 Mobile Safari/537.36")
_CACHE: dict = {}


def _norm(name):
    return re.sub(r"\s+", " ", str(name or "").strip().lower())


def _last(name):
    parts = str(name or "").split()
    return parts[-1].lower() if parts else ""


def _fotmob_day(day):
    out = []
    try:
        r = _rq.get(day.strftime(_FOTMOB_TM), headers={"User-Agent": _FOTMOB_UA,
                     "Accept": "application/json"}, timeout=20)
        if r.status_code != 200:
            return out
        for lg in (r.json().get("leagues") or []):
            lname = lg.get("name", "")
            for m in (lg.get("matches") or [])[:60]:
                h = (m.get("home") or {}).get("longName") or (m.get("home") or {}).get("name", "")
                a = (m.get("away") or {}).get("longName") or (m.get("away") or {}).get("name", "")
                if h and a:
                    out.append((h, a, lname, "fotmob"))
    except Exception:
        pass
    return out


def _fdo_scheduled():
    """Top-competition scheduled fixtures via football-data.org (cached, best-effort)."""
    out = []
    try:
        try:
            from analyst import _fdo_get
        except ImportError:
            from worker.analyst import _fdo_get  # type: ignore
        d = _fdo_get("https://api.football-data.org/v4/competitions", {})
        comps = (d or {}).get("competitions") or []
        for c in comps[:16]:
            cid = c.get("id")
            try:
                d2 = _fdo_get(f"https://api.football-data.org/v4/competitions/{cid}/matches",
                              {"status": "SCHEDULED"})
                for m in ((d2 or {}).get("matches") or [])[:30]:
                    h = (m.get("homeTeam") or {}).get("name", "")
                    a = (m.get("awayTeam") or {}).get("name", "")
                    if h and a:
                        out.append((h, a, (c.get("name") or ""), "fdo"))
            except Exception:
                continue
    except Exception:
        pass
    return out


def get_popular(hours_ahead=48):
    """{(hnorm, anorm): {'league': str, 'src': str}}. Cached per UTC date."""
    daykey = datetime.now(timezone.utc).strftime("%Y-%m-%d") + f":{int(hours_ahead)}"
    if daykey in _CACHE:
        return _CACHE[daykey]
    days = max(1, min(8, int((hours_ahead + 20) // 24)))
    today = datetime.now(timezone.utc).date()
    pop: dict = {}
    for i in range(days):
        for h, a, lg, src in _fotmob_day(today + timedelta(days=i)):
            pop.setdefault((_norm(h), _norm(a)), {"league": lg, "src": src})
    for h, a, lg, src in _fdo_scheduled():
        k = (_norm(h), _norm(a))
        if k in pop:
            pop[k]["src"] += "+fdo"
        else:
            pop[k] = {"league": lg, "src": src}
    _CACHE[daykey] = pop
    return pop


def is_popular(home, away, pop):
    """(matched: bool, league: str). Exact norm match, else last-word match."""
    if not pop:
        return False, ""
    k = (_norm(home), _norm(away))
    if k in pop:
        return True, pop[k]["league"]
    hl, al = _last(home), _last(away)
    if hl and al:
        for (h, a), v in pop.items():
            if hl in h and al in a:
                return True, v["league"]
    return False, ""
