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

_SKIP_UNTIL: dict = {}  # backend -> epoch: skip fast while chronically failing


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
        r = None
        for _att in range(2):  # 429s are routine from datacenters; one backoff retry
            try:
                r = s.get("https://search.brave.com/search", params={"q": query}, timeout=timeout)
                if r.status_code == 200 and 'data-type="web"' in r.text:
                    break
            except Exception:
                r = None
            time.sleep(4)
        if r is None or r.status_code != 200 or 'data-type="web"' not in r.text:
            _rec("web-brave", False, int((_t.time() - _t0) * 1000), "status %s" % getattr(r, "status_code", "?"))
            return out
        for b in r.text.split('data-type="web"')[1:]:
            tm = re.search(r'title="([^"]{10,160})"', b)
            dm = re.search(r'class="[^"]*snippet-description[^"]*"[^>]*>(.*?)</', b, re.DOTALL)
            title = html.unescape(tm.group(1)).strip() if tm else ""
            desc = html.unescape(re.sub(r"<[^>]+>", " ", dm.group(1))) if dm else ""
            desc = re.sub(r"\s+", " ", desc).strip()[:250]
            if title:
                _u = ""
                try:
                    for _h in re.findall(r'href="(https?://[^"]+)"', b):
                        if "brave.com" not in _h and "search.brave" not in _h:
                            _u = html.unescape(_h).strip()
                            break
                except Exception:
                    _u = ""
                out.append({"title": title, "snippet": desc, "source": "brave", "url": _u})
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
    import time as _t
    _t0 = _t.time()
    try:
        body = ""
        s = _sess()  # impersonated session first (datacenter-friendly)
        if s is not None:
            try:
                r = s.post("https://lite.duckduckgo.com/lite/", data={"q": query},
                           headers={"User-Agent": _UA}, timeout=timeout)
                if r.status_code == 200:
                    body = r.text
            except Exception:
                body = ""
        if not body:
            import requests as _rq
            r = _rq.post("https://lite.duckduckgo.com/lite/", data={"q": query},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            if r.status_code == 200:
                body = r.text
        if not body:
            _rec("web-ddg", False, int((_t.time() - _t0) * 1000), "no body")
            return out
        links = re.findall(r"<a[^>]*href=['\"](https?://[^'\"]+)['\"][^>]*class=['\"]result-link['\"]", body)
        titles = re.findall(r"class=['\"]result-link['\"][^>]*>(.*?)</a", body)
        snips = re.findall(r"class=['\"]result-snippet['\"][^>]*>(.*?)</td", body)
        for i, t in enumerate(titles[:max_results]):
            title = html.unescape(re.sub(r"<.*?>", "", t)).strip()
            sn = html.unescape(re.sub(r"<.*?>", "", snips[i])).strip() if i < len(snips) else ""
            url = links[i].strip() if i < len(links) else ""
            if url.startswith("https://duckduckgo.com/"):
                url = ""  # internal redirect, not fetchable content
            if title:
                out.append({"title": title, "snippet": sn[:250], "source": "ddg", "url": url})
        _rec("web-ddg", bool(out), int((_t.time() - _t0) * 1000), "" if out else "empty")
    except Exception as e:
        try:
            _rec("web-ddg", False, 0, str(e)[:120])
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
        _rec("web-tavily", bool(out), 0, "" if out else "empty")
    except Exception as e:
        try:
            _rec("web-tavily", False, 0, str(e)[:120])
        except Exception:
            pass
    return out


def _backend_ok(name):
    """False when a backend is chronically failing (skip fast, save latency),
    with a 30-min re-probe so recoveries are picked up. Never raises."""
    import time as _tt
    try:
        if _SKIP_UNTIL.get(name, 0) > _tt.time():
            return False
        try:
            from learner import source_usable as _su
        except ImportError:
            from worker.learner import source_usable as _su  # type: ignore
        if not _su(name, min_rate=0.15, min_n=5):
            _SKIP_UNTIL[name] = _tt.time() + 1800
            return False
    except Exception:
        pass
    return True


def search(query, max_results=5, allow_tavily=False, timeout=20):
    """Free-first unified search. Returns [{title, snippet, source, url}]. Never raises.
    Chronically-failing backends are skipped fast (30-min re-probe); Tavily
    only when explicitly allowed (paid quota)."""
    out = brave_search(query, max_results, timeout) if _backend_ok("web-brave") else []
    if len(out) < 2 and _backend_ok("web-ddg"):
        out = out + [r for r in ddg_search(query, max_results, timeout)
                     if r["title"] not in {x["title"] for x in out}]
    if allow_tavily and len(out) < 2:
        out = out + tavily_search(query, max_results, timeout)
    return out[:max_results]


class _TextPuller(__import__("html.parser").parser.HTMLParser):
    """Stdlib readable-text extractor (our Tavily-extract, free, no deps).
    Skips nav/chrome tags, keeps paragraph-ish blocks."""
    _SKIP = {"script", "style", "noscript", "header", "footer", "nav",
             "aside", "form", "button", "select", "svg", "iframe"}
    _KEEP = {"p", "h1", "h2", "h3", "h4", "li", "td", "blockquote"}

    def __init__(self):
        super().__init__()
        self._skip = 0
        self._buf: list = []
        self._cur: list = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._KEEP:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._KEEP:
            self._flush()

    def handle_data(self, data):
        if not self._skip:
            self._cur.append(data)

    def _flush(self):
        try:
            txt = re.sub(r"\s+", " ", "".join(self._cur)).strip()
            if len(txt) >= 40:
                self._buf.append(txt)
        except Exception:
            pass
        self._cur = []

    def text(self, max_chars=3000):
        self._flush()
        return " ".join(self._buf)[:max_chars]


def fetch_text(url, timeout=15, max_chars=3000):
    """Fetch a result page and return clean readable text (Tavily-extract,
    free). Stdlib only, phone-browser impersonation. Never raises ("" on fail)."""
    try:
        u = str(url or "").strip()
        if not u.startswith("http"):
            return ""
        if any(bad in u.lower() for bad in ("facebook.com", "instagram.com",
               "twitter.com", "x.com/", "tiktok.com", "youtube.com/watch",
               ".pdf", ".jpg", ".png", ".mp4")):
            return ""
        s = _sess()
        if s is not None:
            r = s.get(u, headers={"User-Agent": _UA, "Accept": "text/html"},
                      timeout=timeout)
            code, body = r.status_code, r.text
        else:
            import requests as _rq
            r = _rq.get(u, headers={"User-Agent": _UA, "Accept": "text/html"},
                        timeout=timeout)
            code, body = r.status_code, r.text
        if code != 200 or not body or len(body) > 1500000:
            return ""
        low = body[:2000].lower()
        if "text/html" not in low and "<html" not in low and "<p" not in low:
            return ""
        p = _TextPuller()
        try:
            p.feed(body[:500000])
        except Exception:
            pass
        return html.unescape(p.text(max_chars)).strip()
    except Exception:
        return ""


def enriched_search(query, max_results=5, fetch_top=1, timeout=20):
    """Our own Tavily: metasearch (Brave -> DDG, free) + fetch top result
    pages into full text. Returns [{title, snippet, page, source}].
    fetch_top bounds latency (1 page ≈ 2-4s). Never raises, never calls Tavily."""
    out = search(query, max_results, timeout=timeout)
    done = 0
    for r in out:
        if done >= max(fetch_top, 0):
            break
        try:
            if r.get("page") or not r.get("url"):
                continue
            txt = fetch_text(r["url"], timeout=min(timeout, 15))
            if txt:
                r["page"] = txt
                r["source"] = (r.get("source") or "free") + "+page"
                done += 1
        except Exception:
            continue
    return out


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
                    _hs, _as = int(m.group(1)), int(m.group(2))
                    if _hs + _as > 11:
                        continue  # cross-fixture pollution, never a real senior scoreline here
                    return _hs, _as
                except Exception:
                    continue
    return None
