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
