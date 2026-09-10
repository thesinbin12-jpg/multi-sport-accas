"""main.py — FastAPI worker: POST /build, GET /status, GET /health (+ /accas, /verify)."""
import threading
import traceback
from datetime import datetime, timezone

from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
import db

app = FastAPI(title="multi-sport-accas worker")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BUILD_STATE = {
    "status": "idle",  # idle | running | done | error
    "started_at": None,
    "finished_at": None,
    "message": "never built",
    "tickets_built": 0,
    "last_ticket_id": None,
    "error": None,
}
_state_lock = threading.Lock()


def _set_state(**kw):
    with _state_lock:
        BUILD_STATE.update(kw)


class BuildRequest(BaseModel):
    max_legs: int | None = None
    use_ai: bool = True
    max_credits: int | None = None
    kind: str = "daily"  # daily | weekly


def _run_build(max_legs, use_ai, max_credits, kind="daily"):
    kind = kind if kind in ("daily", "weekly") else "daily"
    _set_state(status="running", started_at=datetime.now(timezone.utc).isoformat(),
               message=f"scanning odds for {kind} slip…", error=None)
    try:
        import builder

        def progress(msg: str):
            _set_state(message=msg)

        tickets = builder.build_and_save(max_legs=max_legs, use_ai=use_ai,
                                         max_credits=max_credits, progress_cb=progress, kind=kind)
        with _state_lock:
            BUILD_STATE.update(status="done", finished_at=datetime.now(timezone.utc).isoformat(),
                               tickets_built=len(tickets),
                               last_ticket_id=tickets[0]["id"] if tickets else None,
                               message=(f"built {len(tickets)} ticket(s)" if tickets
                                        else "no legs found — try again later"))
    except Exception as e:
        _set_state(status="error", finished_at=datetime.now(timezone.utc).isoformat(),
                   error=f"{e}\n{traceback.format_exc(limit=3)}",
                   message=f"build failed: {e}")


@app.get("/health")
def health():
    return {"ok": True, "service": "multi-sport-accas-worker", "env": config.as_dict()}


@app.get("/status")
def status():
    with _state_lock:
        s = dict(BUILD_STATE)
    try:
        n = db.count_tickets()
        s["tickets_in_db"] = n
        s["accuracy"] = db.get_accuracy_stats()
        if s.get("message") == "never built" and n > 0:
            s["message"] = f"{n} slip(s) on file"
    except Exception as e:
        s["db_error"] = str(e)
    return s


@app.get("/accas")
def accas(limit: int = 20, kind: str | None = None):
    try:
        tickets = db.get_tickets(limit=limit, kind=kind)
        return {"ok": True, "count": len(tickets), "tickets": tickets,
                "accuracy": db.get_accuracy_stats()}
    except Exception as e:
        return {"ok": False, "error": str(e), "tickets": []}


@app.post("/build")
def build(req: BuildRequest, background: BackgroundTasks):
    with _state_lock:
        if BUILD_STATE["status"] == "running":
            return {"ok": False, "error": "build already running", "state": dict(BUILD_STATE)}
    kind = req.kind if req.kind in ("daily", "weekly") else "daily"
    background.add_task(_run_build, req.max_legs or config.MAX_LEGS_PER_ACCA,
                        req.use_ai, req.max_credits or config.MAX_CREDITS_PER_SCAN, kind)
    _set_state(status="running", message=f"{kind} build queued…", error=None,
               started_at=datetime.now(timezone.utc).isoformat())
    return {"ok": True, "message": f"{kind} build started"}


@app.post("/verify")
def verify():
    try:
        import verifier
        summary = verifier.verify_all_pending()
        return {"ok": True, **summary}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/learn")
def learn():
    """Nightly learner: verify + find patterns + save debrief. Safe daily."""
    try:
        try:
            import learner
        except ImportError:
            from worker import learner  # type: ignore
        return learner.nightly_learn()
    except Exception as e:
        return {"ok": False, "error": f"{e}\n{traceback.format_exc(limit=3)}"}


@app.get("/insights")
def insights():
    """Latest debrief + accuracy + strategy for the frontend."""
    try:
        try:
            import learner
        except ImportError:
            from worker import learner  # type: ignore
        return learner.latest_insights()
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
