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
        result TEXT DEFAULT 'pending'
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
                        "INSERT INTO acca_legs (ticket_id, sport, league, match, selection, odds, probability, result) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (ticket_id, leg.get("sport", ""), leg.get("league", ""), leg.get("match", ""),
                         leg.get("selection", ""), float(leg.get("odds", 1.0)),
                         float(leg.get("probability", 0.0)), leg.get("result", "pending")),
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
                _execute(cur, "INSERT INTO acca_legs (ticket_id, sport, league, match, selection, odds, probability, result) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                         (ticket_id, leg.get("sport", ""), leg.get("league", ""), leg.get("match", ""),
                          leg.get("selection", ""), float(leg.get("odds", 1.0)),
                          float(leg.get("probability", 0.0)), leg.get("result", "pending")))
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
            return out


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


def update_leg_result(ticket_id: str, match: str, result: str) -> None:
    init_schema()
    with _lock:
        if _is_postgres():
            conn = _pg_conn()
            try:
                cur = conn.cursor()
                cur.execute("UPDATE acca_legs SET result=%s WHERE ticket_id=%s AND match=%s", (result, ticket_id, match))
                cur.execute("UPDATE acca_tickets SET status=%s WHERE id=%s", (result if False else "checked", ticket_id))
                conn.commit()
            finally:
                conn.close()
        else:
            conn = _sqlite_conn()
            cur = conn.cursor()
            _execute(cur, "UPDATE acca_legs SET result=%s WHERE ticket_id=%s AND match=%s", (result, ticket_id, match))
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
