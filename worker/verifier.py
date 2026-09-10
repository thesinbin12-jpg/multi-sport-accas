"""verifier.py — Result verification via Odds API scores + accuracy learning."""
from datetime import datetime, timezone

try:
    from scanner import OddsScanner, SCAN_CONFIG
except ImportError:
    from worker.scanner import OddsScanner, SCAN_CONFIG  # type: ignore

try:
    import db
except ImportError:
    from worker import db  # type: ignore


def _all_sport_keys() -> list:
    keys = []
    for group in SCAN_CONFIG.values():
        keys.extend(group)
    return keys


def verify_all_pending(days_from: int = 3) -> dict:
    """Check pending tickets against recent scores. Returns summary."""
    db.init_schema()
    tickets = [t for t in db.get_tickets(limit=50) if t.get("status") == "pending"]
    if not tickets:
        return {"checked": 0, "won": 0, "lost": 0, "pending": 0}

    scanner = OddsScanner()
    # Fetch scores once per sport group (cache)
    scores_cache: dict[str, list] = {}
    won = lost = still_pending = 0

    for t in tickets:
        legs = db.get_legs(t["id"]) or t.get("legs", [])
        correct = 0
        decided = 0
        for leg in legs:
            match = leg.get("match", "")
            res = _check_leg_result(match, scanner, scores_cache, days_from)
            if res in ("won", "lost"):
                decided += 1
                db.update_leg_result(t["id"], match, res)
                if res == "won":
                    correct += 1
        total = len(legs)
        if decided < total:
            still_pending += 1
            continue
        ticket_won = (correct == total and total > 0)
        db.record_verification(t["id"], ticket_won, correct, total,
                               {"checked_at": datetime.now(timezone.utc).isoformat()})
        if ticket_won:
            won += 1
        else:
            lost += 1

    stats = db.get_accuracy_stats()
    db.record_accuracy(stats["verified_tickets"], stats["won_tickets"], notes="auto verify")
    return {"checked": won + lost, "won": won, "lost": lost, "pending": still_pending}


def _check_leg_result(match: str, scanner: OddsScanner, cache: dict, days_from: int) -> str:
    """Try to resolve 'Home vs Away' to a completed score. Returns won/lost/pending."""
    if " vs " not in match:
        return "pending"
    home, away = [p.strip().lower() for p in match.split(" vs ", 1)]
    for sk in _all_sport_keys():
        if sk not in cache:
            try:
                cache[sk] = scanner.get_scores(sk, days_from=days_from) or []
            except Exception:
                cache[sk] = []
        for g in cache[sk]:
            h = str(g.get("home_team", "")).lower()
            a = str(g.get("away_team", "")).lower()
            if h == home and a == away and g.get("completed"):
                scores = {s.get("name", "").lower(): s.get("score") for s in g.get("scores", []) or []}
                hs = _num(scores.get(h))
                aws = _num(scores.get(a))
                if hs is None or aws is None:
                    return "pending"
                # legs are favourite-pick; selection check needs stored selection — caller handles won/lost via scores only when draw-aware.
                # Without stored selection here we mark decided only if a winner exists; main verify uses selection below.
                return "decided"  # placeholder, refined by verify_ticket_with_selection
    return "pending"


def verify_ticket_with_selection(ticket_id: str, days_from: int = 3) -> dict:
    """Precise per-ticket verification using stored selection + scores."""
    legs = db.get_legs(ticket_id)
    scanner = OddsScanner()
    cache: dict[str, list] = {}
    correct = 0
    decided = 0
    for leg in legs:
        outcome = _resolve_match_winner(leg.get("match", ""), scanner, cache, days_from)
        if outcome is None:
            continue
        decided += 1
        sel = str(leg.get("selection", "")).lower()
        winner = outcome.lower()  # team name or 'draw'
        won_leg = (sel == winner) or (winner in sel) or (sel in winner)
        db.update_leg_result(ticket_id, leg.get("match", ""), "won" if won_leg else "lost")
        if won_leg:
            correct += 1
    total = len(legs)
    if decided < total:
        return {"ticket_id": ticket_id, "status": "pending", "correct": correct, "total": total}
    ticket_won = (correct == total and total > 0)
    db.record_verification(ticket_id, ticket_won, correct, total, {})
    return {"ticket_id": ticket_id, "status": "won" if ticket_won else "lost",
            "correct": correct, "total": total}


def _resolve_match_winner(match: str, scanner: OddsScanner, cache: dict, days_from: int):
    if " vs " not in match:
        return None
    home, away = [p.strip().lower() for p in match.split(" vs ", 1)]
    for sk in _all_sport_keys():
        if sk not in cache:
            try:
                cache[sk] = scanner.get_scores(sk, days_from=days_from) or []
            except Exception:
                cache[sk] = []
        for g in cache[sk]:
            h = str(g.get("home_team", "")).lower()
            a = str(g.get("away_team", "")).lower()
            if h == home and a == away and g.get("completed"):
                scores = {str(s.get("name", "")).lower(): s.get("score") for s in g.get("scores", []) or []}
                hs = _num(scores.get(h))
                aws = _num(scores.get(a))
                if hs is None or aws is None:
                    return None
                if hs > aws:
                    return g.get("home_team", "")
                if aws > hs:
                    return g.get("away_team", "")
                return "draw"
    return None


def _num(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    print(verify_all_pending())
