"""
espn.py — Free score settler via ESPN scoreboard API (keyless, datacenter-friendly).

Covers majors + US leagues (NWSL verified). Complements FotMob (blocked from
some datacenter IPs) and football-data.org (13 comps). Day-cached per process.
"""

import requests as _rq
import time as _time
from datetime import datetime as _dt, timezone as _tz, timedelta as _td

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/%s/scoreboard"
_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
       "Chrome/120.0 Mobile Safari/537.36")

# (Betika category/competition keyword) -> ESPN league slugs to try
LEAGUES = {
    "england premier league": ["eng.1"],
    "england championship": ["eng.2"],
    "england league one": ["eng.3"],
    "spain laliga": ["esp.1"],
    "germany bundesliga": ["ger.1"],
    "italy serie a": ["ita.1"],
    "france ligue 1": ["fra.1"],
    "netherlands eredivisie": ["ned.1"],
    "portugal": ["por.1"],
    "usa nwsl": ["usa.nwsl"],
    "usa mls": ["usa.mls"],
    "usa usl": ["usa.usl.1", "usa.usl1"],
    "mexico": ["mex.1"],
    "brazil serie a": ["bra.1"],
    "champions league": ["uefa.champions"],
    "europa league": ["uefa.europa"],
    "spain": ["esp.1", "esp.2"],
    "germany": ["ger.1", "ger.2"],
    "italy": ["ita.1", "ita.2"],
    "france": ["fra.1", "fra.2"],
    "england": ["eng.1", "eng.2"],
    "scotland": ["sco.1"],
    "belgium": ["bel.1"],
    "turkey": ["tur.1"],
    "saudi": ["sau.1"],
}

_DAY: dict = {}
_SESS = None


def _session():
    global _SESS
    if _SESS is None:
        try:
            from curl_cffi import requests as _cr
            _SESS = _cr.Session(impersonate="chrome120")
        except ImportError:
            _SESS = False
    return _SESS


def _day_scores(slug, day):
    key = f"{slug}:{day.isoformat()}"
    if key in _DAY:
        return _DAY[key]
    out = []
    try:
        s = _session()
        if s:
            r = s.get(BASE % slug, params={"dates": day.strftime("%Y%m%d")},
                      headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=20)
        else:
            r = _rq.get(BASE % slug, params={"dates": day.strftime("%Y%m%d")},
                        headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=20)
        if r.status_code == 429:
            _time.sleep(15)
            r = (s or _rq).get((BASE % slug), params={"dates": day.strftime("%Y%m%d")},
                               headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=20)
        if r.status_code == 200:
            for e in (r.json().get("events") or []):
                try:
                    st = (e.get("status") or {}).get("type", {}).get("name", "")
                    if "FULL_TIME" not in st and "STATUS_FULL_TIME" not in st:
                        continue
                    comps = (e.get("competitions") or [{}])[0].get("competitors", [])
                    h = next((c for c in comps if c.get("homeAway") == "home"), {})
                    a = next((c for c in comps if c.get("homeAway") == "away"), {})
                    out.append((h.get("team", {}).get("displayName", ""),
                                a.get("team", {}).get("displayName", ""),
                                h.get("score"), a.get("score")))
                except Exception:
                    continue
    except Exception:
        pass
    if out:
        _DAY[key] = out
    return out


def _slugs_for(league_hint):
    lh = str(league_hint or "").lower()
    slugs = []
    for k, v in LEAGUES.items():
        if k in lh:
            slugs.extend(v)
    seen = []
    for s in slugs:
        if s not in seen:
            seen.append(s)
    return seen[:4]


def find_score(home, away, league_hint="", ref_date=None, span=2, match_fn=None):
    """(hs, aws) or None. Tries mapped ESPN leagues around ref_date."""
    def _default(h1, a1, h2, a2):
        return (h1 == h2 and a1 == a2) or (h1 in h2 and a1 in a2) or (h2 in h1 and a2 in a1)
    mf = match_fn or _default
    try:
        base = ref_date or _dt.now(_tz).date()
        if isinstance(base, str):
            base = _dt.fromisoformat(base[:10]).date()
        hn, an = str(home or "").strip().lower(), str(away or "").strip().lower()
        slugs = _slugs_for(league_hint) or ["eng.1", "esp.1", "ger.1", "ita.1", "fra.1",
                                            "usa.nwsl", "usa.mls", "mex.1"]
        for d in range(-span, 1):
            day = base + _td(days=d)
            for slug in slugs:
                for h, a, hs, aws in _day_scores(slug, day):
                    try:
                        if mf(h.lower(), a.lower(), hn, an):
                            return int(hs), int(aws)
                    except Exception:
                        continue
    except Exception:
        pass
    return None
