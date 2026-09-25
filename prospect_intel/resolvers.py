"""Pass 0 identity grounding (deterministic, no scraping).

Queries authoritative structured registers BEFORE any web search so a bare
brand ("Zee") resolves to its legal entity (ZEE ENTERTAINMENT ENTERPRISES
LIMITED, LEI 254900EQIYPXZEO10B94) without crawler noise.

Contract (mirrors enrich.py): get_ground_truth() returns
(firmo_extra, docs, degraded, identity_verified, resolved_name,
official_website). Callers feed those into the existing pipeline
(discovery -> acquire -> passes 2-4); nothing here fetches web pages,
writes briefs, or invents keys — only the existing contract fields
(firmo_extra / unknowns via gaps / degraded) are populated.
"""
from __future__ import annotations
import re
import time

import httpx

from . import audit

GLEIF_API = "https://api.gleif.org/api/v1/lei-records"
GLEIF_UA = {"User-Agent": "ProspectIntel/1.0 (internal research brief tool; contact: ops@example.com)"}
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
SPARQL_UA = {"User-Agent": "ProspectIntel/1.0 (internal research brief tool; contact: ops@example.com)"}
CACHE_TTL_S = 30 * 86400


def _disabled() -> bool:
    import os
    return os.environ.get("ENRICH_DISABLE", "").strip() == "1"


def _norm_alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _query_tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]{3,}", (s or "").lower())]


def _cache_get(source: str, ckey: str):
    try:
        from . import enrich as _enrich
        return _enrich._cache_get(source, ckey)
    except Exception:
        return None


def _cache_put(source: str, ckey: str, payload) -> None:
    try:
        from . import enrich as _enrich
        _enrich._cache_put(source, ckey, payload)
    except Exception:
        pass


def _expired(deadline: float | None) -> bool:
    try:
        from . import deadline as _dl
        return _dl.expired(deadline)
    except Exception:
        return False


def _gleif_extract(item: dict) -> dict:
    """Pull the fields we keep from one lei-record item."""
    try:
        attrs = item.get("attributes", {}) or {}
        ent = attrs.get("entity", {}) or {}
        legal = ((ent.get("legalName", {}) or {}).get("name", "") or "").strip()
        reg = attrs.get("registration", {}) or {}
        legal_addr = ent.get("legalAddress", {}) or {}
        hq_addr = ent.get("headquartersAddress", {}) or {}

        def _addr_str(a: dict) -> str:
            parts = []
            city = str(a.get("city", "") or "").strip()
            country = str(a.get("country", "") or "").strip()
            lines = [str(x) for x in (a.get("addressLines", []) or [])][:2]
            extra = [x for x in lines if x and x not in (city, country)]
            if city:
                parts.append(city)
            if country and country not in parts:
                parts.append(country)
            return ", ".join(extra + parts)

        hq = _addr_str(hq_addr) or _addr_str(legal_addr)
        return {
            "legal_name": legal,
            "lei": str(attrs.get("lei", "") or "").strip().upper(),
            "status": str(ent.get("status", "") or ""),
            "jurisdiction": str(ent.get("jurisdiction", "") or ""),
            "city": str(legal_addr.get("city", "") or ""),
            "hq": hq,
            "cin": str(ent.get("registeredAs", "") or ""),
            "reg_status": str(reg.get("status", "") or ""),
        }
    except Exception:
        return {}


def query_gleif(name: str, deadline: float | None = None
                ) -> tuple[dict, list, str | None, bool]:
    """GLEIF name lookup with the namesake ambiguity gate.

    filter[entity.legalName] is substring: "Zee" matches Zee Entertainment,
    Zee Learn, Zee Media, ... Blindly taking index 0 is wrong-entity bait
    (M1 gate, same rule as enrich.gleif_by_lei). Returns
    (firmo_extra, docs, note, matched_exactly).
    """
    from .schemas import SourceClass
    if _disabled() or not (name or "").strip() or _expired(deadline):
        return {}, [], None, False
    q = (name or "").strip()
    try:
        from .discovery import canonical_company as _canon
        canon = _canon(q)
    except Exception:
        canon = q
    ckey = f"pass0-gleif:{_norm_alnum(canon) or _norm_alnum(q)}"
    cached = _cache_get("gleif", ckey)
    payload = cached
    if payload is None:
        try:
            with httpx.Client(timeout=8, headers=GLEIF_UA) as c:
                r = c.get(GLEIF_API, params={
                    "filter[entity.legalName]": canon, "page[size]": 10})
                if r.status_code != 200:
                    audit.log("tier_down", {"tier": "tier1_gleif_pass0",
                                            "reason": f"HTTP {r.status_code}"})
                    return {}, [], (f"GLEIF connector failed "
                                     f"(HTTP {r.status_code}): registry proof "
                                     f"skipped, not 'no data'"), False
                payload = r.json()
                _cache_put("gleif", ckey, payload or {})
        except Exception as e:
            audit.log("tier_down", {"tier": "tier1_gleif_pass0",
                                    "reason": f"{type(e).__name__}: {e}"})
            return {}, [], (f"GLEIF connector failed ({type(e).__name__}): "
                             "registry proof skipped, not 'no data'"), False
    try:
        items = (payload or {}).get("data", []) or []
    except Exception:
        items = []
    if not items:
        return {}, [], (f"GLEIF: no record matching '{q}' "
                         "(looked up, none found)"), False
    # Gate: keep ACTIVE records whose legal name carries the query tokens;
    # exact/alias equality outranks substring.
    qtoks = _query_tokens(canon)
    scored: list[tuple[int, dict, dict]] = []
    for it in items:
        info = _gleif_extract(it)
        if not info.get("legal_name") or not info.get("lei"):
            continue
        legal_norm = _norm_alnum(info["legal_name"])
        if _norm_alnum(canon) == legal_norm or _norm_alnum(q) == legal_norm:
            score = 3
        elif qtoks and all(t in legal_norm for t in qtoks):
            score = 2 if (info.get("status", "").upper() == "ACTIVE") else 1
        else:
            continue
        scored.append((score, info, it))
    if not scored:
        return {}, [], (f"GLEIF: {len(items)} candidate(s) for '{q}', none "
                         "matches this company: rejected, not 'no data'"), False
    scored.sort(key=lambda t: -t[0])
    top_score = scored[0][0]
    tied = [s for s in scored if s[0] == top_score]
    if len(tied) > 1 and top_score < 3:
        # Genuine ambiguity (Zee Entertainment vs Zee Learn vs Zee Media):
        # do NOT guess. Caller falls back to COMPANY_ALIASES.
        audit.log("tier_down", {"tier": "tier1_gleif_pass0",
                                "reason": f"namesake ambiguity for {q!r}"})
        return {}, [], ("GLEIF namesake ambiguity "
                         f"({len(tied)} records match '{q}'): identity not "
                         "verified, alias fallback used"), False
    info = tied[0][1]
    lei = info["lei"]
    label = info["legal_name"] or q
    lines = [f"{label} — Legal Entity Identifier: {lei} (GLEIF)"]
    if info.get("jurisdiction") or info.get("cin"):
        lines.append(f"{label} — registered in {info.get('jurisdiction') or '?'}"
                     + (f" as {info['cin']}" if info.get("cin") else "")
                     + " (GLEIF)")
    if info.get("status"):
        lines.append(f"{label} — entity status: {info['status']} (GLEIF)")
    if info.get("reg_status"):
        lines.append(f"{label} — LEI registration status: {info['reg_status']} (GLEIF)")
    if info.get("hq"):
        lines.append(f"{label} — headquarters: {info['hq']} (GLEIF)")
    url = f"https://search.gleif.org/#/record/{lei}"
    firmo: dict = {"sources": [url],
                   "legal_name": label,
                   "lei": lei,
                   "registry": f"LEI {lei}"
                               + (f" ({info['jurisdiction']})" if info.get("jurisdiction") else "")
                               + " (verified via GLEIF)",
                   "status": info.get("status", ""),
                   "jurisdiction": info.get("jurisdiction", "")}
    if info.get("hq"):
        firmo["hq"] = info["hq"]
    if info.get("cin"):
        firmo["cin"] = info["cin"]
    doc = None
    try:
        from .enrich import _enrich_doc
        doc = _enrich_doc(url, f"{label} (registry)", SourceClass.REGISTRY,
                          lines, "GLEIF (ISO 17442 Legal Entity Identifier)")
    except Exception:
        doc = None
    audit.log("tier1_gleif", {"lei": lei, "company": q, "pass0": True})
    return firmo, ([doc] if doc else []), None, True


def query_wikidata(name: str, deadline: float | None = None
                   ) -> tuple[dict, list, str | None]:
    """Wikidata primary (action API) with selective SPARQL fallback.

    Keeps enrich.wikidata_company as the primary path (fuzzy search +
    gated pick + P452/P159/P1128/P571/P856/P1278 extractors). SPARQL runs
    at most once, only when the primary yields nothing — 429-avoidance.
    """
    if _disabled() or not (name or "").strip() or _expired(deadline):
        return {}, [], None
    q = (name or "").strip()
    try:
        from .discovery import canonical_company as _canon
        canon = _canon(q)
    except Exception:
        canon = q
    try:
        from . import enrich as _enrich
        for cand in ([canon] if canon.lower() != q.lower() else []) + [q]:
            docs, firmo, note = _enrich.wikidata_company(cand)
            if docs or firmo:
                return firmo, docs, note
            primary_note = note
    except Exception as e:
        return {}, [], (f"Wikidata connector error ({type(e).__name__}): "
                         "enrichment skipped, not 'no data'")
    # Selective SPARQL fallback (one shot, short timeout, contact UA).
    if _expired(deadline):
        return {}, [], primary_note
    ckey = f"pass0-wd-sparql:{_norm_alnum(canon)}"
    cached = _cache_get("wikidata", ckey)
    if cached is None:
        sparql = (
            "SELECT ?item ?itemLabel ?inception ?hqLabel ?website WHERE { "
            f'?item rdfs:label "{canon}"@en. '
            "OPTIONAL { ?item wdt:P571 ?inception. } "
            "OPTIONAL { ?item wdt:P159 ?hq. } "
            "OPTIONAL { ?item wdt:P856 ?website. } "
            'SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } '
            "} LIMIT 1"
        )
        try:
            with httpx.Client(timeout=8, headers=SPARQL_UA) as c:
                r = c.get(WIKIDATA_SPARQL,
                          params={"query": sparql, "format": "json"})
                if r.status_code == 429:
                    audit.log("tier_down", {"tier": "tier1_wikidata_pass0",
                                            "reason": "HTTP 429"})
                    return {}, [], ("Wikidata throttled (429): SPARQL fallback "
                                     "skipped, not 'no data'")
                if r.status_code != 200:
                    return {}, [], primary_note
                cached = r.json()
                _cache_put("wikidata", ckey, cached or {})
        except Exception as e:
            audit.log("tier_down", {"tier": "tier1_wikidata_pass0",
                                    "reason": f"{type(e).__name__}: {e}"})
            return {}, [], primary_note
    try:
        bindings = (cached or {}).get("results", {}).get("bindings", []) or []
        if not bindings:
            return {}, [], primary_note
        b = bindings[0]
        firmo: dict = {}
        lines: list[str] = []
        item = (b.get("item", {}) or {}).get("value", "")
        qid = item.rstrip("/").split("/")[-1] if item else ""
        label = (b.get("itemLabel", {}) or {}).get("value", "") or canon
        if "inception" in b:
            y = str(b["inception"].get("value", ""))[:4]
            if re.match(r"(19|20)\d{2}", y):
                firmo["founded"] = y
                lines.append(f"{label} — founded: {y} (Wikidata)")
        if "hqLabel" in b and b["hqLabel"].get("value"):
            firmo["hq"] = str(b["hqLabel"]["value"])[:120]
            lines.append(f"{label} — headquarters: {firmo['hq']} (Wikidata)")
        if "website" in b and str(b["website"].get("value", "")).startswith("http"):
            _wh = str(b["website"]["value"]).rstrip("/")
            try:
                from .discovery import homepage_matches_company as _hmc3
                if _hmc3(_wh, canon):
                    firmo["homepage"] = _wh
                    lines.append(f"{label} — homepage: {_wh} (Wikidata)")
                else:
                    audit.log("wrong_entity_dropped",
                              {"tier": "tier1_wikidata_pass0",
                               "company": canon, "homepage": _wh[:120]})
                    # Jointly suspect: inception year belongs to the same
                    # wrong entity — drop it with the homepage.
                    firmo.pop("founded", None)
                    lines[:] = [ln for ln in lines
                                if "founded:" not in ln]
            except Exception:
                firmo["homepage"] = _wh
                lines.append(f"{label} — homepage: {_wh} (Wikidata)")
        if not lines:
            return {}, [], primary_note
        url = f"https://www.wikidata.org/wiki/{qid}" if qid else \
            "https://www.wikidata.org/"
        if url != "https://www.wikidata.org/":
            firmo["sources"] = [url]
        doc = None
        try:
            from .enrich import _enrich_doc
            from .schemas import SourceClass as _SC
            doc = _enrich_doc(url, label, _SC.OTHER, lines,
                              f"Wikidata {qid} (CC0)" if qid else "Wikidata (CC0)")
        except Exception:
            doc = None
        audit.log("tier1_wikidata", {"query": q, "pass0_sparql": True})
        return firmo, ([doc] if doc else []), None
    except Exception:
        return {}, [], primary_note


def get_ground_truth(query: str, deadline: float | None = None
                     ) -> tuple[dict, list, list, bool, str, str]:
    """Merge Pass-0 sources into (firmo_extra, docs, degraded,
    identity_verified, resolved_name, official_website).

    Fill-missing only across sources; GLEIF legal name wins for display.
    Unknowns stay unknown — callers map them into gaps, never invent.
    """
    q = (query or "").strip()
    firmo: dict = {}
    docs: list = []
    degraded: list[str] = []
    gleif_ok = False
    wiki_ok = False

    def _absorb(new_docs, new_firmo, new_note):
        for d in new_docs or []:
            if d.doc_id not in {x.doc_id for x in docs}:
                docs.append(d)
        for k, v in (new_firmo or {}).items():
            if k == "sources":
                firmo["sources"] = list(dict.fromkeys(
                    firmo.get("sources", []) + list(v or [])))[:5]
            elif v and k not in firmo:
                firmo[k] = v
        if new_note and new_note not in degraded:
            degraded.append(new_note)

    if q and not _expired(deadline):
        g_firmo, g_docs, g_note, g_ok = query_gleif(q, deadline)
        _absorb(g_docs, g_firmo, g_note)
        gleif_ok = bool(g_ok)
    if q and not _expired(deadline):
        w_firmo, w_docs, w_note = query_wikidata(q, deadline)
        # Don't double-log the same no-match: keep the more specific note.
        _absorb(w_docs, w_firmo, w_note)
        wiki_ok = bool(w_docs or w_firmo)
    elif q and not (docs or firmo):
        degraded.append("Pass-0 enrichment skipped (request budget spent): "
                        "registry proof reduced")

    resolved = firmo.get("legal_name", "") or q
    website = firmo.get("homepage", "") or "unknown"
    # Surface Pass-0 gaps as loud notes (callers fold into unknowns[]).
    if q and not firmo.get("lei"):
        if not any("LEI" in n for n in degraded):
            degraded.append("GLEIF: no verified LEI for "
                            f"'{q}' — registry proof unavailable")
    verified = bool(gleif_ok or wiki_ok)
    return firmo, docs, degraded, verified, resolved, website
