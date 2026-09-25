"""Pass 1 scoped queries: governance + filings + segment strategy.

Replaces bare-brand queries ("Zee") that drown SearXNG in consumer noise
(piano teachers, snacks, dictionary entries). Domain-constrained governance
queries hit investor-relations/governance pages; filings queries hit
registries; strategy queries preserve the buyer segment (segment.py
rss_angles: media_ott ZEE5/advertising/partnerships) so strategic context
is never lost.

Engine-agnostic: `site:` and quoted phrases are hints — every hit still
passes the existing hit_relevant gate downstream, so an engine that
ignores the operator degrades to breadth, never to wrong-entity fetch.
Tier routing is preserved: person flows MUST set is_person=True so
callers keep Tier-3 refused (§5.1).
"""
from __future__ import annotations
from urllib.parse import urlparse


def _domain_of(website: str) -> str:
    try:
        host = (urlparse(website or "").hostname or "").lower()
        return host.removeprefix("www.") if host else ""
    except Exception:
        return ""


def build_scoped_queries(ground_truth: dict | None,
                         segment: dict | None = None,
                         is_person: bool = False) -> list[str]:
    """Return scoped query strings (governance, filings, strategy...).

    is_person only tags intent for the caller — this module never touches
    the network, so it cannot violate Tier routing by itself; callers MUST
    map is_person -> person_query=True for Tier-3.
    """
    gt = ground_truth or {}
    name = (gt.get("legal_name", "") or gt.get("company", "") or "").strip()
    if not name:
        return []
    website = gt.get("official_website", "") or gt.get("homepage", "") or ""
    domain = _domain_of(website) if website != "unknown" else ""
    queries: list[str] = []
    # 1. Governance / investor relations (domain-constrained when known).
    if domain:
        queries.append(
            f'site:{domain} ("board of directors" OR "investor relations" '
            f'OR "annual report" OR governance)')
    else:
        queries.append(f'"{name}" (corporate OR "board of directors" '
                       f'OR "annual report" OR governance)')
    # 2. Registries / filings.
    queries.append(f'"{name}" (filing OR "registration number" OR MCA '
                   f'OR "companies house" OR "SEC EDGAR" OR CIN OR LEI)')
    # 3. Strategy: buyer-segment angles (never dropped — media_ott context).
    try:
        seg = segment or {}
        angles = seg.get("rss_angles", ()) or ()
        short = name.split(" LIMITED")[0].split(" Ltd")[0].strip() or name
        for a in list(angles)[:3]:
            try:
                queries.append(a.format(co=short))
            except Exception:
                continue
    except Exception:
        pass
    # Dedupe, preserve order.
    seen, out = set(), []
    for q in queries:
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out
