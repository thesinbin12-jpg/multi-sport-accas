"""
simulator.py — Monte Carlo fixture simulation (thousands of runs, 3 perspectives).

Pure-code engine, AI-driven use: the analyst feeds each finalist's data context
in, runs 2000 matches x 3 perspectives (form / H2H-tilted / market-calibrated),
and the LLM synthesizer interprets the hit-rates alongside all other evidence.
Deterministic per seed. Milliseconds per fixture — no LLM cost, no quota.
"""

import math
import random


def _sample_poisson(rng, mu):
    if mu <= 0:
        return 0
    if mu > 30:
        return int(rng.gauss(mu, math.sqrt(mu)) + 0.5)
    L = math.exp(-mu)
    k, p = 0, 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return k - 1


def simulate(exp_h, exp_a, p_btts=0.5, n=2000, seed=7, h2h_tilt=0.0, market_mix=0.0,
             mkt_h=0.33, mkt_d=0.33, mkt_a=0.34):
    """Run n matches. h2h_tilt shifts xG toward the H2H-dominant side (-1..1).
    market_mix blends outcome mass toward bookmaker implied (0..0.5).
    Returns {1X2, BTTS_Y, O15, O25, O35, DC_1X, DC_X2, DC_12} hit rates."""
    perspectives = {
        "form": (exp_h, exp_a, p_btts),
        "h2h": (max(0.1, exp_h * (1 + 0.15 * h2h_tilt)), max(0.1, exp_a * (1 - 0.15 * h2h_tilt)), p_btts),
        "market": (exp_h, exp_a, p_btts),
    }
    agg = {}
    for pname, (eh, ea, pb) in perspectives.items():
        rng = random.Random(seed + (0 if pname == "form" else (1 if pname == "h2h" else 2)))
        c = {"H": 0, "D": 0, "A": 0, "BTTS_Y": 0, "O15": 0, "O25": 0, "O35": 0,
             "DC_1X": 0, "DC_X2": 0, "DC_12": 0}
        for _ in range(max(100, n)):
            i, j = _sample_poisson(rng, eh), _sample_poisson(rng, ea)
            if i > j:
                c["H"] += 1
            elif i == j:
                c["D"] += 1
            else:
                c["A"] += 1
            btts = (i > 0 and j > 0)
            if pname == "market" and rng.random() < market_mix:
                # blend: resample outcome bucket from market implied
                r = rng.random()
                c["H"] += 1 if r < mkt_h else 0
                c["D"] += 1 if mkt_h <= r < mkt_h + mkt_d else 0
                c["A"] += 1 if r >= mkt_h + mkt_d else 0
            if btts or (pname == "market" and rng.random() < pb * market_mix):
                c["BTTS_Y"] += 1
            tot = i + j
            if tot > 1.5:
                c["O15"] += 1
            if tot > 2.5:
                c["O25"] += 1
            if tot > 3.5:
                c["O35"] += 1
            if i >= j:
                c["DC_1X"] += 1
            if j >= i:
                c["DC_X2"] += 1
            if i != j:
                c["DC_12"] += 1
        t = max(100, n)
        agg[pname] = {k: round(v / t, 4) for k, v in c.items()}
    # consensus = mean across perspectives
    keys = list(agg["form"])
    consensus = {k: round(sum(agg[p][k] for p in agg) / len(agg), 4) for k in keys}
    return {"perspectives": agg, "consensus": consensus}


def describe(sim, selection="", market="", home="", away=""):
    """One compact line-set the LLM can reason over."""
    c = sim["consensus"]
    lines = ["Monte Carlo 2000x3 (form/H2H/market): " +
             "H %.2f/D %.2f/A %.2f, BTTS %.2f, O1.5 %.2f/O2.5 %.2f/O3.5 %.2f, 1X %.2f/X2 %.2f/12 %.2f"
             % (c["H"], c["D"], c["A"], c["BTTS_Y"], c["O15"], c["O25"], c["O35"],
                c["DC_1X"], c["DC_X2"], c["DC_12"])]
    key = _sim_key(selection, market, home, away)
    if key:
        hit = round(1 - c.get(key[2:], 0), 4) if key.startswith("1-") else c.get(key, 0)
        lines.append("Sim says '%s' hits ~%.0f%% of runs." % (selection, 100 * hit))
    return " ".join(lines)


def _sim_key(selection, market="", home="", away=""):
    s = str(selection or "").lower()
    if s.startswith("btts:"):
        return "BTTS_Y" if "yes" in s else None
    if s.startswith("over"):
        try:
            line = float(s.split()[1])
            return {1.5: "O15", 2.5: "O25", 3.5: "O35"}.get(line)
        except Exception:
            return None
    if s.startswith("under"):
        try:
            line = float(s.split()[1])
            k = {1.5: "O15", 2.5: "O25", 3.5: "O35"}.get(line)
            return ("1-%s" % k) if k else None
        except Exception:
            return None
    if s in ("1x", "x2", "12"):
        return {"1x": "DC_1X", "x2": "DC_X2", "12": "DC_12"}[s]
    if s == "draw":
        return "D"
    if home and s == str(home).lower():
        return "H"
    if away and s == str(away).lower():
        return "A"
    return None
