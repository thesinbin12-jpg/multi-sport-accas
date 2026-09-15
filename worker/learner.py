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
from datetime import datetime, timezone, timedelta

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
        url = (os.environ.get("DATABASE_URL") or config.DATABASE_URL or "").replace("-pooler.", ".")
        kwargs = {
            "connect_timeout": 10,
            "tcp_user_timeout": 15000,
            "keepalives": 1, "keepalives_idle": 30,
            "keepalives_interval": 10, "keepalives_count": 3,
            "options": "-c statement_timeout=20000",  # direct endpoint accepts it; zombie queries error in 20s
        }
        return psycopg2.connect(url, **kwargs)
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
        personas = """
        CREATE TABLE IF NOT EXISTS acca_persona_stats (
            name TEXT PRIMARY KEY,
            n INTEGER NOT NULL DEFAULT 0,
            brier_sum REAL NOT NULL DEFAULT 0.0,
            hits INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
        usage = """
        CREATE TABLE IF NOT EXISTS acca_llm_usage (
            day TEXT PRIMARY KEY,
            n INTEGER NOT NULL DEFAULT 0
        )
        """
        formcache = """
        CREATE TABLE IF NOT EXISTS acca_form_cache (
            team TEXT PRIMARY KEY,
            week TEXT NOT NULL,
            data TEXT DEFAULT '{}',
            updated_at TEXT NOT NULL
        )
        """
        llmerr = """
        CREATE TABLE IF NOT EXISTS acca_llm_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            provider TEXT DEFAULT '',
            model TEXT DEFAULT '',
            stage TEXT DEFAULT '',
            error TEXT DEFAULT ''
        )
        """
        llmmodels = """
        CREATE TABLE IF NOT EXISTS acca_llm_models (
            day TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            n INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, provider, model)
        )
        """
        srcstat = """
        CREATE TABLE IF NOT EXISTS acca_source_stats (
            source TEXT PRIMARY KEY,
            ok INTEGER NOT NULL DEFAULT 0,
            fail INTEGER NOT NULL DEFAULT 0,
            last_ok TEXT DEFAULT '',
            last_err TEXT DEFAULT '',
            avg_ms INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
        calib = """
        CREATE TABLE IF NOT EXISTS acca_calib (
            bucket TEXT PRIMARY KEY,
            n INTEGER NOT NULL DEFAULT 0,
            won INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
        tmem = """
        CREATE TABLE IF NOT EXISTS acca_team_memory (
            team TEXT PRIMARY KEY,
            n INTEGER NOT NULL DEFAULT 0,
            won INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
        src_audit = """
        CREATE TABLE IF NOT EXISTS acca_source_audit (
            pair TEXT PRIMARY KEY,
            agree INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
        if _is_pg():
            patterns = patterns.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            debrief = debrief.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            llmerr = llmerr.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
        cur.execute(patterns)
        cur.execute(debrief)
        cur.execute(personas)
        cur.execute(usage)
        cur.execute(formcache)
        cur.execute(llmerr)
        cur.execute(llmmodels)
        cur.execute(srcstat)
        cur.execute(calib)
        cur.execute(tmem)
        cur.execute(src_audit)
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
    """Best-effort JSON object extraction. Handles ``` fences, <think>
    preamble (reasoning models), trailing prose, and trailing commas.
    Scans string-aware balanced objects, tries the LAST one first
    (the answer, not the thinking)."""
    if not text:
        return None
    t = re.sub(r"```(?:json)?", "", text)
    cands: list = []
    i, n = 0, len(t)
    while i < n:
        if t[i] == "{":
            depth, instr, esc, j = 0, False, False, i
            while j < n:
                c = t[j]
                if instr:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        instr = False
                else:
                    if c == '"':
                        instr = True
                    elif c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            cands.append(t[i:j + 1])
                            break
                j += 1
            i = j + 1 if (j < n and depth == 0) else i + 1
        else:
            i += 1
    for raw in reversed(cands):
        for attempt in (raw, re.sub(r",\s*([}\]])", r"\1", raw)):
            try:
                d = json.loads(attempt)
                if isinstance(d, dict):
                    return d
            except Exception:
                continue
    return None


# ---- step 1: gather (tools) ----

def _all_decided_legs(limit: int = 200) -> list:
    legs = []
    for t in db.get_tickets(limit=limit):
        kind = t.get("kind") or "daily"
        for leg in db.get_legs(t["id"]):
            if leg.get("result") in ("won", "lost"):
                leg["kind"] = kind
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
    by_kind: dict[str, list] = {}
    by_market: dict[str, list] = {}
    for leg in legs:
        by_league.setdefault(leg.get("league") or "unknown", []).append(leg)
        by_sport.setdefault(leg.get("sport") or "unknown", []).append(leg)
        by_band.setdefault(_odds_band(leg.get("odds")), []).append(leg)
        by_kind.setdefault(leg.get("kind") or "daily", []).append(leg)
        by_market.setdefault(leg.get("market") or "unknown", []).append(leg)

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

    return {"by_league": pack(by_league), "by_sport": pack(by_sport), "by_band": pack(by_band),
            "by_kind": pack(by_kind), "by_market": pack(by_market)}


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


# ---- persona track record (draw-predictor per-agent accuracy, multi-market) ----

def _norm_persona(name: str) -> str:
    n = str(name or "").strip().lower()
    n = re.sub(r"^the\s+", "", n)
    return re.sub(r"[^a-z]+", "_", n).strip("_") or "unknown"


def _parse_scores(analysis: str) -> dict:
    """Extract {persona: score} from the SCORES trailer stored by analyst.py."""
    out = {}
    try:
        m = re.search(r"SCORES:\s*(.+)", str(analysis or ""))
        if not m:
            return out
        for part in m.group(1).split("|"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            out[_norm_persona(k)] = max(0.01, min(0.99, float(v)))
    except Exception:
        pass
    return out


def _monday() -> str:
    from datetime import timedelta as _td
    today = datetime.now(timezone.utc).date()
    return (today - _td(days=today.weekday())).isoformat()


def get_cached_form(team: str) -> dict | None:
    """This week's FDO form struct for a team, or None (read-through by analyst)."""
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "SELECT data FROM acca_form_cache WHERE team=%s AND week=%s",
                  (str(team or '').strip().lower(), _monday()))
            row = cur.fetchone()
        finally:
            conn.close()
        if row and row[0]:
            return json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
    except Exception:
        pass
    return None


def save_cached_form(team: str, struct: dict) -> None:
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            data = json.dumps(struct or {"matches": []})
            if _is_pg():
                _exec(cur, "INSERT INTO acca_form_cache (team, week, data, updated_at) VALUES (%s,%s,%s,%s) "
                           "ON CONFLICT (team) DO UPDATE SET week=EXCLUDED.week, data=EXCLUDED.data, updated_at=EXCLUDED.updated_at",
                      (str(team or '').strip().lower(), _monday(), data, _now()))
            else:
                _exec(cur, "INSERT OR REPLACE INTO acca_form_cache (team, week, data, updated_at) VALUES (%s,%s,%s,%s)",
                      (str(team or '').strip().lower(), _monday(), data, _now()))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def gm_left() -> int:
    """Gemini free calls remaining today (default 30; Google cuts dynamically, stay small)."""
    try:
        budget = int(os.environ.get("GEMINI_DAILY", getattr(config, "GEMINI_DAILY", 30) or 30))
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "SELECT n FROM acca_llm_usage WHERE day=%s", ("gm-" + datetime.now(timezone.utc).strftime("%Y-%m-%d"),))
            row = cur.fetchone()
        finally:
            conn.close()
        return max(0, budget - (row[0] if row else 0))
    except Exception:
        return 10 ** 9


def log_gm(n: int = 1) -> None:
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            day = "gm-" + datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if _is_pg():
                _exec(cur, "INSERT INTO acca_llm_usage (day, n) VALUES (%s, %s) "
                           "ON CONFLICT (day) DO UPDATE SET n=acca_llm_usage.n+EXCLUDED.n", (day, n))
            else:
                _exec(cur, "SELECT n FROM acca_llm_usage WHERE day=%s", (day,))
                row = cur.fetchone()
                if row:
                    _exec(cur, "UPDATE acca_llm_usage SET n=%s WHERE day=%s", (row[0] + n, day))
                else:
                    _exec(cur, "INSERT INTO acca_llm_usage (day, n) VALUES (%s,%s)", (day, n))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def or_left() -> int:
    """OpenRouter :free calls remaining today (default 45 of ~50/day free tier)."""
    try:
        budget = int(os.environ.get("OR_FREE_DAILY", getattr(config, "OR_FREE_DAILY", 45) or 45))
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "SELECT n FROM acca_llm_usage WHERE day=%s", ("or-" + datetime.now(timezone.utc).strftime("%Y-%m-%d"),))
            row = cur.fetchone()
        finally:
            conn.close()
        return max(0, budget - (row[0] if row else 0))
    except Exception:
        return 10 ** 9


def log_or(n: int = 1) -> None:
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            day = "or-" + datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if _is_pg():
                _exec(cur, "INSERT INTO acca_llm_usage (day, n) VALUES (%s, %s) "
                           "ON CONFLICT (day) DO UPDATE SET n=acca_llm_usage.n+EXCLUDED.n", (day, n))
            else:
                _exec(cur, "SELECT n FROM acca_llm_usage WHERE day=%s", (day,))
                row = cur.fetchone()
                if row:
                    _exec(cur, "UPDATE acca_llm_usage SET n=%s WHERE day=%s", (row[0] + n, day))
                else:
                    _exec(cur, "INSERT INTO acca_llm_usage (day, n) VALUES (%s,%s)", (day, n))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def log_llm_error(provider: str, model: str, stage: str, error: str) -> None:
    """Persist a provider failure for /insights visibility. Bounded table. Never raises."""
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "INSERT INTO acca_llm_errors (ts, provider, model, stage, error) VALUES (%s,%s,%s,%s,%s)",
                  (_now(), str(provider or "")[:40], str(model or "")[:80],
                   str(stage or "")[:40], str(error or "")[:300]))
            _exec(cur, "DELETE FROM acca_llm_errors WHERE id NOT IN (SELECT id FROM acca_llm_errors ORDER BY id DESC LIMIT 50)")
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def recent_llm_errors(limit: int = 10) -> list:
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "SELECT ts, provider, model, stage, error FROM acca_llm_errors ORDER BY id DESC LIMIT %s" % int(limit))
            rows = cur.fetchall()
        finally:
            conn.close()
        return [{"ts": r[0], "provider": r[1], "model": r[2], "stage": r[3], "error": r[4]} for r in rows]
    except Exception:
        return []


def log_model(model: str) -> None:
    """Count a successful call per (day, provider, model). Provider inferred
    from router model lists. Never raises."""
    try:
        m = str(model or "")
        provider = "other"
        try:
            from ai_router import AIRouter
            _r = AIRouter()
            if m in (_r.groq_models or []):
                provider = "groq"
            elif m in (_r.gemini_models or []) or m in (_r.gemma_models or []):
                provider = "gemini"
            elif m in (_r.or_models or []):
                provider = "orouter"
            elif m in NIM_MODELS_LOCAL():
                provider = "nim"
        except Exception:
            pass
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if _is_pg():
                _exec(cur, "INSERT INTO acca_llm_models (day, provider, model, n) VALUES (%s,%s,%s,1) "
                           "ON CONFLICT (day, provider, model) DO UPDATE SET n=acca_llm_models.n+1",
                      (day, provider, m[:120]))
            else:
                _exec(cur, "SELECT n FROM acca_llm_models WHERE day=%s AND provider=%s AND model=%s",
                      (day, provider, m[:120]))
                row = cur.fetchone()
                if row:
                    _exec(cur, "UPDATE acca_llm_models SET n=%s WHERE day=%s AND provider=%s AND model=%s",
                          (row[0] + 1, day, provider, m[:120]))
                else:
                    _exec(cur, "INSERT INTO acca_llm_models (day, provider, model, n) VALUES (%s,%s,%s,1)",
                          (day, provider, m[:120]))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def NIM_MODELS_LOCAL():
    try:
        import os as _os
        return [m.strip() for m in _os.environ.get(
            "NIM_MODELS",
            "mistralai/mistral-nemotron,meta/muse-glimmer-30b,moonshotai/kimi-k3,nvidia/nemotron-3-super-120b-a12b,nvidia/nemotron-3.5-lightning-30b-a3b"
        ).split(",") if m.strip()]
    except Exception:
        return []


def model_split(limit: int = 15) -> list:
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "SELECT provider, model, n FROM acca_llm_models WHERE day=%s ORDER BY n DESC LIMIT " + str(int(limit)),
                  (datetime.now(timezone.utc).strftime("%Y-%m-%d"),))
            rows = cur.fetchall()
        finally:
            conn.close()
        return [{"provider": r[0], "model": r[1], "n": r[2]} for r in rows]
    except Exception:
        return []


def source_record(source: str, ok: bool, ms: int = 0, err: str = "") -> None:
    """Log one fetch outcome for adaptive source ordering. Never raises."""
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "SELECT ok, fail, avg_ms FROM acca_source_stats WHERE source=%s", (source,))
            row = cur.fetchone()
            if row:
                n_ok = row[0] + (1 if ok else 0)
                n_fail = row[1] + (0 if ok else 1)
                avg = int(((row[2] or 0) + max(0, ms)) / 2) if ms else (row[2] or 0)
                if ok:
                    _exec(cur, "UPDATE acca_source_stats SET ok=%s, fail=%s, avg_ms=%s, last_ok=%s, updated_at=%s WHERE source=%s",
                          (n_ok, n_fail, avg, _now(), _now(), source))
                else:
                    _exec(cur, "UPDATE acca_source_stats SET ok=%s, fail=%s, avg_ms=%s, last_err=%s, updated_at=%s WHERE source=%s",
                          (n_ok, n_fail, avg, str(err)[:200], _now(), source))
            else:
                _exec(cur, "INSERT INTO acca_source_stats (source, ok, fail, last_ok, last_err, avg_ms, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                      (source, 1 if ok else 0, 0 if ok else 1, _now() if ok else "", "" if ok else str(err)[:200], max(0, ms), _now()))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def source_health() -> list:
    """[{source, rate, ok, fail, last_err}] worst-first. Never raises."""
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "SELECT source, ok, fail, last_err FROM acca_source_stats")
            rows = cur.fetchall()
        finally:
            conn.close()
        out = []
        for s, ok, fail, err in rows:
            tot = (ok or 0) + (fail or 0)
            out.append({"source": s, "rate": round((ok or 0) / tot, 2) if tot else None,
                        "ok": ok, "fail": fail, "last_err": err})
        return sorted(out, key=lambda r: (r["rate"] is None, r["rate"] or 0))
    except Exception:
        return []


def source_usable(name: str, min_rate: float = 0.15, min_n: int = 3) -> bool:
    """False when a source keeps failing (skip it, save time). Unknown/new = usable."""
    try:
        for r in source_health():
            if r["source"] == name and (r["ok"] + r["fail"]) >= min_n and (r["rate"] or 0) < min_rate:
                return False
    except Exception:
        pass
    return True


def log_llm(n: int = 1) -> None:
    """Count an LLM call against today's budget. Never raises."""
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if _is_pg():
                _exec(cur, "INSERT INTO acca_llm_usage (day, n) VALUES (%s, %s) "
                           "ON CONFLICT (day) DO UPDATE SET n=acca_llm_usage.n+EXCLUDED.n", (day, n))
            else:
                _exec(cur, "SELECT n FROM acca_llm_usage WHERE day=%s", (day,))
                row = cur.fetchone()
                if row:
                    _exec(cur, "UPDATE acca_llm_usage SET n=%s WHERE day=%s", (row[0] + n, day))
                else:
                    _exec(cur, "INSERT INTO acca_llm_usage (day, n) VALUES (%s,%s)", (day, n))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def llm_used_today() -> int:
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "SELECT n FROM acca_llm_usage WHERE day=%s",
                  (datetime.now(timezone.utc).strftime("%Y-%m-%d"),))
            row = cur.fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else 0
    except Exception:
        return -1


def llm_left() -> int:
    """LLM calls remaining in today's budget (default 400). Fail-open: errors mean unlimited."""
    try:
        budget = int(os.environ.get("DAILY_LLM_BUDGET", getattr(config, "DAILY_LLM_BUDGET", 400) or 400))
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "SELECT n FROM acca_llm_usage WHERE day=%s",
                  (datetime.now(timezone.utc).strftime("%Y-%m-%d"),))
            row = cur.fetchone()
        finally:
            conn.close()
        return max(0, budget - (row[0] if row else 0))
    except Exception:
        return 10 ** 9
def _score_personas(legs: list) -> dict:
    """Brier-score every persona verdict on settled legs; persist; return weights.
    Weight: 1.0 default (sample < 5); else clamp(mean_brier / persona_brier, 0.5, 1.5).
    Never raises."""
    try:
        init_learner_schema()
        agg: dict[str, list] = {}
        for leg in legs:
            res = leg.get("result")
            if res not in ("won", "lost"):
                continue
            outcome = 1.0 if res == "won" else 0.0
            for name, s in _parse_scores(leg.get("analysis") or "").items():
                agg.setdefault(name, [0, 0.0, 0])[0] += 1
                agg[name][1] += (s - outcome) ** 2
                agg[name][2] += 1 if (s >= 0.5) == (outcome == 1.0) else 0
        conn = _conn()
        try:
            cur = conn.cursor()
            for name, (n, bs, h) in agg.items():
                if _is_pg():
                    _exec(cur, "INSERT INTO acca_persona_stats (name, n, brier_sum, hits, updated_at) "
                               "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (name) DO UPDATE SET "
                               "n=acca_persona_stats.n+EXCLUDED.n, brier_sum=acca_persona_stats.brier_sum+EXCLUDED.brier_sum, "
                               "hits=acca_persona_stats.hits+EXCLUDED.hits, updated_at=EXCLUDED.updated_at",
                          (name, n, bs, h, _now()))
                else:
                    cur2 = conn.cursor()
                    _exec(cur2, "SELECT n, brier_sum, hits FROM acca_persona_stats WHERE name=%s", (name,))
                    row = cur2.fetchone()
                    if row:
                        _exec(cur, "UPDATE acca_persona_stats SET n=%s, brier_sum=%s, hits=%s, updated_at=%s WHERE name=%s",
                              (row[0] + n, row[1] + bs, row[2] + h, _now(), name))
                    else:
                        _exec(cur, "INSERT INTO acca_persona_stats (name, n, brier_sum, hits, updated_at) VALUES (%s,%s,%s,%s,%s)",
                              (name, n, bs, h, _now()))
            conn.commit()
            _exec(cur, "SELECT name, n, brier_sum, hits FROM acca_persona_stats")
            rows = cur.fetchall()
        finally:
            conn.close()
        briers = [(r[0], r[1], (r[2] / r[1]) if r[1] else 1.0) for r in rows]
        proven = [(nm, n, b) for nm, n, b in briers if n >= 5]
        mean_b = sum(b for _, _, b in proven) / len(proven) if proven else 0.25
        weights = {}
        for nm, n, b in briers:
            weights[nm] = round(max(0.5, min(1.5, mean_b / b)), 2) if n >= 5 and b > 0 else 1.0
        return {"weights": weights,
                "table": [{"persona": nm, "n": n, "brier": round(b, 3),
                             "weight": weights[nm]} for nm, n, b in briers]}
    except Exception:
        return {"weights": {}, "table": []}


def _swarm_misses(legs: list, n: int = 8) -> list:
    """Self-critique fuel: confident losers + dismissed winners."""
    out = []
    for leg in legs:
        try:
            p = float(leg.get("probability") or 0)
        except Exception:
            continue
        r = leg.get("result")
        if (r == "lost" and p >= 0.55) or (r == "won" and p < 0.45):
            out.append({"match": leg.get("match"), "selection": leg.get("selection"),
                        "predicted": p, "actual": r,
                        "why": str(leg.get("analysis") or "")[:300]})
    return out[-n:]


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

Swarm self-critique (legs where the analyst swarm was confidently wrong):
{misses}

Persona track record (Brier score: lower is better; weight >1 means trusted, <1 means distrusted):
{personas}

Previous debrief notes:
{prev_notes}

Decide strategy, comparing daily vs weekly performance where data allows. Reply with exactly this JSON shape:
{{
  "blocked_leagues": ["league names with >=3 settled legs and clearly cold rates, or []"],
  "preferred_band": "one of 1.4-2.0, 2.0-3.0, 3.0-5.0, 5.0+ with the best proven rate, or null",
  "lessons": ["2-4 concrete lessons, each naming a league/sport/odds pattern seen in the data"],
  "persona_notes": ["which personas to trust/distrust based on the track record above, or []"],
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


def reason(patterns: dict, legs: list, prev_notes: str, persona: dict | None = None) -> dict:
    """Ask the LLM to interpret stats and set strategy. Grounded + guarded."""
    stats = {
        "by_league": patterns["by_league"],
        "by_sport": patterns["by_sport"],
        "by_band": patterns["by_band"],
        "by_kind": patterns.get("by_kind", {}),
        "decided_legs": len(legs),
    }
    persona = persona or {}
    prompt = ANALYST_PROMPT.format(
        stats=json.dumps(stats)[:6000],
        lost=json.dumps(_lost_leg_sample(legs))[:2500],
        misses=json.dumps(_swarm_misses(legs))[:2500],
        personas=json.dumps(persona.get("table") or [])[:1500],
        prev_notes=(prev_notes or "none — first debrief")[:800],
    )
    text, model = _ask_llm(prompt, ANALYST_SYSTEM)
    if not text:
        d = _heuristic_fallback(patterns)
        d["notes"] += f" (LLM unavailable)"
        return d
    decision = _extract_json(text)
    if not decision:
        # Log the raw head so /insights shows WHY parsing failed (model? truncation?).
        try:
            log_llm_error("learn", model or "", "learn-reason",
                          ("unparseable JSON reply head: " + (text or ""))[:300])
        except Exception:
            pass
        # One retry: nudge for bare JSON only (reasoning models bury it in prose).
        try:
            text2, model2 = _ask_llm(
                "Reply with ONLY the JSON object, no prose, no fences, no thinking tags:\n" + prompt,
                ANALYST_SYSTEM)
            if text2:
                _d2 = _extract_json(text2)
                if _d2:
                    decision, model, text = _d2, model2, text2
        except Exception:
            pass
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
        "persona_notes": list(decision.get("persona_notes") or [])[:4],
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
        for kd, p in (patterns.get("by_kind") or {}).items():
            rows.append(("kind", kd, p["sample"], p["wins"], p["rate"], ""))
        for mk, p in (patterns.get("by_market") or {}).items():
            # rule: cold markets auto-avoided (rate<0.45, sample>=8) — data, not opinion
            _mact = "avoid" if (p["sample"] >= 8 and p["rate"] < 0.45 and mk != "unknown") else ""
            rows.append(("market", mk, p["sample"], p["wins"], p["rate"], _mact))
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


# ---- weekly lifecycle: watch, dissolve, rebuild (user spec) ----

def _latest_ticket(kind: str, statuses=("pending",)) -> dict | None:
    try:
        for t in db.get_tickets(limit=30, kind=kind):
            if (t.get("status") or "pending") in statuses:
                return t
    except Exception:
        pass
    return None


def weekly_watch() -> dict:
    """Nightly watch over the active weekly acca. Never raises.
    holding -> leave it; spoilt (any leg lost) -> dissolve + rebuild spec
    (fewer legs, lower combined); all won -> mark won."""
    try:
        t = _latest_ticket("weekly")
        if not t:
            return {"state": "none"}
        legs = db.get_legs(t["id"])
        results = [l.get("result") for l in legs]
        if results and all(r == "won" for r in results):
            db.set_ticket_status(t["id"], "won")
            return {"state": "won", "ticket": t["id"]}
        lost = [l for l in legs if l.get("result") == "lost"]
        if lost:
            db.set_ticket_status(t["id"], "dissolved")
            alive = [l for l in legs if l.get("result") != "lost"]
            ceiling = max(2, len(legs) - len(lost) - 1) if legs else 5
            return {"state": "dissolved", "ticket": t["id"],
                    "lost": [l.get("match") for l in lost],
                    "rebuild": {"kind": "weekly", "max_legs": ceiling}}
        return {"state": "holding", "ticket": t["id"],
                "decided": sum(1 for r in results if r in ("won", "lost")),
                "total": len(results)}
    except Exception as e:
        return {"state": "error", "error": str(e)[:200]}


def dissolved_rebuild_spec() -> dict | None:
    """Catch-up: latest weekly dissolved with no pending replacement (e.g. a
    restart killed the learn-task rebuild). Returns a rebuild spec or None.
    Never raises."""
    try:
        if _latest_ticket("weekly", statuses=("pending",)):
            return None
        dis = _latest_ticket("weekly", statuses=("dissolved",))
        if not dis:
            return None
        n = len(db.get_legs(dis["id"]))
        return {"kind": "weekly", "max_legs": max(2, n - 2),
                "after": dis["id"]}
    except Exception:
        return None


def _explain_losses(legs: list, max_n: int = 6) -> list:
    """For newly-lost legs: find out WHAT happened (score + web story) and log it.
    1 LLM call per leg, bounded. Persists to acca_legs.lost_why. Never raises."""
    out = []
    try:
        fresh = [l for l in legs
                 if l.get("result") == "lost" and not (l.get("lost_why") or "")]
        # confident misses first: biggest surprises write the deepest map
        try:
            fresh.sort(key=lambda l: -float(l.get("probability", 0) or 0))
        except Exception:
            pass
        fresh = fresh[:max_n]
        if not fresh:
            return out
        for leg in fresh:
            why = ""
            try:
                ctx = verifier.leg_context(leg.get("match", "")) or {}
                bits = []
                for r in (ctx.get("results") or ctx.get("answer") or [])[:3]:
                    if isinstance(r, dict):
                        bits.append(f"{r.get('title', '')}: {str(r.get('content', r.get('snippet', '')))[:250]}")
                    elif isinstance(r, str):
                        bits.append(r[:250])
                story = " | ".join(bits)[:1200] or str(ctx.get("answer", ""))[:600]
                if story:
                    t, _m = _ask_llm(
                        f"Match: {leg.get('match')} | Selection was: {leg.get('selection')} @ {leg.get('odds')} "
                        f"| Swarm had said: {str(leg.get('analysis', ''))[:500]} | Match story: {story}\n"
                        "In ONE sentence: what actually happened in the match and which factor killed the bet?",
                        "You are a betting post-mortem analyst. One sentence, specific (score, minute, player, red card, etc.).")
                    why = (t or "").strip()[:400]
            except Exception:
                pass
            if why:
                try:
                    conn = _conn()
                    try:
                        cur = conn.cursor()
                        _exec(cur, "UPDATE acca_legs SET lost_why=%s WHERE ticket_id=%s AND match=%s",
                              (why, leg.get("ticket_id"), leg.get("match")))
                        conn.commit()
                    finally:
                        conn.close()
                except Exception:
                    pass
            out.append({"match": leg.get("match"), "why": why or "unexplained"})
    except Exception:
        pass
    return out


def recent_loss_notes(league: str = "", n: int = 3) -> list:
    """Latest post-mortems (lost_why) overall + for this league. Never raises."""
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "SELECT match, selection, lost_why FROM acca_legs WHERE result='lost' "
                       "AND lost_why<>'' ORDER BY id DESC LIMIT %s" % int(n * 4))
            rows = cur.fetchall()
        finally:
            conn.close()
        lg = str(league or "").lower()
        same, other = [], []
        for match, sel, why in rows:
            (same if lg and lg in str(match or "").lower() else other).append(
                {"match": match, "selection": sel, "why": why})
        return (same + other)[:max(1, n)]
    except Exception:
        return []


def _espn_trace() -> list:
    try:
        try:
            from espn import _TRACE
        except ImportError:
            from worker.espn import _TRACE  # type: ignore
        return list(_TRACE or [])[-5:]
    except Exception:
        return []


def get_priors() -> dict:
    """Empirical model (draw-predictor hybrid idea, no sklearn yet): settled win rates
    by (market), (odds band), (league) with samples. Scout blends these as prior.
    Needs >=5 samples per key; below that the key is absent (no vote)."""
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "SELECT kind, key, sample, rate FROM acca_patterns WHERE sample>=5")
            rows = cur.fetchall()
        finally:
            conn.close()
        priors: dict = {"market": {}, "band": {}, "league": {}}
        for kind, key, n, r in rows:
            if kind == "market":
                priors["market"][key] = round(float(r), 3)
            elif kind == "odds_band":
                priors["band"][key] = round(float(r), 3)
            elif kind == "league":
                priors["league"][key] = round(float(r), 3)
        return priors
    except Exception:
        return {"market": {}, "band": {}, "league": {}}


def probe_sources() -> dict:
    """Nightly self-audit: 1 cheap call per data source, record health.
    The system watches its own senses and routes around dead ones."""
    out = {}
    # odds: Betika list page
    try:
        import time as _t
        _t0 = _t.time()
        import requests as _rq
        r = _rq.get("https://api.betika.com/v1/matches",
                    params={"sport_id": 14, "sub_type_id": "1", "page": 1, "limit": 1},
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        ok = r.status_code == 200 and bool((r.json().get("data") or []))
        source_record("odds-betika", ok, int((_t.time() - _t0) * 1000), "" if ok else str(r.status_code))
        out["odds-betika"] = ok
    except Exception as e:
        source_record("odds-betika", False, 0, str(e)[:120])
        out["odds-betika"] = False
    # scores: ESPN one league-day
    try:
        import time as _t
        _t0 = _t.time()
        try:
            from espn import _day_scores
        except ImportError:
            from worker.espn import _day_scores  # type: ignore
        evs = _day_scores("eng.1", datetime.now(timezone.utc).date() - timedelta(days=1))
        ok = isinstance(evs, list)
        source_record("scores-espn", True, int((_t.time() - _t0) * 1000))
        out["scores-espn"] = ok
    except Exception as e:
        source_record("scores-espn", False, 0, str(e)[:120])
        out["scores-espn"] = False
    # scores: FotMob one day
    try:
        import time as _t
        _t0 = _t.time()
        try:
            from fotmob import _fm_day
        except ImportError:
            from worker.fotmob import _fm_day  # type: ignore
        d = _fm_day(datetime.now(timezone.utc).date() - timedelta(days=1))
        ok = len(d) > 50
        source_record("scores-fotmob", ok, int((_t.time() - _t0) * 1000), "" if ok else "empty")
        out["scores-fotmob"] = ok
    except Exception as e:
        source_record("scores-fotmob", False, 0, str(e)[:120])
        out["scores-fotmob"] = False
    # web: Brave one query
    try:
        import time as _t
        _t0 = _t.time()
        try:
            from websearch import brave_search
        except ImportError:
            from worker.websearch import brave_search  # type: ignore
        res = brave_search("test", max_results=1)
        ok = isinstance(res, list)
        source_record("web-brave", True, int((_t.time() - _t0) * 1000))
        out["web-brave"] = ok
    except Exception as e:
        source_record("web-brave", False, 0, str(e)[:120])
        out["web-brave"] = False
    return out


# ---- public API ----

CALIB_BUCKETS = ["0.20-0.35", "0.35-0.50", "0.50-0.65", "0.65-0.80", "0.80-1.01"]


def _calib_bucket(prob: float) -> str:
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return CALIB_BUCKETS[2]
    if p < 0.35:
        return CALIB_BUCKETS[0]
    if p < 0.50:
        return CALIB_BUCKETS[1]
    if p < 0.65:
        return CALIB_BUCKETS[2]
    if p < 0.80:
        return CALIB_BUCKETS[3]
    return CALIB_BUCKETS[4]


def rebuild_calibration() -> dict:
    """Rebuild the calibration ledger from ALL decided legs (delete+insert =
    idempotent, never double-counts). Returns {bucket: {n, won, rate}}."""
    out: dict = {}
    try:
        init_learner_schema()
        legs = _all_decided_legs(limit=5000)
        agg: dict = {}
        for leg in legs:
            try:
                b = _calib_bucket(leg.get("probability", 0.5))
                e = agg.setdefault(b, [0, 0])
                e[0] += 1
                if leg.get("result") == "won":
                    e[1] += 1
            except Exception:
                continue
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "DELETE FROM acca_calib")
            for b, (n, w) in agg.items():
                _exec(cur, "INSERT INTO acca_calib (bucket, n, won, updated_at) VALUES (%s,%s,%s,%s)",
                       (b, n, w, _now()))
                out[b] = {"n": n, "won": w, "rate": round(w / n, 3) if n else 0.0}
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    return out


def calibration_line() -> str:
    """One honest self-check line + overconfidence flag (n>=5). Never raises."""
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "SELECT bucket, n, won FROM acca_calib ORDER BY bucket")
            rows = cur.fetchall()
        finally:
            conn.close()
        if not rows:
            return ""
        bits, worst = [], None
        for b, n, w in rows:
            try:
                rate = (w or 0) / max(n or 0, 1)
                bits.append(f"{b}: {rate:.0%} (n={n})")
                lo, hi = float(str(b).split("-")[0]), float(str(b).split("-")[1])
                gap = rate - (lo + hi) / 2
                if (n or 0) >= 5 and (worst is None or gap < worst[0]):
                    worst = (gap, b, rate, (lo + hi) / 2)
            except Exception:
                continue
        line = "Calibration (my odds vs reality): " + "; ".join(bits)
        if worst and worst[0] < -0.07:
            line += f". Watch: {worst[1]} hits {worst[2]:.0%} vs {worst[3]:.0%} expected — overconfident there."
        return line
    except Exception:
        return ""


def rebuild_team_memory() -> int:
    """Per-club involvement record from ALL decided legs (delete+insert).
    Keyed by teams.canonical so rebrands/aliases converge. Returns clubs."""
    try:
        init_learner_schema()
        try:
            from teams import canonical as _canon
        except ImportError:
            from worker.teams import canonical as _canon  # type: ignore
        legs = _all_decided_legs(limit=5000)
        agg: dict = {}
        for leg in legs:
            try:
                m = str(leg.get("match", ""))
                if " vs " not in m:
                    continue
                h, a = [p.strip() for p in m.split(" vs ", 1)]
                won = 1 if leg.get("result") == "won" else 0
                for tm in {_canon(h), _canon(a)}:
                    if not tm:
                        continue
                    e = agg.setdefault(tm, [0, 0])
                    e[0] += 1
                    e[1] += won
            except Exception:
                continue
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "DELETE FROM acca_team_memory")
            for tm, (n, w) in agg.items():
                _exec(cur, "INSERT INTO acca_team_memory (team, n, won, updated_at) VALUES (%s,%s,%s,%s)",
                       (tm, n, w, _now()))
            conn.commit()
        finally:
            conn.close()
        return len(agg)
    except Exception:
        return 0


def get_team_records(home: str, away: str) -> dict:
    """{query_name: {n, won, rate}} involvement records (min sample 3 else {})."""
    out: dict = {}
    try:
        init_learner_schema()
        try:
            from teams import canonical as _canon2
        except ImportError:
            from worker.teams import canonical as _canon2  # type: ignore
        want = {_canon2(home): home, _canon2(away): away}
        conn = _conn()
        try:
            cur = conn.cursor()
            for canon, orig in want.items():
                if not canon:
                    continue
                _exec(cur, "SELECT n, won FROM acca_team_memory WHERE team=%s", (canon,))
                row = cur.fetchone()
                if row and (row[0] or 0) >= 3:
                    out[str(orig)] = {"n": row[0], "won": row[1],
                                      "rate": round((row[1] or 0) / max(row[0], 1), 2)}
        finally:
            conn.close()
    except Exception:
        pass
    return out


def record_source_audit(a: str, b: str, agree: bool) -> None:
    """One agreement event between two score sources. Never raises."""
    try:
        if not a or not b:
            return
        pair = "-".join(sorted([str(a), str(b)]))[:64]
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            if _is_pg():
                cur.execute("INSERT INTO acca_source_audit (pair, agree, total, updated_at) VALUES (%s,%s,1,%s) "
                            "ON CONFLICT (pair) DO UPDATE SET agree=acca_source_audit.agree+EXCLUDED.agree, "
                            "total=acca_source_audit.total+1, updated_at=EXCLUDED.updated_at",
                            (pair, 1 if agree else 0, _now()))
            else:
                _exec(cur, "INSERT INTO acca_source_audit (pair, agree, total, updated_at) VALUES (%s,%s,1,%s) "
                           "ON CONFLICT (pair) DO UPDATE SET agree=agree+excluded.agree, total=total+1, updated_at=excluded.updated_at",
                           (pair, 1 if agree else 0, _now()))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def source_audit_line() -> str:
    """Agreement rates between score sources. '' when no data. Never raises."""
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "SELECT pair, agree, total FROM acca_source_audit ORDER BY total DESC")
            rows = cur.fetchall()
        finally:
            conn.close()
        bits = []
        for pair, ag, tot in rows:
            try:
                if (tot or 0) >= 3:
                    bits.append(f"{pair} agree {(ag or 0) / max(tot, 1):.0%} (n={tot})")
            except Exception:
                continue
        return ("Source truth audits: " + "; ".join(bits)) if bits else ""
    except Exception:
        return ""


def last_learn_gap_hours() -> float | None:
    """Hours since the last debrief. None when never ran. Never raises."""
    try:
        init_learner_schema()
        conn = _conn()
        try:
            cur = conn.cursor()
            _exec(cur, "SELECT MAX(created_at) FROM acca_debrief")
            row = cur.fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return None
        try:
            dt = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
        except Exception:
            return None
    except Exception:
        return None


def nightly_learn(days_from: int = 5) -> dict:
    """One nightly pass: verify (act) -> LLM reason -> persist (act).

    Safe daily even with no new build: verification may settle nothing,
    but the LLM still revisits full history against the previous debrief.
    """
    init_learner_schema()
    _gap_note = ""
    try:
        _gap = last_learn_gap_hours()
        if _gap is not None and _gap > 30:
            days_from = max(int(days_from or 0), 7)
            _gap_note = f" Watchdog: last learn {_gap:.0f}h ago — catch-up window {days_from}d."
    except Exception:
        pass
    verify_summary = verifier.verify_all_pending(days_from=days_from)
    try:
        _calib = rebuild_calibration()
        _tmem_n = rebuild_team_memory()
    except Exception:
        _calib, _tmem_n = {}, 0
    pruned = 0
    try:
        pruned += db.prune_pending("daily", keep=2)
        pruned += db.prune_pending("weekly", keep=2)
    except Exception:
        pass
    legs = _all_decided_legs()
    patterns = analyze(legs)
    persona = _score_personas(legs)
    prev = _last_debrief()
    # No-new-evidence guard: with zero verified and zero decided legs the LLM
    # invents specifics (seen live). Skip the reason call, keep a factual row.
    if (verify_summary.get("checked") or 0) == 0 and not legs:
        pend = verify_summary.get("pending", 0)
        notes = (f"run {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}: no matches settled "
                 f"({pend} ticket(s) still pending). Nothing to learn yet — strategy unchanged.")
        summary = {"verified": verify_summary, "decided_legs": 0,
                   "accuracy": db.get_accuracy_stats(), "patterns": patterns,
                   "personas": persona.get("table"), "weekly": weekly_watch(),
                   "lost_stories": [], "decision": {"notes": notes}}
        _save_debrief(0, summary, notes)
        out = {"ok": True, "notes": notes, "summary": summary}
        try:
            _w = summary.get("weekly") or {}
            _spec = (_w.get("rebuild") if _w.get("state") == "dissolved" else None) \
                or dissolved_rebuild_spec()
            if _spec:
                out["weekly_rebuild"] = _spec
        except Exception:
            pass
        return out
    decision = reason(patterns, legs, (prev or {}).get("notes", ""), persona)
    _save_patterns(patterns, decision)
    watch = weekly_watch()
    # Dissolve MUST rebuild: live spec wins, else catch-up (restart-kill case).
    _rebuild_spec = None
    if watch.get("state") == "dissolved" and watch.get("rebuild"):
        _rebuild_spec = dict(watch["rebuild"])
    if _rebuild_spec is None:
        _rebuild_spec = dissolved_rebuild_spec()
    wfill = {"priced": 0}
    if watch.get("state") == "holding":
        # WEEKLY FILL: fixture-led shortlist prices Wed-Sun legs as books
        # publish odds. Append-only (results/post-mortems untouched).
        try:
            try:
                from weekly_seed import price_and_append, current_week_id
            except ImportError:
                from worker.weekly_seed import price_and_append, current_week_id  # type: ignore
            _wt = _latest_ticket("weekly")
            if _wt and str(_wt.get("created_at", ""))[:10] >= current_week_id():
                _ex = [dict(l) for l in db.get_legs(_wt["id"])]
                _fresh, wfill = price_and_append({"legs": _ex}, progress_cb=None) or ([], {})
                if _fresh:
                    db.append_ticket_legs(_wt["id"], _fresh)
        except Exception:
            pass
    # CLV snapshot (beat-the-close tracking) + bankroll recompute. Free scan, no LLM.
    try:
        clv = verifier.record_clv_snapshot() or {}
    except Exception:
        clv = {}
    try:
        bank = db.recompute_bankroll()
        clvsum = db.clv_summary()
    except Exception:
        bank, clvsum = 100.0, {"n": 0, "avg_edge": 0.0}
    lost_stories = _explain_losses(legs)

    acc = db.get_accuracy_stats()
    db.record_accuracy(acc["verified_tickets"], acc["won_tickets"], notes="nightly learn")

    header = (f"run {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}: "
              f"verified {verify_summary.get('checked', 0)} ticket(s) "
              f"({verify_summary.get('won', 0)}W/{verify_summary.get('lost', 0)}L, "
              f"{verify_summary.get('pending', 0)} still pending){_gap_note}")
    lessons = " | ".join(decision.get("lessons") or [])
    notes = f"{header}. {decision.get('notes','')}"
    if lessons:
        notes += f" Lessons: {lessons}"
    if watch.get("state") == "dissolved":
        notes += f" Weekly {watch.get('ticket')} DISSOLVED (spoilt: {', '.join(watch.get('lost') or [])}); rebuild queued."
    elif watch.get("state") == "holding":
        notes += f" Weekly {watch.get('ticket')} holding ({watch.get('decided')}/{watch.get('total')} decided)."
    if _rebuild_spec and watch.get("state") != "dissolved":
        notes += " Dissolved weekly has no replacement; rebuild queued."
    for s in (lost_stories or []):
        if s.get("why") and s["why"] != "unexplained":
            notes += f" Lost {s['match']}: {s['why']}."
    try:
        _cline = calibration_line()
        if _cline:
            notes += f" {_cline}."
    except Exception:
        pass
    try:
        _aline = source_audit_line()
        if _aline:
            notes += f" {_aline}."
    except Exception:
        pass
    if decision.get("llm_model"):
        notes += f" (reasoned by {decision['llm_model']})"
    try:
        notes += f" Bankroll {bank:.1f}u."
        if (clvsum or {}).get("n"):
            notes += f" CLV edge {clvsum['avg_edge']:+.1%} over {clvsum['n']} legs."
    except Exception:
        pass

    summary = {
        "verified": verify_summary,
        "decided_legs": len(legs),
        "accuracy": acc,
        "espn_trace": _espn_trace(),
        "patterns": patterns,
        "personas": persona.get("table"),
        "weekly": watch,
        "weekly_fill": wfill,
        "clv": {**(clv or {}), "summary": clvsum},
        "bankroll_units": bank,
        "lost_stories": lost_stories,
        "calibration": _calib,
        "team_memory_teams": _tmem_n,
        "sources": {"health": source_health(), "probe": probe_sources()},
        "decision": {k: v for k, v in decision.items()},
    }
    _save_debrief(verify_summary.get("checked", 0), summary, notes)
    out = {"ok": True, "notes": notes, "summary": summary}
    if _rebuild_spec:
        out["weekly_rebuild"] = _rebuild_spec
    return out


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
        blocked, preferred, blocked_mk = [], None, []
        for r in rows:
            kind, key, action = r[0], r[1], r[2]
            if kind == "league" and action == "avoid":
                blocked.append(key)
            if kind == "market" and action == "avoid":
                blocked_mk.append(key)
            if kind == "odds_band" and action == "prefer":
                preferred = key
        weights: dict = {}
        try:
            conn2 = _conn()
            try:
                cur2 = conn2.cursor()
                _exec(cur2, "SELECT name, n, brier_sum FROM acca_persona_stats")
                for nm, n, bs in cur2.fetchall():
                    weights[str(nm)] = 1.0 if (n or 0) < 5 or not bs else \
                        round(max(0.5, min(1.5, 0.25 / (bs / n))), 2)
            finally:
                conn2.close()
        except Exception:
            pass
        return {"blocked_leagues": blocked, "preferred_band": preferred,
                "blocked_markets": blocked_mk, "persona_weights": weights}
    except Exception:
        return {"blocked_leagues": [], "preferred_band": None, "blocked_markets": [], "persona_weights": {}}


def latest_insights() -> dict:
    return {
        "ok": True,
        "accuracy": db.get_accuracy_stats(),
        "debrief": _last_debrief(),
        "strategy": get_strategy(),
        "llm": "agentic" if _llm_available() else "heuristic (no key)",
        "llm_used_today": llm_used_today(),
        "llm_errors": recent_llm_errors(10),
        "llm_models": model_split(15),
    }


if __name__ == "__main__":
    out = nightly_learn()
    print(out["notes"])
