"""Tier-1 enrichment connectors: free + legal person/company data.

Replaces nothing — sits beside NewsAPI/Wikipedia/DBpedia. All sources here
are official APIs with free tiers (no scraping, no burner accounts):

- Wikidata (keyless, CC0): person employer/position/education, company
  industry/HQ/employees/founded/homepage/LEI.
- GLEIF (keyless, ISO 17442): registry proof via LEI — legal name,
  jurisdiction, registration ID, entity status, HQ address. Reached through
  the LEI chained from the Wikidata record (GLEIF v1 has no name search).
- Hunter.io (free key, monthly credits): domain -> company profile,
  email/LinkedIn-handle -> person title.

Contract (mirrors tier1_* in discovery.py): every public function returns
(docs, firmo_extra, notes). notes are loud degradation strings (§10.2) —
never silent. Quota exhaustion and missing keys report down, never
empty-success. Responses are cached (30d) so repeat briefs cost zero calls.
ENRICH_DISABLE=1 fully disables all three (operator kill-switch, tests).
"""
from __future__ import annotations
import hashlib
import json
import os
import time

import httpx

from . import audit
from .acquisition import extract_entities, snapshot_raw
from .schemas import DocSection, FetchStatus, SourceClass, StructuredDoc

UA = {"User-Agent": "ProspectIntel/1.0 (internal research brief tool; contact: ops@example.com)"}

CACHE_TTL_S = 30 * 86400

_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS enrich_usage(source TEXT NOT NULL, bucket TEXT NOT NULL,
 calls INT NOT NULL, PRIMARY KEY(source, bucket));
CREATE TABLE IF NOT EXISTS enrich_cache(source TEXT NOT NULL, ckey TEXT NOT NULL,
 payload TEXT NOT NULL, ts REAL NOT NULL, PRIMARY KEY(source, ckey));
"""


def _disabled() -> bool:
    return os.environ.get("ENRICH_DISABLE", "").strip() == "1"


def enabled() -> bool:
    """Public gate so callers log tiers honestly (L2: no phantom usage)."""
    return not _disabled()


def _s(v) -> str:
    """Coerce API scalars defensively (M2): dicts/lists stringify into
    garbage facts — only real strings survive."""
    return v.strip() if isinstance(v, str) else ""


def _con():
    from . import store
    con = store.connect()
    con.executescript(_USAGE_SCHEMA)
    return con


def _norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def _fetch_json(url: str, params: dict | None = None,
                timeout: int = 12,
                auth=None) -> tuple[dict | list | None, str | None]:
    """GET JSON. Returns (payload, error). error None on HTTP 200 + valid JSON."""
    try:
        with httpx.Client(timeout=timeout, headers=UA, auth=auth) as c:
            r = c.get(url, params=params or {})
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            try:
                return r.json(), None
            except Exception:
                return None, "invalid JSON"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _quota_ok(source: str) -> str | None:
    """Nil when a call may proceed; otherwise the loud degradation note."""
    import datetime
    try:
        day_cap = 0
        month_cap = int(os.environ.get("HUNTER_MONTHLY_CAP", "25")
                        if source == "hunter" else "0")
    except ValueError:
        day_cap, month_cap = 0, 0
    if not day_cap and not month_cap:
        return None
    try:
        con = _con()
        today = datetime.date.today().isoformat()
        month = today[:7]
        if day_cap:
            row = con.execute(
                "SELECT calls FROM enrich_usage WHERE source=? AND bucket=?",
                (source, today)).fetchone()
            if row and row[0] >= day_cap:
                con.close()
                return (f"{source} daily quota reached ({day_cap}/day): "
                        "brief marked degraded, cached data only")
        if month_cap:
            row = con.execute(
                "SELECT calls FROM enrich_usage WHERE source=? AND bucket=?",
                (source, month)).fetchone()
            if row and row[0] >= month_cap:
                con.close()
                return (f"{source} monthly quota reached ({month_cap}/month): "
                        "brief marked degraded, cached data only")
        con.close()
    except Exception as e:
        audit.log("tier_down", {"tier": f"tier1_{source}",
                                "reason": f"quota check failed: {e}"})
    return None


def _record_use(source: str) -> None:
    import datetime
    try:
        con = _con()
        today = datetime.date.today().isoformat()
        month = today[:7]
        for bucket in (today, month):
            con.execute(
                "INSERT INTO enrich_usage(source, bucket, calls) VALUES (?,?,1) "
                "ON CONFLICT(source, bucket) DO UPDATE SET calls=calls+1",
                (source, bucket))
        con.commit()
        con.close()
    except Exception as e:
        audit.log("tier_down", {"tier": f"tier1_{source}",
                                "reason": f"usage record failed: {e}"})


def _cache_get(source: str, ckey: str):
    try:
        con = _con()
        row = con.execute(
            "SELECT payload, ts FROM enrich_cache WHERE source=? AND ckey=?",
            (source, ckey)).fetchone()
        con.close()
        if row and time.time() - row[1] < CACHE_TTL_S:
            return json.loads(row[0])
    except Exception:
        pass
    return None


def _cache_put(source: str, ckey: str, payload) -> None:
    try:
        con = _con()
        con.execute(
            "INSERT OR REPLACE INTO enrich_cache VALUES (?,?,?,?)",
            (source, ckey, json.dumps(payload), time.time()))
        con.commit()
        con.close()
    except Exception:
        pass


def _enrich_doc(url: str, title: str, source_class: SourceClass,
                lines: list[str], provenance: str) -> StructuredDoc | None:
    """One self-contained sentence per line; each line ends with provenance
    so verifier spans stay citable (§5.4 single-section shape)."""
    body_lines = [ln.strip() for ln in lines if ln and ln.strip()]
    if not body_lines:
        return None
    body = "\n".join(
        ln if ln.endswith(".") else ln + "." for ln in body_lines)
    body += f"\nProvenance: {provenance}"
    try:
        ch = snapshot_raw(body.encode())
    except Exception:
        ch = hashlib.sha256(body.encode()).hexdigest()
    doc_id = "doc_" + ch[:16]
    doc = StructuredDoc(
        doc_id=doc_id, url=url, url_final=url, content_hash=ch,
        fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        fetch_status=FetchStatus.OK, source_class=source_class,
        title=title, sections=[DocSection(
            section_id=doc_id + "#s0", text=body,
            char_start=0, char_end=len(body))],
        entities=extract_entities(body))
    audit.log("doc_structured", {"doc_id": doc_id, "url": url,
                                 "source_class": source_class.value})
    return doc


# ---------------------------------------------------------------- Wikidata

_WD_API = "https://www.wikidata.org/w/api.php"
# Person: employer / position / education. Company: industry / HQ / staff /
# inception / official site. Professional facts only (§8.5).
_WD_PERSON_PROPS = (("P108", "employer"), ("P39", "position held"),
                    ("P69", "educated at"))
_WD_COMPANY_PROPS = (("P452", "industry"), ("P159", "headquarters"),
                     ("P1128", "employees"), ("P571", "founded"),
                     ("P856", "homepage"))


def _wd_claim_values(claims: dict, pid: str) -> list[tuple[str, str]]:
    out = []
    for c in claims.get(pid, []) or []:
        try:
            sn = c.get("mainsnak", {}) or {}
            if sn.get("snaktype") != "value":
                continue
            dv = sn.get("datavalue", {}) or {}
            t, v = dv.get("type"), dv.get("value")
            if t == "wikibase-entityid" and isinstance(v, dict) and v.get("id"):
                out.append(("entity", v["id"]))
            elif t == "time" and isinstance(v, dict) and v.get("time"):
                out.append(("time", v["time"]))
            elif t == "string" and isinstance(v, str):
                out.append(("string", v))
            elif t == "quantity" and isinstance(v, dict) and v.get("amount"):
                out.append(("quantity", str(v["amount"]).lstrip("+")))
            elif t == "monolingualtext" and isinstance(v, dict) and v.get("text"):
                out.append(("string", v["text"]))
        except Exception:
            continue
    return out


def _wd_year(t: str) -> str:
    import re
    m = re.match(r"[+-]?(\d{4})", (t or "").strip())
    return m.group(1) if m and m.group(1) != "0000" else ""


def _wd_labels(qids: list[str]) -> dict[str, str]:
    """Batch-resolve entity labels in ONE call (no N+1)."""
    qids = [q for q in dict.fromkeys(qids) if q][:50]
    if not qids:
        return {}
    payload, err = _fetch_json(_WD_API, {"action": "wbgetentities",
                                         "ids": "|".join(qids),
                                         "props": "labels",
                                         "languages": "en", "format": "json"})
    if err or not isinstance(payload, dict):
        return {}
    out = {}
    for qid in qids:
        try:
            out[qid] = payload["entities"][qid]["labels"]["en"]["value"]
        except Exception:
            continue
    return out


def _wd_search(query: str, limit: int = 5) -> tuple[list[dict], str | None]:
    payload, err = _fetch_json(_WD_API, {"action": "wbsearchentities",
                                         "search": query, "language": "en",
                                         "format": "json", "limit": limit})
    if err:
        return [], f"Wikidata search unreachable ({err})"
    try:
        return payload.get("search", []) or [], None
    except Exception:
        return [], "Wikidata search returned unexpected shape"


def _wd_entity(qid: str) -> tuple[dict, str | None]:
    cached = _cache_get("wikidata", f"entity:{qid}")
    if cached is not None:
        return cached, None
    payload, err = _fetch_json(
        f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json")
    if err:
        return {}, f"Wikidata entity {qid} unreachable ({err})"
    try:
        ent = payload["entities"][qid]
        _cache_put("wikidata", f"entity:{qid}", ent)
        return ent, None
    except Exception:
        return {}, f"Wikidata entity {qid} returned unexpected shape"


def _wd_pick(query: str, candidates: list[dict]) -> dict | None:
    """Surname/label fuzzy gate (§6.3): a wrong-entity page is worse than none."""
    try:
        from .passes import name_match as _nm
    except Exception:
        _nm = None
    for cand in candidates:
        label = str(cand.get("label", ""))
        if _nm and _nm(label, query):
            return cand
        if _norm(label) == _norm(query):
            return cand
    return None


def wikidata_person(name: str) -> tuple[list[StructuredDoc], dict, str | None]:
    """Keyless person facts. Returns (docs, firmo_extra, note)."""
    if _disabled() or not (name or "").strip():
        return [], {}, None
    cands, err = _wd_search(name.strip())
    if err:
        audit.log("tier_down", {"tier": "tier1_wikidata", "reason": err})
        return [], {}, f"Wikidata connector down ({err}): brief marked degraded"
    pick = _wd_pick(name.strip(), cands)
    if pick is None:
        top = str((cands[0].get("label", "?") if cands else "none"))
        return [], {}, (f"Wikidata: no confident match for '{name.strip()}' "
                        f"(top hit '{top}' rejected): not 'no data'")
    ent, err = _wd_entity(pick["id"])
    if err:
        audit.log("tier_down", {"tier": "tier1_wikidata", "reason": err})
        return [], {}, f"Wikidata connector down ({err}): brief marked degraded"
    claims = ent.get("claims", {}) or {}
    need = [qid for pid in [p for p, _ in _WD_PERSON_PROPS]
            for kind, qid in _wd_claim_values(claims, pid) if kind == "entity"]
    labels = _wd_labels(need)
    label_note = None
    if need and not labels:
        label_note = ("Wikidata: entity labels unresolved "
                      "(label lookup failed): employer/school names partial")
    try:
        label = ent.get("labels", {}).get("en", {}).get("value") or pick.get("label", name)
        desc = ent.get("descriptions", {}).get("en", {}).get("value", "")
    except Exception:
        label, desc = pick.get("label", name), ""
    lines = []
    if desc:
        lines.append(f"{label} — {desc} (Wikidata)")
    for pid, human in _WD_PERSON_PROPS:
        vals = []
        for kind, val in _wd_claim_values(claims, pid):
            if kind == "entity" and labels.get(val):
                vals.append(labels[val])
            elif kind == "string":
                vals.append(val)
        vals = list(dict.fromkeys(vals))[:5]
        if vals:
            lines.append(f"{label} — {human}: {', '.join(vals)} (Wikidata)")
    url = f"https://www.wikidata.org/wiki/{pick['id']}"
    doc = _enrich_doc(url, label, SourceClass.OTHER, lines,
                      f"Wikidata {pick['id']} (CC0)")
    audit.log("tier1_wikidata", {"query": name, "entity": pick["id"],
                                 "facts": len(lines)})
    return ([doc] if doc else []), {}, label_note


_WD_PERSON_ONLY = ("P108", "P39", "P69")  # employer/position/educated-at
_WD_COMPANY_ANY = ("P452", "P159", "P1128", "P571", "P856", "P1278")


def _wd_company_pick(company: str
                     ) -> tuple[dict | None, dict, str, str | None]:
    """Resolve a company query to (pick, entity, lei, note).

    L1: a person page matching a company query (Ford -> Gerald Ford) is
    rejected as a type mismatch instead of yielding an empty doc."""
    cands, err = _wd_search(company.strip())
    if err:
        audit.log("tier_down", {"tier": "tier1_wikidata", "reason": err})
        return None, {}, "", f"Wikidata connector down ({err}): brief marked degraded"
    pick = _wd_pick(company.strip(), cands)
    if pick is None:
        top = str((cands[0].get("label", "?") if cands else "none"))
        return None, {}, "", (f"Wikidata: no confident match for '{company.strip()}' "
                              f"(top hit '{top}' rejected): not 'no data'")
    ent, err = _wd_entity(pick["id"])
    if err:
        audit.log("tier_down", {"tier": "tier1_wikidata", "reason": err})
        return None, {}, "", f"Wikidata connector down ({err}): brief marked degraded"
    claims = ent.get("claims", {}) or {}
    if (any(claims.get(p) for p in _WD_PERSON_ONLY)
            and not any(claims.get(p) for p in _WD_COMPANY_ANY)):
        return None, {}, "", (f"Wikidata: '{pick.get('label', pick['id'])}' looks like "
                              f"a person record, not a company: not '{company.strip()}'")
    lei = ""
    for kind, val in _wd_claim_values(claims, "P1278"):
        if kind == "string" and val.strip():
            lei = val.strip().upper()
            break
    return pick, ent, lei, None


def wikidata_company(company: str) -> tuple[list[StructuredDoc], dict, str | None]:
    """Keyless company facts + firmographic fields. Returns (docs, firmo, note)."""
    if _disabled() or not (company or "").strip():
        return [], {}, None
    pick, ent, lei, note = _wd_company_pick(company)
    if pick is None:
        return [], {}, note
    # Negative cache: a previously-dropped wrong entity (Bullet vs Bulletin
    # Q15716527) skips the slow label-batch fetch; docs still gated downstream.
    try:
        from .discovery import disambig_get as _dg2
        if pick.get("id") in (_dg2(company).get("wrong_entities", []) or []):
            return [], {}, (f"Wikidata: '{pick.get('label', '')}' previously "
                            "rejected as wrong entity (cached): not 'no data'")
    except Exception:
        pass
    claims = ent.get("claims", {}) or {}
    need = [qid for pid in [p for p, _ in _WD_COMPANY_PROPS]
            for kind, qid in _wd_claim_values(claims, pid) if kind == "entity"]
    labels = _wd_labels(need)
    label_note = None
    if need and not labels:
        label_note = ("Wikidata: entity labels unresolved "
                      "(label lookup failed): employer/industry names partial")
    try:
        label = ent.get("labels", {}).get("en", {}).get("value") or pick.get("label", company)
        desc = ent.get("descriptions", {}).get("en", {}).get("value", "")
    except Exception:
        label, desc = pick.get("label", company), ""
    firmo: dict = {}
    lines = []
    if desc:
        lines.append(f"{label} — {desc} (Wikidata)")
    # Entity-level host check FIRST: a homepage that mismatches the company
    # (Bullet vs Bulletin) means every field of this record is suspect —
    # pre-resolve so founded/industry/hq never leak from the wrong entity.
    _entity_ok = True
    try:
        from .discovery import homepage_matches_company as _hmc0
        _hp_vals = [v for k, v in _wd_claim_values(claims, "P856")
                    if k == "string" and str(v).startswith("http")]
        if _hp_vals and not _hmc0(_hp_vals[0], company):
            audit.log("wrong_entity_dropped",
                      {"tier": "tier1_wikidata", "company": company,
                       "homepage": _hp_vals[0][:120],
                       "entity": pick.get("id", "")})
            _entity_ok = False
            try:
                from .discovery import disambig_note as _dn0
                from urllib.parse import urlparse as _up0
                _dn0(company, wrong_entity=pick.get("id", ""),
                     wrong_host=(_up0(_hp_vals[0]).hostname or "").lower())
            except Exception:
                pass
    except Exception:
        _entity_ok = True
    for pid, human in _WD_COMPANY_PROPS:
        vals = []
        for kind, val in _wd_claim_values(claims, pid):
            if kind == "entity" and labels.get(val):
                vals.append(labels[val])
            elif kind == "time" and pid == "P571":
                y = _wd_year(val)
                if y:
                    vals.append(y)
            elif kind in ("string", "quantity"):
                vals.append(val)
        vals = list(dict.fromkeys(v for v in vals if v))[:5]
        if not vals:
            continue
        lines.append(f"{label} — {human}: {', '.join(vals)} (Wikidata)")
        if not _entity_ok:
            continue  # wrong entity: lines stay for gated retrieval, no firmo
        if human == "industry":
            firmo["industry"] = vals[0]
        elif human == "headquarters":
            firmo["hq"] = vals[0]
        elif human == "employees":
            firmo["employees"] = vals[0]
        elif human == "founded":
            firmo["founded"] = vals[0][:4]
        elif human == "homepage" and vals[0].startswith("http"):
            try:
                from .discovery import homepage_matches_company as _hmc2
                if _hmc2(vals[0], company):
                    firmo["homepage"] = vals[0].rstrip("/")
                else:
                    audit.log("wrong_entity_dropped",
                              {"tier": "tier1_wikidata", "company": company,
                               "homepage": vals[0][:120],
                               "entity": pick.get("id", "")})
                    # Same-entity fields are jointly suspect (Bullet vs
                    # Bulletin: founded 1911 belongs to the Bulletin, not
                    # the company) — drop founded with the homepage.
                    firmo.pop("founded", None)
                    lines[:] = [ln for ln in lines
                                if "founded:" not in ln and "homepage:" not in ln]
            except Exception:
                firmo["homepage"] = vals[0].rstrip("/")
    if lei:
        firmo["lei"] = lei
    url = f"https://www.wikidata.org/wiki/{pick['id']}"
    if firmo:
        firmo["sources"] = [url]
    doc = _enrich_doc(url, label, SourceClass.OTHER, lines,
                      f"Wikidata {pick['id']} (CC0)")
    audit.log("tier1_wikidata", {"query": company, "entity": pick["id"],
                                 "fields": sorted(firmo)})
    return ([doc] if doc else []), firmo, label_note


# ------------------------------------------------------------------- GLEIF

_GLEIF_BASE = "https://api.gleif.org/api/v1"


def _gleif_addr(addr: dict) -> str:
    """'Berlin, DE' style HQ string from a GLEIF address block."""
    if not isinstance(addr, dict):
        return ""
    parts = []
    city = _s(addr.get("city"))
    country = _s(addr.get("country"))
    if city:
        parts.append(city)
    if country and country not in parts:
        parts.append(country)
    lines = [_s(x) for x in (addr.get("addressLines", []) or [])][:2]
    extra = [x for x in lines if x and x not in parts]
    return ", ".join(extra + parts)


def gleif_by_lei(lei: str, company: str = ""
                 ) -> tuple[list[StructuredDoc], dict, str | None]:
    """LEI -> registry proof (keyless, cached). The LEI arrives chained from
    the Wikidata record (GLEIF v1 exposes no name search). Returns
    (docs, firmo, note)."""
    lei = (lei or "").strip().upper()
    if _disabled() or not lei:
        return [], {}, None
    ckey = f"lei:{lei}"
    cached = _cache_get("gleif", ckey)
    if cached is None:
        payload, err = _fetch_json(
            f"{_GLEIF_BASE}/lei-records",
            {"filter[lei]": lei, "page[size]": 1})
        if err:
            audit.log("tier_down", {"tier": "tier1_gleif", "reason": err})
            return [], {}, (f"GLEIF connector failed ({err}): "
                            "registry proof skipped, not 'no data'")
        _cache_put("gleif", ckey, payload or {})
        cached = payload or {}
    try:
        items = cached.get("data", []) or []
        rec = items[0] if items else {}
        attrs = rec.get("attributes", {}) or {}
        ent = attrs.get("entity", {}) or {}
        reg = attrs.get("registration", {}) or {}
    except Exception:
        rec, attrs, ent, reg = {}, {}, {}, {}
    if not ent:
        return [], {}, (f"GLEIF: no record for LEI '{lei}' "
                        "(looked up, none found)")
    legal = _s((ent.get("legalName", {}) or {}).get("name"))
    # M1 namesake gate: the LEI must belong to THIS company (§6.3). The
    # payload is cached regardless (it is factual); only acceptance is gated.
    if company.strip():
        try:
            from .passes import name_match as _nm
        except Exception:
            _nm = None
        if _nm and not _nm(legal, company.strip()) and _norm(legal) != _norm(company):
            return [], {}, (f"GLEIF: LEI {lei} belongs to '{legal or '?'}', "
                            f"not '{company.strip()}': rejected, not 'no data'")
    jur = _s(ent.get("jurisdiction"))
    estatus = _s(ent.get("status"))
    regas = _s(ent.get("registeredAs"))
    rstatus = _s(reg.get("status"))
    hq = _gleif_addr(ent.get("headquartersAddress", {}) or {}) or \
        _gleif_addr(ent.get("legalAddress", {}) or {})
    label = legal or company or lei
    lines = [f"{label} — Legal Entity Identifier: {lei} (GLEIF)"]
    if jur or regas:
        lines.append(f"{label} — registered in {jur or '?'}"
                     + (f" as {regas}" if regas else "") + " (GLEIF)")
    if estatus:
        lines.append(f"{label} — entity status: {estatus} (GLEIF)")
    if rstatus:
        lines.append(f"{label} — LEI registration status: {rstatus} (GLEIF)")
    if hq:
        lines.append(f"{label} — headquarters: {hq} (GLEIF)")
    url = f"https://search.gleif.org/#/record/{lei}"
    firmo = {"sources": [url],
             "registry": f"LEI {lei}" + (f" ({jur})" if jur else "") +
                         " (verified via GLEIF)"}
    if hq:
        firmo["hq"] = hq
    doc = _enrich_doc(url, f"{label} (registry)", SourceClass.REGISTRY,
                      lines, "GLEIF (ISO 17442 Legal Entity Identifier)")
    audit.log("tier1_gleif", {"lei": lei, "company": company})
    return ([doc] if doc else []), firmo, None


# ---------------------------------------------------------------- Hunter

_H_BASE = "https://api.hunter.io/v2"


def hunter_company(domain: str, company: str = ""
                   ) -> tuple[list[StructuredDoc], dict, str | None]:
    """Domain -> company profile (free key, quota-guarded)."""
    domain = (domain or "").strip().lower()
    if _disabled() or not domain:
        return [], {}, None
    key = os.environ.get("HUNTER_API_KEY", "").strip()
    if not key:
        audit.log("tier_down", {"tier": "tier1_hunter",
                                "reason": "no HUNTER_API_KEY"})
        return [], {}, ("Tier-1 enrichment connector down (no key): "
                        "Hunter company profile skipped, not 'no data'")
    ckey = f"company:{domain}"
    cached = _cache_get("hunter", ckey)
    if cached is None:
        blocked = _quota_ok("hunter")
        if blocked:
            return [], {}, blocked
        payload, err = _fetch_json(f"{_H_BASE}/companies/find",
                                   {"domain": domain, "api_key": key})
        if err:
            audit.log("tier_down", {"tier": "tier1_hunter", "reason": err})
            return [], {}, (f"Hunter connector failed ({err}): "
                            "company profile skipped, not 'no data'")
        _record_use("hunter")
        _cache_put("hunter", ckey, payload or {})
        cached = payload or {}
    try:
        data = cached.get("data", {}) or {}
    except Exception:
        data = {}
    if not data:
        return [], {}, (f"Hunter: no company profile for '{domain}' "
                        "(looked up, none found)")
    label = str(data.get("name", "") or company or domain)
    # Strict relevance (§6.3): the returned profile must be THIS company —
    # a probed domain can belong to someone else. Mismatch: loud note, no
    # docs, no merge.
    if (company or "").strip():
        try:
            from .passes import name_match as _nm
            from .discovery import _company_mentioned as _cm
            from .discovery import company_names_match as _cnm
        except Exception:
            _nm, _cm, _cnm = None, None, None
        _ok = (_nm and _nm(label, company.strip())) or (
            _cm and _cm(label, company.strip())) or (
            _cnm and _cnm(label, company.strip()))
        if not label.strip() or not _ok:
            return [], {}, (f"Hunter: returned profile '{label or '?'}' is not "
                            f"'{company.strip()}': rejected, not 'no data'")
    industry = _s(data.get("industry", ""))
    size = _s(data.get("size", "") or data.get("employees", ""))
    location = _s(data.get("location", ""))
    descr = _s(data.get("description", ""))[:400]
    tech = [t for t in (_s(x) for x in (data.get("tech", []) or [])) if t][:8]
    lines = []
    if descr:
        lines.append(f"{label} — {descr} (Hunter)")
    if industry:
        lines.append(f"{label} — industry: {industry} (Hunter)")
    if size:
        lines.append(f"{label} — company size: {size} (Hunter)")
    if location:
        lines.append(f"{label} — location: {location} (Hunter)")
    if tech:
        lines.append(f"{label} — technologies: {', '.join(tech)} (Hunter)")
    firmo: dict = {"sources": [f"https://hunter.io/companies/{domain}"]}
    if industry:
        firmo["industry"] = industry
    if size:
        firmo["employees"] = size
    if location:
        firmo["hq"] = location
    doc = _enrich_doc(f"https://hunter.io/companies/{domain}", label,
                      SourceClass.OTHER, lines,
                      "Hunter.io (public-source enrichment)")
    audit.log("tier1_hunter", {"domain": domain,
                               "fields": sorted(firmo)})
    return ([doc] if doc else []), firmo, None


def hunter_person(email: str = "", handle: str = ""
                  ) -> tuple[list[StructuredDoc], dict, str | None]:
    """Email/LinkedIn-handle -> person title (free key, quota-guarded).

    Wired for future/CLI use: the name+company web flow rarely knows an
    email upfront, so discovery calls wikidata_person instead."""
    email, handle = (email or "").strip(), (handle or "").strip()
    if _disabled() or not (email or handle):
        return [], {}, None
    key = os.environ.get("HUNTER_API_KEY", "").strip()
    if not key:
        audit.log("tier_down", {"tier": "tier1_hunter",
                                "reason": "no HUNTER_API_KEY"})
        return [], {}, ("Tier-1 enrichment connector down (no key): "
                        "Hunter person lookup skipped, not 'no data'")
    ckey = f"person:{email or handle}".lower()
    cached = _cache_get("hunter", ckey)
    if cached is None:
        blocked = _quota_ok("hunter")
        if blocked:
            return [], {}, blocked
        params = {"api_key": key}
        params["email" if email else "linkedin_handle"] = email or handle
        payload, err = _fetch_json(f"{_H_BASE}/people/find", params)
        if err:
            audit.log("tier_down", {"tier": "tier1_hunter", "reason": err})
            return [], {}, (f"Hunter connector failed ({err}): "
                            "person lookup skipped, not 'no data'")
        _record_use("hunter")
        _cache_put("hunter", ckey, payload or {})
        cached = payload or {}
    try:
        data = cached.get("data", {}) or {}
    except Exception:
        data = {}
    if not data:
        return [], {}, (f"Hunter: no person record for '{email or handle}' "
                        "(looked up, none found)")
    try:
        nm = _s((data.get("name", {}) or {}).get("fullName", ""))
        emp = data.get("employment", {}) or {}
        title = _s(emp.get("title", ""))
        org = _s(emp.get("name", ""))
        loc = _s(data.get("location", ""))
    except Exception:
        nm, title, org, loc = "", "", "", ""
    label = nm or email or handle
    lines = []
    if title or org:
        lines.append(f"{label} — {title + ' at ' if title else ''}{org} (Hunter)")
    if loc:
        lines.append(f"{label} — location: {loc} (Hunter)")
    import urllib.parse as _up
    doc = _enrich_doc("https://hunter.io/people/" + _up.quote(
        label.replace(" ", "-")[:80], safe="-"),
                      label, SourceClass.OTHER, lines,
                      "Hunter.io (public-source enrichment)")
    audit.log("tier1_hunter", {"person": label})
    return ([doc] if doc else []), {}, None


# ------------------------------------------------------------- entry points

def enrich_person(name: str) -> tuple[list[StructuredDoc], dict, list[str]]:
    """Discovery-stage person enrichment (keyless only)."""
    docs, _, note = wikidata_person(name)
    return docs, {}, [note] if note else []


def enrich_company(company: str
                   ) -> tuple[list[StructuredDoc], dict, list[str]]:
    """Discovery-stage company enrichment (keyless + free registries)."""
    docs, firmo, notes = [], {}, []

    def _absorb(new_docs, new_firmo, new_note):
        for d in new_docs:
            if d.doc_id not in {x.doc_id for x in docs}:
                docs.append(d)
        for k, v in (new_firmo or {}).items():
            if k == "sources":
                firmo["sources"] = list(dict.fromkeys(
                    firmo.get("sources", []) + list(v or [])))[:5]
            elif v and k not in firmo:
                firmo[k] = v
        if new_note:
            notes.append(new_note)

    wd_docs, wd_firmo, wd_note = wikidata_company(company)
    # Bare-brand retry: 'Zee' misses, canonical 'Zee Entertainment
    # Enterprises' hits Q12428554. Prefer canonical docs when literal fails.
    try:
        from .discovery import canonical_company as _canon_e
        _canon_c = _canon_e(company)
        if _canon_c.strip().lower() != (company or "").strip().lower() \
                and not wd_docs:
            _cdocs, _cfirmo, _cnote = wikidata_company(_canon_c)
            if _cdocs or _cfirmo:
                wd_docs, wd_firmo, wd_note = _cdocs, _cfirmo, _cnote
    except Exception:
        pass
    _absorb(wd_docs, wd_firmo, wd_note)
    # LEI chain: the Wikidata record carries the LEI (P1278); GLEIF has no
    # name search, so the gated Wikidata pick is the name resolver.
    lei = wd_firmo.get("lei", "")
    if lei:
        g_docs, g_firmo, g_note = gleif_by_lei(lei, company)
        _absorb(g_docs, g_firmo, g_note)
    elif wd_note is None:
        # Wikidata answered but carries no LEI: say why registry proof stops.
        notes.append("GLEIF: no LEI on the Wikidata record — "
                     "registry proof unavailable for this company")
    return docs, firmo, notes


def enrich_company_by_domain(domain: str, company: str = ""
                             ) -> tuple[list[StructuredDoc], dict, list[str]]:
    """Post-Pass-2 enrichment: domain known from website_probe/homepage."""
    docs, firmo, note = hunter_company(domain, company)
    return docs, firmo, [note] if note else []
