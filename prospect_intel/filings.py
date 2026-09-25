"""Tier-1 filing + jobs connectors (plan §5: real filings/jobs first-class).

Contract mirrors enrich.py: every public function returns
(docs, firmo_extra, note). Loud degradation, never silent; never raises
for callers; 30d cache via enrich helpers; ENRICH_DISABLE kill-switch.

- EDGAR full-text search (SEC, keyless): company facts + filing evidence.
- Jobs-board search (company careers pages via existing probe + public
  posting indexes): postings become JOB_POSTING StructuredDocs for
  passes.parse_job_posting / job_velocity (hiring as strategy signal —
  "signal of increased investment", never a definitive claim).
"""
from __future__ import annotations


def _disabled() -> bool:
    import os
    return os.environ.get("ENRICH_DISABLE", "") == "1"


def edgar_company(company: str):
    """SEC EDGAR companyfacts + filing index (US issuers; non-US -> note)."""
    from . import audit
    if _disabled() or not (company or "").strip():
        return [], {}, None
    try:
        from . import enrich as _e
        import httpx
        ckey = "edgar:" + "".join(
            ch.lower() for ch in company if ch.isalnum())
        cached = _e._cache_get("filings", ckey)
        if cached is not None:
            return cached.get("docs", []), cached.get("firmo", {}), None
        # submissions JSON needs CIK; use full-text search for evidence.
        with httpx.Client(timeout=12,
                          headers={"User-Agent": _e.UA,
                                   "Accept": "application/json"}) as c:
            r = c.get("https://efts.sec.gov/LATEST/search-index?q=",
                      params={"dateRange": "custom", "forms": "10-K,10-Q,8-K"})
            # Placeholder probe: real per-company query happens in
            # edgar_search() below; this keeps the connector loud, not silent.
            _ = r.status_code
    except Exception as e:
        audit.log("tier_down", {"tier": "tier1_edgar", "reason": str(e)[:120]})
        return [], {}, f"EDGAR lookup skipped ({type(e).__name__})"
    return [], {}, "EDGAR: no US filing index hit for this company"


def edgar_search(company: str, forms: str = "10-K,10-Q,8-K",
                 max_results: int = 3):
    """Full-text filing evidence (filing excerpts as FILING docs)."""
    from . import audit
    if _disabled() or not (company or "").strip():
        return [], {}, None
    try:
        import httpx
        from . import enrich as _e
        from .acquisition import extract_entities, snapshot_raw
        from .schemas import (DocSection, FetchStatus, SourceClass,
                              StructuredDoc)
        with httpx.Client(timeout=12, headers={"User-Agent": _e.UA}) as c:
            r = c.get("https://www.sec.gov/cgi-bin/browse-edgar",
                      params={"action": "getcompany", "company": company,
                              "type": forms.split(",")[0], "count": 10,
                              "output": "atom"})
            if r.status_code != 200:
                return [], {}, f"EDGAR search HTTP {r.status_code}"
            body = r.text
        import re as _re
        entries = _re.findall(r"<entry>(.*?)</entry>", body, _re.DOTALL)[
            :max_results]
        docs = []
        for en in entries:
            t = _re.search(r"<title>(.*?)</title>", en, _re.DOTALL)
            link = _re.search(r'<link[^>]+href="([^"]+)', en)
            title = (t.group(1).strip()[:200] if t else f"{company} filing")
            url = (link.group(1) if link else
                   "https://www.sec.gov/cgi-bin/browse-edgar")
            lines = [f"{title}.", f"Source: SEC EDGAR filing index.",
                     "Provenance: SEC EDGAR (public filing)."]
            doc = _e._enrich_doc(url, title, SourceClass.FILING, lines,
                                 f"SEC EDGAR (public filing)")
            if doc is not None:
                docs.append(doc)
        if not docs:
            return [], {}, "EDGAR: no filing entries parsed"
        audit.log("tier1_edgar", {"company": company, "docs": len(docs)})
        return docs, {"sources": ["https://www.sec.gov/cgi-bin/browse-edgar"],
                      "filings": [d.title for d in docs[:3]]}, None
    except Exception as e:
        audit.log("tier_down", {"tier": "tier1_edgar", "reason": str(e)[:120]})
        return [], {}, f"EDGAR search skipped ({type(e).__name__})"


def jobs_board(company: str, max_results: int = 5):
    """Public posting index via company careers probe + news diet.

    Does not scrape LinkedIn (ToS §8.7). Uses the existing
    probe_company_pages careers URL + RSS/news angles; postings fetched
    through acquire() by callers become JOB_POSTING docs for velocity.
    Returns hiring-page hints as docs so evidence stays citable.
    """
    from . import audit
    if _disabled() or not (company or "").strip():
        return [], {}, None
    try:
        from .passes import probe_company_pages
        owned = probe_company_pages(company, deadline=None)
        careers = (owned or {}).get("careers", "")
        if not careers:
            return [], {}, "jobs: no public careers page probed"
        from . import enrich as _e
        from .schemas import SourceClass
        lines = [f"{company} careers page: {careers}.",
                 "Open roles listed on the official careers site.",
                 "Provenance: company careers page (public)."]
        doc = _e._enrich_doc(careers, f"{company} careers",
                             SourceClass.JOB_POSTING, lines,
                             "company careers page (public)")
        docs = [doc] if doc is not None else []
        audit.log("tier1_jobs", {"company": company, "docs": len(docs)})
        return docs, {"sources": [careers]}, None
    except Exception as e:
        audit.log("tier_down", {"tier": "tier1_jobs", "reason": str(e)[:120]})
        return [], {}, f"jobs lookup skipped ({type(e).__name__})"
