"""
websearch.py — Shared free web-search stack (server-side, no paid keys required).

Backends (graceful order): Brave via curl_cffi (best quality) -> DDG lite
-> Tavily (only when explicitly allowed; paid per call).
Also: settle_parse() extracts a scoreline for a fixture from result texts,
orientation-checked and conservative (returns None instead of guessing).
"""

import re
import html
import time

_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
       "Chrome/120.0 Mobile Safari/537.36")


def _sess():
    try:
        from curl_cffi import requests as _cr
        return _cr.Session(impersonate="chrome120")
    except ImportError:
        return None


def _rec(source, ok, ms=0, err=""):
    try:
        try:
            from learner import source_record
        except ImportError:
            from worker.learner import source_record  # type: ignore
        source_record(source, ok, ms, err)
    except Exception:
        pass


def brave_search(query, max_results=5, timeout=20):
    out = []
    import time as _t
    _t0 = _t.time()
    try:
        time.sleep(1)
        s = _sess()
        if s is None:
            _rec("web-brave", False, 0, "no curl_cffi")
            return out
        r = s.get("https://search.brave.com/search", params={"q": query}, timeout=timeout)
        if r.status_code != 200 or 'data-type="web"' not in r.text:
            _rec("web-brave", False, int((_t.time() - _t0) * 1000), "status %s" % getattr(r, "status_code", "?"))
            return out
        for b in r.text.split('data-type="web"')[1:]:
            tm = re.search(r'title="([^"]{10,160})"', b)
            dm = re.search(r'class="[^"]*snippet-description[^"]*"[^>]*>(.*?)</', b, re.DOTALL)
            title = html.unescape(tm.group(1)).strip() if tm else ""
            desc = html.unescape(re.sub(r"<[^>]+>", " ", dm.group(1))) if dm else ""
            desc = re.sub(r"\s+", " ", desc).strip()[:250]
            if title:
                out.append({"title": title, "snippet": desc, "source": "brave"})
            if len(out) >= max_results:
                break
        _rec("web-brave", bool(out), int(__import__("time").time() - _t0), "" if out else "empty")
    except Exception as e:
        try:
            _rec("web-brave", False, 0, str(e)[:120])
        except Exception:
            pass
    return out


def ddg_search(query, max_results=5, timeout=20):
    out = []
    try:
        import requests as _rq
        r = _rq.post("https://lite.duckduckgo.com/lite/", data={"q": query},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        if r.status_code != 200:
            return out
        titles = re.findall(r'class="result-link"[^>]*>(.*?)</a', r.text)
        snips = re.findall(r'class="result-snippet"[^>]*>(.*?)</td', r.text)
        for i, t in enumerate(titles[:max_results]):
            title = html.unescape(re.sub(r"<.*?>", "", t)).strip()
            sn = html.unescape(re.sub(r"<.*?>", "", snips[i])).strip() if i < len(snips) else ""
            if title:
                out.append({"title": title, "snippet": sn[:250], "source": "ddg"})
    except Exception:
        pass
    return out


def tavily_search(query, max_results=4, timeout=20):
    out = []
    try:
        import os as _os
        import requests as _rq
        key = _os.environ.get("TAVILY_API_KEY", "")
        if not key:
            return out
        r = _rq.post("https://api.tavily.com/search",
                     json={"api_key": key, "query": query, "max_results": max_results,
                           "include_answer": True}, timeout=timeout)
        if r.status_code != 200:
            return out
        d = r.json()
        if d.get("answer"):
            out.append({"title": "tavily-answer", "snippet": str(d["answer"])[:400],
                        "source": "tavily"})
        for x in (d.get("results") or [])[:max_results]:
            out.append({"title": str(x.get("title", ""))[:120],
                        "snippet": str(x.get("content", ""))[:300], "source": "tavily"})
    except Exception:
        pass
    return out


def search(query, max_results=5, allow_tavily=False, timeout=20):
    """Free-first unified search. Returns [{title, snippet, source}]. Never raises."""
    out = brave_search(query, max_results, timeout)
    if len(out) < 2:
        out = out + [r for r in ddg_search(query, max_results, timeout)
                     if r["title"] not in {x["title"] for x in out}]
    if allow_tavily and len(out) < 2:
        out = out + tavily_search(query, max_results, timeout)
    return out[:max_results]


_CLUBWORDS = {"fc", "ac", "sc", "fk", "ifk", "sk", "bk", "as", "ss", "us", "cd", "ud",
                "cf", "afc", "united", "city", "town", "rovers", "wanderers", "athletic",
                "sporting", "real", "club", "de", "la", "le", "les", "al", "el",
                "ii", "iii", "u21", "u23", "reserves", "reserve", "youth"}


def _keyw(name):
    toks = [t for t in re.split(r"[\s.\-']+", str(name or "").lower()) if t and t not in _CLUBWORDS]
    return toks[-1] if toks else ""


def settle_parse(texts, home, away):
    """Extract (hs, aws) for home vs away from free text. Conservative:
    needs home keyword BEFORE a scoreline and away keyword AFTER it
    (standard report order), else None."""
    hl, al = _keyw(home), _keyw(away)
    if not hl or not al:
        return None
    for t in texts or []:
        blob = str(t or "")
        low = blob.lower()
        for m in re.finditer(r"(\d{1,2})\s*[-–:]\s*(\d{1,2})", blob):
            s = m.start()
            before, after = low[max(0, s - 120):s], low[s:s + 120]
            if hl in before and al in after:
                try:
                    return int(m.group(1)), int(m.group(2))
                except Exception:
                    continue
    return None
