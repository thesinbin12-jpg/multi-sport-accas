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
SUB_OU = "18"
SUB_C12B = "35"
SUB_COUB = "36"
SUB_C12OU = "37"
SUB_DNB = "11"  # draw no bet (lowest-margin 1X2 substitute; draw = void)
SUB_HTOT = "19"  # home team total (over/under lines)
SUB_ATOT = "20"  # away team total (over/under lines)


def _combo_token(tok):
    t = str(tok or "").strip().upper()
    if t in ("1", "X", "2", "YES", "NO"):
        return t
    m = __import__("re").match(r"(OVER|UNDER)\s+(\d+(?:\.5)?)", t)
    if m:
        return ("O" if m.group(1) == "OVER" else "U") + m.group(2)
    return ""


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
        """{suffix: outcomes} for 1X2, DC, BTTS, O/U lines, combos. Names are verifier-ready."""
        groups: dict = {}
        for o in (m.get("odds") or []):
            sub = str(o.get("sub_type_id", ""))
            for leg in (o.get("odds") or []):
                disp = str(leg.get("display", ""))
                val = _f(leg.get("odd_value"))
                if val is None:
                    continue
                if sub == SUB_1X2 and disp in ("1", "X", "2"):
                    name = {"1": m.get("home_team", "?"), "X": "Draw",
                            "2": m.get("away_team", "?")}[disp]
                    groups.setdefault("1x2", []).append({"name": name, "price": val})
                elif sub == SUB_DC and disp in ("1X", "12", "X2", "1/X", "1/2", "X/2"):
                    groups.setdefault("dc", []).append(
                        {"name": {"1/X": "1X", "X/2": "X2", "1/2": "12"}.get(disp, disp),
                         "price": val})
                elif sub == SUB_BTTS and disp.lower() in ("yes", "no"):
                    yn = "Yes" if disp.lower() == "yes" else "No"
                    groups.setdefault("btts", []).append({"name": f"BTTS: {yn}", "price": val})
                elif sub == SUB_DNB and disp in ("1", "2"):
                    groups.setdefault("dnb", []).append({"name": "DNB:" + disp, "price": val})
                elif sub in (SUB_HTOT, SUB_ATOT):
                    import re as _re2
                    mt2 = _re2.match(r"(OVER|UNDER)\s+(\d+(?:\.\d+)?)", disp.upper())
                    if mt2:
                        _key = "hometotal" if sub == SUB_HTOT else "awaytotal"
                        groups.setdefault(_key, []).append(
                            {"name": f"{'Over' if mt2.group(1) == 'OVER' else 'Under'} {mt2.group(2)}",
                             "price": val})
                elif sub == SUB_OU:
                    import re as _re
                    mt = _re.match(r"(OVER|UNDER)\s+(1\.5|2\.5|3\.5)", disp.upper())
                    if mt:
                        groups.setdefault("ou" + mt.group(2), []).append(
                            {"name": f"{'Over' if mt.group(1) == 'OVER' else 'Under'} {mt.group(2)}",
                             "price": val})
                elif sub in (SUB_C12B, SUB_COUB, SUB_C12OU) and "&" in disp.upper():
                    toks = [_combo_token(t) for t in disp.upper().split("&")]
                    if all(toks) and len(toks) == 2:
                        label = {SUB_C12B: "1X2+BTTS", SUB_COUB: "O/U+BTTS",
                                 SUB_C12OU: "1X2+O/U"}[sub]
                        groups.setdefault(label, []).append(
                            {"name": "&".join(toks), "price": val})
        return groups

    def scan(self, hours_ahead=48, markets=("1X2", "DC", "BTTS", "O/U", "COMBO", "DNB", "TTOTAL"), callback=None):
        """Returns [shaped events]. Zero quota cost."""
        out, seen = [], set()
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours_ahead)
        want = set(markets or ())
        subs = []
        if "1X2" in want:
            subs.append(SUB_1X2)
        if "DC" in want:
            subs.append(SUB_DC)
        if "BTTS" in want:
            subs.append(SUB_BTTS)
        if "O/U" in want:
            subs.append(SUB_OU)
        if "COMBO" in want:
            subs.extend([SUB_C12B, SUB_COUB, SUB_C12OU])
        if "DNB" in want:
            subs.append(SUB_DNB)
        if "TTOTAL" in want:
            subs.extend([SUB_HTOT, SUB_ATOT])
        labels = {"1x2": "1X2", "dc": "Double chance", "btts": "BTTS",
                  "ou1.5": "O/U 1.5", "ou2.5": "O/U 2.5", "ou3.5": "O/U 3.5",
                  "1X2+BTTS": "1X2+BTTS", "O/U+BTTS": "O/U+BTTS", "1X2+O/U": "1X2+O/U",
                  "dnb": "DNB", "hometotal": "Home Total", "awaytotal": "Away Total"}
        n = 0
        for sub in subs:
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
                        # time-conscious: only upcoming (10min grace for build latency).
                        # Morning games already played must never enter a midday slip.
                        if dt > cutoff or dt < now - timedelta(minutes=10):
                            continue
                        groups = self._outcomes(m)
                        mid = str(m.get("match_id", ""))
                        league = f"{m.get('category', '')} {m.get('competition_name', '')}".strip()
                        for suffix, outcomes in groups.items():
                            if len(outcomes) < 2:
                                continue
                            key = (mid, suffix)
                            if key in seen:
                                continue
                            seen.add(key)
                            best = max(outcomes, key=lambda o: o["price"])
                            out.append({
                                "id": f"btk-{mid}-{suffix}",
                                "market": labels.get(suffix, suffix),
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


def scan_betika(hours_ahead=48, markets=("1X2", "DC", "BTTS", "O/U", "COMBO", "DNB", "TTOTAL"), callback=None, log=None):
    return BetikaOdds(log=log).scan(hours_ahead=hours_ahead, markets=markets, callback=callback)


if __name__ == "__main__":
    legs = scan_betika(hours_ahead=48, log=print)
    from collections import Counter
    print("markets:", dict(Counter(l["market"] for l in legs)))
    for l in legs[:8]:
        o = l["bookmakers"][0]["markets"][0]["outcomes"]
        prices = " / ".join(f"{x['name']}:{x['price']}" for x in o)
        print(f"[{l['market']}] {l['league'][:22]:22s} {l['home_team'][:16]:16s} vs {l['away_team'][:16]:16s} -> {prices}")
