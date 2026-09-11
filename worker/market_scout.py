"""
market_scout.py — Pure-code per-fixture, per-market data scoring (no LLM).

For every fixture, EVERY market it carries (1X2, BTTS, O/U 2.5, Double chance)
is scored from data: team form + H2H (football-data.org, cached) blended with
the market's own implied probability (hybrid 60/40 with sample, else implied).

Pipeline: preshortlist (odds shape, no history) -> history (FDO, capped budget)
-> score every market -> rank by blended + edge. The LLM swarm then debates
only the survivors. Napoli vs Bologna style: the data names the market first.
"""

import math

try:
    from analyst import team_recent_struct
except ImportError:
    from worker.analyst import team_recent_struct  # type: ignore


_PRIORS: dict = {}


def _refresh_priors():
    """Empirical settled-rates prior from the evening learner (per build)."""
    global _PRIORS
    try:
        try:
            from learner import get_priors
        except ImportError:
            from worker.learner import get_priors  # type: ignore
        _PRIORS = get_priors() or {}
    except Exception:
        _PRIORS = {}


def _leg_outcomes(leg):
    """Outcome (name, price) pairs from either leg shape: extract_legs puts a flat
    'outcomes' list; raw events carry bookmakers[].markets[]. Never raises."""
    try:
        outs = leg.get("outcomes") or []
        if outs:
            return [(o.get("name"), o.get("price")) for o in outs if o.get("price") is not None]
        mk = (leg.get("bookmakers") or [{}])[0].get("markets") or [{}]
        return [(o.get("name"), o.get("price")) for o in (mk[0].get("outcomes", []) or [])
                if o.get("price") is not None]
    except Exception:
        return []


def _norm(name):
    return str(name or "").strip().lower()


def _last_word(name):
    parts = str(name or "").split()
    return parts[-1].lower() if parts else ""


def _team_stats(struct, as_home=None):
    """W/D/L + goals from structured matches. as_home True/False filters venue."""
    gp = w = d = l = gf = ga = scored = 0
    for m in struct.get("matches") or []:
        is_home = _norm(m["home"]) == _norm(struct.get("_me", ""))
        if as_home is not None and is_home != as_home:
            continue
        me_for = m["hs"] if is_home else m["aws"]
        me_ag = m["aws"] if is_home else m["hs"]
        gp += 1
        gf += me_for
        ga += me_ag
        if me_for > 0:
            scored += 1
        if me_for > me_ag:
            w += 1
        elif me_for == me_ag:
            d += 1
        else:
            l += 1
    return {"gp": gp, "w": w, "d": d, "l": l, "gf": gf, "ga": ga,
            "scored_frac": (scored / gp) if gp else 0.0,
            "gf_avg": (gf / gp) if gp else 0.0, "ga_avg": (ga / gp) if gp else 0.0}


def _with_me(struct, team):
    s = dict(struct)
    s["_me"] = team
    return s


def _h2h(home_struct, away_struct, home, away):
    """Meetings between the two clubs found in either sample."""
    out = []
    seen = set()
    hl, al = _last_word(home), _last_word(away)
    for m in (home_struct.get("matches") or []) + (away_struct.get("matches") or []):
        blob = f"{m['home']} {m['away']}".lower()
        if hl and al and hl in blob and al in blob:
            k = (m["home"], m["away"], m["hs"], m["aws"])
            if k not in seen:
                seen.add(k)
                out.append(m)
    return out


def _pois(mu, k):
    return math.exp(-mu) * (mu ** k) / math.factorial(k)


def _match_probs(exp_h, exp_a):
    ph = pd_ = pa = 0.0
    for i in range(9):
        for j in range(9):
            p = _pois(exp_h, i) * _pois(exp_a, j)
            if i > j:
                ph += p
            elif i == j:
                pd_ += p
            else:
                pa += p
    return ph, pd_, pa


def _over_prob(exp_total, line=2.5):
    return 1.0 - sum(_pois(exp_total, k) for k in range(int(line) + 1))


def _implied(odds):
    try:
        o = float(odds)
        return 1.0 / o if o > 1.0 else 0.0
    except Exception:
        return 0.0


def _blend(data_p, implied, sample):
    w = 0.6 if sample >= 8 else (0.35 if sample >= 4 else 0.0)
    return round(data_p * w + implied * (1 - w), 4), w


def score_fixture(home, away, markets, progress_cb=None):
    """markets: {label: [(name, price)]}. Returns [(label, name, price, blended, edge, data_why)]."""
    hs = _with_me(team_recent_struct(home), home)
    aws = _with_me(team_recent_struct(away), away)
    hst = _team_stats(hs)
    ast = _team_stats(aws)
    sample = hst["gp"] + ast["gp"]
    meetings = _h2h(hs, aws, home, away)

    # expected goals: attack vs defence, mild home edge
    exp_h = max(0.15, ((hst["gf_avg"] + ast["ga_avg"]) / 2) * 1.15) if sample else 1.35
    exp_a = max(0.10, ((ast["gf_avg"] + hst["ga_avg"]) / 2) * 0.95) if sample else 1.15
    ph, pd_, pa = _match_probs(exp_h, exp_a)
    exp_total = exp_h + exp_a
    p_over = _over_prob(exp_total)

    p_h_scores = hst["scored_frac"]
    p_a_scores = ast["scored_frac"]
    p_btts = p_h_scores * p_a_scores
    btts_n = 0
    if meetings:
        btts_hit = sum(1 for m in meetings if m["hs"] > 0 and m["aws"] > 0)
        p_btts = (p_btts + btts_hit / len(meetings)) / 2
        btts_n = len(meetings)

    form_txt = (f"{home} {hst['w']}-{hst['d']}-{hst['l']} (GF {hst['gf']}/GA {hst['ga']} in {hst['gp']}); "
                f"{away} {ast['w']}-{ast['d']}-{ast['l']} (GF {ast['gf']}/GA {ast['ga']} in {ast['gp']})")
    h2h_txt = f"H2H {len(meetings)} meetings" + (
        f", BTTS in {sum(1 for m in meetings if m['hs'] > 0 and m['aws'] > 0)}/{len(meetings)}" if meetings else "")

    out = []
    prim = {"H": ph, "D": pd_, "A": pa, "BTTS_Y": p_btts, "BTTS_N": 1 - p_btts,
            "O15": _over_prob(exp_total, 1.5), "O25": p_over, "O35": _over_prob(exp_total, 3.5)}
    prim.update({"U15": 1 - prim["O15"], "U25": 1 - p_over, "U35": 1 - prim["O35"]})

    def _combo_prob(sides):
        """Correlated-combo estimate: independence x correlation factor (capped)."""
        vals = []
        for s in sides:
            if s in ("1", "X", "2"):
                vals.append({"1": ph, "X": pd_, "2": pa}[s])
            elif s in prim:
                vals.append(prim[s])
            else:
                return 0.0
        p = vals[0]
        for v in vals[1:]:
            p *= v
        pair = "".join(sorted(sides))
        if ("BTTS_Y" in sides and "O25" in sides) or ("BTTS_N" in sides and "U25" in sides):
            p *= 1.25  # strongly correlated
        elif ("BTTS_Y" in sides and "U25" in sides) or ("BTTS_N" in sides and "O25" in sides):
            p *= 0.9  # anti-correlated
        elif "BTTS_Y" in sides or "BTTS_N" in sides:
            p *= 1.1
        _ = pair
        return min(0.95, p)

    for label, outcomes in (markets or {}).items():
        for name, price in outcomes:
            imp = _implied(price)
            nl = str(name).lower()
            if label == "1X2":
                data_p = ph if nl not in ("draw", "x") else pd_ if nl in ("draw", "x") else pa
                if home.lower() not in nl and nl not in ("draw", "x") and away.lower() not in nl:
                    data_p = pa if "2" in nl or away.split()[-1].lower() in nl else ph
                why = f"xG {exp_h:.2f}-{exp_a:.2f}; {form_txt}"
            elif label == "BTTS":
                data_p = p_btts if "yes" in nl else 1 - p_btts
                why = f"BTTS model {p_btts:.2f} ({h2h_txt}; scored-rate H {p_h_scores:.2f}/A {p_a_scores:.2f})"
            elif label.startswith("O/U"):
                line = {"O/U 1.5": 1.5, "O/U 2.5": 2.5, "O/U 3.5": 3.5}.get(label, 2.5)
                pover = _over_prob(exp_total, line)
                data_p = pover if "over" in nl else 1 - pover
                why = f"exp goals {exp_total:.2f} -> O{line} model {pover:.2f}; {form_txt}"
            elif label == "Double chance":
                opp = {"1x": pa, "x2": ph, "12": pd_}.get(nl.replace(" ", ""), None)
                data_p = 1 - opp if opp is not None else imp
                why = f"DC from 1X2 model H {ph:.2f}/D {pd_:.2f}/A {pa:.2f}"
            elif "+" in label:
                import re as _re2
                sides = []
                for tok in _re2.split(r"\s*&\s*", str(name).upper()):
                    if tok in ("1", "X", "2"):
                        sides.append(tok)
                    elif tok in ("YES", "NO"):
                        sides.append("BTTS_" + ("Y" if tok == "YES" else "N"))
                    else:
                        mt = _re2.match(r"([OU])(\d+(?:\.5)?)", tok)
                        sides.append(mt.group(1) + mt.group(2) if mt else "?")
                data_p = _combo_prob(sides) if all(s in prim or s in ("1", "X", "2") for s in sides) else imp
                why = f"combo model {data_p:.3f} from H {ph:.2f}/D {pd_:.2f}/A {pa:.2f}, BTTS {p_btts:.2f}, O2.5 {p_over:.2f}"
            else:
                data_p, why = imp, "no data model for this market"
            blended, w = _blend(data_p, imp, sample)
            prior_txt = ""
            try:
                mp = (_PRIORS.get("market") or {}).get(label)
                if mp is not None:
                    blended = round(blended * 0.85 + float(mp) * 0.15, 4)
                    prior_txt = f"; settled-{label} prior {float(mp):.2f}"
            except Exception:
                pass
            if w == 0 and not prior_txt:
                why = f"baseline (no history coverage): implied {imp:.3f}"
            else:
                why = why + prior_txt
            out.append((label, name, price, blended, round(blended - imp, 4), why))
    return out


def _fixture_key(leg):
    return (_norm(leg.get("home_team")), _norm(leg.get("away_team")))


def scout(legs, fdo_budget=24, keep=60, progress_cb=None):
    """Two-stage: odds-shape preshortlist (no history) -> data scoring -> ranked legs.

    Attaches _data=(blended, edge, data_why, fixture_markets) per leg. Never raises.
    """
    def _msg(m):
        if progress_cb:
            try:
                progress_cb(m)
            except Exception:
                pass

    # group legs by fixture
    fixtures: dict = {}
    for leg in legs or []:
        fixtures.setdefault(_fixture_key(leg), []).append(leg)
    # preshortlist: fixtures whose best outcome sits nearest the value sweet spot
    def _fix_val(ls):
        best = 0.0
        for leg in ls:
            for name, price in _leg_outcomes(leg):
                try:
                    p = 1.0 / float(price or 0)
                except Exception:
                    continue
                try:
                    fprice = float(price or 0)
                except Exception:
                    continue
                if 1.5 <= fprice <= 7.0 and p > best:
                    best = p
        return best
    ranked = sorted(fixtures.items(), key=lambda kv: -_fix_val(kv[1]))
    _refresh_priors()
    _msg(f"Scout: {len(fixtures)} fixtures, probing top {min(fdo_budget, len(ranked))} with history…")

    scored = []
    for (hkey, akey), fl in ranked[:max(1, fdo_budget)]:
        home = fl[0].get("home_team", "?")
        away = fl[0].get("away_team", "?")
        markets: dict = {}
        for leg in fl:
            markets.setdefault(leg.get("market", "1X2"), []).extend(_leg_outcomes(leg))
        try:
            res = score_fixture(home, away, markets)
        except Exception:
            continue
        res_by_market = {}
        for label, name, price, blended, edge, why in res:
            res_by_market.setdefault(label, []).append((name, price, blended, edge, why))
            _msg(f"Scout: {home} vs {away} [{label}] {name}@{price} data {blended} edge {edge:+}")
        for leg in fl:
            label = leg.get("market", "1X2")
            cands = res_by_market.get(label) or []
            if not cands:
                continue
            # every outcome scored: {name.lower: (blended, edge, price, why)}.
            # THE pick = best blended INSIDE the 1.5-7.0 band (never min-price).
            picks = {}
            orig = {}
            for cname, cprice, cblended, cedge, cwhy in cands:
                try:
                    p = float(cprice)
                except Exception:
                    continue
                key = str(cname).lower()
                picks[key] = (cblended, cedge, p, cwhy)
                orig.setdefault(key, str(cname))
            if not picks:
                continue
            inband = [(n, b, e, p, w) for n, (b, e, p, w) in picks.items() if 1.5 <= p <= 7.0]
            pool = inband or [(n, b, e, p, w) for n, (b, e, p, w) in picks.items()]
            name, blended, edge, price, why = max(pool, key=lambda c: (c[1], c[2]))
            leg["_picks"] = picks
            leg["_data"] = (blended, edge, why)
            leg["_pick"] = name
            leg["_pick_name"] = orig.get(name, name)
            scored.append(leg)
    scored.sort(key=lambda l: -((l.get("_data") or (0, 0, ""))[0] + max(0, (l.get("_data") or (0, 0, ""))[1]) * 0.5))
    _msg(f"Scout: {len(scored)} legs data-scored, keeping {min(keep, len(scored))}.")
    return scored[:max(1, keep)]