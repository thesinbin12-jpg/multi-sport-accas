"""db.py — Neon DB connection + schema. Postgres when DATABASE_URL is set, else sqlite fallback."""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

import config

_lock = threading.Lock()
_sqlite_conn_obj = None


def _is_postgres() -> bool:
    url = config.DATABASE_URL or ""
    return url.startswith("postgres")


def _pg_conn():
    import psycopg2  # type: ignore
    import psycopg2.extras  # type: ignore
    return psycopg2.connect(config.DATABASE_URL)


def _sqlite_conn():
    global _sqlite_conn_obj
    if _sqlite_conn_obj is None:
        path = os.environ.get("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "accas.db"))
        _sqlite_conn_obj = sqlite3.connect(path, check_same_thread=False)
        _sqlite_conn_obj.row_factory = sqlite3.Row
    return _sqlite_conn_obj


def _execute(cur, query, params=()):
    # psycopg2 uses %s, sqlite uses ?. Normalize: write queries with %s, convert for sqlite.
    if not _is_postgres():
        query = query.replace("%s", "?")
    cur.execute(query, params)


SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS acca_tickets (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        combined_odds REAL NOT NULL DEFAULT 1.0,
        legs TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'pending',
        kind TEXT NOT NULL DEFAULT 'daily',
        stake TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS acca_legs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id TEXT NOT NULL,
        sport TEXT DEFAULT '',
        league TEXT DEFAULT '',
        match TEXT DEFAULT '',
        selection TEXT DEFAULT '',
        odds REAL DEFAULT 1.0,
        probability REAL DEFAULT 0.0,
        result TEXT DEFAULT 'pending',
        analysis TEXT DEFAULT '',
        lost_why TEXT DEFAULT '',
        market TEXT DEFAULT '',
        commence_time TEXT DEFAULT '',
        sport_key TEXT DEFAULT '',
        bookmaker TEXT DEFAULT '',
        settle TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS acca_team_aliases (
        variant TEXT PRIMARY KEY,
        canonical TEXT NOT NULL DEFAULT '',
        hits INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS acca_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id TEXT NOT NULL,
        checked_at TEXT NOT NULL,
        won INTEGER NOT NULL DEFAULT 0,
        correct_legs INTEGER NOT NULL DEFAULT 0,
        total_legs INTEGER NOT NULL DEFAULT 0,
        details TEXT DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS acca_accuracy (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        checked_at TEXT NOT NULL,
        total_tickets INTEGER NOT NULL DEFAULT 0,
        won_tickets INTEGER NOT NULL DEFAULT 0,
        accuracy REAL NOT NULL DEFAULT 0.0,
        notes TEXT DEFAULT ''
    )
    """,
]


def ping() -> None:
    """Fail fast if DB unreachable (bad password, rotated creds, wrong host).
    Raises ConnectionError with an actionable message. Call before any scan/API spend."""
    if _is_postgres():
        import urllib.parse
        try:
            conn = _pg_conn()
        except Exception as e:
            try:
                host = urllib.parse.urlparse(config.DATABASE_URL or "").hostname or "postgres"
            except Exception:
                host = "postgres"
            raise ConnectionError(
                f"DB unreachable ({host}): {e}. "
                "Fix: copy the pooled connection string from Neon Console -> Connect "
                "(it is URL-encoded) into Render DATABASE_URL env, then redeploy."
            ) from e
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        finally:
            conn.close()
    else:
        init_schema()


def init_schema() -> None:
    with _lock:
        if _is_postgres():
            conn = _pg_conn()
            try:
                cur = conn.cursor()
                for q in SCHEMA_SQL:
                    # postgres needs SERIAL instead of AUTOINCREMENT — make compatible
                    pq = q.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
                    cur.execute(pq)
                # migrate pre-kind/pre-stake tables
                cur.execute("ALTER TABLE acca_tickets ADD COLUMN IF NOT EXISTS kind TEXT DEFAULT 'daily'")
                cur.execute("ALTER TABLE acca_tickets ADD COLUMN IF NOT EXISTS stake TEXT DEFAULT '{}'")
                cur.execute("ALTER TABLE acca_legs ADD COLUMN IF NOT EXISTS analysis TEXT DEFAULT ''")
                cur.execute("ALTER TABLE acca_legs ADD COLUMN IF NOT EXISTS lost_why TEXT DEFAULT ''")
                cur.execute("ALTER TABLE acca_legs ADD COLUMN IF NOT EXISTS market TEXT DEFAULT ''")
                cur.execute("ALTER TABLE acca_legs ADD COLUMN IF NOT EXISTS commence_time TEXT DEFAULT ''")
                cur.execute("ALTER TABLE acca_legs ADD COLUMN IF NOT EXISTS sport_key TEXT DEFAULT ''")
                cur.execute("ALTER TABLE acca_legs ADD COLUMN IF NOT EXISTS bookmaker TEXT DEFAULT ''")
                cur.execute("ALTER TABLE acca_legs ADD COLUMN IF NOT EXISTS settle TEXT DEFAULT ''")
                cur.execute("CREATE TABLE IF NOT EXISTS acca_team_aliases (variant TEXT PRIMARY KEY, canonical TEXT NOT NULL DEFAULT '', hits INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT '')")
                conn.commit()
            finally:
                conn.close()
        else:
            conn = _sqlite_conn()
            cur = conn.cursor()
            for q in SCHEMA_SQL:
                cur.execute(q)
            cols = [r[1] for r in cur.execute("PRAGMA table_info(acca_tickets)").fetchall()]
            if "kind" not in cols:
                cur.execute("ALTER TABLE acca_tickets ADD COLUMN kind TEXT DEFAULT 'daily'")
            if "stake" not in cols:
                cur.execute("ALTER TABLE acca_tickets ADD COLUMN stake TEXT DEFAULT '{}'")
            leg_cols = [r[1] for r in cur.execute("PRAGMA table_info(acca_legs)").fetchall()]
            if "analysis" not in leg_cols:
                cur.execute("ALTER TABLE acca_legs ADD COLUMN analysis TEXT DEFAULT ''")
            if "lost_why" not in leg_cols:
                cur.execute("ALTER TABLE acca_legs ADD COLUMN lost_why TEXT DEFAULT ''")
            if "market" not in leg_cols:
                cur.execute("ALTER TABLE acca_legs ADD COLUMN market TEXT DEFAULT ''")
            if "commence_time" not in leg_cols:
                cur.execute("ALTER TABLE acca_legs ADD COLUMN commence_time TEXT DEFAULT ''")
            if "sport_key" not in leg_cols:
                cur.execute("ALTER TABLE acca_legs ADD COLUMN sport_key TEXT DEFAULT ''")
            if "bookmaker" not in leg_cols:
                cur.execute("ALTER TABLE acca_legs ADD COLUMN bookmaker TEXT DEFAULT ''")
            if "settle" not in leg_cols:
                cur.execute("ALTER TABLE acca_legs ADD COLUMN settle TEXT DEFAULT ''")
            cur.execute("CREATE TABLE IF NOT EXISTS acca_team_aliases (variant TEXT PRIMARY KEY, canonical TEXT DEFAULT '', hits INTEGER DEFAULT 1, updated_at TEXT DEFAULT '')")
            conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- tickets ----

def save_ticket(ticket_id: str, combined_odds: float, legs: list, status: str = "pending", kind: str = "daily", stake: dict | None = None) -> None:
    kind = kind if kind in ("daily", "weekly") else "daily"
    stake_json = json.dumps(stake or {})
    init_schema()
    with _lock:
        if _is_postgres():
            conn = _pg_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO acca_tickets (id, created_at, combined_odds, legs, status, kind, stake) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET combined_odds=EXCLUDED.combined_odds, legs=EXCLUDED.legs, status=EXCLUDED.status, kind=EXCLUDED.kind, stake=EXCLUDED.stake",
                    (ticket_id, _now(), float(combined_odds), json.dumps(legs), status, kind, stake_json),
                )
                cur.execute("DELETE FROM acca_legs WHERE ticket_id = %s", (ticket_id,))
                for leg in legs:
                    cur.execute(
                        "INSERT INTO acca_legs (ticket_id, sport, league, match, selection, odds, probability, result, analysis, market, commence_time, sport_key, bookmaker) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (ticket_id, leg.get("sport", ""), leg.get("league", ""), leg.get("match", ""),
                         leg.get("selection", ""), float(leg.get("odds", 1.0)),
                         float(leg.get("probability", 0.0)), leg.get("result", "pending"),
                         str(leg.get("analysis", "") or "")[:2000], str(leg.get("market", "") or ""),
                         str(leg.get("commence_time", "") or ""), str(leg.get("sport_key", "") or ""),
                         str(leg.get("bookmaker", "") or "")),
                    )
                conn.commit()
            finally:
                conn.close()
        else:
            conn = _sqlite_conn()
            cur = conn.cursor()
            _execute(cur, "INSERT OR REPLACE INTO acca_tickets (id, created_at, combined_odds, legs, status, kind, stake) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                     (ticket_id, _now(), float(combined_odds), json.dumps(legs), status, kind, stake_json))
            _execute(cur, "DELETE FROM acca_legs WHERE ticket_id = %s", (ticket_id,))
            for leg in legs:
                _execute(cur, "INSERT INTO acca_legs (ticket_id, sport, league, match, selection, odds, probability, result, analysis, market, commence_time, sport_key, bookmaker) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (ticket_id, leg.get("sport", ""), leg.get("league", ""), leg.get("match", ""),
                          leg.get("selection", ""), float(leg.get("odds", 1.0)),
                          float(leg.get("probability", 0.0)), leg.get("result", "pending"),
                          str(leg.get("analysis", "") or "")[:2000], str(leg.get("market", "") or ""),
                          str(leg.get("commence_time", "") or ""), str(leg.get("sport_key", "") or ""),
                          str(leg.get("bookmaker", "") or "")))
            conn.commit()


def get_tickets(limit: int = 20, kind: str | None = None) -> list:
    init_schema()
    where = "" if kind not in ("daily", "weekly") else "WHERE kind = %s"
    params: tuple = () if kind not in ("daily", "weekly") else (kind,)
    with _lock:
        if _is_postgres():
            import psycopg2.extras  # type: ignore
            conn = _pg_conn()
            try:
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute(f"SELECT id, created_at, combined_odds, legs, status, kind, stake FROM acca_tickets {where} ORDER BY created_at DESC LIMIT %s", (*params, limit))
                rows = cur.fetchall()
                out = []
                for r in rows:
                    legs = r["legs"]
                    if isinstance(legs, str):
                        try:
                            legs = json.loads(legs)
                        except Exception:
                            legs = []
                    try:
                        stake = json.loads(r["stake"]) if isinstance(r["stake"], str) else (r["stake"] or {})
                    except Exception:
                        stake = {}
                    out.append({"id": r["id"], "created_at": r["created_at"],
                                "combined_odds": float(r["combined_odds"]), "legs": legs, "status": r["status"],
                                "kind": r.get("kind") or "daily", "stake": stake})
                _hydrate_leg_results(conn, out, _pg=True)
                return out
            finally:
                conn.close()
        else:
            conn = _sqlite_conn()
            cur = conn.cursor()
            _execute(cur, f"SELECT id, created_at, combined_odds, legs, status, kind, stake FROM acca_tickets {where} ORDER BY created_at DESC LIMIT %s", (*params, limit))
            out = []
            for r in cur.fetchall():
                try:
                    legs = json.loads(r["legs"])
                except Exception:
                    legs = []
                cols = r.keys()
                try:
                    stake = json.loads(r["stake"]) if "stake" in cols and isinstance(r["stake"], str) else {}
                except Exception:
                    stake = {}
                out.append({"id": r["id"], "created_at": r["created_at"],
                            "combined_odds": float(r["combined_odds"]), "legs": legs, "status": r["status"],
                            "kind": (r["kind"] if "kind" in cols else None) or "daily", "stake": stake})
            _hydrate_leg_results(conn, out, _pg=False)
            return out


def _hydrate_leg_results(conn, tickets: list, _pg: bool = False) -> None:
    """Overlay live acca_legs results onto the frozen legs JSON snapshot so
    /accas (and the frontend) shows Won/Lost per leg as the verifier works.
    Never raises."""
    try:
        ids = [t.get("id") for t in tickets if t.get("id")]
        if not ids:
            return
        ph = "%s" if _pg else "?"
        q = f"SELECT ticket_id, match, result, settle FROM acca_legs WHERE ticket_id IN ({','.join(ph for _ in ids)})"
        if _pg:
            import psycopg2.extras  # type: ignore
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(q, tuple(ids))
            rows = [dict(r) for r in cur.fetchall()]
        else:
            cur = conn.cursor()
            cur.execute(q.replace("%s", "?"), tuple(ids))
            rows = [dict(r) for r in cur.fetchall()]
        live: dict = {}
        for r in rows:
            try:
                live[(r.get("ticket_id"), str(r.get("match", "")))] = (r.get("result") or "pending",
                                                                          str(r.get("settle", "") or ""))
            except Exception:
                continue
        for t in tickets:
            try:
                for leg in (t.get("legs") or []):
                    lv = live.get((t.get("id"), str(leg.get("match", ""))))
                    if lv and lv[0] != "pending":
                        leg["result"] = lv[0]
                        if lv[1]:
                            leg["settle"] = lv[1]
            except Exception:
                continue
    except Exception:
        pass


def count_tickets() -> int:
    init_schema()
    with _lock:
        if _is_postgres():
            conn = _pg_conn()
            try:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM acca_tickets")
                return int(cur.fetchone()[0])
            finally:
                conn.close()
        else:
            conn = _sqlite_conn()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM acca_tickets")
            return int(cur.fetchone()[0])


def update_ticket_status(ticket_id: str, status: str) -> None:
    init_schema()
    with _lock:
        if _is_postgres():
            conn = _pg_conn()
            try:
                cur = conn.cursor()
                cur.execute("UPDATE acca_tickets SET status=%s WHERE id=%s", (status, ticket_id))
                conn.commit()
            finally:
                conn.close()
        else:
            conn = _sqlite_conn()
            cur = conn.cursor()
            _execute(cur, "UPDATE acca_tickets SET status=%s WHERE id=%s", (status, ticket_id))
            conn.commit()


def get_team_aliases() -> dict:
    """{variant: canonical} learned name pairs. Never raises."""
    try:
        init_schema()
        with _lock:
            if _is_postgres():
                conn = _pg_conn()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT variant, canonical FROM acca_team_aliases")
                    return {str(r[0]): str(r[1]) for r in cur.fetchall()}
                finally:
                    conn.close()
            conn = _sqlite_conn()
            cur = conn.cursor()
            cur.execute("SELECT variant, canonical FROM acca_team_aliases")
            return {str(r[0]): str(r[1]) for r in cur.fetchall()}
    except Exception:
        return {}


def save_team_alias(canonical: str, variant: str) -> None:
    """Persist one learned pair (upsert, bump hits). Never raises."""
    try:
        if not canonical or not variant:
            return
        init_schema()
        with _lock:
            if _is_postgres():
                conn = _pg_conn()
                try:
                    cur = conn.cursor()
                    cur.execute("INSERT INTO acca_team_aliases (variant, canonical, hits, updated_at) "
                                "VALUES (%s,%s,1,%s) ON CONFLICT (variant) DO UPDATE SET "
                                "canonical=EXCLUDED.canonical, hits=acca_team_aliases.hits+1, updated_at=EXCLUDED.updated_at",
                                (variant, canonical, _now()))
                    conn.commit()
                finally:
                    conn.close()
                return
            conn = _sqlite_conn()
            cur = conn.cursor()
            _execute(cur, "INSERT INTO acca_team_aliases (variant, canonical, hits, updated_at) VALUES (%s,%s,1,%s) "
                           "ON CONFLICT (variant) DO UPDATE SET canonical=excluded.canonical, hits=hits+1, updated_at=excluded.updated_at",
                     (variant, canonical, _now()))
            conn.commit()
    except Exception:
        pass


def update_leg_result(ticket_id: str, match: str, result: str, settle: str = "") -> None:
    init_schema()
    with _lock:
        if _is_postgres():
            conn = _pg_conn()
            try:
                cur = conn.cursor()
                try:
                    cur.execute("UPDATE acca_legs SET result=%s, settle=%s WHERE ticket_id=%s AND match=%s",
                                (result, str(settle or "")[:160], ticket_id, match))
                except Exception:
                    cur.execute("UPDATE acca_legs SET result=%s WHERE ticket_id=%s AND match=%s", (result, ticket_id, match))
                conn.commit()
            finally:
                conn.close()
        else:
            conn = _sqlite_conn()
            cur = conn.cursor()
            try:
                _execute(cur, "UPDATE acca_legs SET result=%s, settle=%s WHERE ticket_id=%s AND match=%s",
                         (result, str(settle or "")[:160], ticket_id, match))
            except Exception:
                _execute(cur, "UPDATE acca_legs SET result=%s WHERE ticket_id=%s AND match=%s", (result, ticket_id, match))
            conn.commit()


def prune_pending(kind: str, keep: int = 2) -> int:
    """Delete oldest pending tickets of a kind, keeping the newest `keep`.
    Settled (won/lost/dissolved) tickets are never touched. Returns deleted count."""
    init_schema()
    with _lock:
        if _is_postgres():
            conn = _pg_conn()
            try:
                cur = conn.cursor()
                cur.execute("SELECT id FROM acca_tickets WHERE kind=%s AND status='pending' "
                            "AND NOT EXISTS (SELECT 1 FROM acca_legs l WHERE l.ticket_id=acca_tickets.id "
                            "AND l.result IN ('won','lost')) "
                            "ORDER BY created_at DESC OFFSET %s", (kind, int(keep)))
                ids = [r[0] for r in cur.fetchall()]
                for tid in ids:
                    cur.execute("DELETE FROM acca_legs WHERE ticket_id=%s", (tid,))
                    cur.execute("DELETE FROM acca_tickets WHERE id=%s", (tid,))
                conn.commit()
                return len(ids)
            finally:
                conn.close()
        else:
            conn = _sqlite_conn()
            cur = conn.cursor()
            _execute(cur, "SELECT id FROM acca_tickets WHERE kind=%s AND status='pending' "
                          "AND NOT EXISTS (SELECT 1 FROM acca_legs l WHERE l.ticket_id=acca_tickets.id "
                          "AND l.result IN ('won','lost')) "
                          "ORDER BY created_at DESC LIMIT 1000000 OFFSET %s", (kind, int(keep)))
            ids = [r[0] for r in cur.fetchall()]
            for tid in ids:
                _execute(cur, "DELETE FROM acca_legs WHERE ticket_id=%s", (tid,))
                _execute(cur, "DELETE FROM acca_tickets WHERE id=%s", (tid,))
            conn.commit()
            return len(ids)


def set_ticket_status(ticket_id: str, status: str) -> None:
    init_schema()
    with _lock:
        if _is_postgres():
            conn = _pg_conn()
            try:
                cur = conn.cursor()
                cur.execute("UPDATE acca_tickets SET status=%s WHERE id=%s", (status, ticket_id))
                conn.commit()
            finally:
                conn.close()
        else:
            conn = _sqlite_conn()
            cur = conn.cursor()
            _execute(cur, "UPDATE acca_tickets SET status=%s WHERE id=%s", (status, ticket_id))
            conn.commit()


def get_legs(ticket_id: str) -> list:
    init_schema()
    with _lock:
        if _is_postgres():
            import psycopg2.extras  # type: ignore
            conn = _pg_conn()
            try:
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("SELECT * FROM acca_legs WHERE ticket_id=%s ORDER BY id", (ticket_id,))
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()
        else:
            conn = _sqlite_conn()
            cur = conn.cursor()
            _execute(cur, "SELECT * FROM acca_legs WHERE ticket_id=%s ORDER BY id", (ticket_id,))
            return [dict(r) for r in cur.fetchall()]


# ---- results / accuracy (learning) ----

def record_verification(ticket_id: str, won: bool, correct: int, total: int, details: dict | None = None) -> None:
    init_schema()
    with _lock:
        if _is_postgres():
            conn = _pg_conn()
            try:
                cur = conn.cursor()
                cur.execute("INSERT INTO acca_results (ticket_id, checked_at, won, correct_legs, total_legs, details) VALUES (%s,%s,%s,%s,%s,%s)",
                            (ticket_id, _now(), 1 if won else 0, correct, total, json.dumps(details or {})))
                cur.execute("UPDATE acca_tickets SET status=%s WHERE id=%s", ("won" if won else "lost", ticket_id))
                conn.commit()
            finally:
                conn.close()
        else:
            conn = _sqlite_conn()
            cur = conn.cursor()
            _execute(cur, "INSERT INTO acca_results (ticket_id, checked_at, won, correct_legs, total_legs, details) VALUES (%s,%s,%s,%s,%s,%s)",
                     (ticket_id, _now(), 1 if won else 0, correct, total, json.dumps(details or {})))
            _execute(cur, "UPDATE acca_tickets SET status=%s WHERE id=%s", ("won" if won else "lost", ticket_id))
            conn.commit()


def record_accuracy(total: int, won: int, notes: str = "") -> float:
    acc = (won / total) if total else 0.0
    init_schema()
    with _lock:
        if _is_postgres():
            conn = _pg_conn()
            try:
                cur = conn.cursor()
                cur.execute("INSERT INTO acca_accuracy (checked_at, total_tickets, won_tickets, accuracy, notes) VALUES (%s,%s,%s,%s,%s)",
                            (_now(), total, won, acc, notes))
                conn.commit()
            finally:
                conn.close()
        else:
            conn = _sqlite_conn()
            cur = conn.cursor()
            _execute(cur, "INSERT INTO acca_accuracy (checked_at, total_tickets, won_tickets, accuracy, notes) VALUES (%s,%s,%s,%s,%s)",
                     (_now(), total, won, acc, notes))
            conn.commit()
    return acc


def get_accuracy_stats() -> dict:
    init_schema()
    with _lock:
        if _is_postgres():
            import psycopg2.extras  # type: ignore
            conn = _pg_conn()
            try:
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(won),0) AS w FROM acca_results")
                r = cur.fetchone()
                n, w = int(r["n"] or 0), int(r["w"] or 0)
                return {"verified_tickets": n, "won_tickets": w, "accuracy": (w / n if n else 0.0)}
            finally:
                conn.close()
        else:
            conn = _sqlite_conn()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(won),0) AS w FROM acca_results")
            r = cur.fetchone()
            n, w = int(r["n"] or 0), int(r["w"] or 0)
            return {"verified_tickets": n, "won_tickets": w, "accuracy": (w / n if n else 0.0)}


if __name__ == "__main__":
    init_schema()
    print("schema ok, tickets:", count_tickets(), "accuracy:", get_accuracy_stats())
