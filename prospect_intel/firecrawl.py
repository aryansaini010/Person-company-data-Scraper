"""Rendered fetch via local self-hosted Firecrawl (spec §2.1's headless-browser
role: JS-rendered pages, main-content markdown, no model dependency).

Policy:
- SSRF pre-validation runs BEFORE submitting (a local renderer would fetch
  internal targets if asked; our guard stays in front).
- robots_denied is NEVER bypassed (§5.2 politeness MUST) — except aggregator
  redirect links (news.google.com), where the disallow covers the redirector,
  not the publisher article it resolves to. That exception is audit-logged.
- Plain HTTP stays first (fast, polite); rendering is the fallback for
  blocked/paywalled/empty/error outcomes.
"""
from __future__ import annotations
import os
import time

import httpx

from . import audit
from .security import resolve_and_assert_no_ssrf

def _env_base() -> str:
    return os.environ.get("FIRECRAWL_URL", "http://localhost:3002").rstrip("/")


def _env_key() -> str:
    return os.environ.get("FIRECRAWL_API_KEY", "")


def _env_timeout() -> int:
    try:
        return max(5, int(os.environ.get("FIRECRAWL_TIMEOUT", "45") or "45"))
    except (ValueError, TypeError):
        return 45


BASE = _env_base()
KEY = _env_key()
TIMEOUT_S = _env_timeout()
AGGREGATOR_HOSTS = {"news.google.com"}

_live: bool | None = None
_live_ts: float = 0.0
_LIVE_TTL_S = 60.0


def available() -> bool:
    """Liveness probe with 60s TTL (a recovered renderer comes back; a dead
    one stops stalling every URL). Any HTTP response counts as up."""
    global _live, _live_ts
    import time as _t
    if _live is not None and _t.time() - _live_ts < _LIVE_TTL_S:
        return _live
    if os.environ.get("FIRECRAWL_DISABLE", ""):
        _live, _live_ts = False, _t.time()
        return _live
    try:
        httpx.get(_env_base() + "/", timeout=5)
        _live = True
    except Exception:
        _live = False
    _live_ts = _t.time()
    return _live


def _post_with_retries(c: httpx.Client, path: str, payload: dict,
                       tries: int = 3) -> httpx.Response:
    """Retry transient failures (429/5xx/timeout) with backoff."""
    last = None
    for i in range(tries):
        try:
            r = c.post(path, json=payload)
            if r.status_code in (429, 500, 502, 503, 504):
                last = r
                audit.log("render_retry", {"path": path,
                                           "http": r.status_code,
                                           "try": i + 1})
                import time as _t
                _t.sleep(min(4.0, 0.5 * (2 ** i)))
                continue
            return r
        except Exception as e:
            last = e
            import time as _t
            _t.sleep(min(4.0, 0.5 * (2 ** i)))
    if isinstance(last, httpx.Response):
        return last
    raise RuntimeError(f"firecrawl unreachable after {tries} tries: {last}")


def scrape(url: str, wait_for: int = 0) -> tuple[str, str, str]:
    """Render a URL to (markdown, title, published). Raises on failure/block/SSRF.

    wait_for (ms) is an optional passthrough for dynamic investor tables;
    0 preserves current behavior. SSRF pre-validation, retry, block-page
    rejection, and markdown cleaning are unchanged — all callers still go
    through fetch_url_smart + structure_result so char spans survive."""
    from urllib.parse import urlparse
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"refusing non-http(s) URL: {url!r}")
    resolve_and_assert_no_ssrf(url)  # guard in front of the renderer
    headers = {"Content-Type": "application/json"}
    _key = _env_key()
    if _key:
        headers["Authorization"] = f"Bearer {_key}"
    _timeout = _env_timeout()
    t0 = time.time()
    payload: dict = {
        "url": url, "formats": ["markdown"], "onlyMainContent": True,
        "timeout": max(10_000, (_timeout - 5) * 1000),
    }
    try:
        wait_ms = max(0, int(wait_for or 0))
    except (ValueError, TypeError):
        wait_ms = 0
    if wait_ms:
        payload["waitFor"] = min(wait_ms, 10_000)
    with httpx.Client(timeout=_timeout) as c:
        r = _post_with_retries(c, _env_base() + "/v1/scrape", payload)
    if r.status_code != 200:
        raise RuntimeError(f"firecrawl HTTP {r.status_code}: {r.text[:200]}")
    try:
        payload = r.json()
    except Exception:
        raise RuntimeError("firecrawl returned invalid JSON")
    if not isinstance(payload, dict) or not payload.get("success"):
        raise RuntimeError(f"firecrawl refused: {str(payload)[:200]}")
    data = payload.get("data", {}) or {}
    if not isinstance(data, dict):
        raise RuntimeError("firecrawl returned unexpected shape")
    md = ((data.get("markdown") or "") if isinstance(data.get("markdown"), str)
          else "")
    md = md.strip()
    if not md:
        raise RuntimeError("firecrawl returned empty markdown")
    meta = data.get("metadata", {}) or {}
    title = str(meta.get("title") or "")[:300]
    published = str(meta.get("publishedTime") or "")[:60]
    audit.log("rendered", {"url": url, "chars": len(md),
                           "elapsed_s": round(time.time() - t0, 2)})
    import re as _re
    from .security import CAPTCHA_MARKERS
    if _re.search(r"error\s*4\d\d|bad request|access denied|forbidden|"
                  r"are you a robot|" + "|".join(_re.escape(m) for m in CAPTCHA_MARKERS),
                  md + "\n" + title, _re.IGNORECASE):
        raise RuntimeError(f"rendered block/error page: {title[:100]}")
    md = clean_markdown(md)
    return md, title, published


def clean_markdown(md: str) -> str:
    """Drop markdown furniture (images, rule lines, transcript slide markers)
    so it can't become claims. Preserves inline `#` (C#, hashtags) and
    citation URLs — only structural markers are stripped."""
    import re as _re
    md = _re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)
    md = _re.sub(r"\[([^\]]*)\]\(([^)]*)\)",  # [text](url) -> text + footnote
                  lambda m: ((m.group(1) or "") + f" [{m.group(2)}]"
                             if m.group(2) and m.group(2).startswith("http")
                             else (m.group(1) or "")), md)
    md = "\n".join(ln.rstrip("\\").rstrip() for ln in md.splitlines())
    md = "\n".join(_re.sub(r"^#{1,6}\s+", "", ln) for ln in md.splitlines())
    md = "\n".join(ln.replace("**", "") for ln in md.splitlines())
    md = "\n".join(ln for ln in md.splitlines()
                   if ln.strip() and not _re.match(r"^[-*_#>=\s|]+$", ln)
                   and not _re.match(r"^\[[^\]]{1,40}\]$", ln)
                   and not _re.match(r"^\*{0,2}\s*Vslide\b", ln))
    return _re.sub(r"\n{3,}", "\n\n", md).strip()


_GOV_PATH_HINTS = ("investor", "governance", "annual-report",
                    "annualreport", "filing", "filings", "board-of-directors",
                    "shareholder", "financial-results", "results")


def prioritize_governance_urls(urls: list[str]) -> list[str]:
    """Stable reorder: investor/governance/filings/annual-report URLs first.

    Never drops strategy URLs — only reorders so Firecrawl/render budget
    hits high-density governance pages before general content.     Duplicates
    removed, order otherwise preserved.
    """
    from urllib.parse import urlparse

    def _score(u: str) -> int:
        try:
            path = (urlparse(u).path or "").lower()
        except Exception:
            path = ""
        return 0 if any(h in path for h in _GOV_PATH_HINTS) else 1

    seen, out = set(), []
    for u in urls or []:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return sorted(out, key=_score)


def bypass_allowed(url: str, plain_status: str) -> bool:
    """Fallback allowed for blocked/paywalled/empty/error. robots_denied is
    final — unless the URL is an aggregator redirect (its disallow covers
    the redirector, not the publisher article). Render is skipped for
    wikipedia.org (MediaWiki API extraction covers it; a 45s browser render
    of an encyclopedia page never beats the API path)."""
    try:
        from urllib.parse import urlparse as _upb
        if "wikipedia.org" in ((_upb(url or "").hostname or "").lower()):
            return False
    except Exception:
        pass
    if plain_status in ("blocked", "paywalled", "empty", "error"):
        return True
    if plain_status == "robots_denied":
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        if host in AGGREGATOR_HOSTS:
            audit.log("aggregator_redirect_render",
                      {"url": url[:120],
                       "note": "redirector disallow does not cover publisher"})
            return True
    return False


def search(query: str, max_results: int = 5) -> list[tuple[str, str, str]]:
    """Breadth search via local Firecrawl (upstream engines — Tier-3 class:
    NEVER for person/intent queries, enforced by callers). Returns
    [(url, title, description)]. Keyless locally; raises if unconfigured."""
    if not available():
        raise RuntimeError("local firecrawl down")
    max_results = max(1, min(int(max_results or 5), 20))
    headers = {"Content-Type": "application/json"}
    _key = _env_key()
    if _key:
        headers["Authorization"] = f"Bearer {_key}"
    with httpx.Client(timeout=30, headers=headers) as c:
        r = _post_with_retries(c, _env_base() + "/v1/search",
                               {"query": query, "limit": max_results})
    if r.status_code != 200:
        raise RuntimeError(f"firecrawl search HTTP {r.status_code}")
    try:
        payload = r.json()
    except Exception:
        raise RuntimeError("firecrawl search returned invalid JSON")
    items = payload.get("data", []) if isinstance(payload, dict) else []
    out = []
    for item in (items or [])[:max_results]:
        if not isinstance(item, dict):
            continue
        url = item.get("url", "")
        if isinstance(url, str) and url.startswith("http"):
            out.append((url, str(item.get("title", ""))[:300],
                        str(item.get("description", ""))[:500]))
    audit.log("firecrawl_search", {"query": query, "hits": len(out)})
    return out
