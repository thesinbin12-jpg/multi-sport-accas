"""main.py — FastAPI worker: POST /build, GET /status, GET /health (+ /accas, /verify).

CANONICAL SEQUENCES (draw-predictor pattern, adapted multi-sport acca — keep this order):
MORNING (build): db.ping -> scan (Betika primary, Smarkets 2nd, Odds API guarded)
  -> value-zone sort -> learner blocked-leagues -> kickoff window -> analyst swarm
  (history FDO + news Tavily->DDG + 9 personas + synthesizer, finalists only)
  -> rank/stake meta-agent (_agentic_stake) -> save -> status.
EVENING (learn, cron 01:00 Accra): verify_all_pending (Odds API scores +
  football-data.org fallback) -> learner patterns -> LLM reason/debrief -> persist.
"""
import os
import threading
import time
import traceback
from datetime import datetime, timezone

from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
import db

WORKER_SECRET = os.environ.get("WORKER_SECRET", "")


def _authed(request) -> bool:
    """Shared-secret gate for trigger endpoints. Open only when no secret set (local dev).
    Accepts x-accas-secret header (frontend, curl) or HTTP Basic password
    (cron-job.org, whose API persists auth but not custom headers)."""
    if not WORKER_SECRET:
        return True
    try:
        if request.headers.get("x-accas-secret", "") == WORKER_SECRET:
            return True
        import base64 as _b64
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                decoded = _b64.b64decode(auth[6:]).decode("utf-8", "ignore")
            except Exception:
                decoded = ""
            if ":" in decoded and decoded.rsplit(":", 1)[1] == WORKER_SECRET:
                return True
    except Exception:
        pass
    return False

app = FastAPI(title="multi-sport-accas worker")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://accas-roan.vercel.app", "http://localhost:3000"],
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
    max_legs: int | None = None  # 2..20, capped server-side
    use_ai: bool = True
    max_credits: int | None = None
    kind: str = "daily"  # daily | weekly


def _run_build(max_legs, use_ai, max_credits, kind="daily"):
    kind = kind if kind in ("daily", "weekly") else "daily"
    _set_state(status="running", started_at=datetime.now(timezone.utc).isoformat(),
               message=f"scanning odds for {kind} slip…", error=None)
    try:
        db.ping()  # fail fast on bad/rotated DB creds BEFORE any scan/API spend
        import builder

        def progress(msg: str):
            _set_state(message=msg)
            try:
                import logging as _lg
                _lg.getLogger("acca").info(str(msg)[:220])
            except Exception:
                pass

        _detail: dict = {}
        tickets = builder.build_and_save(max_legs=max_legs, use_ai=use_ai,
                                         max_credits=max_credits, progress_cb=progress, kind=kind,
                                         detail=_detail)
        try:
            _kinds = [(t.get("stake") or {}).get("tier", "value") for t in tickets]
        except Exception:
            _kinds = []
        try:
            _main_id = next((t["id"] for t in tickets
                             if (t.get("stake") or {}).get("tier", "value") != "dream"), None)
        except Exception:
            _main_id = None
        with _state_lock:
            try:
                _bits = []
                if _detail.get("steady_legs"):
                    _bits.append(f"steady {_detail.get('steady_comb')}x ({_detail.get('steady_legs')} legs)")
                if _detail.get("dream_legs"):
                    _bits.append(f"dreamer {_detail.get('dream_comb')}x ({_detail.get('dream_legs')} legs)")
                _det = (" — " + " + ".join(_bits)) if _bits else ""
                if not _bits and _detail.get("kind") == "daily":
                    _det = (f" — steady cands {_detail.get('steady_cands', '?')}, "
                            f"dreamer cands {_detail.get('dream_cands', '?')}")
            except Exception:
                _det = ""
            BUILD_STATE.update(status="done", finished_at=datetime.now(timezone.utc).isoformat(),
                               tickets_built=len(tickets),
                               last_ticket_id=_main_id or (tickets[0]["id"] if tickets else None),
                               last_build=_detail,
                               message=(f"built {len(tickets)} ticket(s)" + (" (value+dream)" if "dream" in _kinds else "") + _det if tickets
                                        else "no legs found — try again later"))
    except Exception as e:
        _set_state(status="error", finished_at=datetime.now(timezone.utc).isoformat(),
                   error=f"{e}\n{traceback.format_exc(limit=3)}",
                   message=f"build failed: {e}")


def _self_ping_loop():
    """Layer 2 keepalive: ping our own /health every 10 min so Render free
    never idles. Runs only on Render (RENDER_EXTERNAL_URL is auto-set there,
    absent locally). Layer 1 is the cron-job.org keepalive job."""
    import urllib.request
    base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    url = (base or "https://multisportaccas.onrender.com") + "/health"
    while True:
        try:
            time.sleep(600)
            urllib.request.urlopen(url, timeout=20).read(16)
        except Exception:
            pass


@app.on_event("startup")
def _startup():
    try:
        import logbuf
    except ImportError:
        from worker import logbuf  # type: ignore
    logbuf.attach()
    if os.environ.get("RENDER_EXTERNAL_URL"):
        threading.Thread(target=_self_ping_loop, daemon=True).start()


@app.get("/diag")
def diag(request: Request, home: str = "", away: str = "", league: str = "", date: str = ""):
    """Trace score-source resolution for one fixture (agentic observability)."""
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    trace: dict = {"fixture": f"{home} vs {away}", "league": league, "date": date}
    trace["serving"] = os.environ.get("RENDER_GIT_COMMIT", "local")[:9]
    try:
        import socket as _sk
        trace["instance"] = _sk.gethostname()
    except Exception:
        pass
    try:
        try:
            import espn as _espn
        except ImportError:
            from worker import espn as _espn  # type: ignore
        slugs = _espn._slugs_for(league) or ["eng.1", "esp.1", "usa.nwsl"]
        trace["espn_slugs"] = slugs
        try:
            base = datetime.fromisoformat(date[:10]).date() if date else datetime.now(timezone.utc).date()
        except Exception:
            base = datetime.now(timezone.utc).date()
        pls = []
        for slug in slugs[:4]:
            try:
                evs = _espn._day_scores(slug, base)
                names = [(h, a) for h, a, _hs, _aws in evs]
                pls.append({"slug": slug, "events": len(evs),
                            "same_date": [f"{h} vs {a}" for h, a in names
                                         if home.lower()[:4] in h.lower() or away.lower()[:4] in a.lower()]})
            except Exception as e:
                pls.append({"slug": slug, "error": str(e)[:120]})
        trace["espn"] = pls
        trace["espn_find"] = _espn.find_score(home, away, league_hint=league, ref_date=base)
    except Exception as e:
        trace["espn_error"] = str(e)[:200]
    try:
        tid = request.query_params.get("ticket", "")
        if tid:
            try:
                import verifier as _ver2
            except ImportError:
                from worker import verifier as _ver2  # type: ignore
            trace["ticket_verify"] = _ver2.verify_ticket_with_selection(tid, days_from=5)
    except Exception as e:
        trace["ticket_error"] = str(e)[:300]
    try:
        try:
            from fotmob import _fm_day
        except ImportError:
            from worker.fotmob import _fm_day  # type: ignore
        trace["fotmob_day_size"] = len(_fm_day(base))
    except Exception as e:
        trace["fotmob_error"] = str(e)[:200]
    try:
        try:
            import verifier as _ver
        except ImportError:
            from worker import verifier as _ver  # type: ignore
        from scanner import OddsScanner as _OS
        _leg = {"match": f"{home} vs {away}", "league": league,
                "commence_time": date + "T00:00:00Z" if date else "",
                "selection": "x", "result": "pending"}
        _sc = _ver._resolve_score(f"{home} vs {away}", _OS(), {}, 5, _leg)
        trace["verify_resolve"] = {"score": _sc, "src": _leg.get("_src")}
    except Exception as e:
        trace["verify_error"] = str(e)[:300]
    return {"ok": True, "trace": trace}


@app.get("/logs")
def logs(request: Request, tail: int = 200):
    """Recent worker log lines (self-served; Render has no public logs API)."""
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        import logbuf
    except ImportError:
        from worker import logbuf  # type: ignore
    return {"ok": True, "lines": logbuf.tail(tail)}


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
def build(req: BuildRequest, background: BackgroundTasks, request: Request):
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
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
def verify(background: BackgroundTasks, request: Request):
    """Async like /learn (Vercel kills long calls): runs in background,
    watch public /status + last_verify for the summary."""
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    with _state_lock:
        if BUILD_STATE.get("status") == "running":
            return {"ok": False, "error": "busy: " + str(BUILD_STATE.get("message", ""))}
    _set_state(status="running", started_at=datetime.now(timezone.utc).isoformat(),
               message="verify queued…", error=None)
    background.add_task(_run_verify)
    return {"ok": True, "message": "verify started"}


def _run_verify():
    try:
        try:
            import verifier
        except ImportError:
            from worker import verifier  # type: ignore

        def progress(msg: str):
            _set_state(message=msg)
            try:
                import logging as _lg
                _lg.getLogger("acca").info(str(msg)[:220])
            except Exception:
                pass

        summary = verifier.verify_all_pending(progress_cb=progress)
        with _state_lock:
            BUILD_STATE.update(status="done", finished_at=datetime.now(timezone.utc).isoformat(),
                               last_verify=summary,
                               message=(f"verify: {summary.get('checked', 0)} checked, "
                                        f"{summary.get('won', 0)} won, {summary.get('lost', 0)} lost, "
                                        f"{summary.get('pending', 0)} still pending"))
    except Exception as e:
        _set_state(status="error", finished_at=datetime.now(timezone.utc).isoformat(),
                   error=f"{e}\n{traceback.format_exc(limit=3)}",
                   message=f"verify failed: {e}")


@app.post("/learn")
def learn(background: BackgroundTasks, request: Request):
    """Nightly learner, fire-and-forget (Vercel kills long calls): runs in
    background including any weekly rebuild, watch /status + /insights."""
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    _set_state(status="running", started_at=datetime.now(timezone.utc).isoformat(),
               message="nightly learn queued…", error=None)
    background.add_task(_run_learn)
    return {"ok": True, "message": "nightly learn started"}


def _run_learn():
    try:
        try:
            import learner
        except ImportError:
            from worker import learner  # type: ignore
        out = learner.nightly_learn()
        spec = (out.get("summary") or {}).get("weekly_rebuild") or out.get("weekly_rebuild")
        if spec:
            ceiling = int(spec.get("max_legs", 5))
            try:
                import builder as _b2
            except ImportError:
                from worker import builder as _b2  # type: ignore
            _b2.build_and_save(max_legs=ceiling, use_ai=True, max_credits=None, kind="weekly")
            out["weekly_rebuild_done"] = True
        _set_state(status="done", finished_at=datetime.now(timezone.utc).isoformat(),
                   message="nightly learn done", error=None)
    except Exception as e:
        _set_state(status="error", finished_at=datetime.now(timezone.utc).isoformat(),
                   error="%s\n%s" % (e, traceback.format_exc(limit=3)),
                   message="learn failed: %s" % e)


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
