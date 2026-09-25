"""Four research passes inside the Secure Core (NO internet egress).

P1 entity resolution — HUMAN CONFIRMS (highest error rate, worst blast radius).
P2 firmographic — deterministic lookups w/ 90-day TTL cache (~quarterly refresh);
    probes the company website + stores result; API-registry providers plug in here.
P3 strategy — bounded loop with hard ceilings; job postings are first-class signal.
    Candidate claims are real sentences from docs, each blind-verified.
P4 pitch synthesis — zero network access, reasons over internal collateral only.

Split plan (no logic change yet): P1 -> passes_p1.py, P2+hiring ->
passes_p2.py, P3+transcripts -> passes_p3.py, P4/contract/export-helpers ->
passes_p4.py. This file stays as the compat re-export shim so the 30 tests
importing prospect_intel.passes.* keep working.
"""
from __future__ import annotations
import re
from . import audit
from .models import ToolBoundary
from .schemas import (Brief, CapabilityMap, Claim, Contradiction, Objection,
                      PersonIdentity, Priority, StructuredDoc, Verdict)
from .verifier import Verifier

P3_MAX_DOCS = 20
P3_MAX_CLAIMS = 10
FIRMO_SCHEMA = 5  # bump when the profile shape changes: stale cache re-probes
# v4: parked-domain rejection + homepage host-agreement gate (wrong-entity
# Bullet/Bulletin records cached under v3 must re-probe).
# v5: entity-level Wikidata gate (founded/industry/hq blocked with homepage).
# v3: canonical aliases (Zee->Zee Entertainment Enterprises) + alias domains

_SENT = re.compile(r"(?<=[.!?])\s+")


def pass1_propose_candidates(raw_name: str, raw_company: str) -> list[PersonIdentity]:
    cands = [PersonIdentity(full_name=raw_name.strip(), company=raw_company.strip(),
                            human_confirmed=False)]
    audit.log("pass1_proposed", {"n": len(cands)})
    return cands


def pass1_confirm(candidate: PersonIdentity) -> PersonIdentity:
    """Human gate: brief cannot proceed until a person confirms identity."""
    if not candidate.human_confirmed:
        raise PermissionError("Pass 1 requires human confirmation")
    try:
        from . import store
        con = store.connect()
        try:
            con.execute("INSERT OR REPLACE INTO persons VALUES (?,?,?,?,?)",
                        (candidate.full_name + "@" + candidate.company,
                         candidate.full_name, candidate.company,
                         candidate.title, 1))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass
    audit.log("pass1_confirmed", {"name": candidate.full_name,
                                  "company": candidate.company})
    return candidate


def _company_domains(company: str) -> list[str]:
    company = company or ""
    guess = "".join(c.lower() if c.isalnum() else "" for c in company)
    if not guess or company.strip().lower() == "unknown":
        return []
    out = [f"https://{guess}.com", f"https://www.{guess}.com"]
    try:
        from .discovery import canonical_company as _canon
        canon = _canon(company)
        if canon.strip().lower() != company.strip().lower():
            for tok in ("".join(c.lower() if c.isalnum() else "" for c in canon),):
                if tok and f"https://{tok}.com" not in out:
                    out.extend([f"https://{tok}.com", f"https://www.{tok}.com"])
            # Known owned roots that guesses miss (zee.com vs
            # zeeentertainmententerprises.com).
            for known in ("https://www.zee.com", "https://zee.com",
                          "https://www.zee5.com"):
                if "zee" in guess and known not in out:
                    out.append(known)
    except Exception:
        pass
    return out


def probe_company_pages(company: str,
                          deadline: float | None = None) -> dict[str, str]:
    """Deterministic Tier-1 page probes: newsroom/press (exec statements),
    careers (hiring = involuntary roadmap), investors (earnings language).
    Returns {role: url} for pages that fetch OK. No model involved.
    Uses rendered fetch: owned pages are often JS-heavy. If domain guesses
    fail, a company-name-only breadth lookup (explicitly Tier-3-allowed,
    §5.1) finds the real homepage from a title match.
    Role paths probe in parallel (4 workers) under a 40s cap plus the
    request deadline — probes can never blow the 3-4 minute budget."""
    from .fetcher import fetch_url_smart
    from .security import resolve_and_assert_no_ssrf
    from . import deadline as _pdl
    if _pdl.expired(deadline):
        return {}
    found: dict[str, str] = {}
    bases = _company_domains(company)
    try:
        from .discovery import canonical_company as _canon_fn
        canon = _canon_fn(company)
    except Exception:
        canon = company
    try:  # DBpedia homepage first: the real domain beats guesses (ril.com!)
        from .discovery import dbpedia_company
        dbp, _ = dbpedia_company(company)
        if dbp.get("homepage"):
            root = dbp["homepage"].rstrip("/")
            if root not in bases:
                bases.insert(0, root)
    except Exception:
        pass
    try:
        # Company-name-only lookup (Tier-3-allowed, §5.1): Firecrawl first,
        # self-hosted SearXNG merged in (either engine alone misses real
        # homepages). Social/directory hits are never owned homepages.
        # Acceptance is core-tokens (legal suffixes stripped): a homepage
        # titled "Imperial Milestone Pvt Ltd" proves "Imperial Milestone
        # Private Limited" — a full-legal-name substring never would.
        # Candidate root is ACCEPTED only on fetched proof (§6.3).
        from .discovery import _core_tokens_mentioned
        # Skip breadth lookup when the budget is nearly spent; guesses +
        # DBpedia above are already in bases.
        import time as _pt
        _deadline_soon = bool(deadline) and (deadline - _pt.time() < 45.0)
        results: list = []
        if not _pdl.expired(deadline) and not _deadline_soon:
            try:
                from .firecrawl import search as fc_search
                results = list(fc_search(canon + " official website", 5))
            except Exception:
                results = []
            try:
                from .search import SearxngProvider
                for h in SearxngProvider().search(
                        canon + " official website", 5):
                    results.append((h.url, h.title or "", ""))
            except Exception:
                pass
        _NOISE_HOSTS = ("youtube.", "youtu.be", "spotify.", "tiktok.",
                        "dictionary.", "merriam-webster.", "wiktionary.",
                        "facebook.", "instagram.", "linkedin.")
        _seen_roots = set(bases)
        for url, title, _ in results:
            if _pdl.expired(deadline):
                break
            from urllib.parse import urlparse as _up
            host = (_up(url).hostname or "").lower()
            if not host or "wikipedia.org" in host:
                continue
            if host.startswith(("linkedin.", "facebook.", "instagram.")) or \
                    host in ("linkedin.com", "facebook.com", "instagram.com",
                             "x.com", "twitter.com"):
                continue  # social profiles are never the owned homepage
            if any(n in host for n in _NOISE_HOSTS):
                continue  # dictionary/video/audio/social noise
            # Pre-filter on title before any fetch: the hit must look like
            # the company (core tokens), else fetching piano/snack sites
            # wastes the 40s probe budget (§6.3).
            if title and not _core_tokens_mentioned(
                    f"{title} {host}", canon):
                continue
            root = f"https://{host}"
            if root in _seen_roots:
                continue
            _seen_roots.add(root)
            try:
                resolve_and_assert_no_ssrf(root, dns_timeout_s=5.0)
                fr = fetch_url_smart(root)
                lead = (fr.title + " " + fr.body_text[:2000]).lower()
                if _core_tokens_mentioned(lead, canon) or \
                        _core_tokens_mentioned(lead, company):
                    bases.append(root)
                    break
            except Exception:
                continue
    except Exception:
        pass
    paths = {"newsroom": ["/newsroom", "/press", "/news", "/press-releases"],
             "careers": ["/careers", "/jobs", "/careers/jobs"],
             "investors": ["/investors", "/investor-relations", "/ir"]}
    import concurrent.futures as _cf
    import time as _t
    from . import deadline as _dl

    def _probe_one(url: str) -> str | None:
        try:
            resolve_and_assert_no_ssrf(url, dns_timeout_s=5.0)
            fr = fetch_url_smart(url)
            if fr.status_code == 200 and len(fr.body_text) > 500:
                return fr.url_final or url
        except Exception:
            pass
        return None

    tasks = [(base, role, path)
             for base in bases
             for role, options in paths.items()
             for path in options]
    _end = _t.time() + 40.0
    if deadline:
        _end = min(_end, deadline)
    with _cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_probe_one, base + path): role
                for base, role, path in tasks}
        try:
            for fut in _cf.as_completed(
                    futs, timeout=max(1.0, _end - _t.time())):
                role = futs[fut]
                if role in found:
                    continue
                try:
                    url = fut.result(timeout=5)
                except Exception:
                    continue
                if url:
                    found[role] = url
                if len(found) >= len(paths) or _t.time() > _end \
                        or _dl.expired(deadline):
                    break
        except TimeoutError:
            audit.log("probe_capped", {"company": company,
                                       "found": sorted(found)})
        finally:
            for f in futs:
                f.cancel()
    return found


_PARKED_RE = re.compile(
    r"(premium domain broker|buy (this|this domain)|domain (for sale|broker)|"
    r"parked (domain|free)|this domain (is for sale|may be for sale)|"
    r"get your domain|sell domain names)",
    re.IGNORECASE)


def _is_parked_page(text: str, title: str = "") -> bool:
    """Parked/broker domains (e.g. bullet.com broker page) are not company
    homepages — accepting them poisons website_probe + Hunter domain.
    Title/lead always checked (brokers brand the title); full-text length
    only decides the fallback scan so nav-heavy brokers can't hide."""
    blob_lead = f"{title or ''}\n{(text or '')[:800]}"
    if _PARKED_RE.search(blob_lead or ""):
        return True
    if len((text or "").split()) > 120:
        return False  # substantive page without broker branding
    return bool(_PARKED_RE.search(f"{title or ''}\n{text or ''}" or ""))


def _probe_company_website(company: str) -> dict:
    """Best-effort deterministic probe: company homepage title/meta.
    Real registry/filings/funding APIs plug in here (need customer keys).
    Parked/broker pages are rejected (probe stays none + parked note)."""
    info = {"company": company, "registry": "unverified-manual-check",
            "filings": [], "funding": "unknown", "website_probe": "none",
            "probe_status": "no-data"}
    try:
        from .discovery import disambig_get as _dg, disambig_note as _dn
        _neg = _dg(company)
        _parked_known = set(_neg.get("parked_hosts", []) or [])
    except Exception:
        _parked_known, _dn = set(), None
    for url in _company_domains(company):
        try:
            from urllib.parse import urlparse as _up0
            if (_up0(url).hostname or "").lower() in _parked_known:
                info["probe_status"] = f"parked (cached): {url}"
                continue  # negative cache: skip slow re-fetch of broker page
        except Exception:
            pass
        try:
            from .fetcher import fetch_url
            from .security import resolve_and_assert_no_ssrf
            resolve_and_assert_no_ssrf(url, dns_timeout_s=5.0)  # fail fast
            fr = fetch_url(url)
            if fr.status_code == 200 and fr.body_text.strip():
                if _is_parked_page(fr.body_text, fr.title):
                    info["probe_status"] = f"parked: {url}"
                    try:
                        from . import audit as _audit
                        _audit.log("parked_domain",
                                   {"company": company, "url": url})
                        if _dn is not None:
                            from urllib.parse import urlparse as _up1
                            _dn(company,
                                parked_host=(_up1(url).hostname or "").lower())
                    except Exception:
                        pass
                    continue
                info["website_probe"] = url
                info["homepage_excerpt"] = fr.body_text[:500]
                info["probe_status"] = "ok"
                break
        except Exception as e:
            info["probe_status"] = f"unreachable: {type(e).__name__}"
            continue
    return info


# Classified unknowns (§6.5): every gaps[] line ends with a machine cause
# code in brackets so consumers can tell a stealth startup (absent_data,
# normal) from an outage (source_unreachable / budget_exhausted) without
# parsing prose. Legacy "field: detail" prefixes are preserved verbatim.
GAP_CODES = ("absent_data", "source_unreachable", "budget_exhausted",
             "contradiction_unresolved", "filtered")


def gap(field: str, code: str, detail: str) -> str:
    """Build one classified unknowns line. Unknown codes fall back to
    absent_data rather than emitting garbage."""
    code = code if code in GAP_CODES else "absent_data"
    field = (field or "general").strip() or "general"
    return f"{field}: {detail} [{code}]"


def firmographic_unknowns(profile: dict, degraded: list) -> tuple[list, list]:
    """§6.5 item 4 + §5.1: every empty company field gets its own unknowns
    line; connector failure vs no-data are different states (§5.1).

    Each line carries a machine-readable cause code (suffix in brackets):
    absent_data (looked up, nothing reported), source_unreachable (the
    connector/probe itself failed), budget_exhausted (request budget spent),
    contradiction_unresolved (sources disagreed, dropped per gate),
    filtered (gated as off-topic/irrelevant). Legacy substrings are kept
    intact so existing consumers keep matching."""
    gaps, notes = [], []
    if not profile.get("registry") or profile["registry"] in (
            "unknown", "unverified-manual-check"):
        gaps.append(gap("company.registry", "absent_data",
                        "not verified — no registry record found "
                        "(GLEIF/registry lookup empty, not 'no data' "
                        "unless small private companies carry no LEI)"))
        notes.append("Tier-1 registry lookup empty (Wikidata/GLEIF have no "
                     "record): registry unverified, not 'no data'")
    if not profile.get("filings"):
        gaps.append(gap("company.filings", "absent_data",
                        "no public data found"))
    if not profile.get("funding") or profile["funding"] == "unknown":
        gaps.append(gap("company.funding", "absent_data",
                        "no public data found"))
    probe = profile.get("website_probe", "none") or "none"
    status = profile.get("probe_status", "no-data") or "no-data"
    if probe == "none":
        if status.startswith("unreachable"):
            gaps.append(gap("company.website_probe", "source_unreachable",
                            f"homepage unreachable ({status})"))
            notes.append(f"Tier-1 homepage probe failed ({status}): "
                         "connector failure, not 'no data'")
        else:
            gaps.append(gap("company.website_probe", "absent_data",
                            "no public data found"))
    degraded.extend(n for n in notes if n not in degraded)
    return gaps, degraded


def merge_firmo_extra(profile: dict, extra: dict | None) -> dict:
    """Fold Tier-1 enrichment fields (Wikidata/GLEIF/Companies House/Hunter) into a
    firmographic profile. Fill-missing only — never clobbers probed/DBpedia
    values — except `registry`, which upgrades unknown/unverified states
    (an official registry record beats 'not verified'). Sources accumulate."""
    if not extra:
        return profile
    out = dict(profile)
    for k, v in extra.items():
        if k == "sources":
            out["sources"] = list(dict.fromkeys(
                list(out.get("sources", []) or []) + list(v or [])))[:5]
        elif k == "registry":
            if out.get("registry", "unknown") in (
                    "unknown", "", None, "unverified-manual-check") and v:
                out["registry"] = v
        elif k == "website_probe":
            # A failed probe ("none") yields to a real enriched homepage.
            if out.get("website_probe", "none") in ("none", "", None) and v:
                out["website_probe"] = v
                if out.get("probe_status", "") != "ok":
                    out["probe_status"] = "enriched"
        elif v and not out.get(k):
            out[k] = v
    if extra and out.get("website_probe", "none") != "none":
        out["confidence"] = max(float(out.get("confidence", 0) or 0), 0.6)
    return out


def _firmo_key(company: str) -> str:
    """Normalized store key: 'Reliance ' and 'reliance' share one row."""
    return (company or "").strip().casefold()


def pass2_firmographic(company: str, cache: dict | None = None,
                       extra: dict | None = None) -> dict:
    company = (company or "").strip()
    ckey = _firmo_key(company)
    try:
        from . import store
        con = store.connect()
        try:
            hit = store.firmo_get(con, ckey)
            if hit and hit.get("schema") == FIRMO_SCHEMA:
                return merge_firmo_extra(hit, extra)
            profile = _probe_company_website(company)
            from .discovery import dbpedia_company  # keyless Tier-1 firmographics
            dbp, dbp_note = dbpedia_company(company)
            if dbp_note:
                audit.log("tier_down", {"tier": "tier1_dbpedia", "reason": dbp_note})
            # Wrong-entity gate: a DBpedia homepage whose host carries no
            # company core token (Bullet vs seismosoc.org Bulletin) poisons
            # every field from that record — drop the whole record loudly.
            # Acronyms allowed: ril.com for Reliance Industries (initials).
            if dbp and dbp.get("homepage"):
                try:
                    from .discovery import homepage_matches_company as _hmc
                    if not _hmc(dbp["homepage"], company):
                        audit.log("wrong_entity_dropped",
                                  {"tier": "tier1_dbpedia", "company": company,
                                   "homepage": dbp["homepage"][:120]})
                        try:
                            from .discovery import disambig_note as _dn1
                            from urllib.parse import urlparse as _up3b
                            _dn1(company, wrong_host=(
                                _up3b(dbp["homepage"]).hostname or "").lower())
                        except Exception:
                            pass
                        dbp = {}
                except Exception:
                    pass
            for k in ("employees", "hq", "industry", "founded", "revenue"):
                if dbp.get(k):
                    profile[k] = dbp[k]
            profile["schema"] = FIRMO_SCHEMA
            sources = []
            # Fill-missing only: a probed homepage (or parked==none) is never
            # overwritten by DBpedia — probe wins ties.
            if dbp.get("homepage") and profile.get(
                    "website_probe", "none") in ("none", "", None):
                profile["website_probe"] = dbp["homepage"]
                profile["probe_status"] = "ok"
                sources.append(dbp["homepage"])
            if dbp:
                sources.append("https://dbpedia.org/page/"
                               + company.replace(" ", "_"))
            if profile.get("website_probe", "none") != "none":
                profile["confidence"] = 0.75 if dbp else 0.6
                profile["sources"] = sources
            else:
                profile["confidence"] = 0.3
                profile["sources"] = sources if dbp else []
            # L5: persist the ENRICHED profile so direct /briefs calls (which skip
            # discovery) see the same registry proof. Store-hit path below still
            # returns without writing (no TTL refresh on reads).
            profile = merge_firmo_extra(profile, extra)
            store.firmo_put(con, ckey, profile)
            audit.log("pass2_profile", {"company": company, "cached": False})
            return profile
        finally:
            con.close()
    except Exception:
        if cache is not None and ckey in cache:
            return merge_firmo_extra(cache[ckey], extra)
        profile = {"company": company, "registry": "unknown", "filings": [],
                   "funding": "unknown"}
        if cache is not None:
            # Bound fallback cache: normalized key, evict oldest past 500.
            if len(cache) >= 500:
                try:
                    cache.pop(next(iter(cache)))
                except Exception:
                    cache.clear()
            cache[ckey] = profile
        return merge_firmo_extra(profile, extra)


_MD_JUNK = re.compile(
    r"(\]\(https?://|https?://\S|#{1,6}\s+\S*\[?\]?\(?https?://|"
    r"\b(add to my network|people also viewed|sign up to unlock|"
    r"related people|related professional|discover, organize, and deepen|"
    r"from the creators of|the best in tv industry come together|"
    r"scan to download|download\s+[A-Z][\w&]*\s+app\b|"
    r"follow\s+us|dribbble|behance|instagram|impressions|"
    r"seamless\s+r\w*|press.?copyright|aboutpress))",
    re.IGNORECASE)

# Sentence splitter that also breaks on blank lines: tag/section headers
# ("HIREN GADA") must not glue onto the next headline. Offsets stay exact
# (match positions), so verifier spans keep working.
_SENT_SPLIT = re.compile(r".+?(?:[.!?](?:\s|$)|\n\s*\n|$)", re.DOTALL)

_HEADER_LINES = re.compile(r"^(?:[A-Z][A-Z\s&'.,-]{3,}\n)+")

_FOOTER_JUNK = re.compile(
    r"(?:\s*\|\s*)?(?:-\s*YouTube)?\s*AboutPressCopyright\s*$"
    r"|(?:\s+-\s+YouTube(?:About.*)?)$")


def _strip_headers(sent: str, cs: int) -> tuple[str, int]:
    """Cut leading ALL-CAPS header lines (ET tag pages: 'HIREN GADA' glued
    onto the headline with a single newline). Returns (text, new_start);
    the span stays inside the source, so blind verification still holds."""
    m = _HEADER_LINES.match(sent)
    if m:
        cut = len(m.group(0))
        return sent[cut:].strip(), cs + cut
    return sent, cs


def _strip_footers(sent: str) -> str:
    """Cut glued player/chrome tails ('- YouTubeAboutPressCopyright') so a
    real headline survives instead of dying as junk."""
    return _FOOTER_JUNK.sub("", sent).strip()

_FLUFF_RE = re.compile(
    r"\b(trailblazer|thought leader|numerous milestones|"
    r"commitment to excellence|deep understanding of evolving|"
    r"distinguished figure|clear vision,? coupled)\b",
    re.IGNORECASE)


def _has_substance(sent: str) -> bool:
    """PR-fluff without a single concrete anchor (number, date, org, role,
    money) is not a fact — verified-verbatim or not."""
    if _FLUFF_RE.search(sent):
        return bool(re.search(
            r"\b(\d|₹|\$|%|CEO|director|founder|19\d{2}|20\d{2}|"
            r"January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\b", sent))
    return True


def _candidate_sentences(doc: StructuredDoc) -> list[tuple[str, int, int]]:
    """Yield (sentence, char_start, char_end) for section 0, longest-first
    filtered to substance (>=4 tokens). Bounded by caller ceilings."""
    if not doc.sections:
        return []
    text = doc.sections[0].text
    out = []
    for m in _SENT_SPLIT.finditer(text):
        s = m.group(0).strip()
        s, cs = _strip_headers(s, m.start())
        ce = min(m.end(), len(text))  # span ⊇ claim: footer cut, span kept
        s = _strip_footers(s)
        if not s:
            continue
        low = s.lower()
        if "read more" in low or "skip to" in low or "cookie" in low.split()[:4]:
            continue  # nav/related-link furniture, not claims
        if _MD_JUNK.search(s):
            continue  # markdown/social chrome, never a claim
        if not _has_substance(s):
            continue  # praise with no anchor, not a fact
        if len(re.findall(r"\d+%", s)) >= 2:
            continue  # stat block (100% X, 98% Y), not a strategy claim
        if len(re.findall(r"[a-z0-9]{3,}", low)) >= 4 and len(s) <= 600:
            out.append((s, cs, ce))
    return sorted(out, key=lambda t: -len(t[0]))[:3]


# --- Hiring-signal structure (§6.3: postings are an involuntary roadmap) ---
# Deterministic parse only: no model, no inference. Boilerplate (EEO,
# benefits, "equal opportunity") is filtered so aggregate velocity counts
# roles, not paragraphs. Individual postings still flow through the normal
# claim path; velocity is an aggregate context line with its evidence
# set cited explicitly — never a bare number (§1.4).
_JOB_BOILERPLATE_RE = re.compile(
    r"equal opportunity|eeo is the law|benefits\s+(include|offered)|"
    r"we are an equal|reasonable accommodation|privacy notice|"
    r"terms of employment|job requisition id",
    re.IGNORECASE)
_JOB_DEPT_RE = re.compile(
    r"\b(engineering|software|data|design|product|sales|marketing|support|"
    r"operations|finance|legal|people|hr|talent|security|platform|mobile|"
    r"backend|frontend|devops|qa)\b", re.IGNORECASE)
_JOB_SENIORITY_RE = re.compile(
    r"\b(intern(?:ship)?|fresher|entry.?level|junior|associate|senior|staff|"
    r"principal|lead|manager|director|vp|vice.?president|head|chief)\b",
    re.IGNORECASE)
_JOB_GEO_RE = re.compile(
    r"\b(mumbai|bengaluru|bangalore|delhi|new delhi|hyderabad|chennai|pune|"
    r"kolkata|ahmedabad|noida|gurgaon|gurugram|kochi|remote|hybrid|"
    r"india|united states|uk|singapore)\b", re.IGNORECASE)
_JOB_STACK_RE = re.compile(
    r"\b(python|java|react|node\.?js|aws|azure|gcp|kubernetes|docker|sql|"
    r"spark|kafka|tensorflow|pytorch|android|ios|swift|go\b|rust|"
    r"salesforce|figma|tableau)\b", re.IGNORECASE)


def parse_job_posting(text: str) -> dict:
    """Strict sub-schema for one posting body. Empty dict when the text is
    boilerplate-only or carries no role signal."""
    t = (text or "")[:20000]
    if not t.strip() or _JOB_BOILERPLATE_RE.search(t) and \
            len(re.findall(r"[a-z0-9]{3,}", t.lower())) < 30:
        return {}
    dept = _JOB_DEPT_RE.search(t)
    sen = _JOB_SENIORITY_RE.search(t)
    geo = _JOB_GEO_RE.search(t)
    stack = sorted({m.group(1).lower() for m in _JOB_STACK_RE.finditer(t)})[:8]
    if not dept and not sen and not stack:
        return {}
    _ENG_ALIAS = {"backend", "frontend", "mobile", "devops", "qa", "platform"}
    dept_name = dept.group(1).lower() if dept else "unknown"
    if dept_name in _ENG_ALIAS:
        dept_name = "engineering"
    return {"role_department": dept_name,
            "seniority_level": sen.group(1).lower() if sen else "unknown",
            "geo_location": geo.group(1).lower() if geo else "unknown",
            "tech_stack_or_specialty": stack}


def job_velocity(docs: list[StructuredDoc]) -> dict | None:
    """Aggregate hiring counts over JOB_POSTING docs. Returns None when
    fewer than 2 parseable postings exist (a single posting is anecdote,
    not velocity). Evidence doc_ids are part of the payload."""
    from collections import Counter
    parsed: list[tuple[str, dict]] = []
    for d in docs or []:
        sc = getattr(d.source_class, "value", None)
        if sc != "job_posting":
            continue
        blob = "\n".join(s.text or "" for s in d.sections)[:20000]
        p = parse_job_posting(blob)
        if p:
            parsed.append((d.doc_id, p))
    if len(parsed) < 2:
        return None
    depts = Counter(p["role_department"] for _, p in parsed)
    geos = Counter(p["geo_location"] for _, p in parsed)
    top_d = ", ".join(f"{k}x{v}" for k, v in depts.most_common(3))
    top_g = ", ".join(f"{k}x{v}" for k, v in geos.most_common(3))
    return {"postings": len(parsed),
            "departments": dict(depts.most_common(5)),
            "geos": dict(geos.most_common(5)),
            "evidence": [doc_id for doc_id, _ in parsed][:8],
            "line": (f"{len(parsed)} open postings tracked ({top_d}"
                     + (f"; {top_g}" if top_g else "") + ")")}


def attach_hiring_velocity(docs: list[StructuredDoc],
                           firmo: dict | None) -> dict:
    """Fold the velocity line into firmographics (free-form dict — no
    output-contract change). Evidence doc_ids ride along in the value."""
    try:
        v = job_velocity(docs)
    except Exception:
        v = None
    if not v or not isinstance(firmo, dict):
        return firmo
    ev = ", ".join(v["evidence"][:5])
    firmo["hiring_velocity"] = (
        f"{v['line']} (aggregate count over {v['postings']} postings, "
        f"not a verified claim; evidence: {ev})")
    return firmo


# --- Transcript section scoping (prepared remarks vs Q&A) ---
# Prepared remarks are rehearsed PR; Q&A is unrehearsed analyst pressure.
# Tagging is intake metadata: Q&A sentences get a ranking boost, identical
# in kind to the source_class boost. Gates and verifier are untouched.
_QA_ENTER_RE = re.compile(
    r"question[\s-]*and[\s-]*answer|q\s*&\s*a session|q&a\b|"
    r"analyst (questions|day)|operator.*(question|line)|"
    r"we will now (begin|take|open).*question|first question",
    re.IGNORECASE)
_QA_SPEAKER_RE = re.compile(
    r"^(analyst|operator|moderator)[,:]|"
    r"\b(unidentified analyst|questioner)\b",
    re.IGNORECASE)
_PREPARED_RE = re.compile(
    r"prepared remarks|opening remarks|safe harbor|forward[\s-]*looking statements",
    re.IGNORECASE)


def transcript_zones(text: str) -> list[tuple[int, int, str]]:
    """Split transcript body into (start, end, zone) with zone in
    {'prepared', 'qa'}. Non-transcript text returns one 'prepared' zone."""
    t = text or ""
    if not t:
        return []
    if not (_QA_ENTER_RE.search(t) or _QA_SPEAKER_RE.search(t)
            or _PREPARED_RE.search(t) or "transcript" in t.lower()[:500]):
        return [(0, len(t), "prepared")]
    m = _QA_ENTER_RE.search(t)
    cut = m.start() if m else len(t)
    # Speaker-attributed lines after the cut stay Q&A even without headers.
    return [(0, cut, "prepared"), (cut, len(t), "qa")] if cut < len(t) \
        else [(0, len(t), "prepared")]


def transcript_qa_boost(doc, char_pos: int) -> float:
    """+0.15 when char_pos sits in a transcript Q&A zone. Zero otherwise —
    including for non-transcript docs (title/source_class check first)."""
    try:
        sc = getattr(doc.source_class, "value", "")
        blob = ((doc.title or "") + " " +
                ((doc.sections[0].text[:500] if doc.sections
                  and doc.sections[0].text else ""))).lower()
        if sc != "transcript" and "transcript" not in blob \
                and "earnings call" not in blob and "earnings-call" not in blob:
            return 0.0
        text = doc.sections[0].text if doc.sections else ""
        for s, e, zone in transcript_zones(text):
            if s <= (char_pos or 0) < e:
                return 0.15 if zone == "qa" else 0.0
    except Exception:
        pass
    return 0.0


def pass3_strategy(docs: list[StructuredDoc], tools: ToolBoundary,
                   verifier: Verifier, person_name: str = "",
                   company: str = "",
                   owned: set[str] | None = None) -> tuple[list, list[str]]:
    """Bounded loop. Every claim must survive blind verification or be dropped.
    A2: strategy sentences must also be ABOUT the company (mention or
    company-owned doc) — third-party strays never reach the verifier."""
    docs = docs[:P3_MAX_DOCS]
    if not docs:
        audit.log("pass3_done", {"verified": 0})
        return [], ["no strategy signal found [absent_data]"]
    ranked_urls = tools.reranker.rerank(
        "strategy priorities hiring earnings expansion", [d.url for d in docs])
    by_url = {d.url: d for d in docs}
    ordered = [by_url[u] for u in ranked_urls if u in by_url]
    verified, gaps = [], []
    pool = []  # (relevance, doc, sent, cs, ce) — verify relevant first
    from .segment import outlet_boost, relevance_query
    rel_q = relevance_query()
    for doc in ordered:
        # §5.4: source_class is load-bearing — filings, job postings and
        # press outrank generic pages carrying equal words.
        _sc = getattr(doc.source_class, "value", None)
        boost = {"filing": 0.25, "job_posting": 0.25, "press_release": 0.25,
                 "news": 0.2, "transcript": 0.2, "registry": 0.15,
                 "vendor_page": 0.1}.get(_sc, 0.0)
        boost += outlet_boost(doc.url_final or doc.url, doc.title or "")
        # §7.5: dated-current beats undated — a rep acts on "now", not "once".
        m = re.search(r"\b(202[5-9]|203\d)\b", doc.published_at or "")
        if m:
            boost += 0.2
        if person_name and person_name.lower() in (
                (doc.title or "") + " " + ((doc.sections[0].text[:2000]
                                            if doc.sections and
                                            doc.sections[0].text else ""))).lower():
            boost += 0.3  # about THIS person, not a namesake/relative
        for sent, cs, ce in _candidate_sentences(doc):
            pool.append((tools.utility.score(rel_q, sent) + boost
                         + transcript_qa_boost(doc, cs),
                         doc, sent, cs, ce))
    def _story_tokens(s: str) -> set[str]:
        # same story across outlets: drop Outlet/Published metadata lines and
        # outlet suffixes before comparing, so the comedy-slate story told by
        # variety + imdb + moneycontrol clusters instead of filling the brief.
        lines = [ln for ln in s.splitlines()
                 if not re.match(r"^(outlet|published)\s*:",
                                 ln.strip(), re.IGNORECASE)]
        t = " ".join(lines)
        t = re.sub(r"\s+-\s+[A-Za-z][\w. ]{2,60}$", "", t).strip()
        return set(re.findall(r"[a-z0-9]{4,}", t.lower()))

    def _same_story(a: set[str], b: set[str]) -> bool:
        if not a or not b:
            return False
        return len(a & b) / len(a | b) >= 0.55

    pool.sort(key=lambda t: -t[0])
    n_claims = 0
    gated_out = 0
    offtopic_out = 0
    seen_texts: set[str] = set()  # §4.1 dedup: same sentence twice, verify once
    kept_stories: list[set[str]] = []  # one item per story, best outlet wins
    for _, doc, sent, cs, ce in pool:
        norm = re.sub(r"\s+", " ", sent.lower()).strip()
        if norm in seen_texts:
            continue
        seen_texts.add(norm)
        toks = _story_tokens(sent)
        if any(_same_story(toks, k) for k in kept_stories):
            gated_out += 1
            audit.log("duplicate_story",
                      {"doc_id": doc.doc_id,
                       "reason": "another outlet already kept",
                       "excerpt": sent[:120]})
            continue
        keep, reason = relevance_gate(sent)  # §6.3.1: relevance BEFORE faithfulness
        if not keep:
            gated_out += 1
            audit.log("relevance_gate",
                      {"doc_id": doc.doc_id, "verdict": "rejected",
                       "reason": reason, "excerpt": sent[:120]})
            continue
        if not _sentence_company_bound(sent, doc, company, owned):
            gated_out += 1
            offtopic_out += 1
            audit.log("irrelevant_story",
                      {"doc_id": doc.doc_id,
                       "reason": f"not about {(company or '').strip()}",
                       "excerpt": sent[:120]})
            continue
        audit.log("relevance_gate",
                  {"doc_id": doc.doc_id, "verdict": "kept",
                   "reason": reason, "excerpt": sent[:120]})
        if n_claims >= P3_MAX_CLAIMS or len(verified) >= P3_MAX_CLAIMS:
            break
        year = _recency_of(doc, sent)
        _intent = _is_forward_looking(sent)
        _anchor_year = year
        if _intent and _anchor_year == "undated":
            # Undated forward statement inside visibly old material borrows
            # the body's oldest explicit year — "undated" must not launder
            # a 2021 plan into a current priority.
            _bys = [y for y in _body_years(doc)
                    if re.fullmatch(r"(19|20)\d{2}", y)]
            if _bys:
                _anchor_year = min(_bys)
        if not _fresh_enough(_anchor_year):
            if not _intent:
                gated_out += 1
                audit.log("relevance_gate",
                          {"doc_id": doc.doc_id, "verdict": "rejected",
                           "reason": f"stale news ({year} older than "
                                     f"{_NEWS_MAX_AGE_YEARS}y)",
                           "excerpt": sent[:120]})
                continue
            # Stale forward-looking statement: verify grounding, but cap at
            # partial and label historical intent (§7.5) — never current.
            n_claims += 1
            claim = Claim(claim_id=f"c_{doc.doc_id}_{n_claims}", text=sent,
                          doc_id=doc.doc_id, section_index=0,
                          char_start=cs, char_end=ce,
                          recency=historical_intent_label(_anchor_year))
            v = verifier.check(claim, doc)
            if v.verdict == Verdict.SUPPORTED:
                v = v.model_copy(update={
                    "verdict": Verdict.PARTIALLY_SUPPORTED,
                    "note": "historical intent "
                            f"({_anchor_year}): forward statement, no "
                            f"recent anchor — {v.note}"})
            if v.verdict in (Verdict.SUPPORTED, Verdict.PARTIALLY_SUPPORTED):
                verified.append(v)
                kept_stories.append(toks)
            audit.log("historical_intent",
                      {"doc_id": doc.doc_id, "year": _anchor_year,
                       "excerpt": sent[:120]})
            continue
        n_claims += 1
        # Utility scores relevance; Agent wording would go here with weights.
        # Claim text stays the verbatim sentence so the blind check is exact.
        claim = Claim(claim_id=f"c_{doc.doc_id}_{n_claims}", text=sent,
                      doc_id=doc.doc_id, section_index=0,
                      char_start=cs, char_end=ce, recency=year)
        v = verifier.check(claim, doc)
        if v.verdict in (Verdict.SUPPORTED, Verdict.PARTIALLY_SUPPORTED):
            verified.append(v)
            kept_stories.append(toks)
        # unsupported: dropped + logged (verifier.py), never shown
    if not verified:
        gaps.append("no strategy signal found [absent_data]")
    if offtopic_out and (company or "").strip().lower() not in ("", "unknown"):
        gaps.append(gap("strategy.offtopic", "filtered",
                        f"{offtopic_out} sentence(s) not about "
                        f"{company.strip()} — dropped, never verified"))
    audit.log("pass3_done", {"verified": len(verified), "candidates": n_claims,
                             "relevance_rejected": gated_out,
                             "offtopic_dropped": offtopic_out})
    return verified, gaps


def pass4_synthesize(brief: Brief, collateral: list[str],
                     tools: ToolBoundary | None = None) -> Brief:
    """No network access: score internal collateral against verified signals.
    Pitch is synthesized prose over gated findings (§6.5 item 3).
    GPU path: gpt-oss:20b draft over verified gists only (never raw web);
    deterministic gist fallback on any failure."""
    if not brief.strategy_signals:
        brief.pitch = "No verified signals — no pitch generated."
        if not any("no strategy signal found" in g for g in brief.gaps):
            brief.gaps.append("no strategy signal found [absent_data]")
    else:
        base = synthesize_pitch(brief.strategy_signals)
        brief.pitch = base
        try:
            agent = tools.agent if tools is not None else None
            if agent is not None:
                gists = "; ".join(
                    _gist(v.claim.text) for v in brief.strategy_signals[:3])
                draft = agent.draft_claim(
                    f"Buyer priorities: {gists}. Write a 2-sentence sales "
                    f"pitch grounded ONLY in these facts.")
                if draft and len(draft.strip()) >= 20:
                    # Guardrail: draft must reuse verified vocabulary —
                    # else keep deterministic pitch (no hallucinations).
                    import re as _re2
                    vocab = set(_re2.findall(
                        r"[a-z0-9]{4,}", gists.lower()))
                    dtoks = set(_re2.findall(r"[a-z0-9]{4,}",
                                             draft.lower()))
                    if vocab and len(vocab & dtoks) >= 3:
                        brief.pitch = draft[:600]
        except Exception:
            pass
    audit.log("pass4_done", {"pitch_len": len(brief.pitch)})
    return brief


_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

# §6.3.1 relevance gate (utility-model call type; deterministic reference
# implementation). Faithfulness (§7.4) is not relevance: a faithfully
# extracted anecdote is still not a strategy signal. Keep only spans that
# state a company/person priority, strategic intent, or forward plan.
_REL_CUES = re.compile(
    r"\b(plan|plans|planning|strategy|strategic|growth|expand|expansion|"
    r"launch|launched|launching|invest|investment|investments|revenue|"
    r"earnings|profit|ebitda|hiring|hire|hires|jobs|careers|roadmap|target|"
    r"acquisition|acquire|merger|partnership|partner|subscriber|subscription|"
    r"ipo|guidance|forecast|outlook|research|development|project|management|"
    r"rank|ranking|global|brand|advertising|sponsorship|content|streaming|"
    r"ai\b|digital|retail|telecom|energy|"
    r"appoint|appointed|appointment|appoints|named|succeeds|successor|"
    r"steps down|step down|resign|resigns|resigned|exits|joins)\b",
    re.IGNORECASE)
_IRR_CUES = re.compile(
    r"\b(born|birth|school|schooling|college|professor|wife|husband|son\b|"
    r"daughter|family|married|marriage|childhood|anecdote|awards?|recognition|"
    r"honou?rs?|philanthrop|women\b|community|outreach|teacher|donat|"
    r"podium|slides?|applause|transcript|webcast|disclaimer|disclosure|"
    r"solicitation|newsletter|advertisement|privacy|cookies?|copyright|"
    r"all rights reserved|terms of (use|service)|"
    r"firearm|ammunition|projectile|cartridge|propellant|caliber|muzzle|"
    r"ballistic)\b",
    re.IGNORECASE)


def _company_mentioned(sent: str, company: str) -> bool:
    """Local mirror of discovery._company_mentioned (avoids import weight
    in hot paths; keep semantics identical)."""
    t, c = (sent or "").lower(), (company or "").strip().lower()
    if not c or c == "unknown":
        return False
    if c in t:
        return True
    toks = [p for p in re.findall(r"[a-z]{3,}", c)]
    return bool(toks) and toks[0] in t


_ENTITY_HOSTS = ("wikidata.org", "search.gleif.org", "hunter.io",
                 "dbpedia.org")


def company_owned_hosts(firmo: dict | None, extra_urls: list | None = None) -> set[str]:
    """Hosts treated as company-owned for the Pass-3 company rule: probed
    homepage + entity records + caller-supplied owned pages."""
    from urllib.parse import urlparse as _up
    out = set(_ENTITY_HOSTS)
    for u in list((firmo or {}).get("website_probe") and
                  [firmo.get("website_probe"), firmo.get("homepage")] or []) \
            + list(extra_urls or []):
        try:
            h = (_up(u or "").hostname or "").lower()
        except Exception:
            h = ""
        if h and h != "none":
            out.add(h)
    return out


def _sentence_company_bound(sent: str, doc, company: str,
                            owned: set[str] | None) -> bool:
    """A2: strategy sentences must be ABOUT the company — mention it, or
    come from a company-owned doc. Third-party strays (a BNY Mellon
    sentence in an Imperial Milestone brief) die here, never verified."""
    co = (company or "").strip()
    if not co or co.lower() == "unknown":
        return True
    if _company_mentioned(sent, co):
        return True
    try:
        from urllib.parse import urlparse as _up
        host = (_up(doc.url_final or doc.url or "").hostname or "").lower()
    except Exception:
        host = ""
    return bool(host) and host in (owned or set())


def relevance_gate(sent: str) -> tuple[bool, str]:
    """Binary: is this span a priority/strategic-intent statement? Returns
    (keep, reason). Rejected spans never reach the verifier and never count
    toward coverage. Tuned on Appendix A: kills the 6 trivia false
    positives, keeps genuine priorities."""
    rel = len(_REL_CUES.findall(sent))
    irr = len(_IRR_CUES.findall(sent))
    if sent.strip().endswith("?"):
        return False, "question, not a stated priority"
    if irr > 0 and rel - 2 * irr < 2:
        return False, f"biographical/personal markers outweigh intent ({irr})"
    if rel >= 2:
        return True, f"strategic-intent markers ({rel})"
    return False, "no stated priority or forward plan found"

_ROLE_PATTERNS = [
    re.compile(r"\b(?:CEO|chief executive|founder|co-founder|cofounder|"
               r"president|chairman|owner|chief executive officer)\b"
               r"(?:\s+[a-z,]+){0,3}\s+(?:of|at)\s+"
               r"([A-Z][A-Za-z0-9&.'-]+(?:\s+[A-Z][A-Za-z0-9&.'-]+){0,3})"),
    re.compile(r"\b([A-Z][A-Za-z0-9&]+(?:\s+[A-Z][A-Za-z0-9&]+){0,2})\s+"
               r"(?:CEO|founder|co-founder|cofounder)\b"),
]

_ORG_STOP = {"The", "This", "That", "Forbes", "Bloomberg", "Reuters",
             "Associated Press", "New York", "Wall Street",
             # adjectives captured by role patterns, never companies:
             "Best", "Young", "Deputy", "Former", "Acting", "New", "Top",
             "Chief", "Co", "Deputy Chief"}


def infer_companies(name: str, docs: list[StructuredDoc],
                    top_n: int = 2) -> list[tuple[str, float, list[str]]]:
    """Suggest companies for a bare name from role-pattern evidence.

    Returns [(company, confidence, [doc_ids])]. Deterministic extractors only;
    the human still confirms before anything proceeds (§6.3)."""
    votes: dict[str, list[str]] = {}
    for d in docs:
        blob = ((d.title or "") + "\n" + "\n".join(
            s.text or "" for s in d.sections))[:30000]
        if not name_match(blob, name or ""):
            continue
        for pat in _ROLE_PATTERNS:
            for m in pat.finditer(blob):
                org = re.sub(r"[.,' ]+$", "", m.group(1)).strip()
                org = org.split(". ")[0].strip()  # stop sentence bleed
                if len(org) < 2 or org in _ORG_STOP or org.lower() == name.lower():
                    continue
                votes.setdefault(org, [])
                if d.doc_id not in votes[org]:
                    votes[org].append(d.doc_id)
    ranked = sorted(votes.items(), key=lambda kv: -len(kv[1]))[:top_n]
    return [(org, min(0.85, 0.45 + 0.1 * len(ids)), ids[:3])
            for org, ids in ranked]


_ROLE_WORD = re.compile(
    r"\b(CEO|chief executive|founder|co-founder|cofounder|president|"
    r"chairman|owner|CTO|director|head)\b",
    re.IGNORECASE)


def roles_from_evidence(name: str, docs: list[StructuredDoc],
                        top_n: int = 3) -> list[dict]:
    """LinkedIn-experience-style card without LinkedIn: company + role words
    captured alongside the association, all evidence-bound (§8.7).

    Same-sentence rule: a role word counts only in a sentence that names
    BOTH the person and the org. Blob-wide matching over-attributed
    (e.g. 'co-founder' from an Inflection sentence landing on Microsoft)."""
    out = []
    for org, conf, ids in infer_companies(name, docs, top_n):
        roles: set[str] = set()
        for d in docs:
            if d.doc_id not in ids:
                continue
            blob = ((d.title or "") + "\n" + "\n".join(
                s.text or "" for s in d.sections))[:30000]
            for m in _SENT_SPLIT.finditer(blob):
                sent = re.sub(r"\s+", " ", m.group(0)).strip()
                if len(sent) > 400 or len(sent.split()) < 4:
                    continue
                if not name_match(sent, name):
                    continue
                if org.lower() not in sent.lower():
                    continue
                for rm in _ROLE_WORD.finditer(sent):
                    roles.add(rm.group(1).upper())
                    if len(roles) >= 3:
                        break
                if len(roles) >= 3:
                    break
        out.append({"company": org, "role": ", ".join(sorted(roles)),
                    "evidence": ids})
    return out


_BIO_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("born", re.compile(
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\s+was\s+born\s+(?:on\s+)?"
        r"([^.;]{3,120})", re.IGNORECASE)),
    ("father", re.compile(
        r"(?:father|dad)\s+(?:is|was)\s+([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,3})",
        re.IGNORECASE)),
    ("mother", re.compile(
        r"(?:mother|mom)\s+(?:is|was)\s+([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,3})",
        re.IGNORECASE)),
    ("parents", re.compile(
        r"(?:son|daughter)\s+of\s+([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,3})"
        r"\s+and\s+([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,3})",
        re.IGNORECASE)),
    ("spouse", re.compile(
        r"(?:wife|husband|spouse|married(?:\s+to)?)\s+(?:is\s+|was\s+)?"
        r"([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,3})", re.IGNORECASE)),
    ("children", re.compile(
        r"(?:children|sons?|daughters?|kids)\s*(?:are|is|include)?\s*[:\-]?\s*"
        r"([A-Z][A-Za-z.'-]*(?:\s*,\s*[A-Z][A-Za-z.'-]*)*"
        r"(?:\s+and\s+[A-Z][A-Za-z.'-]*)?)", re.IGNORECASE)),
    ("education", re.compile(
        r"((?:graduated|degree|studied|studied\s+at|holds\s+a|"
        r"University|College|Institute|School)[^.;]{5,160})", re.IGNORECASE)),
    ("career", re.compile(
        r"((?:joined|appointed(?:\s+as)?|served\s+as|founded|"
        r"became|took\s+over\s+as|named\s+|currently\s+serves\s+|"
        r"is\s+the\s+(?:CEO|director|founder|chairman|president|head|chief\s+\w+)\s+of|"
        r"works\s+as\s+)[^.;]{5,180})", re.IGNORECASE)),
]


def _title_about_other(title: str, name: str) -> bool:
    """True when the doc title names a DIFFERENT person sharing the surname
    (Nita/Tina/Anil Ambani pages when asked about Mukesh). Those pages
    poison spouse/children/career with the relative's facts."""
    import difflib
    t, n = (title or "").lower(), name.lower().strip()
    if not t or not n:
        return False
    nparts = [p for p in re.findall(r"[a-z]{2,}", n)]
    if not nparts:
        return False
    if nparts[-1] not in t:  # surname absent from title: not a person page
        return False
    if n in t:
        return False  # own page
    twords = set(re.findall(r"[a-z]{2,}", t))
    first = nparts[0]
    if first in twords:
        return False
    if difflib.get_close_matches(first, twords, n=1, cutoff=0.75):
        return False
    return True


def extract_bio(name: str, docs: list[StructuredDoc]) -> dict:
    """Full-profile bio (not recency-capped): full name, parents, spouse,
    children, education, career highlights. Deterministic regex over docs
    that fuzzy-match the person; every item keeps doc evidence. Old facts
    are kept — this is the opposite of the 2-3-year strategy window."""
    name = name or ""
    bio: dict[str, list] = {"full_name": [name.strip()], "evidence": {}}
    for _k, _ in _BIO_PATTERNS:
        bio.setdefault(_k, [])
    seen: dict[str, set] = {k: set() for k, _ in _BIO_PATTERNS}
    seen.setdefault("birth", set())
    for d in docs:
        if _title_about_other(d.title or "", name):
            continue  # relative's page: not about THIS person
        blob = ((d.title or "") + "\n" + "\n".join(
            s.text or "" for s in d.sections))[:30000]
        if not name_match(blob, name):
            continue
        own_page = name_match(d.title or "", name) and not _title_about_other(
            d.title or "", name)
        for m in _SENT_SPLIT.finditer(blob):
            sent = re.sub(r"\s+", " ", m.group(0)).strip()
            if len(sent) > 400 or len(sent.split()) < 4:
                continue
            if _MD_JUNK.search(sent):
                continue  # nav/social markdown, never bio
            if not name_match(sent, name):
                # family/education sentences may lead with a relative's name
                # — allow those ONLY on the person's own page.
                if not own_page:
                    continue
                if not any(k in sent.lower() for k in
                           ("wife", "husband", "married", "son of",
                            "daughter of", "father", "mother", "graduated",
                            "studied", "degree", "born")):
                    continue
            for key, pat in _BIO_PATTERNS:
                if key == "born":
                    continue  # birth handled as dated fact below
                for mm in pat.finditer(sent):
                    val = re.sub(r"\s+", " ", mm.group(0)).strip()[:200]
                    if len(val) < 8:
                        continue
                    if key in ("father", "mother", "parents",
                               "spouse", "children"):
                        # patterns run IGNORECASE: reject matches whose
                        # names are lowercase words (son of Reliance
                        # Retail and handed...) — not real names.
                        tail = re.split(r"\b(?:of|is|was|to)\b", val,
                                        maxsplit=1)
                        nouns = re.findall(r"[A-Za-z][a-z.'-]*",
                                           tail[-1] if len(tail) > 1 else val)
                        if not nouns or not nouns[0][0].isupper():
                            continue
                        if " and " in val.lower():
                            after = re.split(r"\band\b", val,
                                             flags=re.IGNORECASE)[-1].strip()
                            if not after or not after[0].isupper():
                                continue
                        if key in ("father", "mother", "parents",
                                   "spouse", "children"):
                            # employers aren't family: "children: Sony and
                            # Viacom" is a career sentence misread.
                            if re.search(
                                    r"\b(netflix|sony|viacom|zee|reliance|"
                                    r"disney|amazon|hotstar|jio|tata|adani)\b",
                                    val, re.IGNORECASE):
                                continue
                    low = val.lower()
                    if key in seen and low in seen[key]:
                        continue
                    seen[key].add(low)
                    bio[key].append(val)
                    bio["evidence"].setdefault(key, [])
                    if d.doc_id not in bio["evidence"][key]:
                        bio["evidence"][key].append(d.doc_id)
                    break
        # birth year / birthplace: same attribution rule — the born-sentence
        # must name him (or sit on his own page), else it's someone else's
        # birth (e.g. Basu's) quoted in the same article.
        for m2 in _SENT_SPLIT.finditer(blob):
            bsent = re.sub(r"\s+", " ", m2.group(0)).strip()
            if "born" not in bsent.lower():
                continue
            if not name_match(bsent, name) and not own_page:
                continue
            for my in re.finditer(r"\bborn\b[^.;]{3,100}", bsent,
                                  re.IGNORECASE):
                val = re.sub(r"\s+", " ", my.group(0)).strip()[:160]
                if val.lower() not in seen.get("birth", set()):
                    seen.setdefault("birth", set()).add(val.lower())
                    bio.setdefault("birth", []).append(val)
    # cap lists so the card stays readable; evidence preserved
    for k in list(bio.keys()):
        if k in ("full_name", "evidence"):
            continue
        bio[k] = bio.get(k, [])[:4]
    return bio


_DATELINE_RE = re.compile(
    r"\b(?:(\d{1,2})\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|"
    r"May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s*,?\s*((?:19|20)\d{2})|"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|"
    r"May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})\s*,?\s*((?:19|20)\d{2})|"
    r"((?:19|20)\d{2})-(\d{2})-(\d{2}))", re.IGNORECASE)

_NEWS_MAX_AGE_YEARS = 2


def _recency_of(doc: StructuredDoc, sent: str = "") -> str:
    """Recency: record's published_at first; else an explicit news dateline
    in the sentence (month+year or ISO date — not a bare mined year, so
    'born 1957' never labels). Missing date renders 'undated'."""
    m = _YEAR_RE.search(doc.published_at if isinstance(
        doc.published_at, str) else "")
    if m:
        return m.group(0)
    if sent:
        dm = _DATELINE_RE.search(sent)
        if dm:
            for g in dm.groups():
                if g and re.fullmatch(r"(19|20)\d{2}", g):
                    return g
    return "undated"


def _fresh_enough(recency: str, max_age_years: int = _NEWS_MAX_AGE_YEARS) -> bool:
    """News freshness: explicit old years (< cutoff) fail; undated passes
    (can't prove stale — bio/reference material lives here)."""
    import time as _t
    if recency == "undated":
        return True
    try:
        return _t.gmtime().tm_year - int(recency) <= max_age_years
    except (ValueError, TypeError):
        return True


# Temporal entailment (§7.5): a 2021 "we plan to launch by Q3", verified as
# grounded in isolation, becomes distortion if presented as a 2026 plan.
# Forward-looking statements bind to their publish timestamp: stale ones
# are labeled historical intent and capped at partial, never current.
_FORWARD_RE = re.compile(
    r"\b(will|plans?\s+to|aims?\s+to|targets?\s+to|expects?\s+to|intends?\s+to|"
    r"launch(?:ing|ed)?\s+(by|in)|by\s+Q[1-4]|in\s+FY\d{2}|"
    r"over\s+the\s+next|going\s+to)\b",
    re.IGNORECASE)


def _is_forward_looking(sent: str) -> bool:
    """True when the sentence states intent about the future (launch, plan,
    target with a horizon) rather than a completed fact."""
    return bool(_FORWARD_RE.search(sent or ""))


def _body_years(doc) -> list[str]:
    """Explicit years mined from the doc body (for undated forward-looking
    sentences sitting in visibly old material)."""
    try:
        text = doc.sections[0].text if doc.sections else ""
    except Exception:
        return []
    return sorted(set(_YEAR_RE.findall(text or "")))


def historical_intent_label(year: str) -> str:
    """Recency tag for a stale forward-looking statement: keeps the date
    visible and marks it past intent, never present reality."""
    return f"{year} · historical intent"


# Point-in-time confidence decay (§7.5): volatile metrics (headcount,
# funding stage, titles, velocity) go stale fast. Explicit old years get a
# visible label; undated/fresh values pass through untouched. Already-labeled
# intent strings are never double-tagged. Labels only — nothing verified is
# ever silently dropped.
_STALE_AFTER_YEARS = 2


def staleness_label(recency: str | None) -> str:
    """'' when fresh/undated/unparseable; else ' — stale, unconfirmed
    since {year}'."""
    r = (recency or "").strip()
    if not r or r == "undated" or "historical intent" in r or "stale" in r:
        return ""
    m = re.search(r"\b((?:19|20)\d{2})\b", r)
    if not m:
        return ""
    try:
        import time as _t
        if _t.gmtime().tm_year - int(m.group(1)) > _STALE_AFTER_YEARS:
            return f" — stale, unconfirmed since {m.group(1)}"
    except (ValueError, TypeError):
        pass
    return ""


def _mentions(text: str, *needles: str) -> bool:
    t = text.lower()
    return all(n.lower() in t for n in needles if n)


def name_match(text: str, name: str) -> bool:
    """Fuzzy person match: exact substring wins; else surname exact +
    first-name fuzzy (Punit/Puneet typo via 3-letter stem or edit check)."""
    import difflib
    t, n = text.lower(), name.lower().strip()
    if not n:
        return False
    if n in t:
        return True
    parts = [p for p in re.findall(r"[a-z]{2,}", n)]
    if not parts:
        return False
    surname = parts[-1]
    if surname not in t:
        return False
    if len(parts) == 1:
        return True
    first = parts[0]
    if first in t or first[:3] in t:
        return True
    words = set(re.findall(r"[a-z]{2,}", t))
    return bool(difflib.get_close_matches(first, words, n=1, cutoff=0.75))


def person_evidence_strength(name: str, company: str,
                               docs: list[StructuredDoc],
                               cands: list) -> tuple[bool, str]:
    """Thin-person detector for the company-depth fallback.

    Strong when the top candidate carries real confidence OR at least two
    independent docs name the person. Otherwise the brief pivots: full
    company depth, person fields as honest unknowns — a sparse person is
    never padded with weak evidence (§6.3)."""
    matched = 0
    for d in docs:
        blob = ((d.title or "") + "\n" + "\n".join(
            s.text for s in d.sections))[:8000]
        if name_match(blob, name):
            matched += 1
    top = cands[0].confidence if cands else 0.0
    if top >= 0.6 or matched >= 2:
        return True, ""
    return False, (f"person evidence thin (top confidence {top:.2f}, "
                   f"{matched} name-matched doc(s)): company-depth brief, "
                   f"person fields unknown")


def pass1_candidates(name: str, company: str,
                     docs: list[StructuredDoc]) -> list[PersonIdentity]:
    """§6.3 entity resolution: 2–3 candidates WITH evidence for the human.

    Ranked by evidence strength: name+company mentions, then name-only,
    then company-only (wrong-person risk made explicit, not hidden).
    Company empty/unknown: name mentions count as direct evidence."""
    name, company = (name or ""), (company or "")
    company_known = company.strip().lower() not in ("", "unknown")
    both, name_only, co_only = [], [], []
    for d in docs:
        blob = ((d.title or "") + "\n" + "\n".join(
            s.text or "" for s in d.sections))[:8000]
        has_name, has_co = name_match(blob, name), (
            _mentions(blob, company) if company_known else False)
        if has_name and (has_co or not company_known):
            both.append(d.doc_id)
        elif has_name:
            name_only.append(d.doc_id)
        elif has_co:
            co_only.append(d.doc_id)
    cands = []
    if both or True:
        cands.append(PersonIdentity(
            full_name=name.strip(), company=company.strip(),
            confidence=min(0.95, 0.5 + 0.1 * len(both)),
            sources=both[:5] or ["no direct mention yet — confirm from role evidence"]))
    if name_only:
        cands.append(PersonIdentity(
            full_name=name.strip(), company=company.strip() + " (unconfirmed — name found without company)",
            confidence=0.35, sources=name_only[:5]))
    if co_only:
        cands.append(PersonIdentity(
            full_name=name.strip() + " (unconfirmed — company found without name)",
            company=company.strip(), confidence=0.25, sources=co_only[:5]))
    audit.log("pass1_candidates", {"n": len(cands),
                                   "evidence_docs": len(both) + len(name_only)})
    return cands[:3]


def _gist(statement: str, words: int = 14) -> str:
    """Paraphrase-free gist for generation: strip outlet/pub metadata lines
    and outlet suffixes, cut at a word boundary. No quotes, no truncation
    mid-word — the raw string never reaches narrative output (§6.5)."""
    lines = [ln for ln in statement.splitlines()
             if ln.strip() and not re.match(
                 r"^(outlet|published)\s*:", ln.strip(), re.IGNORECASE)]
    text = " ".join(lines)
    text = re.sub(r"\s+-\s+[A-Za-z][\w. ]{2,60}$", "", text).strip()
    text = text.rstrip(".")
    return " ".join(text.split()[:words])


def synthesize_pitch(verified: list, budget: int = 600) -> str:
    """§6.5 item 3: bounded generated paragraph over gated findings — never
    a concatenation, never cut mid-sentence. Agent model owns this prose
    with weights; this is the deterministic reference shape."""
    if not verified:
        return "No verified signals — no pitch generated."
    gists = [_gist(v.claim.text) for v in verified[:3]]
    recencies = sorted({v.claim.recency for v in verified
                        if v.claim.recency != "undated"})
    para = ("Verified priorities lead with " + gists[0] + ". "
            + ("Supporting signals include " + "; ".join(gists[1:]) + ". "
               if len(gists) > 1 else "")
            + (f"Signals are current to {recencies[-1]}. "
               if recencies else "Sources are undated reference material. "))
    if len(para) > budget:  # conclude cleanly: cut at last sentence boundary
        cut = para[:budget].rsplit(".", 1)[0] + "."
        para = cut if len(cut) > 100 else para[:budget].rsplit(" ", 1)[0] + "…"
    return para.strip()


def _real_collateral(collateral: list[str]) -> list[str]:
    """§6.5 item 1: placeholder collateral fails closed — it must never
    reach capability_map[] as if it were real."""
    return [c for c in collateral if "[DEFAULT" not in c]


def build_profile_card(name: str, docs: list[StructuredDoc],
                       ref_hits: list | None = None,
                       firmo: dict | None = None) -> tuple[list, list]:
    """Google-style structured card: current roles (evidence-bound) +
    manual-check references. LinkedIn is linked, never fetched (§8.7)."""
    from .schemas import Reference, RoleRef
    roles = [RoleRef(company=r["company"], role=r["role"], evidence=r["evidence"])
             for r in roles_from_evidence(name, docs)]
    refs: list = []
    seen = set()
    for h in ref_hits or []:
        url = getattr(h, "url", "")
        if not url or url in seen:
            continue
        # Belt over suspenders: drop namesake LinkedIn profiles that
        # slipped in via a stored session (§6.3). Search/company URLs
        # and name-matching titles pass; 'Aryan Gupta' for an
        # 'Aryan Saini' query does not.
        try:
            from .discovery import _linkedin_relevant as _lr
            if "linkedin." in url and not _lr(
                    url, getattr(h, "title", "") or "", name):
                continue
        except Exception:
            pass
        seen.add(url)
        refs.append(Reference(
            label=getattr(h, "title", "") or "LinkedIn profile",
            url=url, note="open manually — never scraped (§8.7)"))
    for d in docs:
        u = d.url_final or d.url or ""
        if "wikipedia.org/wiki/" in u and u not in seen:
            # full fuzzy title match only: "Elon Musk" page yes;
            # "Errol Musk" (father) and "Musk (film)" share one token but
            # are different subjects — never link them.
            if name_match(d.title or "", name) and not _title_about_other(
                    d.title or "", name):
                seen.add(u)
                refs.append(Reference(label=d.title or "Wikipedia",
                                      url=u, note="reference profile"))
    site = (firmo or {}).get("website_probe", "none")
    if site and site != "none" and site not in seen:
        refs.append(Reference(label="Company site", url=site,
                              note="official owned page"))
    return roles, refs


def build_output_contract(brief: Brief, collateral: list[str] | None,
                          tools: ToolBoundary | None = None) -> Brief:
    """Fill the §6.5 output contract from verified signals. unknowns[] (gaps)
    is required and already maintained; priorities/capability_map derive ONLY
    from verified claims — conflicting sources are both surfaced (§7.5)."""
    brief.priorities = list(brief.priorities or [])
    brief.capability_map = list(brief.capability_map or [])
    brief.gaps = list(brief.gaps or [])
    collateral = collateral or []
    for v in brief.strategy_signals or []:
        brief.priorities.append(Priority(
            statement=v.claim.text[:300], evidence=[v.claim.claim_id],
            recency=v.claim.recency or "undated",
            confidence=0.8 if v.verdict == Verdict.SUPPORTED else 0.5))
    if brief.priorities and collateral:
        real = _real_collateral(collateral)
        if not real:
            # §6.5 item 1: fail closed into unknowns, never ship placeholder.
            brief.gaps.append("no mapped capability configured for this "
                              "priority (collateral not configured)")
        scored = []
        for p in brief.priorities:
            for c in real:
                s = tools.utility.score(p.statement, c) if tools else 0.0
                scored.append((s, p, c))
        scored.sort(key=lambda t: -t[0])
        seen_p, cmap = set(), []
        for _, p, c in scored:
            if not p.evidence:
                continue
            if p.evidence[0] not in seen_p:
                seen_p.add(p.evidence[0])
                cmap.append(CapabilityMap(
                    our_capability=c[:200], their_priority_ref=p.evidence[0],
                    rationale=f"collateral overlaps verified priority ({p.recency})"))
            if len(cmap) >= 3:
                break
        brief.capability_map = cmap
    if brief.priorities:
        top = brief.priorities[0]
        brief.opening_question = (  # §6.5 item 2: generated sentence, no quotes
            f"What is your current plan for {_gist(top.statement)}?")
        partials = [p for p in brief.priorities if p.confidence < 0.8]
        if partials and partials[0].evidence:
            brief.likely_objection = Objection(
                statement="Evidence is partial here — expect 'prove it' pushback.",
                basis=f"{partials[0].evidence[0]} ({partials[0].recency})")
    elif brief.gaps:
        brief.likely_objection = Objection(
            statement="No public signal found — expect 'why are you talking to us' pushback.",
            basis=brief.gaps[0])
        brief.opening_question = (
            "What is the one initiative your leadership would not cancel this year?")
    brief.contradictions = detect_contradictions(brief.strategy_signals)
    return brief


_NEG_RE = re.compile(
    r"\b(not|no |never|n't|denied|denies|rejected|rejects|opposed|oppose|"
    r"against| false)\b",
    re.IGNORECASE)


PERSON_DETAILS_MAX = 8

_NAV_JUNK = re.compile(
    r"\b(read more|skip to|cookie|subscribe|newsletter|all rights reserved|"
    r"terms of (use|service)|privacy policy|sign in)\b",
    re.IGNORECASE)


def pass_person_details(docs: list[StructuredDoc], verifier: Verifier,
                        person_name: str = "") -> list:
    """Google-style person facts: every verified name-mention sentence.

    Unlike pass3_strategy this does NOT use relevance_gate — bio/profile
    facts (role, company, education, career) are the product, not trivia
    to kill. Still blind-verified: unsupported sentences are dropped.
    Ranked: name-in-title + name-mention + longer sentences first."""
    if not docs or not (person_name or "").strip():
        return []
    pool: list[tuple[float, StructuredDoc, str, int, int]] = []
    for doc in docs:
        if not doc.sections:
            continue
        if _title_about_other(doc.title or "", person_name):
            continue  # relative's page: facts would be about them, not him
        title_hit = 0.5 if name_match(doc.title or "", person_name) else 0.0
        text = doc.sections[0].text or ""
        for m in _SENT_SPLIT.finditer(text):
            sent = m.group(0).strip()
            sent, mcs = _strip_headers(sent, m.start())
            sent = _strip_footers(sent)
            if not sent:
                continue
            low = sent.lower()
            if not name_match(sent, person_name):
                continue  # Google-style: about THIS person only
            if _NAV_JUNK.search(sent) or _MD_JUNK.search(sent):
                continue
            if sent.strip().endswith("?"):
                continue
            if len(re.findall(r"[a-z0-9]{3,}", low)) < 4 or len(sent) > 600:
                continue
            if not _has_substance(sent):
                continue
            if not _fresh_enough(_recency_of(doc, sent)):
                continue  # news older than 2y never surfaces as recent fact
            cs, ce = mcs, min(m.end(), len(text))
            score = title_hit + min(0.5, len(sent) / 600.0)
            if _recency_of(doc, sent) != "undated":
                score += 1.0  # dated-current first: Sep 2026 beats undated bio
            if re.search(r",\s*(?:who\s+is\s+)?(?:director|CEO|chief)\b[^\"']{0,60}"
                         r"\bsaid[,:]?\s*[\"']", sent, re.IGNORECASE):
                # reporting clause: he's the speaker, the quote is about
                # someone else — sink it below sentences about him.
                score -= 0.4
            pool.append((score, doc, sent, cs, ce))
    pool.sort(key=lambda t: -t[0])
    out, seen, n = [], set(), 0
    kept: list[set[str]] = []  # one item per story: variety over imdb+variety
    for _, doc, sent, cs, ce in pool[: PERSON_DETAILS_MAX * 4]:
        norm = re.sub(r"\s+", " ", sent.lower()).strip()
        if norm in seen:
            continue
        seen.add(norm)
        lines = [ln for ln in sent.splitlines()
                 if not re.match(r"^(outlet|published)\s*:",
                                 ln.strip(), re.IGNORECASE)]
        toks = set(re.findall(r"[a-z0-9]{4,}",
                              re.sub(r"\s+-\s+[A-Za-z][\w. ]{2,60}$", "",
                                     " ".join(lines)).lower()))
        if toks and any(len(toks & k) / len(toks | k) >= 0.55 for k in kept):
            continue  # same story, another outlet already kept
        n += 1
        claim = Claim(claim_id=f"p_{doc.doc_id[:8]}_{n}", text=sent,
                      doc_id=doc.doc_id, section_index=0,
                      char_start=cs, char_end=ce,
                      recency=_recency_of(doc, sent))
        v = verifier.check(claim, doc)
        if v.verdict in (Verdict.SUPPORTED, Verdict.PARTIALLY_SUPPORTED):
            out.append(v)
            if toks:
                kept.append(toks)
        if len(out) >= PERSON_DETAILS_MAX:
            break
    audit.log("person_details_done", {"kept": len(out), "candidates": n})
    return out


def detect_contradictions(verified: list) -> list:
    """§7.5: surface claim pairs sharing a distinctive token set but pulling
    opposite directions (negation/polarity cues or different figures), with
    both recencies. Conservative: only flags, never resolves."""
    from .schemas import Contradiction
    from .verifier import _toks
    out = []
    for i in range(len(verified)):
        for j in range(i + 1, len(verified)):
            a, b = verified[i].claim, verified[j].claim
            if a.doc_id == b.doc_id:
                continue
            ta = {t for t in _toks(a.text) if len(t) > 5}
            tb = {t for t in _toks(b.text) if len(t) > 5}
            shared = ta & tb
            if len(shared) < 2:
                continue
            na = set(_NEG_RE.findall(a.text))
            nb = set(_NEG_RE.findall(b.text))
            years_a = set(re.findall(r"\b(?:19|20)\d{2}\b", a.text))
            years_b = set(re.findall(r"\b(?:19|20)\d{2}\b", b.text))
            if na != nb or (years_a and years_b and years_a != years_b):
                out.append(Contradiction(
                    claim_a=a.text[:200], claim_b=b.text[:200],
                    recency_a=a.recency, recency_b=b.recency,
                    note=f"shared: {sorted(shared)[:5]}"))
            if len(out) >= 5:
                return out
    return out
