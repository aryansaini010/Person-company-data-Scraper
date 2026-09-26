"""API: briefs + Pass-1 human gate + live /research (URLs -> full pipeline)."""
from __future__ import annotations
import json
import time
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from prospect_intel import audit
from prospect_intel.acquisition import acquire
from prospect_intel.discovery import discover_company, discover_person
from prospect_intel.models import ToolBoundary
from prospect_intel.passes import (build_output_contract, pass1_confirm,
                                   pass1_propose_candidates,
                                   pass2_firmographic, pass3_strategy,
                                   pass4_synthesize)
from prospect_intel.schemas import (Brief, PersonIdentity, SourceClass,
                                     StructuredDoc, Verdict)
from prospect_intel.verifier import Verifier

app = FastAPI(title="Prospect Intelligence")
tools = ToolBoundary()
verifier = Verifier()
# In-mem firmo fallback cache (only used when store is unreachable).
# Bounded + normalized: pass2 writes ckey (casefold) and evicts past 500.
# Primary cache is SQLite firmographics (90d TTL) — this dict never grows
# unbounded and never diverges by case ("Reliance " == "reliance").
firmo_cache: dict = {}
FIRMO_MEM_MAX = 500
briefs: dict[str, Brief] = {}
sessions: dict = {}  # session_id -> {docs, degraded, collateral, name, ...}
SESSION_TTL_S = 1800  # abandoned confirmations evaporate
import os as _os
if _os.environ.get("WEB_CONCURRENCY", "") not in ("", "1"):
    try:
        from prospect_intel import audit as _audit0
        _audit0.log("multi_worker_sessions",
                    {"note": "sessions persist to SQLite; in-mem is read-through cache"})
    except Exception:
        pass


def _session_sweep() -> None:
    now = time.time()
    for sid in [s for s, v in sessions.items()
                if now - v.get("created_at", now) > SESSION_TTL_S]:
        sessions.pop(sid, None)
    # DB-backed sweep: multi-worker safe expiry (best-effort).
    try:
        from prospect_intel import store as _store
        _con = _store.connect()
        try:
            _store.session_sweep(_con, now)
        finally:
            _con.close()
    except Exception:
        pass


def _session_put(sid: str, sess: dict) -> None:
    sessions[sid] = sess
    # Bound in-mem sessions (abandoned gates) — DB is source of truth.
    if len(sessions) > 500:
        try:
            oldest = min(sessions.items(),
                         key=lambda kv: kv[1].get("created_at", 0))[0]
            sessions.pop(oldest, None)
        except Exception:
            pass
    try:
        from prospect_intel import store as _store
        _con = _store.connect()
        try:
            _store.session_save(_con, sid, sess, SESSION_TTL_S)
        finally:
            _con.close()
    except Exception:
        pass


def _session_get(sid: str):
    sess = sessions.get(sid)
    if sess is not None:
        return sess
    try:
        from prospect_intel import store as _store
        _con = _store.connect()
        try:
            sess = _store.session_load(_con, sid)
        finally:
            _con.close()
        if sess is not None:
            sessions[sid] = sess
        return sess
    except Exception:
        return None


def _session_del(sid: str) -> None:
    sessions.pop(sid, None)
    try:
        from prospect_intel import store as _store
        _con = _store.connect()
        try:
            _store.session_delete(_con, sid)
        finally:
            _con.close()
    except Exception:
        pass

SEED_COLLATERAL = [
    "Platform scaling case study: helped an enterprise client grow engineering hiring while holding release quality.",
    "Earnings-call intelligence brief: how we map exec commentary to buyer priorities.",
    "Hiring-signal playbook: turning job postings into a validated account roadmap.",
]


def _collateral() -> list[str]:
    try:
        from prospect_intel import store
        con = store.connect()
        rows = con.execute("SELECT text FROM collateral").fetchall()
        con.close()
        if rows:
            return [r[0] for r in rows]
    except Exception:
        pass
    from prospect_intel.segment import default_collateral
    return default_collateral()


def _persist_brief(bid: str, brief: Brief) -> None:
    briefs[bid] = brief
    try:
        from prospect_intel import store
        con = store.connect()
        con.execute("INSERT OR REPLACE INTO briefs VALUES (?,?,?)",
                    (bid, brief.model_dump_json(), time.time()))
        for v in brief.strategy_signals:
            c = v.claim
            con.execute("INSERT OR REPLACE INTO claims VALUES (?,?,?,?,?,?,?,?,?)",
                        (c.claim_id, bid, c.text, c.doc_id, c.section_index,
                         c.char_start, c.char_end, v.verdict.value, v.note))
        con.commit()
        con.close()
    except Exception:
        pass


def _auto_push_brief(bid: str, brief: Brief) -> dict:
    """Best-effort auto-push: readable .txt into Open WebUI Files
    (+ Knowledge when OPENWEBUI_KNOWLEDGE_ID is set). Never raises —
    failures return a note and leave the brief itself unaffected."""
    if (_os.environ.get("OPENWEBUI_AUTO_PUSH", "") == "0"
            or "PYTEST_CURRENT_TEST" in _os.environ
            or "pytest" in __import__("sys").modules):
        return {"openwebui_file_id": None,
                "openwebui_note": "auto-push skipped (disabled under test)"}
    try:
        from prospect_intel.export import export_readable
        from prospect_intel import openwebui as _ow
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.gettempdir()) / f"{bid}.txt"
        export_readable(brief, {}, tmp)
        kid = (_os.environ.get("OPENWEBUI_KNOWLEDGE_ID", "") or "").strip()
        fid, note = _ow.upload_brief_file(str(tmp), kid)
        try:
            audit.log("openwebui_auto_push", {"id": bid, "file": fid,
                                              "note": note})
        except Exception:
            pass
        return {"openwebui_file_id": fid, "openwebui_note": note}
    except Exception as e:
        return {"openwebui_file_id": None,
                "openwebui_note": f"auto-push skipped: {type(e).__name__}"}


def _hunter_enrich(firmo: dict, company: str, docs: list[StructuredDoc],
                   degraded: list) -> dict:
    """Hunter-by-domain (free key, quota-guarded): homepage/website_probe
    yields a domain, unlocking company enrichment. Docs join the evidence
    pool BEFORE Pass 3; fields merge fill-missing only. No key/quota →
    loud note, brief proceeds. Mutates docs/degraded in place."""
    _site = firmo.get("website_probe", "none") or "none"
    if _site == "none":
        # Best-of-what-we-have: the Wikidata homepage feeds the same
        # domain lookup when no probe succeeded.
        _site = firmo.get("homepage", "") or "none"
    if _site == "none":
        return firmo
    try:
        from urllib.parse import urlparse as _up
        from prospect_intel import enrich as _enrich
        from prospect_intel.acquisition import queue_to_core, _MEM_QUEUE
        from prospect_intel.passes import merge_firmo_extra
        _domain = (_up(_site).hostname or "").lower().removeprefix("www.")
        if _domain and "." in _domain:
            _hdocs, _hfirmo, _hnotes = _enrich.enrich_company_by_domain(
                _domain, company)
            for _n in _hnotes:
                if _n not in degraded:
                    degraded.append(_n)
            _have = {d.doc_id for d in docs}
            for _hd in _hdocs:
                if _hd.doc_id not in _have:
                    _have.add(_hd.doc_id)
                    docs.append(_hd)
                    try:
                        queue_to_core(_hd, _MEM_QUEUE)
                    except Exception:
                        pass
            firmo = merge_firmo_extra(firmo, _hfirmo)
    except Exception:
        pass  # enrichment best-effort: gaps say so via notes above
    return firmo


def _incomplete_note(degraded: list) -> list:
    from prospect_intel.deadline import INCOMPLETE_NOTE
    if INCOMPLETE_NOTE not in degraded:
        degraded.append(INCOMPLETE_NOTE)
    return degraded


def _run_brief(name: str, company: str, docs: list[StructuredDoc],
               collateral: list[str],
               degraded: list[str] | None = None,
               firmo_extra: dict | None = None,
               deadline: float | None = None) -> tuple[str, Brief]:
    from prospect_intel import deadline as _dl
    from prospect_intel.passes import pass1_candidates
    degraded = degraded or []
    cands = pass1_candidates(name, company or "unknown", docs)
    top = cands[0] if cands else PersonIdentity(full_name=name,
                                                company=company or "unknown")
    person = pass1_confirm(PersonIdentity(
        full_name=top.full_name, company=top.company, title=top.title,
        confidence=top.confidence, sources=top.sources, human_confirmed=True))
    if person.company.strip().lower() in ("", "unknown"):
        try:
            from prospect_intel.passes import infer_companies
            inferred = infer_companies(person.full_name, docs, top_n=1)
            if inferred:
                person.company = inferred[0][0]
                person.confidence = max(person.confidence, inferred[0][1])
                person.sources = list(dict.fromkeys(
                    person.sources + inferred[0][2]))[:5]
        except Exception:
            pass
    # Pass-0 identity grounding for person briefs too (company side only —
    # person intent never touches Tier-3; resolvers are keyless registers).
    # Merges verified LEI/legal_name into firmo_extra BEFORE pass2 so the
    # person brief carries the same registry proof as company briefs.
    try:
        from prospect_intel import resolvers as _res0
        if person.company.strip().lower() not in ("", "unknown") \
                and not _dl.expired(deadline):
            _p0_firmo, _p0_docs, _p0_deg, _p0_ok, _, _ = \
                _res0.get_ground_truth(person.company, deadline)
            for _n in _p0_deg:
                if _n not in degraded:
                    degraded.append(_n)
            _have0 = {d.doc_id for d in docs}
            for _pd in _p0_docs:
                if _pd.doc_id not in _have0:
                    _have0.add(_pd.doc_id)
                    docs.append(_pd)
                    try:
                        from prospect_intel.acquisition import queue_to_core, _MEM_QUEUE
                        queue_to_core(_pd, _MEM_QUEUE)
                    except Exception:
                        pass
            firmo_extra = dict(firmo_extra or {})
            for _k, _v in (_p0_firmo or {}).items():
                if _k == "sources":
                    firmo_extra["sources"] = list(dict.fromkeys(
                        firmo_extra.get("sources", []) + list(_v or [])))[:5]
                elif _v and _k not in firmo_extra:
                    firmo_extra[_k] = _v
            if firmo_extra.get("lei"):
                degraded[:] = [n for n in degraded
                               if "no LEI on the Wikidata record" not in n]
    except Exception:
        pass
    firmo = pass2_firmographic(person.company, firmo_cache,
                                 extra=firmo_extra)
    # Company-site fallback (interactive.py parity): pass2 only probes
    # guessed domains + DBpedia. When both miss (private cos), a
    # company-name-only Firecrawl lookup (Tier-3-allowed, §5.1) finds the
    # real homepage — best-effort, never blocks the brief. Skipped when
    # the request budget is already spent.
    if firmo.get("website_probe", "none") == "none" and person.company.strip().lower() not in ("", "unknown") and not _dl.expired(deadline):
        try:
            import concurrent.futures as _cf
            from prospect_intel.passes import probe_company_pages
            # NB: no `with` block — Executor.__exit__ waits for the worker,
            # re-hanging on slow DNS/render. Shutdown without waiting instead.
            _ex = _cf.ThreadPoolExecutor(max_workers=1)
            try:
                owned = _ex.submit(probe_company_pages, person.company,
                                   deadline).result(timeout=20)
            finally:
                _ex.shutdown(wait=False, cancel_futures=True)
            if owned:
                first = next(iter(owned.values()))
                firmo["website_probe"] = first
                firmo["probe_status"] = "ok"
                firmo["sources"] = list(dict.fromkeys(firmo.get("sources", []) + [first]))[:5]
        except Exception:
            pass  # timeout/failure: keep probe none, gaps say so
    # Hunter-by-domain (free key, quota-guarded): the homepage above yields
    # a domain, which unlocks the company-enrichment lookup. Docs join the
    # evidence pool BEFORE Pass 3 so strategy/details see them; fields merge
    # fill-missing only. No key/quota → loud note, brief proceeds.
    degraded = degraded or []
    firmo = _hunter_enrich(firmo, person.company, docs, degraded) \
        if not _dl.expired(deadline) else firmo
    try:
        from prospect_intel.passes import attach_hiring_velocity
        firmo = attach_hiring_velocity(docs, firmo)
    except Exception:
        pass
    from prospect_intel.passes import firmographic_unknowns
    fgaps, degraded = firmographic_unknowns(firmo, degraded or [])
    # Thin-person fallback: weak identity evidence → company-depth brief.
    # Person facts stay unknown instead of padded with weak evidence (§6.3).
    from prospect_intel.passes import person_evidence_strength
    from prospect_intel.passes import company_owned_hosts
    _strong, _thin_note = person_evidence_strength(
        person.full_name, person.company, docs, cands)
    if not _strong and _thin_note not in degraded:
        degraded.append(_thin_note)
    verified, gaps = pass3_strategy(docs, tools, verifier, person.full_name,
                                    person.company,
                                    company_owned_hosts(firmo))
    if not _strong:
        from prospect_intel.passes import gap as _gap
        gaps = list(gaps) + [_gap(f"person.{k}", "absent_data",
                                  "unknown — identity evidence too thin")
                             for k in ("role", "bio", "details")]
    from prospect_intel.passes import extract_bio, pass_person_details
    person_details = [] if not _strong else pass_person_details(
        docs, verifier, person.full_name)
    if _strong:
        try:
            bio = extract_bio(person.full_name, docs)
        except Exception:
            bio = {}
    else:
        bio = {}
    gaps = fgaps + gaps
    if _dl.expired(deadline):
        _incomplete_note(degraded)
    brief = Brief(person=person, firmographic=firmo,
                  strategy_signals=verified, gaps=gaps,
                  person_details=person_details, bio=bio,
                  degraded=degraded or [])
    brief = pass4_synthesize(brief, collateral or _collateral(), tools)
    brief = build_output_contract(brief, collateral or _collateral(), tools)
    bid = f"brief_{uuid.uuid4().hex[:12]}"
    _persist_brief(bid, brief)
    audit.log("brief_created", {"id": bid,
                                "signals": len(verified), "gaps": gaps})
    return bid, brief


class BriefRequest(BaseModel):
    name: str
    company: str
    docs: list[StructuredDoc] = []
    collateral: list[str] = []


class ResearchRequest(BaseModel):
    name: str
    company: str
    urls: list[str] = []
    query: str = ""
    max_results: int = 5
    source_class: SourceClass = SourceClass.OTHER
    collateral: list[str] = []


class EntityResolveRequest(BaseModel):
    name: str
    company: str = ""


@app.post("/entity-resolve")
def entity_resolve(req: EntityResolveRequest):
    return pass1_propose_candidates(req.name, req.company)


@app.post("/entity-confirm")
def entity_confirm(p: PersonIdentity):
    p.human_confirmed = True
    try:
        return pass1_confirm(p)
    except PermissionError as e:
        raise HTTPException(403, str(e))


@app.post("/briefs")
def create_brief(req: BriefRequest):
    bid, brief = _run_brief(req.name, req.company, req.docs,
                            req.collateral or _collateral())
    # Same profile card as the confirm path (roles + manual refs).
    try:
        from prospect_intel.passes import build_profile_card
        roles, refs = build_profile_card(brief.person.full_name, req.docs,
                                         [], brief.firmographic)
        brief.current_roles = roles
        brief.references = refs
        _persist_brief(bid, brief)
    except Exception:
        pass
    return {"id": bid, "brief": brief, **_auto_push_brief(bid, brief)}


@app.post("/research")
def research(req: ResearchRequest):
    """Step 1 (human gate): discover + fetch, return candidates WITH evidence.
    The rep selects one; only then does step 2 run passes 2-4. Never
    auto-confirms — a wrong-company brief is worse than no brief (§6.3)."""
    import uuid
    # Normalize whitespace: " " is empty, not a query (UI sends ""/blanks).
    req.name = (req.name or "").strip()
    req.company = (req.company or "").strip()
    req.query = (req.query or "").strip()
    urls = [u.strip() for u in (req.urls or []) if (u or "").strip()]
    degraded: list[str] = []
    from prospect_intel import deadline as _rdl
    deadline = _rdl.start()  # 3-4 minute budget: partial + marked on breach
    disc = None
    if not urls and not req.query and (req.name or req.company):
        req.query = f"{req.name} {req.company or ''}".strip()
    if not urls and req.query:
        disc = discover_person(req.name or req.query, req.company or "",
                               req.query, deadline=deadline)
        degraded.extend(disc.degraded)
        urls = [h.url for h in disc.hits]
    if not urls:
        if req.name or req.company or req.query:
            # Discovery yielded zero URLs (e.g. SearXNG engines suspended,
            # NewsAPI down, no wiki match for a private name). Don't 400 —
            # the wiki + RSS floor below may still produce docs; else the
            # existing no-docs branch returns a gaps-only brief.
            degraded.append(
                "discovery returned no URLs (all search tiers empty); "
                "continuing with reference + news floor")
        else:
            raise HTTPException(
                400, "provide urls or query (fill name + company, "
                "or a query, or paste URLs)")
    docs = acquire([u for u in urls[: req.max_results or 5]
                    if "wikipedia.org" not in u], req.source_class,
                    deadline=deadline)
    from prospect_intel.discovery import (tier1_wikipedia_docs,
                                              tier1_wikipedia_docs_for_urls)
    from prospect_intel.acquisition import queue_to_core, _MEM_QUEUE
    wiki_docs, wiki_note = tier1_wikipedia_docs(
        " ".join(x for x in (req.name, req.company, req.query) if x),
        subject=(req.name, req.company))
    if wiki_note:
        degraded.append(wiki_note)
    for wd in wiki_docs + tier1_wikipedia_docs_for_urls(urls):
        if wd.doc_id not in {d.doc_id for d in docs}:
            docs.append(wd)
            try:
                queue_to_core(wd, _MEM_QUEUE)
            except Exception:
                pass
    # Tier-1 enrichment docs (Wikidata/GLEIF/Companies House: pre-structured,
    # spans, no fetch). These survive even when every search tier is empty.
    if disc is not None and disc.docs:
        _have_ids = {d.doc_id for d in docs}
        for _ed in disc.docs:
            if _ed.doc_id not in _have_ids:
                _have_ids.add(_ed.doc_id)
                docs.append(_ed)
                try:
                    queue_to_core(_ed, _MEM_QUEUE)
                except Exception:
                    pass
    # Current-news floor + company diet (shared helper): segment
    # press/hiring angles, relevance-gated; headlines only here (person
    # flow stays fast — full articles belong to company flows).
    try:
        from prospect_intel.discovery import company_diet
        company_diet(req.company or "", req.name or "", docs, degraded,
                     fetch_full=False, max_angles=3, deadline=deadline)
    except Exception:
        pass
    if not docs:
        bid, brief = _run_brief(req.name, req.company, [], req.collateral,
                                degraded,
                                disc.firmo_extra if disc else None,
                                deadline)
        # Keep manual-check refs (LinkedIn) even on a gaps-only brief —
        # otherwise Sources goes empty when discovery found no URLs.
        try:
            from prospect_intel.discovery import linkedin_manual_refs
            from prospect_intel.passes import build_profile_card
            _rh = list(disc.references) if disc and disc.references else \
                linkedin_manual_refs(req.name, req.company or "")
            _roles, _refs = build_profile_card(
                req.name, [], _rh, brief.firmographic)
            brief.current_roles = _roles
            brief.references = _refs
            _persist_brief(bid, brief)
        except Exception:
            pass
        return {"status": "brief", "id": bid, "brief": brief, "fetched": 0,
                "warning": "no fetchable documents; brief contains gaps only",
                "doc_urls": {}, "evidence": [],
                "unknowns": _unknowns_flat(brief)}
    from prospect_intel.passes import pass1_candidates
    cands = pass1_candidates(req.name, req.company or "unknown", docs)
    sid = f"sess_{uuid.uuid4().hex[:12]}"
    try:
        from prospect_intel.discovery import linkedin_manual_refs as _lmr
        _sess_refs = list(disc.references) if disc and disc.references else \
            _lmr(req.name, req.company or "")
    except Exception:
        _sess_refs = list(disc.references) if disc else []
    _session_put(sid, {"docs": docs, "degraded": degraded,
                     "collateral": req.collateral, "name": req.name,
                     "company": req.company, "references": _sess_refs,
                     "firmo_extra": dict(disc.firmo_extra) if disc else {},
                     "created_at": time.time(),
                     "shown": [c.model_dump() for c in cands]})
    _session_sweep()
    audit.log("pass1_candidates_shown", {"session": sid, "n": len(cands)})
    return {"status": "needs_confirmation", "session_id": sid,
            "candidates": [c.model_dump() for c in cands],
            "fetched": [{"doc_id": d.doc_id, "url": d.url_final or d.url,
                         "snapshot": d.content_hash} for d in docs],
            "degraded": degraded}


class ResearchConfirm(BaseModel):
    session_id: str
    index: int = 0


@app.post("/research/confirm")
def research_confirm(req: ResearchConfirm):
    """Step 2: rep-selected identity → passes 2-4 → §6.5 brief."""
    _session_sweep()
    sess = _session_get(req.session_id)
    if sess is None:
        raise HTTPException(404, "unknown/expired session — re-run research")
    from prospect_intel.passes import pass1_candidates
    cands = pass1_candidates(sess["name"], sess["company"] or "unknown",
                             sess["docs"])
    if not (0 <= req.index < len(cands)):
        raise HTTPException(400, "candidate index out of range")
    # Index into the SHOWN list when it matches (no silent re-derivation);
    # strip display-only suffixes ("... (unconfirmed — ...)") before any
    # firmographic lookup so the suffix never pollutes company data.
    sel = cands[req.index]
    try:
        shown = sess.get("shown", []) or []
        if 0 <= req.index < len(shown) and len(shown) == len(cands):
            sel = PersonIdentity(**shown[req.index])
    except Exception:
        pass
    base_company = sel.company.split(" (unconfirmed")[0].strip()
    from prospect_intel import deadline as _cdl
    bid, brief = _run_brief(sel.full_name, base_company or sel.company,
                            sess["docs"], sess["collateral"], sess["degraded"],
                            sess.get("firmo_extra"), _cdl.start())
    # bind the human-selected evidence, not a re-guessed identity —
    # except when the selection had no company: keep the doc-inferred one
    # (bare-name flow) instead of wiping it back to "unknown".
    brief.person.full_name = sel.full_name
    if base_company.strip().lower() not in ("", "unknown"):
        brief.person.company = base_company
    brief.person.confidence = max(sel.confidence, brief.person.confidence)
    brief.person.sources = list(dict.fromkeys(
        sel.sources + brief.person.sources))[:5]
    from prospect_intel.passes import build_profile_card
    from prospect_intel.passes import person_evidence_strength as _pes
    roles, refs = build_profile_card(sel.full_name, sess["docs"],
                                     sess.get("references"), brief.firmographic)
    # Thin-person fallback: roles from weak evidence would be wrong-person
    # bait — drop them, keep the manual refs. The note is already on the
    # brief (added inside _run_brief on the shared degraded list).
    _strong_c, _ = _pes(sel.full_name, base_company, sess["docs"], cands)
    brief.current_roles = roles if _strong_c else []
    brief.references = refs
    _persist_brief(bid, brief)
    doc_urls = {d.doc_id: (d.url_final or d.url) for d in sess["docs"]}
    _session_del(req.session_id)
    try:
        from prospect_intel.fusion import fuse_signals
        _fused = fuse_signals(brief.strategy_signals)
    except Exception:
        _fused = []
    return {"status": "brief", "id": bid, "brief": brief,
            "doc_urls": doc_urls, "evidence": _evidence_flat(brief, doc_urls),
            "unknowns": _unknowns_flat(brief), "fused": _fused,
            **_auto_push_brief(bid, brief)}


class CompanyRequest(BaseModel):
    company: str
    max_results: int = 8
    collateral: list[str] = []


@app.post("/company-research")
def company_research(req: CompanyRequest):
    """Company-first brief: company name in, company brief out. No person,
    no Pass-1 identity gate — the company IS the subject, and company data
    resolves deterministically (site, registries, filings, press)."""
    import time as _t0mod
    _t0 = _t0mod.time()
    _timings: dict[str, float] = {}

    def _mark(stage: str) -> None:
        _timings[stage] = round(_t0mod.time() - _t0, 1)

    degraded: list[str] = []
    company = (req.company or "").strip()
    if not company:
        raise HTTPException(400, "provide company (company name required)")
    from prospect_intel import deadline as _odl
    deadline = _odl.start()
    disc = discover_company(company, req.max_results or 8, deadline=deadline)
    degraded.extend(disc.degraded)
    _mark("discovery_s")
    # Pass-0 resolved identity (legal name + official site) feeds the
    # scoped stages below; display name stays as typed.
    _legal = (disc.firmo_extra.get("legal_name", "") or company).strip() or company
    urls = [h.url for h in disc.hits]
    # Pass 2 targeted crawl ordering: governance/filings/annual-report
    # URLs render first so the fetch budget hits density, not homepages.
    try:
        from prospect_intel.firecrawl import prioritize_governance_urls
        urls = prioritize_governance_urls(urls)
    except Exception:
        pass
    docs = acquire([u for u in urls[: req.max_results or 8]
                    if "wikipedia.org" not in u], SourceClass.OTHER,
                    deadline=deadline)
    from prospect_intel.discovery import (hit_relevant,
                                          tier1_google_news_rss,
                                          tier1_wikipedia_docs,
                                          tier1_wikipedia_docs_for_urls)
    from prospect_intel.acquisition import queue_to_core, _MEM_QUEUE
    wiki_docs, wiki_note = tier1_wikipedia_docs(company, subject=("", company))
    # Canonical retry: bare "Zee" search returns the disambiguation page;
    # the legal name returns the enterprise article.
    if _legal.strip().lower() != company.strip().lower():
        try:
            _cw, _cn = tier1_wikipedia_docs(_legal, subject=("", _legal))
            wiki_docs = list(_cw) + list(wiki_docs)
            wiki_note = wiki_note or _cn
        except Exception:
            pass
    if wiki_note:
        degraded.append(wiki_note)
    _have_ids = {d.doc_id for d in docs}
    for wd in wiki_docs + tier1_wikipedia_docs_for_urls(urls):
        if wd.doc_id not in _have_ids:
            _have_ids.add(wd.doc_id)
            docs.append(wd)
            try:
                queue_to_core(wd, _MEM_QUEUE)
            except Exception:
                pass
    if disc.docs:
        for _ed in disc.docs:
            if _ed.doc_id not in _have_ids:
                _have_ids.add(_ed.doc_id)
                docs.append(_ed)
                try:
                    queue_to_core(_ed, _MEM_QUEUE)
                except Exception:
                    pass
    # Company diet: shared helper (segment angles, gated, full fetch).
    try:
        from prospect_intel.discovery import company_diet
        company_diet(company, "", docs, degraded,
                     fetch_full=True, max_angles=4, deadline=deadline)
    except Exception:
        pass
    # Bounded planner fan-out (opt-in via PLANNER_ENABLE): parallel
    # news/filings/jobs Round 1 with sufficiency-stop (max 2 rounds).
    # Tier-1 only; person queries never touch Tier-3 (preserved inside).
    try:
        import os as _os2
        if _os2.environ.get("PLANNER_ENABLE", "") == "1":
            from prospect_intel import planner as _pl
            _plan = _pl.build_plan("", company, tools)
            audit.log("plan_built", {"company": company,
                                     "cats": sorted(_plan.keys())})
            _pl.execute_round(_plan, docs, degraded, deadline,
                              fetch_full=True)
    except Exception:
        pass
    # Owned pages (newsroom/careers), then registries + enrichment.
    try:
        from prospect_intel.passes import probe_company_pages
        owned = probe_company_pages(company, deadline=deadline)
    except Exception:
        owned = {}
    for role, url in (owned or {}).items():
        if url in {d.url_final or d.url for d in docs}:
            continue
        sc = (SourceClass.JOB_POSTING if role == "careers"
              else SourceClass.PRESS_RELEASE if role == "newsroom"
              else SourceClass.VENDOR_PAGE)
        try:
            for _dd in acquire([url], sc, deadline=deadline):
                if _dd.doc_id not in {d.doc_id for d in docs}:
                    docs.append(_dd)
        except Exception:
            pass
    _mark("fetch_diet_probe_s")
    if _odl.expired(deadline):
        degraded = _incomplete_note(degraded)
    firmo = pass2_firmographic(company, firmo_cache,
                               extra=disc.firmo_extra)
    # Owned pages prove the real homepage even when guesses fail: derive it
    # so Hunter-by-domain can fire (depth chain for obscure companies).
    if firmo.get("website_probe", "none") in ("none", "", None) and owned:
        try:
            from urllib.parse import urlparse as _up2
            _h = (_up2(next(iter(owned.values()))).hostname or "")
            if _h:
                firmo["website_probe"] = f"https://{_h}"
                firmo["probe_status"] = "owned-page"
                firmo["sources"] = list(dict.fromkeys(
                    firmo.get("sources", []) + [f"https://{_h}"]))[:5]
        except Exception:
            pass
    firmo = _hunter_enrich(firmo, company, docs, degraded)
    try:
        from prospect_intel.passes import attach_hiring_velocity
        firmo = attach_hiring_velocity(docs, firmo)
    except Exception:
        pass
    from prospect_intel.passes import firmographic_unknowns
    fgaps, degraded = firmographic_unknowns(firmo, degraded)
    from prospect_intel.passes import company_owned_hosts
    _mark("firmographics_s")
    verified, gaps = pass3_strategy(docs, tools, verifier, "", company,
                                    company_owned_hosts(
                                        firmo, list((owned or {}).values())))
    _mark("strategy_s")
    gaps = fgaps + gaps
    person = PersonIdentity(full_name="", company=company, confidence=0.0,
                            sources=[], human_confirmed=False)
    brief = Brief(person=person, firmographic=firmo,
                  strategy_signals=verified, gaps=gaps,
                  person_details=[], bio={},
                  degraded=degraded or [])
    gaps = brief.gaps + ["person.unknown: company-only brief — no person "
                         "researched, nothing inferred"]
    brief.gaps = gaps
    brief = pass4_synthesize(brief, req.collateral or _collateral(), tools)
    brief = build_output_contract(brief, req.collateral or _collateral(),
                                  tools)
    from prospect_intel.passes import build_profile_card
    roles, refs = build_profile_card("", docs, disc.references, firmo)
    brief.current_roles = roles
    brief.references = refs
    bid = f"brief_{uuid.uuid4().hex[:12]}"
    _persist_brief(bid, brief)
    _mark("synthesis_s")
    audit.log("brief_created", {"id": bid, "company": company,
                                "signals": len(verified), "gaps": brief.gaps,
                                "timings": _timings})
    doc_urls = {d.doc_id: (d.url_final or d.url) for d in docs}
    try:
        from prospect_intel.fusion import fuse_signals
        fused = fuse_signals(verified)
        audit.log("fusion_built", {"id": bid, "signals": len(fused)})
    except Exception:
        fused = []
    return {"status": "brief", "id": bid, "brief": brief,
            "fetched": len(docs), "doc_urls": doc_urls,
            "evidence": _evidence_flat(brief, doc_urls),
            "unknowns": _unknowns_flat(brief), "fused": fused,
            "timings": _timings, **_auto_push_brief(bid, brief)}


def _evidence_flat(brief: Brief, doc_urls: dict) -> list[dict]:
    """Agent-friendly evidence: one hop instead of priorities→claims→docs.
    Each row carries statement, verbatim quote, source URL, recency,
    confidence — no joins required."""
    out = []
    for v in brief.strategy_signals or []:
        c = v.claim
        out.append({"statement": c.text[:300], "quote": c.text[:300],
                    "url": (doc_urls or {}).get(c.doc_id, ""),
                    "recency": c.recency or "undated",
                    "confidence": 0.8 if v.verdict == Verdict.SUPPORTED else 0.5,
                    "verdict": v.verdict.value})
    return out


def _unknowns_flat(brief: Brief) -> list[dict]:
    return [{"field": (g.split(":")[0] if ":" in g else "general"),
             "code": "unknown", "detail": g} for g in (brief.gaps or [])]


@app.get("/briefs/{bid}/readable.txt", response_class=PlainTextResponse)
def get_brief_readable(bid: str):
    """Agent-closable loop: plain-text brief for upload/RAG/chat grounding."""
    brief = briefs.get(bid)
    if brief is None:
        try:
            from prospect_intel import store
            con = store.connect()
            row = con.execute("SELECT data FROM briefs WHERE id=?",
                              (bid,)).fetchone()
            con.close()
            if row:
                brief = Brief.model_validate_json(row[0])
        except Exception:
            brief = None
    if brief is None:
        raise HTTPException(404, "unknown brief")
    try:
        from prospect_intel.export import export_readable
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.gettempdir()) / f"{bid}.txt"
        export_readable(brief, {}, tmp)
        return PlainTextResponse(tmp.read_text(encoding="utf-8"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"render failed: {type(e).__name__}")


class PushRequest(BaseModel):
    brief_id: str
    knowledge_id: str = ""


@app.post("/openwebui/push")
def openwebui_push(req: PushRequest):
    """Export→upload server-side: brief readable into Open WebUI Files
    (+ optional Knowledge attach). Needs OPENWEBUI_API_KEY; without it,
    loud degraded note — the brief itself is unaffected."""
    from prospect_intel import openwebui as _ow
    brief = briefs.get(req.brief_id)
    if brief is None:
        try:
            from prospect_intel import store
            con = store.connect()
            row = con.execute("SELECT data FROM briefs WHERE id=?",
                              (req.brief_id,)).fetchone()
            con.close()
            if row:
                brief = Brief.model_validate_json(row[0])
        except Exception:
            brief = None
    if brief is None:
        raise HTTPException(404, "unknown brief")
    try:
        from prospect_intel.export import export_readable
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.gettempdir()) / f"{req.brief_id}.txt"
        export_readable(brief, {}, tmp)
    except Exception as e:
        raise HTTPException(500, f"render failed: {type(e).__name__}")
    fid, note = _ow.upload_brief_file(str(tmp), req.knowledge_id or "")
    if fid is None:
        raise HTTPException(502, note or "upload failed")
    return {"file_id": fid, "note": note}


@app.get("/briefs/{bid}")
def get_brief(bid: str):
    if bid in briefs:
        return briefs[bid]
    try:
        from prospect_intel import store
        con = store.connect()
        row = con.execute("SELECT data FROM briefs WHERE id=?", (bid,)).fetchone()
        con.close()
        if row:
            return json.loads(row[0])
    except Exception:
        pass
    raise HTTPException(404, "unknown brief")


@app.get("/")
def root():
    return RedirectResponse("/ui")


@app.get("/ui")
def ui():
    from pathlib import Path
    return HTMLResponse(Path(__file__).parent.joinpath("ui.html").read_text(encoding="utf-8"))


@app.get("/metrics")
def metrics():
    """§10.1 ops metrics: fetch distribution, verifier rejects, briefs."""
    from prospect_intel import metrics as M
    return M.compute()


@app.get("/audit/tail")
def audit_tail(n: int = 20):
    try:
        from prospect_intel import store
        con = store.connect()
        rows = con.execute(
            "SELECT ts, event, payload, hash FROM audit_log ORDER BY seq DESC LIMIT ?",
            (n,)).fetchall()
        con.close()
        return [{"ts": t, "event": e, "payload": json.loads(p), "hash": h}
                for t, e, p, h in rows]
    except Exception as e:
        raise HTTPException(500, str(e))
