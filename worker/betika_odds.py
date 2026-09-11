"""
betika_odds.py — Free primary odds source (Betika bookmaker API, keyless, zero quota).

Public REST, no auth: https://api.betika.com/v1/matches
Supports ?sport_id=14 (soccer) + ?sub_type_id= (1=1X2, 10=Double Chance, 29=BTTS),
paginated (?page=N, 100/page). Real bookmaker prices, stakeable by the user.

Times are EAT (UTC+3, Betika Kenya). Simulated/SRL + esports excluded.
Emits the same odds-api-shaped events as smarkets_odds (ids btk-*, market labels).
"""

import time
from datetime import datetime, timezone, timedelta

import requests

BTK_BASE = "https://api.betika.com/v1/matches"
BTK_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
EAT = timezone(timedelta(hours=3))

BOOK_KEY = "betika"
BOOK_TITLE = "Betika"

SUB_1X2 = "1"
SUB_DC = "10"
SUB_BTTS = "29"


def _f(x):
    try:
        v = float(x)
        return v if v > 1.0 else None
    except Exception:
        return None


class BetikaOdds:
    def __init__(self, log=None):
        self.log = log
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": BTK_UA, "Accept": "application/json"})

    def _msg(self, m):
        if self.log:
            try:
                self.log(m)
            except Exception:
                pass

    def _page(self, sub_type, page, timeout=25):
        try:
            r = self.session.get(BTK_BASE, params={"sport_id": 14, "sub_type_id": sub_type,
                                                  "page": page, "limit": 100}, timeout=timeout)
            if r.status_code != 200:
                return [], 0
            d = r.json()
            meta = d.get("meta") or {}
            try:
                total = int(meta.get("total") or 0)
            except Exception:
                total = 0
            return d.get("data") or [], total
        except Exception:
            return [], 0

    @staticmethod
    def _real(m):
        if str(m.get("is_srl") or "") in ("1", "True", "true"):
            return False
        if str(m.get("is_esport") or "") in ("1", "True", "true"):
            return False
        comp = f"{m.get('competition_name', '')} {m.get('category', '')}".upper()
        if "SRL" in comp or "SIMULATED" in comp or "ESPORT" in comp or "ZOOM" in comp:
            return False
        return True

    @staticmethod
    def _outcomes(m):
        """(1X2 outcomes, DC outcomes, BTTS outcomes) from the odds list."""
        o12, odc, obtts = [], [], []
        for o in (m.get("odds") or []):
            for leg in (o.get("odds") or []):
                key = str(leg.get("odd_key", ""))
                disp = str(leg.get("display", ""))
                val = _f(leg.get("odd_value"))
                if val is None:
                    continue
                if disp in ("1", "X", "2"):
                    name = {"1": m.get("home_team", "?"), "X": "Draw",
                            "2": m.get("away_team", "?")}[disp]
                    o12.append({"name": name, "price": val})
                elif disp in ("1X", "12", "X2", "1/X", "1/2", "X/2"):
                    odc.append({"name": {"1/X": "1X", "X/2": "X2", "1/2": "12"}.get(disp, disp),
                                "price": val})
                elif disp.lower() in ("yes", "no") or key.lower() in ("yes", "no", "gg", "ng"):
                    yn = "Yes" if disp.lower() == "yes" or key.lower() in ("yes", "gg") else "No"
                    obtts.append({"name": f"BTTS: {yn}", "price": val})
        return o12, odc, obtts

    def scan(self, hours_ahead=48, markets=("1X2", "DC", "BTTS"), callback=None):
        """Returns [shaped events]. Zero quota cost."""
        out, seen = [], set()
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours_ahead)
        subs = []
        if "1X2" in markets:
            subs.append((SUB_1X2, "1x2", "1X2"))
        if "DC" in markets:
            subs.append((SUB_DC, "dc", "Double chance"))
        if "BTTS" in markets:
            subs.append((SUB_BTTS, "btts", "BTTS"))
        n = 0
        for sub, suffix, label in subs:
            page, pages = 1, 1
            while page <= pages and page <= 10:
                ms, total = self._page(sub, page)
                if page == 1 and total:
                    pages = min(10, (total + 99) // 100)
                if not ms:
                    break
                for m in ms:
                    try:
                        if not self._real(m):
                            continue
                        dt = datetime.strptime(str(m.get("start_time", "")),
                                               "%Y-%m-%d %H:%M:%S").replace(tzinfo=EAT)
                        if dt > cutoff or dt < now - timedelta(hours=3):
                            continue
                        o12, odc, obtts = self._outcomes(m)
                        outcomes = {"1x2": o12, "dc": odc, "btts": obtts}[suffix]
                        if len(outcomes) < 2:
                            continue
                        mid = str(m.get("match_id", ""))
                        key = (mid, suffix)
                        if key in seen:
                            continue
                        seen.add(key)
                        best = max(outcomes, key=lambda o: o["price"])
                        league = f"{m.get('category', '')} {m.get('competition_name', '')}".strip()
                        out.append({
                            "id": f"btk-{mid}-{suffix}",
                            "market": label,
                            "sport_key": "soccer_betika",
                            "sport_title": "Soccer",
                            "league": league or "Soccer",
                            "home_team": m.get("home_team", "?"),
                            "away_team": m.get("away_team", "?"),
                            "commence_time": dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "best_odds": best["price"],
                            "best_bookmaker": BOOK_TITLE,
                            "bookmakers": [{"key": BOOK_KEY, "title": BOOK_TITLE,
                                            "markets": [{"key": "h2h", "outcomes": outcomes}]}],
                        })
                        n += 1
                    except Exception:
                        continue
                page += 1
                time.sleep(0.3)
        self._msg(f"Betika: {n} market legs, 0 credits.")
        if callback:
            try:
                callback(f"Betika: {n} market legs (free).")
            except Exception:
                pass
        return out


def scan_betika(hours_ahead=48, markets=("1X2", "DC", "BTTS"), callback=None, log=None):
    return BetikaOdds(log=log).scan(hours_ahead=hours_ahead, markets=markets, callback=callback)


if __name__ == "__main__":
    legs = scan_betika(hours_ahead=48, log=print)
    from collections import Counter
    print("markets:", dict(Counter(l["market"] for l in legs)))
    for l in legs[:8]:
        o = l["bookmakers"][0]["markets"][0]["outcomes"]
        prices = " / ".join(f"{x['name']}:{x['price']}" for x in o)
        print(f"[{l['market']}] {l['league'][:22]:22s} {l['home_team'][:16]:16s} vs {l['away_team'][:16]:16s} -> {prices}")
