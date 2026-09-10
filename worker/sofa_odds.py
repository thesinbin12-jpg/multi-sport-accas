"""
sofa_odds.py — Self-built soccer odds fetcher (SofaScore, keyless, zero quota).

Primary odds source for soccer. The Odds API (paid quota) is fallback only.
Emits events in The-Odds-API-compatible shape so scanner.extract_legs works unchanged.

Chain (all keyless JSON):
  unique-tournament/{tid}/seasons          -> current season id (seasons[0])
  unique-tournament/{tid}/season/{sid}/events/next/0 -> fixtures (teams + kickoff)
  sport/football/odds/1/{YYYY-MM-DD}       -> full-time 1X2 odds for the day (fractional)

Requires curl_cffi (Akamai WAF blocks plain requests).
"""

import time
from datetime import datetime, timezone, timedelta

try:
    from curl_cffi import requests as _cr
    _HAS_CURL = True
except ImportError:
    _HAS_CURL = False

SOFA_BASE = "https://api.sofascore.com/api/v1"

# sport_key -> SofaScore uniqueTournament id (resolved 2026-09-10)
SOFA_LEAGUES = {
    "soccer_epl": 17,
    "soccer_spain_la_liga": 8,
    "soccer_germany_bundesliga": 35,
    "soccer_italy_serie_a": 23,
    "soccer_france_ligue_one": 34,
    "soccer_uefa_champs_league": 7,
    "soccer_uefa_europa_league": 679,
    "soccer_uefa_europa_conference_league": 17015,
    "soccer_england_efl_cup": 21,
    "soccer_england_league1": 24,
    "soccer_england_league2": 25,
    "soccer_efl_champ": 18,
    "soccer_spl": 36,
    "soccer_netherlands_eredivisie": 37,
    "soccer_belgium_first_div": 38,
    "soccer_portugal_primeira_liga": 238,
    "soccer_switzerland_superleague": 215,
    "soccer_greece_super_league": 185,
    "soccer_denmark_superliga": 39,
    "soccer_norway_eliteserien": 20,
    "soccer_sweden_allsvenskan": 40,
    "soccer_finland_veikkausliiga": 41,
    "soccer_poland_ekstraklasa": 202,
    "soccer_turkey_super_league": 52,
    "soccer_austria_bundesliga": 45,
    "soccer_saudi_arabia_pro_league": 955,
    "soccer_usa_mls": 242,
    "soccer_japan_j_league": 196,
    "soccer_korea_kleague1": 410,
    "soccer_mexico_ligamx": 11621,
    "soccer_russia_premier_league": 203,
    "soccer_australia_aleague": 136,
}

BOOK_KEY = "sofascore"
BOOK_TITLE = "SofaScore"


def frac_to_dec(frac):
    """'6/5' -> 2.2, '2/1' -> 3.0. Returns None on garbage."""
    try:
        n, d = str(frac).strip().split("/")
        return round(float(n) / float(d) + 1.0, 3)
    except Exception:
        return None


class SofaOdds:
    """Keyless SofaScore soccer odds fetcher."""

    def __init__(self, log=None):
        self.log = log
        self.session = None
        if _HAS_CURL:
            self.session = _cr.Session(impersonate="chrome120")
        self._seasons = {}   # tid -> sid
        self._day_odds = {}  # date -> {event_id: {1,X,2} decimal}

    def _msg(self, m):
        if self.log:
            try:
                self.log(m)
            except Exception:
                pass

    def _get(self, path, timeout=20):
        if not self.session:
            return None
        try:
            r = self.session.get(f"{SOFA_BASE}/{path}", timeout=timeout)
            if r.status_code == 200:
                return r.json()
            return None
        except Exception:
            return None

    def season_id(self, tid):
        if tid in self._seasons:
            return self._seasons[tid]
        d = self._get(f"unique-tournament/{tid}/seasons")
        seasons = (d or {}).get("seasons", [])
        if not seasons:
            return None
        sid = seasons[0].get("id")
        self._seasons[tid] = sid
        return sid

    def league_events(self, tid):
        """Upcoming fixtures for a league: list of dicts."""
        sid = self.season_id(tid)
        if not sid:
            return []
        d = self._get(f"unique-tournament/{tid}/season/{sid}/events/next/0")
        events = (d or {}).get("events", [])
        if not events:
            # season rollover: try previous season entry once
            try:
                dd = self._get(f"unique-tournament/{tid}/seasons") or {}
                seasons = dd.get("seasons", [])
                if len(seasons) > 1:
                    sid2 = seasons[1].get("id")
                    d2 = self._get(f"unique-tournament/{tid}/season/{sid2}/events/next/0")
                    events = (d2 or {}).get("events", [])
                    if events:
                        self._seasons[tid] = sid2
            except Exception:
                pass
        return events or []

    def event_markets(self, eid):
        """Full per-match markets: {marketId: [market_obj]}. Cached per scan."""
        if not hasattr(self, "_em"):
            self._em = {}
        if eid in self._em:
            return self._em[eid]
        d = self._get(f"event/{eid}/odds/1/all")
        mkts = {}
        for m in ((d or {}).get("markets", []) or []):
            try:
                if m.get("suspended") or m.get("isLive"):
                    continue
                mkts.setdefault(m.get("marketId"), []).append(m)
            except Exception:
                continue
        self._em[eid] = mkts
        return mkts

    def _market_outcomes(self, m, wanted):
        """Extract {name: decimal} for wanted choice names. None if incomplete."""
        try:
            got = {}
            for c in m.get("choices", []):
                if c.get("name") in wanted:
                    dec = frac_to_dec(c.get("fractionalValue"))
                    if dec:
                        got[c["name"]] = dec
            return got if all(w in got for w in wanted) else None
        except Exception:
            return None

    def expand_event(self, base, eid, markets):
        """Extra shaped events for BTTS / O-U-2.5 / DC. Same shape as 1X2."""
        extra = []
        home = base["home_team"]
        away = base["away_team"]

        def _shape(suffix, market_label, outcomes, best):
            ev = dict(base)
            ev["id"] = f"{base['id']}-{suffix}"
            ev["market"] = market_label
            ev["bookmakers"] = [{"key": BOOK_KEY, "title": BOOK_TITLE,
                                   "markets": [{"key": "h2h", "outcomes": outcomes}]}]
            ev["best_odds"] = best["price"]
            ev["best_bookmaker"] = BOOK_TITLE
            return ev

        # BTTS (id 5)
        for m in markets.get(5, []):
            oc = self._market_outcomes(m, ("Yes", "No"))
            if oc:
                outs = [{"name": "BTTS: Yes", "price": oc["Yes"]},
                        {"name": "BTTS: No", "price": oc["No"]}]
                best = max(outs, key=lambda o: o["price"])
                extra.append(_shape("btts", "BTTS", outs, best))
                break
        # O/U 2.5 only (id 9, choiceGroup 2.5)
        for m in markets.get(9, []):
            if str(m.get("choiceGroup", "")) != "2.5":
                continue
            oc = self._market_outcomes(m, ("Over", "Under"))
            if oc:
                outs = [{"name": "Over 2.5", "price": oc["Over"]},
                        {"name": "Under 2.5", "price": oc["Under"]}]
                best = max(outs, key=lambda o: o["price"])
                extra.append(_shape("ou25", "O/U 2.5", outs, best))
                break
        # Double chance (id 2)
        for m in markets.get(2, []):
            oc = self._market_outcomes(m, ("1X", "X2", "12"))
            if oc:
                outs = [{"name": "DC: 1X", "price": oc["1X"]},
                        {"name": "DC: X2", "price": oc["X2"]},
                        {"name": "DC: 12", "price": oc["12"]}]
                best = max(outs, key=lambda o: o["price"])
                extra.append(_shape("dc", "Double chance", outs, best))
                break
        return extra

    def day_odds(self, datestr):
        """{event_id_str: {'1': dec, 'X': dec, '2': dec}} for one date."""
        if datestr in self._day_odds:
            return self._day_odds[datestr]
        d = self._get(f"sport/football/odds/1/{datestr}")
        out = {}
        for eid, m in ((d or {}).get("odds", {}) or {}).items():
            try:
                if m.get("suspended") or m.get("isLive"):
                    continue
                ch = {c.get("name"): c.get("fractionalValue") for c in m.get("choices", [])}
                dec = {k: frac_to_dec(v) for k, v in ch.items()}
                if dec.get("1") and dec.get("X") and dec.get("2"):
                    out[str(eid)] = dec
            except Exception:
                continue
        self._day_odds[datestr] = out
        return out

    def scan(self, sport_keys, hours_ahead=48, callback=None, markets="1X2"):
        """
        Returns {sport_key: [odds-api-shaped events]}.
        Zero quota cost. Skips leagues/events without odds.
        markets="1X2" (fast, 1 bulk call/day) or "full" (+BTTS/O-U-2.5/DC,
        1 extra call per match).
        """
        results = {}
        if not self.session:
            self._msg("SofaScore: curl_cffi missing, skipping.")
            return results
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours_ahead)
        # dates we need odds for
        ndays = max(1, int(hours_ahead // 24) + 2)
        dates = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(ndays)]
        odds_by_date = {}
        for ds in dates:
            odds_by_date[ds] = self.day_odds(ds)
            time.sleep(0.3)
        n_legs = 0
        for sk in sport_keys:
            tid = SOFA_LEAGUES.get(sk)
            if not tid:
                continue
            try:
                events = self.league_events(tid)
            except Exception:
                continue
            shaped = []
            for e in events:
                try:
                    ts = e.get("startTimestamp")
                    if not ts:
                        continue
                    dt = datetime.fromtimestamp(ts, timezone.utc)
                    if dt > cutoff or dt < now - timedelta(hours=3):
                        continue
                    eid = str(e.get("id"))
                    ds = dt.strftime("%Y-%m-%d")
                    dec = odds_by_date.get(ds, {}).get(eid)
                    if not dec:
                        continue
                    home = (e.get("homeTeam") or {}).get("name", "?")
                    away = (e.get("awayTeam") or {}).get("name", "?")
                    base = {
                        "id": f"sofa-{eid}",
                        "match_id": f"match-{eid}",
                        "market": "1X2",
                        "sport_key": sk,
                        "sport_title": (e.get("tournament") or {}).get("name", sk),
                        "league": (e.get("tournament") or {}).get("name", sk),
                        "home_team": home,
                        "away_team": away,
                        "commence_time": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "bookmakers": [{
                            "key": BOOK_KEY,
                            "title": BOOK_TITLE,
                            "markets": [{
                                "key": "h2h",
                                "outcomes": [
                                    {"name": home, "price": dec["1"]},
                                    {"name": "Draw", "price": dec["X"]},
                                    {"name": away, "price": dec["2"]},
                                ],
                            }],
                        }],
                    }
                    shaped.append(base)
                    if markets == "full":
                        try:
                            mkts = self.event_markets(eid)
                            if mkts:
                                shaped.extend(self.expand_event(base, eid, mkts))
                            time.sleep(0.2)
                        except Exception:
                            continue
                except Exception:
                    continue
            if shaped:
                results[sk] = shaped
                n_legs += len(shaped)
            time.sleep(0.3)
        self._msg(f"SofaScore: {len(results)} leagues, {n_legs} priced matches, 0 credits.")
        if callback:
            try:
                callback(f"SofaScore: {len(results)} leagues, {n_legs} priced matches (free).")
            except Exception:
                pass
        return results


def scan_sofa_soccer(sport_keys, hours_ahead=48, callback=None, log=None, markets="1X2"):
    """Convenience: scan and return odds-api-shaped results."""
    return SofaOdds(log=log).scan(sport_keys, hours_ahead=hours_ahead,
                                  callback=callback, markets=markets)


if __name__ == "__main__":
    s = SofaOdds(log=print)
    res = s.scan(["soccer_epl", "soccer_spain_la_liga"], hours_ahead=72)
    for sk, evs in res.items():
        print(f"{sk}: {len(evs)} matches")
        for e in evs[:2]:
            o = e["bookmakers"][0]["markets"][0]["outcomes"]
            print(f"  {e['home_team']} vs {e['away_team']} {e['commence_time']} -> "
                  f"{o[0]['price']} / {o[1]['price']} / {o[2]['price']}")
