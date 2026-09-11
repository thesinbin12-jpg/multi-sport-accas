"""
logbuf.py — In-memory ring-buffer log handler + GET /logs support.

Render offers no public runtime-logs API, so the worker exposes its own recent
logs (last N lines, secret-gated like /build). Attach once at startup.
"""

import logging
from collections import deque

BUF = deque(maxlen=400)


class RingHandler(logging.Handler):
    def emit(self, record):
        try:
            BUF.append(self.format(record))
        except Exception:
            pass


def attach(level=logging.INFO):
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                            datefmt="%H:%M:%S")
    h = RingHandler()
    h.setFormatter(fmt)
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.addHandler(h)
        lg.setLevel(level)
    return h


def tail(n=200):
    try:
        n = max(1, min(int(n or 200), 400))
    except Exception:
        n = 200
    return list(BUF)[-n:]
