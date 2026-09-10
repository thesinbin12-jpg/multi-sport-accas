"""learner.py — Agentic nightly learner: verify, reason with LLM, adjust strategy.

Each nightly pass is an agent loop:
  1. ACT (tools): verify pending tickets, gather decided legs + stats.
  2. REASON (LLM): feed real numbers to the model; it names patterns,
     lessons, leagues to avoid, bands to prefer, and what to revisit.
  3. ACT (tools): persist patterns + debrief; builder reads them next build.

Heuristics only ground the LLM (real samples/rates) and guard its output
(blocks must match observed leagues with sample >= 3). If no LLM key is set,
it degrades to heuristic-only mode and says so.

Tables (created here, alongside db.py tables):
  acca_patterns — per league/sport/odds-band win rates + LLM actions
  acca_debrief  — nightly JSON report + human notes
"""
import json
import os
import re
from datetime import datetime, timezone

try:
    import db
except ImportError:
    from worker import db  # type: ignore

try:
    import verifier
except ImportError:
    from worker import verifier  # type: ignore

try:
    import config
except ImportError:
    from worker import config  # type: ignore

try:
    from ai_router import router
    _HAS_LLM = True
except ImportError:
    try:
        from worker.ai_router import router  # type: ignore
        _HAS_LLM = True
    except ImportError:
        _HAS_LLM = False


# ---- storage ----

def _is_pg() -> bool:
    url = os.environ.get("DATABASE_URL", "") or getattr(config, "DATABASE_URL", "")
    return url.startswith("postgres")


def _conn():
    if _is_pg():
        import psycopg2  # type: ignore
        return psycopg2.connect(os.environ.get("DATABASE_URL") or config.DATABASE_URL)
    import sqlite3  # type: ignore
    path = os.environ.get("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "accas.db"))
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _exec(cur, query, params=()):
    if not _is_pg():
        query = query.replace("%s", "?")
    cur.execute(query, params)


def init_learner_schema() -> None:
    conn = _conn()
    try:
        cur = conn.cursor()
        patterns = """
        CREATE TABLE IF NOT EXISTS acca_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            key TEXT NOT NULL,
            sample INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            rate REAL NOT NULL DEFAULT 0.0,
            action TEXT DEFAULT '',
            updated_at TEXT NOT NULL,
            UNIQUE(kind, key)
        )
        """
        debrief = """
        CREATE TABLE IF NOT EXISTS acca_debrief (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            verified_new INTEGER NOT NULL DEFAULT 0,
            summary TEXT DEFAULT '{}',
            notes TEXT DEFAULT ''
        )
        """
        if _is_pg():
            patterns = patterns.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            debrief = debrief.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
        cur.execute(patterns)
        cur.execute(debrief)
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _odds_band(odds: float) -> str:
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return "unknown"
    if o < 2.0:
        return "1.4-2.0"
    if o < 3.0:
        return "2.0-3.0"
    if o < 5.0:
        return "3.0-5.0"
    return "5.0+"


def _llm_available() -> bool:
    return _HAS_LLM and bool(
        os.environ.get("GROQ_API_KEY") or os.environ.get("GEMINI_API_KEY")
        or getattr(config, "GROQ_API_KEY", "") or getattr(config, "GEMINI_API_KEY", "")
    )


def _ask_llm(prompt: str, system: str) -> tuple[str | None, str | None]:
    """Returns (text, model_used) or (None, error)."""
    if not _llm_available():
        return None, "no LLM key"
    try:
        text, model, err, _elapsed = router.analyze(prompt, system_prompt=system)
        if err or not text:
            return None, err or "empty reply"
        return text, model
    except Exception as e:
        return None, str(e)


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# ---- step 1: gather (tools) ----

def _all_decided_legs(limit: int = 200) -> list:
    legs = []
    for t in db.get_tickets(limit=limit):
        for leg in db.get_legs(t["id"]):
            if leg.get("result") in ("won", "lost"):
                legs.append(leg)
    return legs


def _rate(rows: list) -> tuple[int, int, float]:
    n = len(rows)
    w = sum(1 for r in rows if r.get("result") == "won")
    return n, w, (w / n if n else 0.0)


def analyze(legs: list) -> dict:
    by_league: dict[str, list] = {}
    by_sport: dict[str, list] = {}
    by_band: dict[str, list] = {}
    for leg in legs:
        by_league.setdefault(leg.get("league") or "unknown", []).append(leg)
        by_sport.setdefault(leg.get("sport") or "unknown", []).append(leg)
        by_band.setdefault(_odds_band(leg.get("odds")), []).append(leg)

    def pack(groups: dict) -> dict:
        out = {}
        for k, rows in groups.items():
            n, w, r = _rate(rows)
            probs = [float(x.get("probability") or 0) for x in rows if x.get("probability")]
            out[k] = {
                "sample": n, "wins": w, "rate": round(r, 3),
                "avg_predicted": round(sum(probs) / len(probs), 3) if probs else None,
            }
        return out

    return {"by_league": pack(by_league), "by_sport": pack(by_sport), "by_band": pack(by_band)}


def _lost_leg_sample(legs: list, n: int = 12) -> list:
    lost = [l for l in legs if l.get("result") == "lost"]
    out = []
    for leg in lost[-n:]:
        out.append({
            "match": leg.get("match"), "league": leg.get("league"),
            "sport": leg.get("sport"), "selection": leg.get("selection"),
            "odds": leg.get("odds"), "predicted": leg.get("probability"),
        })
    return out


# ---- step 2: reason (LLM agent) ----

ANALYST_SYSTEM = (
    "You are the learning brain of a multi-sport accumulator builder. "
    "You get REAL settled-bet statistics. Never invent leagues, matches, or numbers. "
    "Always reply with exactly one JSON object, no other text."
)

ANALYST_PROMPT = """Settled legs statistics (ground truth, do not invent beyond this):
{stats}

Recently lost legs (revisit these for recurring causes):
{lost}

Previous debrief notes:
{prev_notes}

Decide strategy. Reply with exactly this JSON shape:
{{
  "blocked_leagues": ["league names with >=3 settled legs and clearly cold rates, or []"],
  "preferred_band": "one of 1.4-2.0, 2.0-3.0, 3.0-5.0, 5.0+ with the best proven rate, or null",
  "lessons": ["2-4 concrete lessons, each naming a league/sport/odds pattern seen in the data"],
  "revisit": ["picks or patterns to re-examine next cycle and why"],
  "notes": "2-3 sentence human-readable nightly debrief"
}}"""


def _heuristic_fallback(patterns: dict) -> dict:
    blocked = [lg for lg, p in patterns["by_league"].items()
               if p["sample"] >= 5 and p["rate"] < 0.25 and lg != "unknown"]
    cands = [(b, p) for b, p in patterns["by_band"].items() if p["sample"] >= 5 and b != "unknown"]
    cands.sort(key=lambda kv: kv[1]["rate"], reverse=True)
    return {
        "blocked_leagues": blocked,
        "preferred_band": cands[0][0] if cands else None,
        "lessons": ["Heuristic mode (no LLM key): thresholds only, no interpretation."],
        "revisit": ["Enable GROQ_API_KEY or GEMINI_API_KEY for agentic review."],
        "notes": "Heuristic pass — no LLM available.",
        "llm_model": None,
    }


def reason(patterns: dict, legs: list, prev_notes: str) -> dict:
    """Ask the LLM to interpret stats and set strategy. Grounded + guarded."""
    stats = {
        "by_league": patterns["by_league"],
        "by_sport": patterns["by_sport"],
        "by_band": patterns["by_band"],
        "decided_legs": len(legs),
    }
    prompt = ANALYST_PROMPT.format(
        stats=json.dumps(stats)[:6000],
        lost=json.dumps(_lost_leg_sample(legs))[:2500],
        prev_notes=(prev_notes or "none — first debrief")[:800],
    )
    text, model = _ask_llm(prompt, ANALYST_SYSTEM)
    if not text:
        d = _heuristic_fallback(patterns)
        d["notes"] += f" (LLM unavailable)"
        return d
    decision = _extract_json(text)
    if not decision:
        d = _heuristic_fallback(patterns)
        d["notes"] = "LLM reply was not valid JSON — fell back to heuristics."
        return d
    # Guard: LLM may only block leagues actually observed with sample >= 3
    known = {lg for lg, p in patterns["by_league"].items() if p["sample"] >= 3}
    blocked = [lg for lg in (decision.get("blocked_leagues") or []) if lg in known]
    band = decision.get("preferred_band")
    if band not in ("1.4-2.0", "2.0-3.0", "3.0-5.0", "5.0+", None):
        band = None
    return {
        "blocked_leagues": blocked,
        "preferred_band": band,
        "lessons": list(decision.get("lessons") or [])[:6],
        "revisit": list(decision.get("revisit") or [])[:6],
        "notes": str(decision.get("notes") or "")[:800],
        "llm_model": model,
    }


# ---- step 3: persist (tools) ----

def _save_patterns(patterns: dict, decision: dict) -> None:
    init_learner_schema()
    conn = _conn()
    try:
        cur = conn.cursor()
        rows = []
        for lg, p in patterns["by_league"].items():
            rows.append(("league", lg, p["sample"], p["wins"], p["rate"],
                         "avoid" if lg in decision["blocked_leagues"] else ""))
        for sp, p in patterns["by_sport"].items():
            rows.append(("sport", sp, p["sample"], p["wins"], p["rate"], ""))
        for bd, p in patterns["by_band"].items():
            rows.append(("odds_band", bd, p["sample"], p["wins"], p["rate"],
                         "prefer" if bd == decision["preferred_band"] else ""))
        for kind, key, n, w, r, action in rows:
            if _is_pg():
                _exec(cur, "INSERT INTO acca_patterns (kind, key, sample, wins, rate, action, updated_at) "
                           "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                           "ON CONFLICT (kind, key) DO UPDATE SET sample=EXCLUDED.sample, wins=EXCLUDED.wins, "
                           "rate=EXCLUDED.rate, action=EXCLUDED.action, updated_at=EXCLUDED.updated_at",
                      (kind, key, n, w, r, action, _now()))
            else:
                _exec(cur, "INSERT OR REPLACE INTO acca_patterns (kind, key, sample, wins, rate, action, updated_at) "
                           "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                      (kind, key, n, w, r, action, _now()))
        conn.commit()
    finally:
        conn.close()


def _save_debrief(verified_new: int, summary: dict, notes: str) -> None:
    init_learner_schema()
    conn = _conn()
    try:
        cur = conn.cursor()
        _exec(cur, "INSERT INTO acca_debrief (created_at, verified_new, summary, notes) VALUES (%s,%s,%s,%s)",
              (_now(), verified_new, json.dumps(summary), notes))
        conn.commit()
    finally:
        conn.close()


def _last_debrief() -> dict | None:
    init_learner_schema()
    conn = _conn()
    try:
        cur = conn.cursor()
        _exec(cur, "SELECT summary, notes, created_at FROM acca_debrief ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        summary = row[0]
        if isinstance(summary, str):
            try:
                summary = json.loads(summary)
            except Exception:
                summary = {}
        return {"summary": summary, "notes": row[1], "created_at": row[2]}
    finally:
        conn.close()


# ---- public API ----

def nightly_learn(days_from: int = 5) -> dict:
    """One nightly pass: verify (act) -> LLM reason -> persist (act).

    Safe daily even with no new build: verification may settle nothing,
    but the LLM still revisits full history against the previous debrief.
    """
    init_learner_schema()
    verify_summary = verifier.verify_all_pending(days_from=days_from)
    legs = _all_decided_legs()
    patterns = analyze(legs)
    prev = _last_debrief()
    decision = reason(patterns, legs, (prev or {}).get("notes", ""))
    _save_patterns(patterns, decision)

    acc = db.get_accuracy_stats()
    db.record_accuracy(acc["verified_tickets"], acc["won_tickets"], notes="nightly learn")

    header = (f"verified {verify_summary.get('checked', 0)} ticket(s) "
              f"({verify_summary.get('won', 0)}W/{verify_summary.get('lost', 0)}L, "
              f"{verify_summary.get('pending', 0)} still pending)")
    lessons = " | ".join(decision.get("lessons") or [])
    notes = f"{header}. {decision.get('notes','')}"
    if lessons:
        notes += f" Lessons: {lessons}"
    if decision.get("llm_model"):
        notes += f" (reasoned by {decision['llm_model']})"

    summary = {
        "verified": verify_summary,
        "decided_legs": len(legs),
        "accuracy": acc,
        "patterns": patterns,
        "decision": {k: v for k, v in decision.items()},
    }
    _save_debrief(verify_summary.get("checked", 0), summary, notes)
    return {"ok": True, "notes": notes, "summary": summary}


def get_strategy() -> dict:
    """Current strategy for builder. Empty-safe: builder ignores failures."""
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "SELECT kind, key, action FROM acca_patterns")
            rows = cur.fetchall()
        finally:
            conn.close()
        blocked, preferred = [], None
        for r in rows:
            kind, key, action = r[0], r[1], r[2]
            if kind == "league" and action == "avoid":
                blocked.append(key)
            if kind == "odds_band" and action == "prefer":
                preferred = key
        return {"blocked_leagues": blocked, "preferred_band": preferred}
    except Exception:
        return {"blocked_leagues": [], "preferred_band": None}


def latest_insights() -> dict:
    return {
        "ok": True,
        "accuracy": db.get_accuracy_stats(),
        "debrief": _last_debrief(),
        "strategy": get_strategy(),
        "llm": "agentic" if _llm_available() else "heuristic (no key)",
    }


if __name__ == "__main__":
    out = nightly_learn()
    print(out["notes"])
