"""
smarkets_odds.py — Second free odds source (Smarkets exchange, keyless, zero quota).

Public REST API, no auth for reads. Exchange prices (sharp, no margin).
Discovery via popular football event ids; per-match markets + quotes.

Price format: percent x 100 (4717 = 47.17%). Back at best offer:
  decimal = 10000 / best_offer_price
Emits odds-api-shaped events (ids smk-*, market labels, same contract as betika_odds).
"""

import time
from datetime import datetime, timezone, timedelta

try:
    from curl_cffi import requests as _cr
    _HAS_CURL = True
except ImportError:
    _HAS_CURL = False

SMK_BASE = "https://api.smarkets.com/v3"

BOOK_KEY = "smarkets"
BOOK_TITLE = "Smarkets"

# market slugs we care about
M_FT = "winner"               # Full-time result (1X2)
M_BTTS = "both-teams-score"   # Both teams to score
M_OU25 = "over-under-2.5"     # Match goals O/U 2.5


def pct_to_dec(pct):
    """4717 -> 2.12. Returns None on garbage."""
    try:
        p = float(pct)
        if p <= 0 or p >= 10000:
            return None
        return round(10000.0 / p, 3)
    except Exception:
        return None


class SmarketsOdds:
    def __init__(self, log=None):
        self.log = log
        self.session = None
        if _HAS_CURL:
            self.session = _cr.Session(impersonate="chrome120")

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
            r = self.session.get(f"{SMK_BASE}/{path}", timeout=timeout)
            if r.status_code == 200:
                return r.json()
            return None
        except Exception:
            return None

    def popular_football_ids(self, limit=100):
        d = self._get(f"popular/event_ids/?sport=football&limit={limit}")
        return (d or {}).get("popular_event_ids", []) or []

    def event(self, eid):
        d = self._get(f"events/{eid}/")
        evs = (d or {}).get("events", [])
        return evs[0] if evs else None

    def event_markets(self, eid):
        d = self._get(f"events/{eid}/markets/?limit=200&sort=display-order")
        return (d or {}).get("markets", []) or []

    def quotes(self, mid):
        d = self._get(f"markets/{mid}/quotes/")
        return d or {}

    def _best_offer(self, quotes, cid):
        """Best (lowest) offer price for a contract -> decimal."""
        try:
            offers = (quotes.get(str(cid), {}) or {}).get("offers", []) or []
            # ignore stub 9999 offers with dust quantity
            real = [o for o in offers if o.get("price") not in (None, 9999)]
            if not real:
                return None
            return pct_to_dec(min(o["price"] for o in real))
        except Exception:
            return None

    def match_markets(self, eid, markets):
        """Fetch 1X2 + BTTS + O/U 2.5 shaped events for one match event."""
        ev = self.event(eid)
        if not ev:
            return []
        slug = str(ev.get("full_slug", ""))
        if "/sport/football/" not in slug:
            return []
        name = ev.get("name", "")
        if " vs " not in name:
            return []
        home, away = [p.strip() for p in name.split(" vs ", 1)]
        ts = ev.get("start_datetime", "")
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            return []
        base = {
            "sport_key": "soccer_smarkets",
            "sport_title": "Soccer",
            "league": self._league_from_slug(slug),
            "home_team": home,
            "away_team": away,
            "commence_time": dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "match_id": None,  # filled by merger via fuzzy key
        }
        mkts = self.event_markets(eid)
        by_slug = {}
        for m in mkts:
            by_slug.setdefault(m.get("slug"), m)
        out = []

        def _shape(suffix, label, outcomes):
            if not outcomes or any(o.get("price") is None for o in outcomes):
                return None
            best = max(outcomes, key=lambda o: o["price"])
            return {
                "id": f"smk-{eid}-{suffix}",
                "market": label,
                "sport_key": base["sport_key"],
                "sport_title": base["sport_title"],
                "league": base["league"],
                "home_team": home,
                "away_team": away,
                "commence_time": base["commence_time"],
                "best_odds": best["price"],
                "best_bookmaker": BOOK_TITLE,
                "bookmakers": [{"key": BOOK_KEY, "title": BOOK_TITLE,
                                "markets": [{"key": "h2h", "outcomes": outcomes}]}],
            }

        if "1X2" in markets and M_FT in by_slug:
            m = by_slug[M_FT]
            cons = {c["name"]: c["id"] for c in
                    (self._get(f"markets/{m['id']}/contracts/") or {}).get("contracts", [])}
            q = self.quotes(m["id"])
            ph = self._best_offer(q, cons.get(home))
            px = self._best_offer(q, cons.get("Draw"))
            pa = self._best_offer(q, cons.get(away))
            if ph and px and pa:
                out.append(_shape("1x2", "1X2", [
                    {"name": home, "price": ph},
                    {"name": "Draw", "price": px},
                    {"name": away, "price": pa}]))
        if "BTTS" in markets and M_BTTS in by_slug:
            m = by_slug[M_BTTS]
            cons = {c["name"]: c["id"] for c in
                    (self._get(f"markets/{m['id']}/contracts/") or {}).get("contracts", [])}
            q = self.quotes(m["id"])
            py = self._best_offer(q, cons.get("Yes"))
            pn = self._best_offer(q, cons.get("No"))
            if py and pn:
                out.append(_shape("btts", "BTTS", [
                    {"name": "BTTS: Yes", "price": py},
                    {"name": "BTTS: No", "price": pn}]))
        if "OU25" in markets and M_OU25 in by_slug:
            m = by_slug[M_OU25]
            cons = {c["name"]: c["id"] for c in
                    (self._get(f"markets/{m['id']}/contracts/") or {}).get("contracts", [])}
            over_id = next((i for n, i in cons.items() if "over" in n.lower()), None)
            under_id = next((i for n, i in cons.items() if "under" in n.lower()), None)
            q = self.quotes(m["id"])
            po = self._best_offer(q, over_id)
            pu = self._best_offer(q, under_id)
            if po and pu:
                out.append(_shape("ou25", "O/U 2.5", [
                    {"name": "Over 2.5", "price": po},
                    {"name": "Under 2.5", "price": pu}]))
        return out

    @staticmethod
    def _league_from_slug(slug):
        try:
            parts = slug.split("/sport/football/")
            seg = parts[1].split("/")[0] if len(parts) > 1 else ""
            return seg.replace("-", " ").title() or "Soccer"
        except Exception:
            return "Soccer"

    def scan(self, hours_ahead=48, markets=("1X2", "BTTS", "OU25"), callback=None):
        """Returns [shaped events]. Zero quota cost."""
        out = []
        if not self.session:
            self._msg("Smarkets: curl_cffi missing, skipping.")
            return out
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours_ahead)
        ids = self.popular_football_ids()
        n = 0
        for eid in ids:
            try:
                ev = self.event(eid)
                if not ev:
                    continue
                slug = str(ev.get("full_slug", ""))
                if "/sport/football/" not in slug or " vs " not in str(ev.get("name", "")):
                    continue
                try:
                    dt = datetime.fromisoformat(str(ev.get("start_datetime", "")).replace("Z", "+00:00"))
                except Exception:
                    continue
                if dt > cutoff or dt < now - timedelta(hours=3):
                    continue
                legs = self.match_markets(eid, markets)
                out.extend(legs)
                n += 1
                time.sleep(0.3)
            except Exception:
                continue
        self._msg(f"Smarkets: {n} matches, {len(out)} market legs, 0 credits.")
        if callback:
            try:
                callback(f"Smarkets: {n} matches, {len(out)} market legs (free).")
            except Exception:
                pass
        return out


def scan_smarkets(hours_ahead=48, markets=("1X2", "BTTS", "OU25"), callback=None, log=None):
    return SmarketsOdds(log=log).scan(hours_ahead=hours_ahead, markets=markets, callback=callback)


if __name__ == "__main__":
    s = SmarketsOdds(log=print)
    legs = s.scan(hours_ahead=72)
    for l in legs[:8]:
        o = l["bookmakers"][0]["markets"][0]["outcomes"]
        prices = " / ".join(f"{x['name']}:{x['price']}" for x in o)
        print(f"[{l['market']}] {l['league'][:22]:22s} {l['home_team'][:16]:16s} vs {l['away_team'][:16]:16s} -> {prices}")
