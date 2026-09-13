"""
fotmob.py — Public FotMob JSON API wrapper for soccer stats, standings, and form.
No API key required — uses FotMob's undocumented public JSON endpoints.
"""

import os, requests, json, time

FOTMOB_BASE = "https://www.fotmob.com/api"

# League ID mappings for FotMob
LEAGUE_IDS = {
    "soccer_epl": 47,
    "soccer_spain_la_liga": 87,
    "soccer_germany_bundesliga": 54,
    "soccer_italy_serie_a": 55,
    "soccer_france_ligue_one": 53,
    "soccer_uefa_champs_league": 42,
    "soccer_uefa_europa_league": 73,
    "soccer_uefa_europa_conference_league": 140,
    "soccer_efl_champ": 49,
    "soccer_netherlands_eredivisie": 57,
    "soccer_belgium_first_div": 60,
    "soccer_portugal_primeira_liga": 61,
    "soccer_spl": 50,
    "soccer_turkey_super_league": 62,
    "soccer_denmark_superliga": 63,
    "soccer_sweden_allsvenskan": 64,
    "soccer_norway_eliteserien": 65,
    "soccer_switzerland_superleague": 66,
    "soccer_greece_super_league": 67,
    "soccer_finland_veikkausliiga": 68,
    "soccer_poland_ekstraklasa": 69,
    "soccer_austria_bundesliga": 70,
    "soccer_russia_premier_league": 71,
    "soccer_usa_mls": 72,
    "soccer_japan_j_league": 74,
    "soccer_korea_kleague1": 75,
    "soccer_mexico_ligamx": 76,
    "soccer_australia_aleague": 77,
    "soccer_saudi_arabia_pro_league": 78,
}

class FotMob:
    """Wrapper for FotMob public API."""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36",
            "Accept": "application/json",
        })
    
    def get_league_standings(self, league_id):
        """Get current standings for a league."""
        url = f"{FOTMOB_BASE}/leagues?id={league_id}"
        try:
            r = self.session.get(url, timeout=15)
            if r.status_code == 200:
                return r.json()
            return None
        except Exception:
            return None
    
    def get_team_form(self, team_id):
        """Get recent form for a team."""
        url = f"{FOTMOB_BASE}/teams?id={team_id}"
        try:
            r = self.session.get(url, timeout=15)
            if r.status_code == 200:
                return r.json()
            return None
        except Exception:
            return None
    
    def get_match_details(self, match_id):
        """Get detailed match data including H2H."""
        url = f"{FOTMOB_BASE}/matchDetails?matchId={match_id}"
        try:
            r = self.session.get(url, timeout=15)
            if r.status_code == 200:
                return r.json()
            return None
        except Exception:
            return None
    
    def get_league_matches(self, league_id):
        """Get upcoming matches for a league."""
        url = f"{FOTMOB_BASE}/leagues?season=&id={league_id}"
        try:
            r = self.session.get(url, timeout=15)
            if r.status_code == 200:
                return r.json()
            return None
        except Exception:
            return None
    
    def extract_form_summary(self, league_id):
        """
        Extract team form summary from league standings.
        Returns dict with team stats.
        """
        data = self.get_league_standings(league_id)
        if not data:
            return {}
        
        try:
            standings = data.get("table", {}).get("data", {})
            tables = standings.get("table", {})
            total_table = tables.get("total", [])
            
            form_summary = {}
            for team_entry in total_table:
                team_name = team_entry.get("name", "?")
                form_summary[team_name] = {
                    "position": team_entry.get("idx", "?"),
                    "played": team_entry.get("played", 0),
                    "wins": team_entry.get("wins", 0),
                    "draws": team_entry.get("draws", 0),
                    "losses": team_entry.get("losses", 0),
                    "goals_for": team_entry.get("scoresFor", 0),
                    "goals_against": team_entry.get("scoresAgainst", 0),
                    "goal_difference": team_entry.get("goalConDiff", 0),
                    "points": team_entry.get("points", 0),
                    "form": team_entry.get("recentForm", ""),
                }
            
            return form_summary
        except Exception as e:
            return {"error": str(e)}


# Singleton
fotmob = FotMob()

def get_fotmob():
    return fotmob

def league_odds_to_fotmob(sport_key):
    """Convert Odds API sport_key to FotMob league ID."""
    return LEAGUE_IDS.get(sport_key)


def team_form(home, away, days_back=30, limit=5):
    """Recent form for two teams from finished FotMob results (~185 leagues).
    The standings endpoint is dead; results don't lie. Returns
    {query_name: {w,d,l,gp,gf,ga,form}} with form oldest->latest (W/D/L).
    Days with no matches skipped. Cached day-pools make this cheap.
    Never raises."""
    out = {}
    try:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        try:
            from teams import tags as _tseg, normalize as _tnorm
        except ImportError:
            from worker.teams import tags as _tseg, normalize as _tnorm  # type: ignore
        base = _dt.now(_tz.utc).date()
        pls = [(q, _tnorm(q)) for q in (home, away)]
        recs = {nq: [] for _, nq in pls}
        for back in range(0, max(1, int(days_back or 0))):
            try:
                pool = _fm_day(base - _td(days=back)) or {}
            except Exception:
                continue
            if not pool:
                continue
            for orig, q in pls:
                if not q:
                    continue
                try:
                    for (h, a), score in pool.items():
                        try:
                            hs, aws = score
                        except Exception:
                            continue
                        try:
                            from teams import score as _tss
                        except ImportError:
                            from worker.teams import score as _tss  # type: ignore
                        try:
                            sh, sa = _tss(q, h), _tss(q, a)
                        except Exception:
                            continue
                        if max(sh, sa) < 0.5:
                            continue
                        # same-day men's/women's duplicates: prefer our segment
                        try:
                            _mine_seg = _tseg(orig)
                            _sides = [s for s, _sc in ((h, sh), (a, sa)) if _sc >= 0.5]
                            _segged = [s for s in _sides if _tseg(s) == _mine_seg]
                            if _segged and len(_sides) > len(_segged):
                                if h in _segged and a in _segged:
                                    pass
                                elif h in _segged:
                                    sa = 0.0
                                elif a in _segged:
                                    sh = 0.0
                                else:
                                    continue
                        except Exception:
                            pass
                        try:
                            if sh >= sa and sh >= 0.5:
                                recs[q].append((int(hs), int(aws)))
                            elif sa >= 0.5:
                                recs[q].append((int(aws), int(hs)))
                        except Exception:
                            continue
                except Exception:
                    continue
        for orig, q in pls:
            try:
                rs = list(reversed(recs[q][:limit]))
                if not rs:
                    continue
                out[str(orig)] = {
                    "w": sum(1 for gf, ga in rs if gf > ga),
                    "d": sum(1 for gf, ga in rs if gf == ga),
                    "l": sum(1 for gf, ga in rs if gf < ga),
                    "gp": len(rs),
                    "gf": sum(gf for gf, _ in rs),
                    "ga": sum(ga for _, ga in rs),
                    "form": "".join("W" if gf > ga else ("D" if gf == ga else "L") for gf, ga in rs),
                }
            except Exception:
                continue
    except Exception:
        pass
    return out


if __name__ == "__main__":
    f = FotMob()
    print("Testing FotMob...")
    
    # Test EPL standings
    epl_id = LEAGUE_IDS.get("soccer_epl")
    print(f"EPL ID: {epl_id}")
    
    data = f.get_league_standings(epl_id)
    if data:
        print(f"EPL data keys: {list(data.keys())[:5]}")
        standings = data.get("table", {}).get("data", {})
        tables = standings.get("table", {})
        total_table = tables.get("total", [])
        print(f"Teams in table: {len(total_table)}")
        if total_table:
            top3 = total_table[:3]
            for t in top3:
                print(f"  {t.get('idx')}. {t.get('name')} — {t.get('points')} pts")
    else:
        print("No EPL data")


# ---- live scores (revived 2026-09-12): /api/data/matches?date=YYYYMMDD ----
import requests as _rq2
from datetime import datetime as _dt, timezone as _tz, timedelta as _td

_FM_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
          "Chrome/120.0 Mobile Safari/537.36")
_FM_DAY_CACHE: dict = {}


def _fm_day(day):
    """{(hnorm, anorm): (hs, aws)} finished matches for a date. Cached per process."""
    key = day.isoformat()
    if key in _FM_DAY_CACHE:
        return _FM_DAY_CACHE[key]
    out = {}
    try:
        try:
            from curl_cffi import requests as _cr
            _sess = _cr.Session(impersonate="chrome120")
            r = _sess.get("https://www.fotmob.com/api/data/matches?date=" + day.strftime("%Y%m%d"),
                           headers={"User-Agent": _FM_UA, "Accept": "application/json"}, timeout=20)
        except ImportError:
            r = _rq2.get("https://www.fotmob.com/api/data/matches?date=" + day.strftime("%Y%m%d"),
                         headers={"User-Agent": _FM_UA, "Accept": "application/json"}, timeout=20)
        if r.status_code == 200:
            for lg in (r.json().get("leagues") or []):
                for m in (lg.get("matches") or []):
                    try:
                        st = (m.get("status") or {})
                        if not st.get("finished"):
                            continue
                        h = ((m.get("home") or {}).get("longName")
                             or (m.get("home") or {}).get("name", ""))
                        a = ((m.get("away") or {}).get("longName")
                             or (m.get("away") or {}).get("name", ""))
                        hs, aws = (m.get("home") or {}).get("score"), (m.get("away") or {}).get("score")
                        if h and a and hs is not None and aws is not None:
                            out[(str(h).strip().lower(), str(a).strip().lower())] = (int(hs), int(aws))
                    except Exception:
                        continue
    except Exception:
        pass
    if out:
        _FM_DAY_CACHE[key] = out
    try:
        import logging as _lg
        _lg.getLogger("acca").info("FotMob %s: %d finished", key, len(out))
    except Exception:
        pass
    return out


def find_finished_score(home, away, ref_date=None, span=2, match_fn=None):
    """(hs, aws) or None. Searches ref_date ± span days. Keyless, ~185 leagues.
    match_fn(h1, a1, h2, a2) fuzzy-matches names; default handles exact/contains."""
    def _default(h1, a1, h2, a2):
        import re as _re2
        h1 = _re2.sub(r"\s*\([^)]*\)", "", h1).strip()
        a1 = _re2.sub(r"\s*\([^)]*\)", "", a1).strip()
        return (h1 == h2 and a1 == a2) or (h1 in h2 and a1 in a2) or (h2 in h1 and a2 in a1)
    mf = match_fn or _default
    try:
        base = ref_date or _dt.now(_tz.utc).date()
        if isinstance(base, str):
            base = _dt.fromisoformat(base[:10]).date()
        hn = str(home or "").strip().lower()
        an = str(away or "").strip().lower()
        for d in range(-span, 1):
            day = base + _td(days=d)
            for (h, a), score in _fm_day(day).items():
                try:
                    if mf(h, a, hn, an):
                        return score
                except Exception:
                    continue
    except Exception:
        pass
    return None
