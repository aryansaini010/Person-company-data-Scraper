"""Tiered discovery (§5.1): Tier 1 direct connectors (primary) → Tier 2 SearXNG
self-hosted (breadth) → Tier 3 metered commercial API (availability floor only).

Query routing rule (MUST): queries with a natural-person name, or company +
inferred intent, MUST NOT go to Tier 3. Company-only / public-document
queries may. Tier-1 outage alerts loudly (§10.2) — never silently degrades.
"""
from __future__ import annotations
import os
import re
import time
from dataclasses import dataclass, field

import httpx

from . import audit
from .search import DuckDuckGoProvider, SearchHit


@dataclass
class TierResult:
    tier: str
    hits: list[SearchHit] = field(default_factory=list)
    available: bool = True
    note: str = ""


# §8.7 / §2.2: platforms prohibiting automated collection are excluded on
# ToS grounds — dropped at discovery, loudly, never fetched.
EXCLUDED_HOSTS = ("linkedin.", "facebook.", "instagram.", "x.com",
                  "twitter.")


def _excluded(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return host in ("x.com", "twitter.com", "linkedin.com", "facebook.com",
                    "instagram.com") or host.startswith(
        ("linkedin.", "facebook.", "instagram.")) or ".linkedin." in host


def _company_mentioned(text: str, company: str) -> bool:
    """Company-side relevance: full-name hit or first significant token.
    Mirrors the surname rule for persons (§6.3): 'Reliance' suffices for
    'Reliance Industries'; a bare 'Industries' does not."""
    t, c = (text or "").lower(), (company or "").strip().lower()
    if not c or c == "unknown":
        return False
    if c in t:
        return True
    toks = [p for p in re.findall(r"[a-z]{3,}", c)]
    return bool(toks) and toks[0] in t


# Short-name / brand aliases: bare brands ("Zee") never resolve via
# DBpedia/Wikidata slugs ("Zee" is a disambiguation page). Canonicalize
# before registry/probe lookups; display name stays as typed by caller.
COMPANY_ALIASES = {
    "zee": "Zee Entertainment Enterprises",
    "zee5": "Zee Entertainment Enterprises",
    "zee entertainment": "Zee Entertainment Enterprises",
    "zeel": "Zee Entertainment Enterprises",
    "zee media": "Zee Media Corporation",
    "vaultstackai": "VaultStack AI",
    "vaultstack ai": "VaultStack AI",
}


def canonical_company(company: str) -> str:
    """Return the registry-grade name for a bare brand, else the input."""
    key = " ".join((company or "").strip().lower().split())
    return COMPANY_ALIASES.get(key, (company or "").strip())


def _norm_alnum(s: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]", "", (s or "").lower())


def company_names_match(a: str, b: str) -> bool:
    """Space/punct-insensitive equality: 'VaultStack AI' == 'VaultStackAI'."""
    return bool(a and b) and _norm_alnum(a) == _norm_alnum(b)


def _core_company_tokens(company: str) -> list[str]:
    """Significant tokens minus legal suffixes: 'Imperial Milestone Private
    Limited' -> ['imperial', 'milestone']. Used where a full legal name
    would never appear verbatim (homepage titles, lead text)."""
    legal = {"private", "limited", "pvt", "ltd", "inc", "incorporated",
             "corp", "corporation", "llc", "llp", "plc", "gmbh", "pty",
             "co", "company", "group", "holdings", "ventures", "labs"}
    return [p for p in re.findall(r"[a-z]{3,}", (company or "").lower())
            if p not in legal]


def _core_tokens_mentioned(text: str, company: str) -> bool:
    toks = _core_company_tokens(company)
    if not toks:
        return _company_mentioned(text, company)
    t = (text or "").lower()
    return all(tok in t for tok in toks)


def disambig_get(company: str) -> dict:
    """Negative disambiguation cache (30d, enrich_cache): remembers parked
    hosts + wrong registry entities/hosts for ambiguous names (Bullet) so
    repeat briefs skip slow re-probes. Returns {} on miss. Never raises."""
    try:
        from . import enrich as _e
        hit = _e._cache_get("disambig",
                            _norm_alnum(company or "")[:60])
        if isinstance(hit, dict):
            return hit
    except Exception:
        pass
    return {}


def disambig_note(company: str, parked_host: str = "",
                  wrong_entity: str = "", wrong_host: str = "") -> None:
    """Record one disambiguation verdict (additive, deduped, best-effort)."""
    try:
        from . import enrich as _e
        cur = disambig_get(company)
        changed = False
        for key, val in (("parked_hosts", parked_host),
                         ("wrong_entities", wrong_entity),
                         ("wrong_hosts", wrong_host)):
            if val and val not in cur.get(key, []):
                cur[key] = (cur.get(key, []) + [val])[:20]
                changed = True
        if changed:
            _e._cache_put("disambig", _norm_alnum(company or "")[:60], cur)
    except Exception:
        pass


def homepage_matches_company(homepage: str, company: str) -> bool:
    """Host-agreement gate against wrong-entity merges (Bullet vs
    seismosoc.org Bulletin). True when the homepage host carries a company
    core token or its acronym (ril.com for Reliance Industries). Shared by
    pass2 DBpedia gate + Wikidata enrich gate so both registries agree."""
    try:
        from urllib.parse import urlparse as _up
        host = (_up(homepage or "").hostname or "").lower()
        if not host or not (company or "").strip():
            return False
        toks = [t.lower() for t in (_core_company_tokens(company) or [])]
        if not toks:
            return True  # nothing to check against; fail open
        if any(t in host for t in toks):
            return True
        acro = "".join(t[:1] for t in toks)
        halnum = re.sub(r"[^a-z0-9]", "", host)
        return len(acro) >= 2 and acro in halnum
    except Exception:
        return True  # fail open; callers audit on drop only


def hit_relevant(url: str, title: str, name: str, company: str) -> bool:
    """Strict entry gate: a hit is fetched ONLY if its title/URL names the
    person (fuzzy) or the company (first-token rule). Anything else is
    unrelated data — dropped loudly, never fetched (§6.3, §10.2)."""
    blob = f"{title or ''}\n{url or ''}"
    if (name or "").strip():
        try:
            from .passes import name_match as _nm
        except Exception:
            _nm = None
        if _nm and _nm(blob, name):
            return True
    return _company_mentioned(blob, company)


def _gate_hits(hits: list, name: str, company: str, tier: str) -> tuple[list, str | None]:
    """Apply hit_relevant; audit-log each drop. A tier whose hits ALL miss
    the subject reports it loudly (coverage reduced, not silent).
    Kept hits are ordered strong-relationship first (both name+company)
    so fetch budget hits density, not same-name noise."""
    kept = []
    for h in hits:
        if hit_relevant(getattr(h, "url", ""), getattr(h, "title", ""),
                        name, company):
            kept.append(h)
        else:
            audit.log("irrelevant_dropped",
                      {"tier": tier, "url": getattr(h, "url", "")[:120],
                       "title": getattr(h, "title", "")[:80]})
    if hits and not kept:
        return kept, (f"{tier}: {len(hits)} hit(s), none about this "
                      f"person/company — dropped, never fetched")
    try:
        kept.sort(key=lambda h: 0 if identity_relationship(
            getattr(h, "url", ""), getattr(h, "title", ""), name,
            company) == "strong" else 1)
    except Exception:
        pass
    return kept, None


def identity_relationship(url: str, title: str, name: str, company: str,
                          owned_hosts: set | None = None) -> str:
    """Two-stage identity gate (Stage-1 name / Stage-2 relationship).

    A search hit naming the person is NOT evidence about the card until
    the company relationship is established — same-name people (YouTube /
    IG / NYU-Langone Azams) must never enter the Bullet evidence set.

    Returns: strong (name AND company, or owned company host),
    uncertain (name XOR company — candidate, fetch last), wrong (neither).
    Never raises; empty name/company degrades to hit_relevant semantics.
    """
    blob = f"{title or ''}\n{url or ''}"
    try:
        from .passes import name_match as _nm
        name_hit = bool((name or "").strip()) and bool(
            _nm and _nm(blob, name))
    except Exception:
        name_hit = False
    try:
        co_hit = bool(_company_mentioned(blob, company))
    except Exception:
        co_hit = False
    if name_hit and co_hit:
        return "strong"
    if owned_hosts:
        try:
            from urllib.parse import urlparse as _up
            host = (_up(url or "").hostname or "").lower()
            if host and any(h and h.lower() in host or host in h.lower()
                            for h in owned_hosts if h):
                return "strong"
        except Exception:
            pass
    if name_hit or co_hit:
        try:
            audit.log("identity_uncertain",
                      {"url": (url or "")[:120], "title": (title or "")[:80]})
        except Exception:
            pass
        return "uncertain"
    return "wrong"


def _linkedin_relevant(url: str, title: str, name: str) -> bool:
    """A LinkedIn hit is only a reference if it is about THIS person.

    SearXNG returns namesakes (Aryan Gupta / Aaryan Nagpal for an
    'Aryan Saini' query) — linking them is worse than linking nothing
    (§6.3 wrong-person risk). Search URLs (linkedin.com/search/...) are
    relevant by construction (they encode our query); profile URLs must
    fuzzy-match the person in title or URL slug."""
    from urllib.parse import urlparse, unquote
    if "linkedin.com/search/" in (url or ""):
        return True  # constructed query, relevant by construction
    if not (name or "").strip():
        return True  # no person context (company-only flow): keep
    try:
        from .passes import name_match as _nm
    except Exception:
        _nm = None
    if _nm and _nm(title or "", name):
        return True
    # URL-slug fallback: /in/aryan-saini-... must carry the surname.
    try:
        slug = unquote(urlparse(url).path or "").lower()
        parts = [p for p in re.findall(r"[a-z]{2,}", name.lower())]
        if parts and parts[-1] in slug:
            if len(parts) == 1 or parts[0] in slug or parts[0][:3] in slug:
                return True
    except Exception:
        pass
    return False


def _filter_hits(hits: list, name: str = "") -> tuple[list, list]:
    """Drop ToS-excluded hosts, audit-logged (§8.7). LinkedIn hits are
    returned separately as manual-check references — never fetched.
    LinkedIn refs must be about THIS person (name=""), namesakes dropped."""
    kept, refs = [], []
    for h in hits:
        if _excluded(h.url):
            audit.log("tos_excluded", {"url": h.url[:120]})
            if "linkedin." in (h.url or ""):
                if _linkedin_relevant(h.url, getattr(h, "title", ""), name):
                    refs.append(h)
                else:
                    audit.log("wrong_person_dropped",
                              {"url": h.url[:120],
                               "title": getattr(h, "title", "")[:80]})
            continue
        kept.append(h)
    return kept, refs


def tier1_direct(urls: list[str]) -> TierResult:
    """Tier 1 deterministic path: explicit URLs (rep-supplied or connector)."""
    return TierResult(tier="tier1_direct",
                      hits=[SearchHit(url=u) for u in urls])


WIKI_UA = {"User-Agent": "ProspectIntel/1.0 (internal research brief tool; contact: ops@example.com)",
           "Api-User-Agent": "ProspectIntel/1.0"}

_WIKI_HEAD = re.compile(r"^==+.*==+\s*$")


def clean_wiki_text(text: str) -> str:
    """Drop MediaWiki section-markup lines and reference clutter so claims
    read as sentences, not page furniture."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or _WIKI_HEAD.match(s):
            continue
        # Citation artifacts: [\[85\]](url), [85], bare cite links.
        s = re.sub(r"\[\s*\\?\[\d+\\?\]\s*\]\([^)]*\)", "", s)
        s = re.sub(r"\[\d+\]", "", s)
        s = re.sub(r"\s{2,}", " ", s).strip()
        if s:
            out.append(s)
    return "\n".join(out)


def rank_wiki_titles(titles: list[str], query: str) -> list[str]:
    """Surname/company-token matches first — a 'Musk (film)'-style page must
    never outrank the person's own page."""
    toks = {t.lower() for t in re.findall(r"[a-z]{3,}", query.lower())}
    def key(t: str):
        tt = {x.lower() for x in re.findall(r"[a-z]{3,}", t.lower())}
        return (0 if toks & tt else 1, t)
    return sorted(titles, key=key)


def _wiki_title_relevant(title: str, name: str, company: str) -> bool:
    """Wikipedia title gate (A1): person-patterned titles must name-match
    the person (or carry the company token when there is no person query);
    org/other titles need the company token when the company is known.
    Stops unrelated pages (a BNY Mellon article in an Imperial Milestone
    brief) from ever becoming citable docs. Bare queries with neither keep
    everything (nothing to judge by)."""
    t = (title or "").strip()
    co = (company or "").strip()
    person_pattern = bool(re.fullmatch(
        r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}", t))
    if person_pattern:
        if (name or "").strip():
            try:
                from .passes import name_match as _nm
            except Exception:
                _nm = None
            return bool(_nm and _nm(t, name))
        if co.lower() not in ("", "unknown"):
            return _company_mentioned(t, co)
        return True
    if co.lower() not in ("", "unknown"):
        return _company_mentioned(t, co)
    return True


def tier1_wikipedia_docs(query: str, max_results: int = 4,
                         subject: tuple[str, str] = ("", "")
                         ) -> tuple[list, str | None]:
    """Fetch page text through the MediaWiki API (the bot-intended path —
    edge HTML blocks generic crawlers per WMF robot policy). Returns
    (§5.4) StructuredDocs + optional degradation note.

    subject=(name, company) gates titles via _wiki_title_relevant so
    unrelated pages never become docs."""
    from .acquisition import extract_entities, snapshot_raw
    from .schemas import (DocSection, FetchStatus, SourceClass, StructuredDoc)
    import time as _t
    import hashlib as _h
    try:
        with httpx.Client(timeout=25, headers=WIKI_UA) as c:
            r = c.get("https://en.wikipedia.org/w/api.php",
                      params={"action": "query", "list": "search",
                              "srsearch": query, "srlimit": max_results,
                              "format": "json"})
            if r.status_code != 200:
                return [], f"Wikipedia search HTTP {r.status_code}"
            titles = [i["title"] for i in
                      r.json().get("query", {}).get("search", [])]
            sname, sco = subject
            if sname or sco:
                kept = []
                for t in titles:
                    if _wiki_title_relevant(t, sname, sco):
                        kept.append(t)
                    else:
                        audit.log("irrelevant_dropped",
                                  {"tier": "tier1_wiki_docs",
                                   "title": t[:80]})
                titles = kept
            else:
                qtoks = {t.lower() for t in re.findall(r"[a-z]{3,}", query)}
                titles = [t for t in titles
                          if not re.fullmatch(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}",
                                               t.strip())
                          or ({x.lower() for x in re.findall(r"[a-z]{3,}", t)}
                              & qtoks)]
            titles = rank_wiki_titles(titles, query)[:max_results]
            docs = []
            for title in titles:
                e = c.get("https://en.wikipedia.org/w/api.php",
                          params={"action": "query", "prop": "extracts",
                                  "explaintext": 1, "titles": title,
                                  "format": "json"})
                pages = e.json().get("query", {}).get("pages", {}).values()
                text = next((p.get("extract", "") for p in pages), "")
                if not text.strip():
                    continue
                import urllib.parse
                page_url = ("https://en.wikipedia.org/wiki/"
                            + urllib.parse.quote(title.replace(" ", "_")))
                body = clean_wiki_text(title + "\n" + text)[:200_000]
                if len(re.findall(r"[a-z0-9]{3,}", body.lower())) < 20:
                    continue
                ch = snapshot_raw(body.encode())
                doc_id = "doc_" + ch[:16]
                docs.append(StructuredDoc(
                    doc_id=doc_id, url=page_url, url_final=page_url,
                    content_hash=ch,
                    fetched_at=_t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
                    fetch_status=FetchStatus.OK, source_class=SourceClass.OTHER,
                    title=title, sections=[DocSection(
                        section_id=doc_id + "#s0", text=body,
                        char_start=0, char_end=len(body))],
                    entities=extract_entities(body)))
            audit.log("tier1_wiki_docs", {"query": query, "docs": len(docs)})
            return docs, (None if docs
                          else "Wikipedia returned no readable pages")
    except Exception as e:
        audit.log("tier_down", {"tier": "tier1_wiki", "reason": str(e)})
        return [], f"Wikipedia docs failed: {e}"


def tier1_wikipedia_docs_for_urls(urls: list[str]) -> list:
    """Fetch exact Wikipedia pages from discover hits (misspelling-proof).

    /research excludes wikipedia.org from plain fetch (edge blocks
    crawlers) and relies on tier1_wikipedia_docs(query) — which misses
    on typos like Puneet/Punit. Discover search often still returns the
    right page URL, so fetch those titles directly via the MediaWiki API."""
    import urllib.parse
    from .acquisition import extract_entities, snapshot_raw
    from .schemas import DocSection, FetchStatus, SourceClass, StructuredDoc
    import time as _t
    titles: list[str] = []
    for u in urls:
        try:
            from urllib.parse import urlparse as _up, unquote as _uq
            p = _up(u)
            if "wikipedia.org" not in (p.hostname or ""):
                continue
            segs = p.path.split("/wiki/")
            if len(segs) != 2 or not segs[1]:
                continue
            title = _uq(segs[1].split("#")[0].split("?")[0].replace("_", " ")).strip()
            if title and title not in titles:
                titles.append(title)
        except Exception:
            continue
    if not titles:
        return []
    docs = []
    try:
        with httpx.Client(timeout=25, headers=WIKI_UA) as c:
            for title in titles[:6]:
                try:
                    e = c.get("https://en.wikipedia.org/w/api.php",
                              params={"action": "query", "prop": "extracts",
                                      "explaintext": 1, "titles": title,
                                      "format": "json"})
                    pages = e.json().get("query", {}).get("pages", {}).values()
                    text = next((p.get("extract", "") for p in pages), "")
                    if not text.strip():
                        continue
                    page_url = ("https://en.wikipedia.org/wiki/"
                                + urllib.parse.quote(title.replace(" ", "_")))
                    body = clean_wiki_text(title + "\n" + text)[:200_000]
                    if len(re.findall(r"[a-z0-9]{3,}", body.lower())) < 20:
                        continue
                    ch = snapshot_raw(body.encode())
                    doc_id = "doc_" + ch[:16]
                    docs.append(StructuredDoc(
                        doc_id=doc_id, url=page_url, url_final=page_url,
                        content_hash=ch,
                        fetched_at=_t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
                        fetch_status=FetchStatus.OK, source_class=SourceClass.OTHER,
                        title=title, sections=[DocSection(
                            section_id=doc_id + "#s0", text=body,
                            char_start=0, char_end=len(body))],
                        entities=extract_entities(body)))
                except Exception:
                    continue
        if docs:
            audit.log("tier1_wiki_docs_direct", {"urls": len(titles), "docs": len(docs)})
    except Exception as e:
        audit.log("tier_down", {"tier": "tier1_wiki_direct", "reason": str(e)})
    return docs


def tier1_wikipedia(query: str, max_results: int = 4) -> TierResult:
    """Tier 1 public-document connector (no key, deterministic first-party API,
    ToS-friendly). Allowed for person queries: it is a public reference work,
    not a behavioral search engine (§5.1 routing targets Tier-3 style engines)."""
    try:
        import urllib.parse
        with httpx.Client(timeout=20, headers=WIKI_UA) as c:
            r = c.get("https://en.wikipedia.org/w/api.php",
                      params={"action": "query", "list": "search",
                              "srsearch": query, "srlimit": max_results,
                              "format": "json"})
            if r.status_code != 200:
                return TierResult(tier="tier1_wiki", available=False,
                                  note=f"Wikipedia search HTTP {r.status_code}")
            items = r.json().get("query", {}).get("search", [])
            hits = [SearchHit(
                url="https://en.wikipedia.org/wiki/" + urllib.parse.quote(
                    i["title"].replace(" ", "_")),
                title=i["title"]) for i in items]
            return TierResult(tier="tier1_wiki", hits=hits)
    except Exception as e:
        audit.log("tier_down", {"tier": "tier1_wiki", "reason": str(e)})
        return TierResult(tier="tier1_wiki", available=False, note=str(e))


def tier1_google_news_rss(query: str, max_results: int = 6
                           ) -> tuple[list[SearchHit], dict[str, tuple[str, str]]]:
    """Tier-1 recent-press feed (no key, bot-friendly RSS). Returns hits +
    meta {url: (title, published_at)} so structuring stamps recency (§7.5)."""
    import urllib.parse
    import xml.etree.ElementTree as ET
    params = {"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)
    hits, meta = [], {}
    try:
        import itertools
        with httpx.Client(timeout=20,
                          headers={"User-Agent": WIKI_UA["User-Agent"]}) as c:
            r = c.get(url)
            if r.status_code != 200:
                return [], {}
            root = ET.fromstring(r.content[:1_000_000])
            for item in itertools.islice(root.iter("item"), max_results):
                link = (item.findtext("link") or "").strip()
                title = (item.findtext("title") or "").strip()[:300]
                pub = (item.findtext("pubDate") or "").strip()
                outlet = title.rsplit(" - ", 1)[-1].strip()[:80] if " - " in title else ""
                if link.startswith("http"):
                    hits.append(SearchHit(url=link, title=title, outlet=outlet))
                    meta[link] = (title, pub)
        audit.log("tier1_news_rss", {"query": query, "hits": len(hits)})
    except Exception as e:
        audit.log("tier_down", {"tier": "tier1_news_rss", "reason": str(e)})
    return hits, meta


def tier1_news_api(query: str, max_results: int = 8) -> TierResult:
    """Tier 1 news connector. Needs NEWSAPI_KEY; without it — or on any
    non-200 (bad key, quota) — reports down LOUDLY, never empty-success."""
    key = os.environ.get("NEWSAPI_KEY", "")
    if not key:
        audit.log("tier_down", {"tier": "tier1_news", "reason": "no NEWSAPI_KEY"})
        return TierResult(tier="tier1_news", available=False,
                          note="Tier-1 news connector down (no key): brief marked degraded, no silent fallback")
    try:
        with httpx.Client(timeout=15) as c:
            r = c.get("https://newsapi.org/v2/everything",
                      params={"q": query, "pageSize": max_results, "apiKey": key})
            if r.status_code != 200:
                audit.log("tier_down", {"tier": "tier1_news",
                                        "reason": f"HTTP {r.status_code}"})
                return TierResult(
                    tier="tier1_news", available=False,
                    note=f"Tier-1 news connector HTTP {r.status_code} "
                         "(bad key or quota): brief marked degraded")
            items = r.json().get("articles", [])
            return TierResult(tier="tier1_news",
                              hits=[SearchHit(url=a["url"], title=a.get("title", ""))
                                    for a in items if a.get("url")])
    except Exception as e:
        return TierResult(tier="tier1_news", available=False, note=str(e))


GNEWS_DAILY_CAP = 100  # free plan: 100 req/day → ≤10 people at ~6-10 req/person
GNEWS_PER_PERSON = 6  # hard per-brief cap: identity(1)+company/news(2)+refill(1)+spare(2)


def _gnews_quota_ok() -> str | None:
    """Daily quota guard: returns loud reason when exhausted, else None."""
    import datetime as _dt
    try:
        cap = int(os.environ.get("GNEWS_DAILY_CAP", str(GNEWS_DAILY_CAP)))
    except (ValueError, TypeError):
        cap = GNEWS_DAILY_CAP
    if cap <= 0:
        return None  # 0 = unlimited (self-hosted/paid)
    try:
        from . import store as _store
        con = _store.connect()
        try:
            bucket = _dt.date.today().isoformat()
            row = con.execute("SELECT calls FROM enrich_usage WHERE source=? AND bucket=?",
                              ("gnews", bucket)).fetchone()
            used = row[0] if row else 0
        finally:
            con.close()
        if used >= cap:
            return (f"GNews daily quota reached ({used}/{cap}): "
                    "cached data only, no new news calls today")
        return None
    except Exception:
        return None  # store unreachable: allow, audit downstream


def _gnews_record_use() -> None:
    import datetime as _dt
    try:
        from . import store as _store
        con = _store.connect()
        try:
            bucket = _dt.date.today().isoformat()
            con.execute("INSERT INTO enrich_usage(source, bucket, calls) VALUES (?,?,1) "
                        "ON CONFLICT(source, bucket) DO UPDATE SET calls=calls+1",
                        ("gnews", bucket))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def tier1_gnews(query: str, max_results: int = 8) -> TierResult:
    """Tier-1 GNews connector (gnews.io, deterministic news source).

    Needs GNEWS_API_KEY (NOT NewsAPI — different service; never stuff a
    GNews key into NEWSAPI_KEY). Quota-guarded (100/day free) + 24h cached
    by normalized query so repeat briefs cost zero. Reports down LOUDLY.
    Hits carry outlet=publisher name; published dates ride the article fetch.
    """
    key = os.environ.get("GNEWS_API_KEY", "")
    if not key:
        audit.log("tier_down", {"tier": "tier1_gnews", "reason": "no GNEWS_API_KEY"})
        return TierResult(tier="tier1_gnews", available=False,
                          note="Tier-1 GNews down (no key): brief marked degraded, no silent fallback")
    # 24h cache FIRST: repeat briefs cost zero quota even when capped.
    ckey = "q:" + re.sub(r"\s+", " ", (query or "").strip().lower())[:120]
    try:
        from . import enrich as _e
        cached = _e._cache_get("gnews", ckey)
        if cached is not None and isinstance(cached, dict):
            hits = [SearchHit(url=h.get("url", ""), title=h.get("title", ""),
                              outlet=h.get("outlet", ""))
                    for h in cached.get("hits", []) if h.get("url")]
            if hits:
                return TierResult(tier="tier1_gnews", hits=hits[:max_results])
    except Exception:
        pass
    blocked = _gnews_quota_ok()
    if blocked:
        audit.log("tier_down", {"tier": "tier1_gnews", "reason": "quota"})
        return TierResult(tier="tier1_gnews", available=False, note=blocked)
    try:
        lang = os.environ.get("GNEWS_LANG", "en") or "en"
        country = os.environ.get("GNEWS_COUNTRY", "") or ""
        params = {"q": query, "token": key, "max": max(1, min(10, max_results)),
                  "lang": lang, "sortby": "publishedAt"}
        if country:
            params["country"] = country
        with httpx.Client(timeout=15) as c:
            r = c.get("https://gnews.io/api/v4/search", params=params)
            if r.status_code in (401, 403):
                audit.log("tier_down", {"tier": "tier1_gnews",
                                        "reason": f"HTTP {r.status_code}"})
                return TierResult(tier="tier1_gnews", available=False,
                                  note=f"Tier-1 GNews HTTP {r.status_code} "
                                       "(bad key): brief marked degraded")
            if r.status_code == 429:
                audit.log("tier_down", {"tier": "tier1_gnews",
                                        "reason": "HTTP 429 quota"})
                return TierResult(tier="tier1_gnews", available=False,
                                  note="Tier-1 GNews quota exhausted (429): "
                                       "cached data only")
            if r.status_code != 200:
                audit.log("tier_down", {"tier": "tier1_gnews",
                                        "reason": f"HTTP {r.status_code}"})
                return TierResult(tier="tier1_gnews", available=False,
                                  note=f"Tier-1 GNews HTTP {r.status_code}: "
                                       "brief marked degraded")
            items = r.json().get("articles", [])
            hits = [SearchHit(url=a.get("url", ""),
                              title=(a.get("title", "") or "")[:300],
                              outlet=((a.get("source") or {}).get("name", "")
                                      or "")[:80])
                    for a in items if a.get("url", "").startswith("http")]
            _gnews_record_use()
            try:
                from . import enrich as _e2
                _e2._cache_put("gnews", ckey,
                               {"hits": [{"url": h.url, "title": h.title,
                                          "outlet": h.outlet} for h in hits]})
            except Exception:
                pass
            audit.log("tier1_gnews", {"query": (query or "")[:80],
                                      "hits": len(hits)})
            return TierResult(tier="tier1_gnews", hits=hits[:max_results])
    except Exception as e:
        audit.log("tier_down", {"tier": "tier1_gnews", "reason": str(e)[:120]})
        return TierResult(tier="tier1_gnews", available=False, note=str(e))


_DBP_ONT = "http://dbpedia.org/ontology/"


def _dbp_first(vals: list, want: str = "literal") -> str:
    for v in vals or []:
        if v.get("type") == want:
            return str(v.get("value", ""))
    return ""


def _dbp_name(uri: str) -> str:
    return uri.rstrip("/").split("/")[-1].replace("_", " ").split("(")[0].strip()


def dbpedia_company(company: str) -> tuple[dict, str | None]:
    """Keyless deterministic firmographics (DBpedia structured data).
    Returns (fields, source_url). Fields: homepage, employees, hq,
    industry, founded. No-data vs failure distinguished by caller.
    Tries the canonical alias first ('Zee' -> 'Zee Entertainment
    Enterprises') then the literal, so bare brands resolve."""
    import urllib.parse
    candidates = [company]
    canon = canonical_company(company)
    if canon.lower() != (company or "").strip().lower():
        candidates.insert(0, canon)
    last_note: str | None = None
    for cand in candidates:
        slug = urllib.parse.quote("_".join(w.capitalize() for w in cand.split()))
        url = f"https://dbpedia.org/data/{slug}.json"
        try:
            with httpx.Client(timeout=20,
                              headers={"User-Agent": WIKI_UA["User-Agent"],
                                       "Accept": "application/json"}) as c:
                r = c.get(url)
                if r.status_code != 200:
                    last_note = f"DBpedia HTTP {r.status_code} for {company}"
                    continue
                rec = r.json().get(f"http://dbpedia.org/resource/{slug}", {})
                if not rec:
                    last_note = f"DBpedia: no record for {company}"
                    continue
                flat: dict[str, list] = {}
                for k, v in rec.items():  # namespaces vary (ontology/property/foaf)
                    flat.setdefault(k.split("/")[-1].split("#")[-1], v)
                fields: dict[str, str] = {}
                hp = _dbp_first(flat.get("homepage", []), "uri")
                if hp:
                    hp = urllib.parse.unquote(hp).split("|")[0].strip().rstrip("/")
                    if hp:
                        fields["homepage"] = hp
                for key, out in (("numberOfEmployees", "employees"),
                                 ("numEmployees", "employees"),
                                 ("foundingYear", "founded"),
                                 ("foundingDate", "founded")):
                    v = _dbp_first(flat.get(key, []))
                    if v and out not in fields:
                        fields[out] = v[:4] if out == "founded" else v
                for key, out in (("locationCity", "hq"), ("locationCountry", "hq"),
                                 ("headquarter", "hq"), ("headquarters", "hq"),
                                 ("industry", "industry")):
                    for v in flat.get(key, []) or []:
                        if v.get("type") == "uri":
                            name = _dbp_name(v["value"])
                            if name and out not in fields:
                                fields[out] = name
                            break
                rev = _dbp_first(flat.get("revenue", []))
                if rev:
                    fields["revenue"] = rev
                # Empty record (e.g. disambiguation "Zee") is not a match:
                # try the next candidate instead of caching emptiness.
                if not fields:
                    last_note = f"DBpedia: no firmographic fields for {company}"
                    continue
                audit.log("tier1_dbpedia", {"company": company,
                                            "fields": sorted(fields)})
                return fields, None
        except Exception as e:
            audit.log("tier_down", {"tier": "tier1_dbpedia", "reason": str(e)})
            last_note = f"DBpedia unreachable: {e}"
            continue
    return {}, last_note or f"DBpedia: no record for {company}"


def tier2_searxng(query: str, max_results: int = 8) -> TierResult:
    """Tier 2 self-hosted SearXNG (queries never leave our infrastructure).
    SEARXNG_URL unset → down (loud). Paced single request; ops hardening
    (rotation, sharding) is a Phase-4 concern per §11."""
    base = os.environ.get("SEARXNG_URL", "").rstrip("/")
    if not base:
        return TierResult(tier="tier2_searxng", available=False,
                          note="SearXNG not configured (SEARXNG_URL): breadth tier unavailable")
    try:
        import random as _rand
        time.sleep(1.0 + _rand.uniform(0, 0.4))  # paced + jitter (§5.1)
        last_err = "no response"
        with httpx.Client(timeout=12) as c:
            for attempt in range(2):
                try:
                    r = c.get(base + "/search",
                              params={"q": query, "format": "json"})
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"
                    time.sleep(min(4.0, 0.5 * (2 ** attempt))
                               + _rand.uniform(0, 0.3))
                    continue
                if r.status_code == 429:
                    # Suspended engines need minutes, not a 0.5s retry —
                    # fail fast (no second attempt) so the budget survives.
                    last_err = f"HTTP {r.status_code}"
                    audit.log("tier_retry", {"tier": "tier2_searxng",
                                             "http": r.status_code,
                                             "try": attempt + 1,
                                             "note": "suspended: no retry"})
                    break
                if r.status_code in (500, 502, 503):
                    last_err = f"HTTP {r.status_code}"
                    audit.log("tier_retry", {"tier": "tier2_searxng",
                                             "http": r.status_code,
                                             "try": attempt + 1})
                    time.sleep(min(4.0, 0.5 * (2 ** attempt))
                               + _rand.uniform(0, 0.3))
                    continue
                try:
                    payload = r.json()
                except Exception:
                    last_err = "invalid JSON"
                    break
                items = (payload.get("results", []) or []) \
                    if r.status_code == 200 else []
                return TierResult(tier="tier2_searxng",
                                  hits=[SearchHit(url=i.get("url", ""),
                                                  title=i.get("title", ""))
                                        for i in items[:max_results]
                                        if isinstance(i, dict)
                                        and i.get("url", "").startswith("http")])
        audit.log("tier_down", {"tier": "tier2_searxng", "reason": last_err})
        return TierResult(tier="tier2_searxng", available=False,
                          note=f"SearXNG failed after retries ({last_err})")
    except Exception as e:
        audit.log("tier_down", {"tier": "tier2_searxng", "reason": str(e)})
        return TierResult(tier="tier2_searxng", available=False, note=str(e))


def tier3_commercial(query: str, max_results: int = 8,
                     person_query: bool = False) -> TierResult:
    """Tier 3 last resort. REFUSES person-name / company+intent queries (§5.1)."""
    if person_query:
        audit.log("tier_refused", {"tier": "tier3",
                                   "reason": "person/intent query routing rule"})
        return TierResult(tier="tier3", available=False,
                          note="Tier-3 refused by routing rule (person/intent query): confidentiality over coverage")
    if not os.environ.get("SERPAPI_KEY", ""):
        # Keyless local breadth: Firecrawl search (Tier-3 class — caller
        # guarantees non-person queries here). DuckDuckGo as last resort.
        try:
            from .firecrawl import search as fc_search
            out = [SearchHit(url=u, title=t)
                   for u, t, _ in fc_search(query, max_results)]
            out, _ = _filter_hits(out)
            if out:
                return TierResult(tier="tier3", hits=out,
                                  note="local Firecrawl breadth search")
        except Exception:
            pass
        return TierResult(tier="tier3", available=False,
                          note="Tier-3 down (no key, no local search)")
    hits = DuckDuckGoProvider().search(query, max_results)  # availability floor
    return TierResult(tier="tier3", hits=hits,
                      note="fallback breadth source (availability floor)")


def linkedin_manual_refs(name: str, company: str) -> list[SearchHit]:
    """Manual-check LinkedIn links, constructed not fetched (§8.7, §2.2).

    Previously refs only appeared when SearXNG happened to return a
    linkedin.com hit. When SearXNG engines suspend (rate limits) refs went
    empty even though the rep could still open LinkedIn manually. These
    URLs are NEVER fetched — only rendered as 'open manually' references."""
    import urllib.parse
    out: list[SearchHit] = []
    n, c = (name or "").strip(), (company or "").strip()
    if n:
        q = urllib.parse.quote_plus(f"{n} {c}".strip())
        out.append(SearchHit(
            url=f"https://www.linkedin.com/search/results/all/?keywords={q}",
            title=f"LinkedIn: {n}" + (f" @ {c}" if c and c.lower() != "unknown" else "")))
    if c and c.lower() != "unknown":
        q = urllib.parse.quote_plus(c)
        out.append(SearchHit(
            url=f"https://www.linkedin.com/search/results/companies/?keywords={q}",
            title=f"LinkedIn company: {c}"))
    return out


@dataclass
class Discovery:
    hits: list[SearchHit] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    tiers_used: list[str] = field(default_factory=list)
    references: list[SearchHit] = field(default_factory=list)  # manual-check only
    docs: list = field(default_factory=list)  # pre-structured Tier-1 enrich docs
    firmo_extra: dict = field(default_factory=dict)  # registry/enrich fields


def discover_person(name: str, company: str, query: str = "",
                    max_results: int = 8,
                    deadline: float | None = None) -> Discovery:
    """Name-in discovery. Person queries NEVER touch Tier 3. Degradation is
    declared in `degraded`, never silent (§10.2). deadline stops intake for
    slow tiers (partial, marked)."""
    from . import deadline as _dl
    q = query or f"{name} {company}"
    d = Discovery()
    t1 = tier1_news_api(q, max_results)
    d.tiers_used.append("tier1_news")
    if not t1.available:
        d.degraded.append(t1.note)
    else:
        # Strict relevance: unrelated hits never reach fetch (§6.3).
        t1kept, t1note = _gate_hits(t1.hits, name, company, "tier1_news")
        d.hits.extend(t1kept)
        if t1note:
            d.degraded.append(t1note)
    # GNews deterministic news (quota: ≤6/brief of 100/day; cached 24h).
    # Runs before breadth so authentic press leads; gated identically.
    if not _dl.expired(deadline):
        try:
            tg = tier1_gnews(q, min(6, max_results))
        except Exception:
            tg = TierResult(tier="tier1_gnews", available=False,
                            note="GNews connector error")
        d.tiers_used.append("tier1_gnews")
        if not tg.available:
            d.degraded.append(tg.note)
        else:
            gkept, gnote = _gate_hits(tg.hits, name, company, "tier1_gnews")
            d.hits.extend(gkept)
            if gnote:
                d.degraded.append(gnote)
    t2 = tier2_searxng(q, max_results) if not _dl.expired(deadline) else TierResult(
        tier="tier2_searxng", available=False,
        note="SearXNG skipped (request budget spent): breadth reduced")
    d.tiers_used.append("tier2_searxng")
    if not t2.available:
        d.degraded.append(t2.note)
    else:
        kept, refs = _filter_hits(t2.hits, name)
        gkept, gnote = _gate_hits(kept, name, company, "tier2_searxng")
        d.hits.extend(gkept)
        d.references.extend(refs)
        if gnote:
            d.degraded.append(gnote)
    tw = tier1_wikipedia(q, 4) if not _dl.expired(deadline) else TierResult(
        tier="tier1_wiki", available=False,
        note="Wikipedia skipped (request budget spent)")
    d.tiers_used.append("tier1_wiki")
    if not tw.available:
        d.degraded.append(tw.note)
    else:
        # person-query relevance: a wiki page titled as a DIFFERENT person's
        # name (Bipasha Basu for Hiren Gada) is never his story — drop it
        # here so it can't seed bio/details downstream. Company/org pages
        # (Enterprises, Group, ...) are kept: they feed company context.
        try:
            from .passes import name_match as _nm
        except Exception:
            _nm = None
        for h in tw.hits:
            title = (h.title or "").replace(" - Wikipedia", "").strip()
            if (_nm and "wikipedia.org/wiki/" in h.url
                    and re.fullmatch(
                        r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}", title)
                    and not _nm(title, name)):
                audit.log("wrong_person_dropped", {"url": h.url[:120]})
                continue
            d.hits.append(h)
    t3 = tier3_commercial(q, max_results, person_query=True)
    d.tiers_used.append("tier3")
    d.degraded.append(t3.note)  # always refused for person queries — by design
    # Tier-1 enrichment (free + legal APIs: Wikidata keyless, GLEIF keyless).
    # Pre-structured docs with exact spans. Skipped whole when the budget
    # is already spent (partial, marked).
    try:
        from . import enrich as _enrich
        _edocs, _enotes, _cdocs, _cfirmo, _cnotes = [], [], [], {}, []
        if _enrich.enabled() and not _dl.expired(deadline):
            d.tiers_used.append("tier1_enrich")
            _edocs, _, _enotes = _enrich.enrich_person(name) if (name or "").strip() else ([], {}, [])
            _cdocs, _cfirmo, _cnotes = _enrich.enrich_company(company) \
                if (company or "").strip().lower() not in ("", "unknown") else ([], {}, [])
        for _n in _enotes + _cnotes:
            if _n not in d.degraded:
                d.degraded.append(_n)
        _seen_docs = set()
        for _doc in _edocs + _cdocs:
            if _doc.doc_id not in _seen_docs:
                _seen_docs.add(_doc.doc_id)
                d.docs.append(_doc)
        for _k, _v in _cfirmo.items():
            if _k == "sources":
                d.firmo_extra["sources"] = list(dict.fromkeys(
                    d.firmo_extra.get("sources", []) + list(_v or [])))[:5]
            elif _v and _k not in d.firmo_extra:
                d.firmo_extra[_k] = _v
    except Exception as e:
        d.degraded.append(f"enrichment connector error ({type(e).__name__}): "
                          "enrichment skipped, not 'no data'")
    # LinkedIn manual refs: always present, never depend on SearXNG health.
    for ref in linkedin_manual_refs(name, company):
        if ref.url not in {h.url for h in d.references}:
            d.references.append(ref)
    seen, uniq = set(), []
    for h in d.hits:
        if h.url not in seen:
            seen.add(h.url)
            uniq.append(h)
    d.hits = uniq[:max_results]
    audit.log("discovered", {"query": q, "hits": len(d.hits),
                             "degraded": d.degraded})
    return d


def company_diet(company: str, name: str, docs: list, degraded: list,
                 fetch_full: bool = True, max_angles: int = 4,
                 deadline: float | None = None) -> list:
    """Shared company diet for all three flows (person /research,
    /company-research, interactive): segment press+hiring angles,
    relevance-gated; optional full-article fetch, always with dated
    headline fallback spans. Full-article fetch is plain-HTTP only
    (headlines already cover; render reserved for owned/direct pages)
    and capped, so diet can never blow the request budget. Mutates
    docs/degraded in place. Returns docs."""
    from . import deadline as _dl
    from .acquisition import acquire, queue_to_core, _MEM_QUEUE
    from .acquisition import stamp_docs, structure_rss_items
    from .schemas import SourceClass
    from .segment import active as _seg
    co = (company or "").strip()
    if not co or co.lower() == "unknown":
        return docs
    try:
        angles = [a.format(co=co) for a in _seg()["rss_angles"]]
        if (name or "").strip():
            angles = [f"{name.strip()} {co}"] + angles
        for _a in angles[:max_angles]:
            if _dl.expired(deadline):
                degraded.append("company diet cut short (request budget "
                                "spent): press coverage reduced")
                break
            _hits, _meta = tier1_google_news_rss(_a, 4)
            _rel = [h for h in _hits if hit_relevant(
                h.url, h.title, name or "", co)]
            for h in _hits:
                if h not in _rel:
                    audit.log("irrelevant_dropped",
                              {"tier": "tier1_news_rss", "url": h.url[:120],
                               "title": (h.title or "")[:80]})
            if fetch_full:
                _new = [h.url for h in _rel
                        if h.url not in {d.url for d in docs}][:6]
                try:
                    docs.extend(stamp_docs(acquire(_new, SourceClass.NEWS,
                                                   deadline=deadline,
                                                   smart=False),
                                           {u: _meta.get(u, ("", ""))
                                            for u in _new}))
                except Exception:
                    pass
            _seen_urls = {d.url for d in docs}
            try:
                for rd in structure_rss_items(
                        [(h.url, h.title, _meta.get(h.url, ("", ""))[1],
                          h.outlet) for h in _rel
                         if h.url not in _seen_urls]):
                    _seen_urls.add(rd.url)
                    docs.append(rd)
                    try:
                        queue_to_core(rd, _MEM_QUEUE)
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception as e:
        degraded.append(f"company diet failed ({type(e).__name__}): "
                        "press coverage reduced")
    return docs


def discover_company(company: str, max_results: int = 8,
                     deadline: float | None = None) -> Discovery:
    """Company-in discovery (no person). Pure-company queries carry no
    person intent, so Tier 3 breadth is ALLOWED here (§5.1 routing targets
    person/intent queries). Person-specific filters are off; the strict
    company-token gate still applies to every hit. deadline stops intake
    for slow tiers (partial, marked)."""
    from . import deadline as _dl
    d = Discovery()
    co = (company or "").strip()
    # Pass 0 identity grounding (deterministic registers before web search).
    # Adds registry docs/firmo (lei/legal_name/cin/hq) + degraded notes;
    # never blocks tiers on failure. Falls back to COMPANY_ALIASES inside
    # resolvers when namesakes collide.
    _resolved, _website, _verified = co, "unknown", False
    try:
        from . import resolvers as _res
        _p0_firmo, _p0_docs, _p0_deg, _p0_ok, _p0_name, _p0_web = \
            _res.get_ground_truth(co, deadline)
        _resolved, _website, _verified = _p0_name or co, _p0_web or "unknown", bool(_p0_ok)
        if _p0_firmo or _p0_docs or _p0_deg:
            d.tiers_used.append("pass0_resolvers")
        for _n in _p0_deg:
            if _n not in d.degraded:
                d.degraded.append(_n)
        _seen_p0 = set()
        for _doc in _p0_docs:
            if _doc.doc_id not in _seen_p0:
                _seen_p0.add(_doc.doc_id)
                d.docs.append(_doc)
        for _k, _v in (_p0_firmo or {}).items():
            if _k == "sources":
                d.firmo_extra["sources"] = list(dict.fromkeys(
                    d.firmo_extra.get("sources", []) + list(_v or [])))[:5]
            elif _v and _k not in d.firmo_extra:
                d.firmo_extra[_k] = _v
    except Exception as e:
        d.degraded.append(f"Pass-0 resolver error ({type(e).__name__}): "
                          "web search proceeds on company name")
    t1 = tier1_news_api(co, max_results)
    d.tiers_used.append("tier1_news")
    if not t1.available:
        d.degraded.append(t1.note)
    else:
        kept, note = _gate_hits(t1.hits, "", co, "tier1_news")
        d.hits.extend(kept)
        if note:
            d.degraded.append(note)
    if not _dl.expired(deadline):
        try:
            tg = tier1_gnews(co, min(6, max_results))
        except Exception:
            tg = TierResult(tier="tier1_gnews", available=False,
                            note="GNews connector error")
        d.tiers_used.append("tier1_gnews")
        if not tg.available:
            d.degraded.append(tg.note)
        else:
            gkept, gnote = _gate_hits(tg.hits, "", co, "tier1_gnews")
            d.hits.extend(gkept)
            if gnote:
                d.degraded.append(gnote)
    t2 = tier2_searxng(co, max_results) if not _dl.expired(deadline) else TierResult(
        tier="tier2_searxng", available=False,
        note="SearXNG skipped (request budget spent): breadth reduced")
    d.tiers_used.append("tier2_searxng")
    if not t2.available:
        d.degraded.append(t2.note)
    else:
        kept, refs = _filter_hits(t2.hits, "")
        gkept, gnote = _gate_hits(kept, "", co, "tier2_searxng")
        d.hits.extend(gkept)
        d.references.extend(refs)
        if gnote:
            d.degraded.append(gnote)
    tw = tier1_wikipedia(co, 4) if not _dl.expired(deadline) else TierResult(
        tier="tier1_wiki", available=False,
        note="Wikipedia skipped (request budget spent)")
    d.tiers_used.append("tier1_wiki")
    if not tw.available:
        d.degraded.append(tw.note)
    else:
        # Company gate (A1): a page titled for someone/something else is
        # never this company's story.
        for h in tw.hits:
            title = (h.title or "").replace(" - Wikipedia", "").strip()
            if _wiki_title_relevant(title or h.url, "", co):
                d.hits.append(h)
            else:
                audit.log("irrelevant_dropped",
                          {"tier": "tier1_wiki", "url": h.url[:120],
                           "title": (h.title or "")[:80]})
    t3 = tier3_commercial(co, max_results, person_query=False) \
        if not _dl.expired(deadline) else TierResult(
            tier="tier3", available=False,
            note="Tier-3 skipped (request budget spent)")
    d.tiers_used.append("tier3")
    if not t3.available:
        d.degraded.append(t3.note)
    else:
        kept, _ = _filter_hits(t3.hits, "")
        gkept, gnote = _gate_hits(kept, "", co, "tier3")
        d.hits.extend(gkept)
        if gnote:
            d.degraded.append(gnote)
        elif t3.note:
            d.degraded.append(t3.note)
    # Pass 1 scoped queries (governance + filings via SearXNG/Tier-3 breadth;
    # company-only so Tier-3 is allowed). Bare-brand tiers above preserve
    # recall; scoped queries add precision (investor/registry pages). Every
    # hit still passes the company-token gate; strategy angles stay in
    # company_diet (segment RSS), not duplicated here.
    try:
        from .search_scoper import build_scoped_queries
        from .segment import active as _seg_active
        _gt = {"legal_name": _resolved, "official_website": _website,
               "company": co}
        _scoped = build_scoped_queries(_gt, _seg_active())[:2]
        for _sq in _scoped:
            if _dl.expired(deadline):
                break
            try:
                _sr = tier2_searxng(_sq, 5)
                if _sr.available:
                    _kept, _refs = _filter_hits(_sr.hits, "")
                    _gkept, _ = _gate_hits(_kept, "", co, "tier2_scoped")
                    # Accept on resolved legal name too (bare "Zee" gate
                    # would drop "ZEE ENTERTAINMENT..." titled hits).
                    if _resolved.lower() != co.lower():
                        _extra = [h for h in _kept
                                  if h not in _gkept and _company_mentioned(
                                      f"{h.title or ''}\n{h.url or ''}",
                                      _resolved)]
                        _gkept = _gkept + _extra
                    d.hits.extend(_gkept)
                    d.references.extend([r for r in _refs
                                         if r.url not in {x.url for x in d.references}])
            except Exception:
                pass
            if _dl.expired(deadline):
                break
            try:
                _cr = tier3_commercial(_sq, 5, person_query=False)
                if _cr.available:
                    _ckept, _ = _filter_hits(_cr.hits, "")
                    _cgkept, _ = _gate_hits(_ckept, "", co, "tier3_scoped")
                    if _resolved.lower() != co.lower():
                        _cextra = [h for h in _ckept
                                   if h not in _cgkept and _company_mentioned(
                                       f"{h.title or ''}\n{h.url or ''}",
                                       _resolved)]
                        _cgkept = _cgkept + _cextra
                    d.hits.extend(_cgkept)
            except Exception:
                pass
    except Exception:
        pass
    try:
        from . import enrich as _enrich
        if _enrich.enabled() and not _dl.expired(deadline):
            d.tiers_used.append("tier1_enrich")
            _cdocs, _cfirmo, _cnotes = _enrich.enrich_company(co)
            for _n in _cnotes:
                if _n not in d.degraded:
                    d.degraded.append(_n)
            _seen_docs = set()
            for _doc in _cdocs:
                if _doc.doc_id not in _seen_docs:
                    _seen_docs.add(_doc.doc_id)
                    d.docs.append(_doc)
            for _k, _v in _cfirmo.items():
                if _k == "sources":
                    d.firmo_extra["sources"] = list(dict.fromkeys(
                        d.firmo_extra.get("sources", []) + list(_v or [])))[:5]
                elif _v and _k not in d.firmo_extra:
                    d.firmo_extra[_k] = _v
    except Exception as e:
        d.degraded.append(f"enrichment connector error ({type(e).__name__}): "
                          "enrichment skipped, not 'no data'")
    # Pass-0 supersedes the legacy LEI-chain note: when direct GLEIF lookup
    # verified an LEI, "no LEI on the Wikidata record" is stale noise —
    # drop it so degraded[] never contradicts a verified registry.
    if d.firmo_extra.get("lei"):
        d.degraded = [n for n in d.degraded
                      if "no LEI on the Wikidata record" not in n]
    for ref in linkedin_manual_refs("", co):
        if ref.url not in {h.url for h in d.references}:
            d.references.append(ref)
    seen, uniq = set(), []
    for h in d.hits:
        if h.url not in seen:
            seen.add(h.url)
            uniq.append(h)
    d.hits = uniq[:max_results]
    audit.log("discovered_company", {"company": co, "hits": len(d.hits),
                                     "degraded": d.degraded})
    return d
