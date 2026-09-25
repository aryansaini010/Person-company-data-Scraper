"""Secure fetcher: ephemeral session per URL, DNS SSRF check, robots.txt, HTML->text.

DMZ rules enforced here:
- resolve + block internal IPs BEFORE connecting (SSRF)
- respect robots.txt (robots_denied never fed to model)
- timeouts + size caps + redirect limits
- fresh httpx.Client per fetch (no cookie/cache carryover between targets)
- output is raw text for acquisition.structure_fetch to classify/structure
"""
from __future__ import annotations
import os
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from . import audit
from .security import resolve_and_assert_no_ssrf

TIMEOUT_S = 10
CONNECT_TIMEOUT_S = 5
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3
USER_AGENT = "ProspectIntel-DMZ/1.0 (+internal research fetcher)"


_CHROME_TAGS = ("nav", "aside")
# header/footer are chrome ONLY when the page carries <article>/<main>
# (then they wrap nav/legal, not the story); on bare pages they may hold
# the only headline, so they are kept. Class/id-based pruning applies to
# container tags and never to paragraph text.
_CHROME_CONTAINERS = ("div", "section", "aside", "nav", "header", "footer",
                      "ul", "ol")
_CHROME_ATTR_RE = re.compile(
    r"cookie|consent|gdpr|newsletter|subscribe|popup|modal|overlay|"
    r"sidebar|related|recommended|commentlist|breadcrumb|pagination|"
    r"share-tools|social-share|advertisement|\bad-container\b",
    re.IGNORECASE)
_ARTICLE_RE = re.compile(r"<\s*(article|main)\b", re.IGNORECASE)


class _TextExtractor(HTMLParser):
    def __init__(self, drop_header_footer: bool = False) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._stack: list[bool] = []  # per-open-tag chrome flag
        self._drop_hf = drop_header_footer

    def _tag_skipped(self, tag, attrs) -> bool:
        if tag in ("script", "style", "noscript"):
            return True
        if tag in _CHROME_TAGS:
            return True
        if tag in ("header", "footer") and self._drop_hf:
            return True
        if tag in _CHROME_CONTAINERS:
            try:
                blob = " ".join(
                    (v or "") for k, v in (attrs or [])
                    if k in ("class", "id", "role")).lower()
            except Exception:
                blob = ""
            if blob and _CHROME_ATTR_RE.search(blob):
                return True
        return False

    def handle_starttag(self, tag, attrs):
        skipped = self._tag_skipped(tag, attrs)
        self._stack.append(skipped)
        if skipped:
            return
        if tag in ("p", "br", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self._stack:
            skipped = self._stack.pop()
        else:
            skipped = False
        if skipped:
            return
        if tag in ("p", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._stack and any(self._stack):
            return
        self.parts.append(data)


def html_to_text(html: str) -> str:
    # Phase 1: Trafilatura (main-content) -> Readability -> legacy extractor.
    # Order matters: Trafilatura strips chrome best; legacy never fails.
    # Offsets stay valid: callers store the whole output as one section.
    clipped = html[:MAX_BYTES]
    try:
        import trafilatura as _tr
        out = _tr.extract(clipped, include_comments=False,
                          include_tables=True, no_fallback=False)
        if out and len([t for t in out.split() if len(t) > 2]) >= 20:
            return "\n".join(
                line.strip() for line in out.splitlines() if line.strip())
    except Exception:
        pass
    try:
        from readability import Document as _Doc
        doc = _Doc(clipped)
        summary = doc.summary(html_partial=True) or ""
        if summary:
            ext = _TextExtractor(drop_header_footer=True)
            try:
                ext.feed(summary)
            finally:
                try:
                    ext.close()
                except Exception:
                    pass
            text = "".join(ext.parts)
            cleaned = "\n".join(line.strip() for line in text.splitlines()
                                 if line.strip())
            if len(cleaned.split()) >= 20:
                return cleaned
    except Exception:
        pass
    ext = _TextExtractor(
        drop_header_footer=bool(_ARTICLE_RE.search(clipped)))
    try:
        ext.feed(clipped)
    finally:
        try:
            ext.close()  # flush buffered tail at truncation boundary
        except Exception:
            pass
    text = "".join(ext.parts)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def robots_allowed(url: str, client: httpx.Client) -> bool:
    """Fetch + parse robots.txt; fail-closed (deny) on error? No — fail-open
    for fetch but log; hard deny only on explicit Disallow. Rationale: a
    transient robots.txt outage should not silently widen crawl scope, but
    blocking all research on it breaks availability; we log and proceed."""
    parts = urlparse(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        r = client.get(robots_url, timeout=TIMEOUT_S)
        if r.status_code != 200 or not r.text.strip():
            return True
        rp = RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp.can_fetch(USER_AGENT, url)
    except Exception as e:
        audit.log("robots_fail_open", {"url": robots_url,
                                       "detail": f"{type(e).__name__}"})
        return True


@dataclass
class FetchResult:
    url: str                    # requested URL
    url_final: str              # after redirects (§5.4)
    status_code: int
    body_text: str              # extracted text (what structuring sees)
    raw_body: bytes = b""       # raw bytes (what the snapshot store keeps)
    title: str = ""
    content_type: str = ""
    robots_denied: bool = False
    rendered_via: str = ""      # "firecrawl" when rendered, else ""
    published: str = ""         # renderer metadata (publishedTime), if any
    # Phase 1 metadata: publisher + cache validators for changed-since-last.
    publisher: str = ""         # og:site_name / outlet / hostname fallback
    etag: str = ""              # HTTP ETag for conditional refetch
    last_modified: str = ""     # HTTP Last-Modified for conditional refetch


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def fetch_url(url: str) -> FetchResult:
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"refusing non-http(s) URL: {url!r}")
    timeout = httpx.Timeout(TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
    with httpx.Client(headers={"User-Agent": USER_AGENT},
                      timeout=timeout, follow_redirects=False) as client:
        current = url
        # Manual redirect loop: re-validate EVERY hop (§8.3), cap depth.
        for _ in range(MAX_REDIRECTS + 1):
            resolved = resolve_and_assert_no_ssrf(current)  # raises: block
            t0 = time.time()
            r = client.get(current)
            if r.is_redirect:
                nxt = r.headers.get("location", "")
                if not nxt:
                    break
                nxt = str(httpx.URL(current).join(nxt))
                if urlparse(nxt).scheme.lower() not in ("http", "https"):
                    raise ValueError(f"redirect to non-http(s) refused: {nxt!r}")
                current = nxt  # next iteration re-resolves + re-validates
                continue
            return _build_result(url, current, r, resolved, time.time() - t0,
                                 client)
        raise ValueError(f"too many redirects (>{MAX_REDIRECTS}): {url!r}")


def _robots_ok(url: str) -> bool:
    """Politeness gate (§5.2 MUST): honor robots.txt before ANY fetch path,
    rendered or plain. Fail-open only when robots.txt itself is unreachable
    (logged); explicit Disallow always wins."""
    from .security import resolve_and_assert_no_ssrf
    resolve_and_assert_no_ssrf(url)  # SSRF first, always
    timeout = httpx.Timeout(TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
    with httpx.Client(headers={"User-Agent": USER_AGENT},
                      timeout=timeout) as client:
        return robots_allowed(url, client)


def fetch_url_smart(url: str, render: bool = True) -> FetchResult:
    """Plain-first: robots gate → plain HTTP → rendered fallback.

    Plain HTTP stays first (fast, polite, per firecrawl.py policy); rendering
    is the fallback for blocked/paywalled/empty/error outcomes via
    fc.bypass_allowed() — unless render=False (high-volume diet fetching:
    plain only, headlines already cover). robots_denied is final (aggregator
    exception: a redirector disallow does not cover the publisher article).
    Rendered output is classified before acceptance. Raises only on
    SSRF/scheme refusal or when every path fails (ValueError propagates
    for triage)."""
    from . import firecrawl as fc
    from .security import classify_fetch as _classify
    from urllib.parse import urlparse as _up
    scheme = _up(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"refusing non-http(s) URL: {url!r}")
    host = (_up(url).hostname or "").lower()
    try:
        allowed = _robots_ok(url)
    except ValueError:
        raise  # SSRF refusal propagates, never rendered around
    except Exception:
        allowed = True  # robots.txt unreachable: log + proceed (fail-open)
    if not allowed and not (host in fc.AGGREGATOR_HOSTS and fc.available()):
        audit.log("fetch_rejected", {"url": url, "status": "robots_denied",
                                     "http": 0})
        return FetchResult(url=url, url_final=url, status_code=0, body_text="",
                           robots_denied=True)
    if not allowed:
        audit.log("aggregator_redirect_render",
                  {"url": url[:120],
                   "note": "redirector disallow does not cover publisher"})
        return _render_or_raise(url, None, "robots_denied", render)
    try:
        plain = fetch_url(url)
    except ValueError:
        raise  # SSRF/scheme/redirect refusal: triage, never masked
    except Exception as e:
        audit.log("fetch_rejected", {"url": url, "status": "error",
                                     "detail": f"{type(e).__name__}: {e}"})
        return _render_or_raise(url, None, "error")
    if not plain.robots_denied and _classify(plain.status_code,
                                             plain.body_text) == "ok":
        return plain
    verdict = ("robots_denied" if plain.robots_denied
               else _classify(plain.status_code, plain.body_text))
    return _render_or_raise(url, plain, verdict, render)


def _render_or_raise(url: str, plain: FetchResult | None,
                     verdict: str, render: bool = True) -> FetchResult:
    """Fallback leg: render when allowed, else return plain / raise."""
    from . import firecrawl as fc
    from .security import classify_fetch as _classify
    if (render and os.environ.get("FIRECRAWL_DISABLE", "") == ""
            and fc.available() and fc.bypass_allowed(url, verdict)):
        try:
            md, title, published = fc.scrape(url)
        except Exception as e:
            audit.log("render_failed", {"url": url,
                                        "detail": str(e)[:200]})
            if plain is not None:
                return plain
            raise RuntimeError(f"all fetch paths failed for {url!r}")
        if _classify(200, md) != "ok":
            audit.log("render_rejected", {"url": url, "status": _classify(200, md)})
            if plain is not None:
                return plain
            raise RuntimeError(f"rendered output rejected for {url!r}")
        audit.log("render_fallback", {"url": url, "for": verdict})
        return FetchResult(url=url, url_final=url, status_code=200,
                           body_text=md, raw_body=md.encode(), title=title,
                           content_type="text/markdown",
                           rendered_via="firecrawl", published=published)
    if plain is not None:
        return plain  # classified downstream; never fed to a model
    raise RuntimeError(f"all fetch paths failed for {url!r}")


def _build_result(url: str, url_final: str, r: httpx.Response,
                  resolved: list[str], elapsed: float,
                  client: httpx.Client) -> FetchResult:
    if not robots_allowed(url_final, client):
        audit.log("fetch_rejected", {"url": url_final, "status": "robots_denied",
                                     "http": 0})
        return FetchResult(url=url, url_final=url_final, status_code=0,
                           body_text="", robots_denied=True)
    raw = r.content[:MAX_BYTES]
    ctype = r.headers.get("content-type", "")
    etag = (r.headers.get("etag", "") or "")[:200]
    last_mod = (r.headers.get("last-modified", "") or "")[:120]
    title, publisher = "", ""
    if "html" in ctype:
        html = raw.decode("utf-8", "replace")
        m = _TITLE_RE.search(html[:50_000])
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()[:300]
        try:
            og = re.search(
                r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)',
                html[:50_000], re.IGNORECASE)
            if og:
                publisher = re.sub(r"\s+", " ", og.group(1)).strip()[:120]
        except Exception:
            publisher = ""
        if not publisher:
            try:
                from urllib.parse import urlparse as _up2
                publisher = (_up2(url_final).hostname or "")[:120]
            except Exception:
                publisher = ""
        text = html_to_text(html)
    else:
        text = raw.decode("utf-8", "replace")
    audit.log("fetched", {"url": url_final, "http": r.status_code,
                          "bytes": len(raw), "resolved": resolved,
                          "elapsed_s": round(elapsed, 2)})
    return FetchResult(url=url, url_final=url_final, status_code=r.status_code,
                       body_text=text, raw_body=bytes(raw), title=title,
                       content_type=ctype, publisher=publisher, etag=etag,
                       last_modified=last_mod)
