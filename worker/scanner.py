"""
scanner.py — Scans odds from The Odds API v4 across multiple sports/leagues.
Returns live odds data for accumulator leg selection.
"""

import os, requests, json, time
from datetime import datetime, timezone

ODDS_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_BASE = "https://api.the-odds-api.com/v4"

# Sports to scan — comprehensive multi-sport coverage
SCAN_CONFIG = {
    # Soccer (primary — richest data)
    "soccer": [
        "soccer_epl",           # Premier League
        "soccer_spain_la_liga",  # La Liga
        "soccer_germany_bundesliga",  # Bundesliga
        "soccer_italy_serie_a",  # Serie A
        "soccer_france_ligue_one",  # Ligue 1
        "soccer_uefa_champs_league",  # Champions League
        "soccer_uefa_europa_league",  # Europa League
        "soccer_uefa_europa_conference_league",  # Conference League
        "soccer_england_efl_cup",  # EFL Cup
        "soccer_england_league1",  # League 1
        "soccer_england_league2",  # League 2
        "soccer_efl_champ",      # Championship
        "soccer_spl",            # Scottish Premiership
        "soccer_netherlands_eredivisie",  # Eredivisie
        "soccer_belgium_first_div",  # Belgium
        "soccer_portugal_primeira_liga",  # Portugal
        "soccer_switzerland_superleague",  # Switzerland
        "soccer_greece_super_league",  # Greece
        "soccer_denmark_superliga",  # Denmark
        "soccer_norway_eliteserien",  # Norway
        "soccer_sweden_allsvenskan",  # Sweden
        "soccer_finland_veikkausliiga",  # Finland
        "soccer_poland_ekstraklasa",  # Poland
        "soccer_turkey_super_league",  # Turkey
        "soccer_austria_bundesliga",  # Austria
        "soccer_saudi_arabia_pro_league",  # Saudi
        "soccer_usa_mls",        # MLS
        "soccer_japan_j_league",  # Japan
        "soccer_korea_kleague1",  # Korea
        "soccer_mexico_ligamx",  # Mexico
        "soccer_russia_premier_league",  # Russia
        "soccer_australia_aleague",  # Australia
    ],
    # Basketball
    "basketball": [
        "basketball_nba",
        "basketball_euroleague",
        "basketball_nba_championship_winner",
        "basketball_wnba",
    ],
    # Tennis
    "tennis": [
        "tennis_atp_us_open",
        "tennis_wta_us_open",
    ],
    # Hockey
    "hockey": [
        "icehockey_nhl",
        "icehockey_liiga",
        "icehockey_mestis",
        "icehockey_sweden_hockey_league",
        "icehockey_sweden_allsvenskan",
    ],
    # MMA / Boxing
    "mma": [
        "mma_mixed_martial_arts",
    ],
    "boxing": [
        "boxing_boxing",
    ],
    # Cricket
    "cricket": [
        "cricket_international_t20",
        "cricket_odi",
        "cricket_test_match",
        "cricket_caribbean_premier_league",
    ],
    # Others
    "rugby_league": ["rugbyleague_nrl", "rugbyleague_nrlw"],
    "american_football": ["americanfootball_nfl", "americanfootball_ncaaf"],
    "baseball": ["baseball_mlb", "baseball_kbo", "baseball_npb"],
    "golf": ["golf_masters_tournament_winner", "golf_pga_championship_winner",
              "golf_the_open_championship_winner", "golf_us_open_winner"],
}


class OddsScanner:
    """Scans The Odds API v4 for live odds across configured sports/leagues."""
    
    def __init__(self, api_key=None, rate_limit_log=None):
        self.api_key = api_key or ODDS_KEY
        self.base = ODDS_BASE
        self.session = requests.Session()
        self.credits_used = 0
        self.credits_remaining = None
        self.rate_limit_log = rate_limit_log
    
    def _get(self, path, params=None, timeout=15):
        """Make a GET request to the odds API."""
        params = params or {}
        params["apiKey"] = self.api_key
        url = f"{self.base}/{path}"
        try:
            r = self.session.get(url, params=params, timeout=timeout)
            # Log rate limit headers
            if r.status_code == 200:
                remaining = r.headers.get("x-requests-remaining")
                used = r.headers.get("x-requests-used")
                cost = r.headers.get("x-requests-last")
                if remaining is not None:
                    self.credits_remaining = int(remaining)
                if used is not None:
                    self.credits_used = int(used)
                if self.rate_limit_log:
                    self.rate_limit_log(f"Odds API: {remaining}/{used} credits, last cost={cost}")
            return r
        except requests.exceptions.Timeout:
            return None
        except Exception as e:
            if self.rate_limit_log:
                self.rate_limit_log(f"Odds API error: {e}")
            return None
    
    def get_sports(self, in_season_only=True):
        """Get list of active sports. Free — no quota cost."""
        params = {}
        if not in_season_only:
            params["all"] = "true"
        r = self._get("sports", params=params)
        if r and r.status_code == 200:
            return r.json()
        return []
    
    def get_events(self, sport_key, commence_time_from=None, commence_time_to=None):
        """Get upcoming events for a sport. Free — no quota cost."""
        params = {}
        if commence_time_from:
            params["commenceTimeFrom"] = commence_time_from
        if commence_time_to:
            params["commenceTimeTo"] = commence_time_to
        r = self._get(f"sports/{sport_key}/events", params=params)
        if r and r.status_code == 200:
            return r.json()
        return []
    
    def get_odds(self, sport_key, regions="us", markets="h2h", bookmakers=None, odds_format="decimal"):
        """
        Get odds for a sport. Costs 1 credit per region per market.
        Returns list of events with odds from multiple bookmakers.
        """
        params = {
            "regions": regions,
            "markets": markets,
            "oddsFormat": odds_format,
        }
        if bookmakers:
            params["bookmakers"] = bookmakers
        
        r = self._get(f"sports/{sport_key}/odds", params=params)
        if r and r.status_code == 200:
            return r.json()
        elif r is None:
            return []
        else:
            # Log error
            if self.rate_limit_log:
                self.rate_limit_log(f"Odds fetch failed for {sport_key}: HTTP {r.status_code}")
            return []
    
    def scan_all(self, sports_filter=None, max_credits=500, callback=None):
        """
        Scan all configured sports for odds.
        Yields (sport_key, league_name, odds_data) for each successful scan.
        Stops when approaching rate limit.
        
        sports_filter: optional dict of {group: [sport_keys]} to scan only specific sports.
        callback: optional function called with progress updates.
        """
        if callback:
            callback("Starting odds scan...")
        
        # Determine which sports to scan
        if sports_filter:
            sport_groups = sports_filter
        else:
            sport_groups = SCAN_CONFIG
        
        total_credits = 0
        results = {}
        
        for group, sport_keys in sport_groups.items():
            if callback:
                callback(f"Scanning {group}...")
            
            for sport_key in sport_keys:
                # Check rate limit before each call
                if self.credits_remaining is not None and total_credits >= max_credits - 10:
                    if callback:
                        callback(f"Approaching rate limit ({total_credits}/{max_credits}), stopping.")
                    break
                
                # Get odds — costs 1 credit (1 region × 1 market)
                odds_data = self.get_odds(sport_key, regions="us", markets="h2h")
                
                if odds_data and isinstance(odds_data, list) and len(odds_data) > 0:
                    results[sport_key] = odds_data
                    total_credits += 1
                    
                    if callback:
                        callback(f"  {sport_key}: {len(odds_data)} matches "
                               f"(credits: {total_credits}/{max_credits})")
                elif odds_data == [] and self.credits_remaining is not None:
                    # Empty response still costs 1 credit per call (confirmed in docs)
                    total_credits += 1
                
                time.sleep(0.3)  # Small delay between calls
        
        if callback:
            callback(f"Scan complete: {len(results)} leagues with odds, "
                    f"{total_credits} credits used, "
                    f"{self.credits_remaining} remaining.")
        
        return results
    
    def get_scores(self, sport_key, days_from=3):
        """Get scores for live + recently completed games. Costs 2 credits if daysFrom set."""
        params = {"daysFrom": days_from} if days_from else {}
        r = self._get(f"sports/{sport_key}/scores", params=params)
        if r and r.status_code == 200:
            return r.json()
        return []
    
    def extract_legs(self, odds_results, min_odds=1.5, max_odds=25.0):
        """
        Extract accumulator leg candidates from odds results.
        Filters for matches with actual odds data.
        
        Returns list of leg dicts with:
        - id, sport_key, home_team, away_team, league, commence_time
        - best_odds (decimal), best_bookmaker
        - outcomes: list of {name, price, bookmaker}
        """
        legs = []
        
        for sport_key, events in odds_results.items():
            for event in events:
                # Skip events without odds
                if not event.get("bookmakers"):
                    continue
                
                home = event.get("home_team", "?")
                away = event.get("away_team", "?")
                commence = event.get("commence_time", "")
                
                # Parse league name from sport_key (or keep source label)
                league = event.get("league") or sport_key.replace("_", " ").title()
                
                # Collect all outcomes across bookmakers for h2h
                all_outcomes = {}
                best_price = None
                best_bookmaker = None
                
                for bm in event.get("bookmakers", []):
                    bm_name = bm.get("title", bm.get("key", "?"))
                    for market in bm.get("markets", []):
                        if market.get("key") == "h2h":
                            for outcome in market.get("outcomes", []):
                                name = outcome.get("name", "?")
                                price = outcome.get("price")
                                if price is not None:
                                    if name not in all_outcomes:
                                        all_outcomes[name] = []
                                    all_outcomes[name].append({
                                        "price": price,
                                        "bookmaker": bm_name,
                                    })
                                    if best_price is None or price < best_price:
                                        # Lower price = higher probability for the team
                                        # But we want the best price for betting
                                        pass
                
                # Find the best odds (highest decimal price) for each outcome
                for name in all_outcomes:
                    prices = [o["price"] for o in all_outcomes[name]]
                    max_price = max(prices)
                    best_bm = [o["bookmaker"] for o in all_outcomes[name] if o["price"] == max_price][0]
                    
                    if best_price is None or max_price > best_price:
                        best_price = max_price
                        best_bookmaker = best_bm
                
                # Filter by odds range
                if best_price and min_odds <= best_price <= max_odds:
                    legs.append({
                        "id": event.get("id"),
                        "match_id": event.get("match_id"),
                        "market": event.get("market", "1X2"),
                        "sport_key": sport_key,
                        "sport": event.get("sport_title", sport_key),
                        "league": league,
                        "home_team": home,
                        "away_team": away,
                        "commence_time": commence,
                        "best_odds": best_price,
                        "best_bookmaker": best_bookmaker,
                        "outcomes": [
                            {
                                "name": name,
                                "price": max(o["price"] for o in all_outcomes[name]),
                                "bookmaker": [o["bookmaker"] for o in all_outcomes[name] 
                                            if o["price"] == max(o["price"] for o in all_outcomes[name])][0],
                                "prices": [o["price"] for o in all_outcomes[name]],
                            }
                            for name in all_outcomes
                        ],
                        "bookmakers_available": [bm.get("title", bm.get("key")) 
                                                for bm in event.get("bookmakers", [])],
                    })
        
        # Sort by odds descending (higher odds = bigger potential payout)
        legs.sort(key=lambda x: x.get("best_odds", 0), reverse=True)
        
        return legs


# Singleton for convenience
scanner = None

def get_scanner():
    global scanner
    if scanner is None:
        scanner = OddsScanner()
    return scanner


if __name__ == "__main__":
    # Quick test
    s = OddsScanner()
    sports = s.get_sports()
    print(f"Active sports: {len(sports)}")
    for sp in sports[:10]:
        print(f"  {sp['key']:35s} {sp.get('title', '?')}")
    print(f"  ... and {len(sports)-10} more")
    
    # Test odds fetch
    if sports:
        test_sport = sports[0]["key"]
        print(f"\nFetching odds for {test_sport}...")
        odds = s.get_odds(test_sport)
        if odds:
            print(f"  Got {len(odds)} events with odds")
            for ev in odds[:2]:
                print(f"    {ev.get('home_team','?')} vs {ev.get('away_team','?')} "
                      f"({ev.get('sport_title','?')}) — odds from {len(ev.get('bookmakers',[]))} bookmakers")
        else:
            print(f"  No odds available")
