"""Search abstraction (query-confidentiality seam).

Plan §2: a vendor search engine that sees your queries learns your targets.
The interface below lets deployments plug a self-hosted index (OpenSearch
over previously fetched docs) without touching the pipeline. Ships with:
- DirectProvider: research an explicit URL list (no query leaves the box).
- DuckDBGoProvider: live web fallback over DuckDuckGo html endpoint.
"""
from __future__ import annotations
import re
from dataclasses import dataclass

import httpx


@dataclass
class SearchHit:
    url: str
    title: str = ""
    outlet: str = ""


class SearchProvider:
    def search(self, query: str, max_results: int = 8) -> list[SearchHit]:
        raise NotImplementedError


class DirectProvider(SearchProvider):
    """No query leaves the box: caller supplies exact URLs to research."""

    def __init__(self, urls: list[str]):
        self.urls = urls

    def search(self, query: str, max_results: int = 8) -> list[SearchHit]:
        return [SearchHit(url=u) for u in self.urls[:max_results]]


class DuckDuckGoProvider(SearchProvider):
    def search(self, query: str, max_results: int = 8) -> list[SearchHit]:
        try:
            with httpx.Client(timeout=15,
                              headers={"User-Agent": "Mozilla/5.0"}) as c:
                r = c.get("https://html.duckduckgo.com/html/",
                          params={"q": query})
                if r.status_code != 200:
                    return []
                urls = re.findall(r'uddg=([^"&]+)', r.text)
                titles = re.findall(
                    r'class="result__a"[^>]*>(.*?)</a>', r.text, re.DOTALL)
                import html as _html
                import urllib.parse
                out, seen = [], set()
                for i, u in enumerate(urls):
                    u = urllib.parse.unquote(u)
                    if u.startswith("http") and u not in seen:
                        seen.add(u)
                        title = ""
                        if i < len(titles):
                            title = re.sub(
                                r"<[^>]+>", "",
                                _html.unescape(titles[i])).strip()[:300]
                        out.append(SearchHit(url=u, title=title))
                    if len(out) >= max_results:
                        break
                return out
        except Exception:
            return []


class SearxngProvider(SearchProvider):
    """Self-hosted breadth search (queries never leave our infrastructure).

    Preferred over DuckDuckGoProvider wherever a provider seam is used;
    falls back to DuckDuckGo only when SEARXNG_URL is unset/down."""

    def __init__(self, fallback: SearchProvider | None = None):
        self.fallback = fallback or DuckDuckGoProvider()

    def search(self, query: str, max_results: int = 8) -> list[SearchHit]:
        import os as _os
        if not _os.environ.get("SEARXNG_URL", "").strip():
            return self.fallback.search(query, max_results)
        try:
            from .discovery import tier2_searxng
            res = tier2_searxng(query, max_results)
            if res.available and res.hits:
                return res.hits
        except Exception:
            pass
        return self.fallback.search(query, max_results)
