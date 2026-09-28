"""API: briefs + Pass-1 human gate + live /research (URLs -> full pipeline)."""
from __future__ import annotations
import json
import time
import uuid
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
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
uploads: dict = {}  # upload_id -> {filename, rows, created_at, expires_at}
UPLOAD_TTL_S = 3600.0  # participant lists live 1h
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


def _upload_sweep() -> None:
    now = time.time()
    for uid in [u for u, v in uploads.items()
                if now > v.get("expires_at", now)]:
        uploads.pop(uid, None)
    try:
        from prospect_intel import store as _store
        _con = _store.connect()
        try:
            _store.upload_sweep(_con, now)
        finally:
            _con.close()
    except Exception:
        pass


def _upload_put(uid: str, filename: str, rows: list[dict]) -> None:
    now = time.time()
    uploads[uid] = {"filename": filename, "rows": rows,
                    "created_at": now, "expires_at": now + UPLOAD_TTL_S}
    if len(uploads) > 100:
        try:
            oldest = min(uploads.items(),
                         key=lambda kv: kv[1].get("created_at", 0))[0]
            uploads.pop(oldest, None)
        except Exception:
            pass
    try:
        from prospect_intel import store as _store
        _con = _store.connect()
        try:
            _store.upload_save(_con, uid, filename, rows, UPLOAD_TTL_S)
        finally:
            _con.close()
    except Exception:
        pass


def _upload_get(uid: str) -> tuple[str, list[dict]] | None:
    ent = uploads.get(uid)
    if ent is not None:
        if time.time() > ent.get("expires_at", 0):
            uploads.pop(uid, None)
        else:
            return ent.get("filename", ""), ent.get("rows", [])
    try:
        from prospect_intel import store as _store
        _con = _store.connect()
        try:
            got = _store.upload_load(_con, uid)
        finally:
            _con.close()
        if got is not None:
            filename, rows = got
            uploads[uid] = {"filename": filename, "rows": rows,
                            "created_at": time.time(),
                            "expires_at": time.time() + UPLOAD_TTL_S}
            return filename, rows
        return None
    except Exception:
        return None


def _sanitize_user_supplied(raw: dict | None) -> dict:
    """Allow-list participant fields; Phone/Mobile never pass through.

    Keeps: position, company_raw, corp_email, email, company_phone,
    country, participant_type, activity, full_name, display, pid.
    Drops: phone, mobile (any casing) + unknown junk over 500 chars.
    """
    if not isinstance(raw, dict):
        return {}
    keep = ("position", "company_raw", "company_primary", "corp_email",
            "email", "company_phone", "country", "participant_type",
            "activity", "full_name", "display", "pid",
            "first_name", "last_name")
    out: dict = {}
    for k in keep:
        v = raw.get(k, "")
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()[:500]
    return out


def _apply_user_supplied(person, firmo: dict, degraded: list,
                         user_supplied: dict | None) -> tuple:
    """Bind participant row OUTSIDE the verifier (ground-truth, not evidence).

    - Position -> person.title/role when empty (DB column exists).
    - Official contacts -> firmo supplied_* keys (never clobbers verified).
    - All labeled via degraded notes as user-supplied (unverified).
    Returns (person, firmo). Mutates degraded in place.
    """
    us = _sanitize_user_supplied(user_supplied)
    if not us:
        return person, firmo
    try:
        pos = us.get("position", "")
        if pos and not (getattr(person, "title", "") or getattr(person, "role", "")):
            person.title = pos[:200]
            person.role = pos[:200]
            note = "position: user-supplied (unverified — from participant file)"
            if note not in degraded:
                degraded.append(note)
    except Exception:
        pass
    try:
        supplied: dict = {}
        if us.get("corp_email"):
            supplied["corp_email"] = us["corp_email"]
        if us.get("email"):
            supplied["email"] = us["email"]
        if us.get("company_phone"):
            supplied["company_phone"] = us["company_phone"]
        if supplied:
            firmo["contact_supplied"] = supplied
            note = ("contact: user-supplied official channels (unverified — "
                    "from participant file, Phone/Mobile excluded)")
            if note not in degraded:
                degraded.append(note)
        if us.get("country"):
            if not firmo.get("country_supplied"):
                firmo["country_supplied"] = us["country"]
        ctx = {k: us[k] for k in ("participant_type", "activity")
               if us.get(k)}
        if ctx:
            firmo["participant_supplied"] = ctx
        # Keep full row for export/UI (no Phone/Mobile — never in us).
        firmo["row_supplied"] = {k: v for k, v in us.items()
                                 if k in ("full_name", "display", "position",
                                          "company_raw", "company_primary")}
    except Exception:
        pass
    return person, firmo

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
               deadline: float | None = None,
               user_supplied: dict | None = None) -> tuple[str, Brief]:
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
    # Company-depth fallback (authentic, bounded, 2-min budget):
    # When the person is obscure, the person-biased discovery above yields
    # ~0 docs. Company info still lives on the official site + news, so do
    # a company-only refresh (Tier-3 legal for company-only queries):
    # full diet bodies + owned newsroom/careers/investors acquisition.
    # Skipped when <45s remain; caps +6 docs; never invents (verifier gates).
    # Disabled under pytest for speed/determinism (unit tests stub tiers).
    try:
        import sys as _sys2
        _under_test = ("PYTEST_CURRENT_TEST" in _os.environ
                       or "pytest" in _sys2.modules)
        _co = (person.company or "").strip()
        _need_depth = (
            not _under_test
            and _co.lower() not in ("", "unknown")
            and (firmo.get("website_probe", "none") in ("none", "", None)
                 or firmo.get("homepage", "") in ("", "none")
                 or firmo.get("funding", "unknown") == "unknown")
            and not _dl.expired(deadline)
            and (deadline - time.time() > 45 if deadline else True)
        )
        if _need_depth:
            from prospect_intel.discovery import company_diet as _cdiet
            from prospect_intel.passes import probe_company_pages as _probe2
            from prospect_intel.acquisition import acquire as _acq2
            from prospect_intel.schemas import SourceClass as _SC2
            import concurrent.futures as _cf2
            _owned2: dict = {}
            try:
                _ex2 = _cf2.ThreadPoolExecutor(max_workers=1)
                try:
                    _owned2 = _ex2.submit(_probe2, _co, deadline).result(timeout=20) or {}
                finally:
                    _ex2.shutdown(wait=False, cancel_futures=True)
            except Exception:
                _owned2 = {}
            if _owned2:
                try:
                    _have_urls = {d.url_final or d.url for d in docs}
                    for _role, _url in list(_owned2.items())[:3]:
                        if _url in _have_urls or _dl.expired(deadline):
                            continue
                        _sc = (_SC2.JOB_POSTING if _role == "careers"
                               else _SC2.PRESS_RELEASE if _role == "newsroom"
                               else _SC2.VENDOR_PAGE)
                        try:
                            for _dd in _acq2([_url], _sc, deadline=deadline):
                                if _dd.doc_id not in {d.doc_id for d in docs}:
                                    docs.append(_dd)
                                    if len(docs) >= 26:
                                        break
                        except Exception:
                            continue
                    _first2 = next(iter(_owned2.values()))
                    if firmo.get("website_probe", "none") in ("none", "", None):
                        firmo["website_probe"] = _first2
                        firmo["probe_status"] = "owned-page"
                        firmo["sources"] = list(dict.fromkeys(
                            firmo.get("sources", []) + [_first2]))[:5]
                except Exception:
                    pass
            try:
                _before = len(docs)
                _cdiet(_co, "", docs, degraded, fetch_full=True,
                       max_angles=2, deadline=deadline)
                # Cap diet growth so a huge company can't blow the 120s budget.
                if len(docs) > _before + 6:
                    del docs[_before + 6:]
            except Exception:
                pass
    except Exception:
        pass  # fallback best-effort: brief proceeds with what we have
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
    # Participant file (user-supplied ground truth, never verifier evidence).
    # Position -> person.title/role; official contacts -> firmo supplied_*.
    # Phone/Mobile never arrive here (_sanitize drops them).
    try:
        person, firmo = _apply_user_supplied(person, firmo, degraded,
                                             user_supplied)
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
    # Company-scope second pass (authentic): if the person has no footprint
    # but the company-depth refresh above found official/news docs, surface
    # company priorities instead of an empty Q3. Company-only query, so
    # verifier + company-bound gates still apply — never invented.
    if not verified and not _strong and docs and not _dl.expired(deadline):
        try:
            _cver, _cgaps = pass3_strategy(
                docs, tools, verifier, "", person.company,
                company_owned_hosts(firmo))
            if _cver:
                verified = _cver
                # Keep company gaps that add information (dedupe).
                for _g in _cgaps:
                    if _g not in gaps:
                        gaps.append(_g)
                note = ("company-depth: person has no public footprint — "
                        "showing verified company direction from official "
                        "site + news")
                if note not in degraded:
                    degraded.append(note)
        except Exception:
            pass
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
    # General-knowledge fallback (UNVERIFIED, never evidence): only when zero
    # verified company data exists after all fallbacks. Local model through
    # Open Web UI chat (gpt-oss:20b), backup direct Ollama same weights.
    general: list[dict] = []
    try:
        _no_firmo = (
            firmo.get("website_probe", "none") in ("none", "", None)
            and firmo.get("homepage", "") in ("", "none")
            and firmo.get("registry", "") in ("", "unknown",
                                              "unverified-manual-check")
        )
        if (not verified and not docs and _no_firmo
                and not _dl.expired(deadline)):
            from prospect_intel.general import get_general_background
            general, _ = get_general_background(person.company, deadline)
            if general:
                note = ("general-knowledge fallback shown separately "
                        "(unverified — local model via Open Web UI)")
                if note not in degraded:
                    degraded.append(note)
    except Exception:
        general = []
    brief = Brief(person=person, firmographic=firmo,
                  strategy_signals=verified, gaps=gaps,
                  person_details=person_details, bio=bio,
                  degraded=degraded or [], general_knowledge=general)
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
    user_supplied: dict = Field(default_factory=dict)


class ResearchRequest(BaseModel):
    name: str
    company: str
    urls: list[str] = []
    query: str = ""
    max_results: int = 5
    source_class: SourceClass = SourceClass.OTHER
    collateral: list[str] = []
    user_supplied: dict = Field(default_factory=dict)


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
                            req.collateral or _collateral(),
                            user_supplied=req.user_supplied)
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
                                deadline,
                                _sanitize_user_supplied(req.user_supplied))
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
                "unknowns": _unknowns_flat(brief),
                "general_knowledge": _general_flat(brief)}
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
                     "user_supplied": _sanitize_user_supplied(req.user_supplied),
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
                            sess.get("firmo_extra"), _cdl.start(),
                            sess.get("user_supplied"))
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
            "general_knowledge": _general_flat(brief),
            **_auto_push_brief(bid, brief)}


class CompanyRequest(BaseModel):
    company: str
    max_results: int = 8
    collateral: list[str] = []


@app.post("/participants/upload")
def participants_upload(file: UploadFile = File(...)):
    """Upload a participant list (.csv/.xlsx, multi-row).

    Returns upload_id + people preview [{pid, display, full_name,
    company_primary}]. Phone/Mobile are dropped on parse and never stored.
    Best-effort partial: bad rows skipped with notes, good rows kept.
    """
    import uuid as _uuid
    from prospect_intel.participants import MAX_BYTES, parse_upload
    _upload_sweep()
    fname = (file.filename or "").strip()[:200] or "upload"
    lname = fname.lower()
    if not (lname.endswith(".csv") or lname.endswith((".xlsx", ".xlsm"))):
        raise HTTPException(400, "unsupported extension (use .csv or .xlsx)")
    try:
        data = file.file.read()
    except Exception:
        raise HTTPException(400, "unreadable upload")
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(400, f"file too large (>{MAX_BYTES} bytes)")
    rows, notes = parse_upload(fname, data)
    if not rows:
        raise HTTPException(400, "; ".join(notes) or "no data rows found")
    uid = f"upl_{_uuid.uuid4().hex[:12]}"
    _upload_put(uid, fname, rows)
    try:
        audit.log("participants_uploaded",
                  {"id": uid, "file": fname, "rows": len(rows)})
    except Exception:
        pass
    people = [{"pid": r.get("pid", ""), "display": r.get("display", ""),
               "full_name": r.get("full_name", ""),
               "company_primary": r.get("company_primary", "")}
              for r in rows]
    return {"upload_id": uid, "filename": fname, "count": len(rows),
            "people": people, "notes": notes}


@app.get("/participants/{upload_id}")
def participants_list(upload_id: str):
    """List people in an upload for the Name+Company selector."""
    _upload_sweep()
    got = _upload_get(upload_id)
    if got is None:
        raise HTTPException(404, "unknown/expired upload — re-upload")
    filename, rows = got
    people = [{"pid": r.get("pid", ""), "display": r.get("display", ""),
               "full_name": r.get("full_name", ""),
               "company_primary": r.get("company_primary", "")}
              for r in rows]
    return {"upload_id": upload_id, "filename": filename,
            "count": len(rows), "people": people}


@app.get("/participants/{upload_id}/{pid}")
def participant_get(upload_id: str, pid: str):
    """Return one row's usable fields (Phone/Mobile never present)."""
    got = _upload_get(upload_id)
    if got is None:
        raise HTTPException(404, "unknown/expired upload — re-upload")
    _, rows = got
    for r in rows:
        if r.get("pid") == pid:
            # Explicit allow-list: Phone/Mobile can never leak even if
            # a future parser keeps them.
            safe = {k: r.get(k, "") for k in (
                "pid", "full_name", "display", "first_name", "last_name",
                "position", "company_raw", "company_primary", "corp_email",
                "email", "company_phone", "country", "participant_type",
                "activity")}
            return {"upload_id": upload_id, "person": safe}
    raise HTTPException(404, "unknown person in this upload")


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
    # General-knowledge fallback for company-only zero-data case too.
    try:
        _no_firmo_c = (
            firmo.get("website_probe", "none") in ("none", "", None)
            and firmo.get("homepage", "") in ("", "none")
            and firmo.get("registry", "") in ("", "unknown",
                                              "unverified-manual-check")
        )
        if (not verified and not docs and _no_firmo_c
                and not _odl.expired(deadline)):
            from prospect_intel.general import get_general_background
            _gen_c, _ = get_general_background(company, deadline)
            if _gen_c:
                brief.general_knowledge = _gen_c
                note_c = ("general-knowledge fallback shown separately "
                          "(unverified — local model via Open Web UI)")
                if note_c not in degraded:
                    degraded.append(note_c)
                    brief.degraded = degraded
    except Exception:
        pass
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
            "general_knowledge": _general_flat(brief),
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


def _general_flat(brief: Brief) -> list[dict]:
    """Unverified general background: separate lane, never evidence."""
    out = []
    for g in (getattr(brief, "general_knowledge", None) or []):
        if isinstance(g, dict) and (g.get("text") or "").strip():
            out.append({"text": g["text"][:500],
                        "label": g.get("label", "GENERAL-KNOWLEDGE-UNVERIFIED"),
                        "model": g.get("model", ""),
                        "via": g.get("via", "")})
    return out


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
