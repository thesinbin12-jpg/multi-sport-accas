"""teams.py — Agentic team-identity layer. ONE registry every source resolves
through (FotMob settle, FDO, form, web keywords). It LEARNS: every successful
match persists variant->canonical, so a name is only ever solved once — the
system gets smarter with every settled ticket. Never raises."""

import re
import unicodedata as _ud

STOPWORDS = {"fc", "ac", "sc", "cf", "cd", "ud", "ss", "us", "as",
             "fk", "sk", "bk", "ifk", "united", "city", "town", "rovers",
             "wanderers", "athletic", "sporting", "real", "club", "de",
             "del", "la", "le", "les", "al", "el", "das", "dos", "the",
             "w", "women", "ladies", "ii", "iii", "iv", "u21", "u23",
             "u19", "reserves", "reserve", "youth", "b"}

# Seed knowledge: bookmaker short -> full. The DB table extends this forever.
SEED_ALIASES = {
    "man utd": "manchester united", "man united": "manchester united",
    "man city": "manchester city", "spurs": "tottenham",
    "tottenham hotspur": "tottenham", "west ham": "west ham united",
    "wolves": "wolverhampton", "leeds": "leeds united",
    "leicester": "leicester city", "norwich": "norwich city",
    "psg": "paris saint-germain", "paris sg": "paris saint-germain",
    "bayern": "bayern munich", "bayern munchen": "bayern munich",
    "dortmund": "borussia dortmund", "bvb": "borussia dortmund",
    "inter": "inter milan", "internazionale": "inter milan",
    "ac milan": "milan", "as roma": "roma",
    "atletico": "atletico madrid", "athletic club": "athletic bilbao",
    "sporting": "sporting cp", "sporting lisbon": "sporting cp",
    "benfica": "benfica", "porto": "fc porto",
    "ajax": "ajax", "psv": "psv eindhoven",
    "celtic": "celtic", "rangers": "rangers",
    "galatasaray": "galatasaray", "fenerbahce": "fenerbahce",
    "ol reign": "seattle reign", "seattle reign fc": "seattle reign",
    "st louis city": "st louis city",
}

_ALIASES: dict = {}
_LOADED = [False]


def normalize(name: str) -> str:
    """Fold accents, saint->st, drop parentheticals/periods, squeeze spaces."""
    try:
        t = _ud.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
        t = t.lower().strip()
        t = re.sub(r"\s*\([^)]*\)", "", t)
        t = t.replace(".", " ")
        t = re.sub(r"\bsaint\b", "st", t)
        return re.sub(r"\s+", " ", t).strip()
    except Exception:
        return str(name or "").lower().strip()


def sig(name: str) -> set:
    """Significant tokens (stopwords dropped)."""
    try:
        return {w for w in re.split(r"[\s\-']+", normalize(name)) if w and w not in STOPWORDS}
    except Exception:
        return set()


def _ensure_loaded():
    if _LOADED[0]:
        return
    _LOADED[0] = True
    try:
        for k, v in SEED_ALIASES.items():
            _ALIASES[k] = v
    except Exception:
        pass
    try:
        try:
            from db import get_team_aliases
        except ImportError:
            from worker.db import get_team_aliases  # type: ignore
        for variant, canonical in (get_team_aliases() or {}).items():
            try:
                if variant and canonical:
                    _ALIASES[variant] = canonical
            except Exception:
                continue
    except Exception:
        pass


def canonical(name: str) -> str:
    """Map a name to its canonical form. Cycle-safe: walks alias chains and
    returns the smallest member, so divergent learnings ('man utd' vs
    'manchester united') always converge instead of ping-ponging."""
    try:
        _ensure_loaded()
        n = normalize(name)
        seen = {n}
        for _ in range(10):
            nxt = _ALIASES.get(n)
            if not nxt or nxt in seen:
                break
            seen.add(nxt)
            n = nxt
        return min(seen)
    except Exception:
        return normalize(name)


def score(query: str, candidate: str) -> float:
    """0.0-1.0 name similarity. Exact 1.0, alias 0.95, contains 0.8,
    token overlap below that. Never raises."""
    try:
        _ensure_loaded()
        q, c = normalize(query), normalize(candidate)
        if not q or not c:
            return 0.0
        if q == c:
            return 1.0
        cq, cc = canonical(q), canonical(candidate)
        if cq == cc:
            return 0.95
        if q in c or c in q:
            return 0.8
        sq, sc = set(re.split(r"[\s\-']+", q)) - STOPWORDS, set(re.split(r"[\s\-']+", c)) - STOPWORDS
        sq.discard("")
        sc.discard("")
        if not sq or not sc:
            return 0.0
        inter = sq & sc
        if not inter:
            return 0.0
        union = sq | sc
        j = len(inter) / max(len(union), 1)
        if j >= 0.5:
            return 0.5 + j * 0.3
        # last-token agreement (Reign==Reign) is a weak but real signal
        try:
            if list(re.split(r"[\s\-']+", q))[-1] == list(re.split(r"[\s\-']+", c))[-1]:
                return 0.55
        except Exception:
            pass
        return 0.0
    except Exception:
        return 0.0


def resolve(query: str, candidates, threshold: float = 0.5, margin: float = 0.15):
    """Best candidate for query, or (None, 0.0). Winner must clear threshold
    AND beat the runner-up by margin (no Madrid-derby guesses). Never raises."""
    try:
        _ensure_loaded()
        scored = []
        for cand in candidates or []:
            try:
                s = score(query, cand)
                if s > 0:
                    scored.append((s, cand))
            except Exception:
                continue
        if not scored:
            return None, 0.0
        scored.sort(key=lambda t: -t[0])
        best, second = scored[0], (scored[1] if len(scored) > 1 else (0.0, None))
        if best[0] >= threshold and (best[0] - second[0]) >= margin:
            return best[1], best[0]
        # lone candidate clearing threshold wins without a contest
        if len(scored) == 1 and best[0] >= threshold:
            return best[1], best[0]
        return None, 0.0
    except Exception:
        return None, 0.0


def learn(query: str, matched: str) -> None:
    """Persist a solved name pair (leg name <-> source name). Never raises.
    Canonical prefers an already-known form (seed/DB), else the shorter name."""
    try:
        _ensure_loaded()
        q, m = normalize(query), normalize(matched)
        if not q or not m or q == m:
            return
        cq, cm = _ALIASES.get(q), _ALIASES.get(m)
        if cq and cm:
            canon = cq if len(cq) <= len(cm) else cm
        else:
            canon = cq or cm or (q if len(q) <= len(m) else m)
        for variant in (q, m):
            if variant == canon or _ALIASES.get(variant) == canon:
                continue
            _ALIASES[variant] = canon
            try:
                try:
                    from db import save_team_alias
                except ImportError:
                    from worker.db import save_team_alias  # type: ignore
                save_team_alias(canon, variant)
            except Exception:
                pass
    except Exception:
        pass


TAG_TOKENS = {"w", "women", "ladies", "feminino", "femenino", "wfc",
              "u18", "u19", "u21", "u23", "academy", "reserves",
              "reserve", "youth", "ii", "iii", "2"}


def tags(name: str) -> set:
    """Variant tags (women's/youth/reserve markers). Empty for senior sides."""
    try:
        return set(normalize(name).split()) & TAG_TOKENS
    except Exception:
        return set()


def resolve_pair(home_q: str, away_q: str, pairs, threshold: float = 0.5, margin: float = 0.15):
    """Joint fixture resolution: score whole PAIRS by their weaker side, so a
    men's/women's duplicate on one side can't win alone. Ties broken by
    segment agreement (query's sides vs pair's sides). Returns
    ((h, a), pair_score) or (None, 0.0). Never raises."""
    try:
        _ensure_loaded()
        pls = list(pairs or [])
        if not pls:
            return None, 0.0
        hseg, aseg = tags(home_q), tags(away_q)
        scored = []
        for h, a in pls:
            try:
                sh, sa = score(home_q, h), score(away_q, a)
                if sh <= 0 or sa <= 0:
                    continue
                tag_bonus = 0.0
                try:
                    # same-named senior/youth/women duplicates: the pair whose
                    # variant tags agree with the query wins the tie
                    if tags(h) == hseg and tags(a) == aseg:
                        tag_bonus = 0.15
                except Exception:
                    pass
                scored.append((min(sh, sa) + tag_bonus, (h, a)))
            except Exception:
                continue
        if not scored:
            return None, 0.0
        scored.sort(key=lambda t: -t[0])
        best = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        if best[0] >= threshold and (best[0] - second) >= margin:
            return best[1], best[0]
        return None, 0.0
    except Exception:
        return None, 0.0
