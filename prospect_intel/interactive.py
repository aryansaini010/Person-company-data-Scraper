"""Interactive brief: give a NAME, get a spec §6.5 brief.

Run:  python -m prospect_intel.interactive

Flow (§§5-6): name -> tiered discovery (Tier 1/2; Tier 3 refused for
person queries) -> fetch/classify/structure -> Pass-1 candidates WITH
evidence -> YOU select -> Pass 2/3/4 -> verified brief with unknowns[].
"""
from __future__ import annotations
from .acquisition import acquire
from .discovery import discover_person, tier1_direct
from .models import ToolBoundary
from .passes import (build_output_contract, pass1_candidates, pass1_confirm,
                     pass2_firmographic, pass3_strategy, pass4_synthesize)
from .schemas import Brief, PersonIdentity, SourceClass
from .verifier import Verifier


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def main() -> None:
    import os
    import sys
    import time
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("=== Prospect Intelligence - interactive brief (spec v1.0) ===\n")
    try:  # §6.4 per-day quota (0 = off)
        from . import store as _store
        _con = _store.connect()
        _quota = int(os.environ.get("PROSPECT_DAILY_QUOTA", "0"))
        if _quota and _store.briefs_today(_con) >= _quota:
            print(f"Daily brief quota reached ({_quota}). Try tomorrow.")
            _con.close()
            return
        _con.close()
    except Exception:
        pass
    deadline = time.time() + float(os.environ.get("PROSPECT_DEADLINE_S", "600"))
    expired = lambda: time.time() > deadline  # noqa: E731
    incomplete = False
    name = ask("Person's full name: ")
    if not name:
        print("A name is required. Exiting.")
        return
    company = ask("Company (Enter if unknown): ")
    query = ask("Extra query terms (Enter to skip): ")
    raw_urls = ask("Known URLs, space-separated (Enter to skip): ")
    given = [u for u in raw_urls.split() if u.startswith(("http://", "https://"))]

    # Discovery (§5.1): Tier 1 direct URLs first, then person-query tiers.
    degraded: list[str] = []
    hits = []
    if given:
        t1 = tier1_direct(given)
        hits.extend(t1.hits)
    disc = discover_person(name, company, query)
    degraded.extend(disc.degraded)
    ref_hits = list(disc.references)
    for h in disc.hits:
        if h.url not in {x.url for x in hits}:
            hits.append(h)
    urls = [h.url for h in hits][:8]
    print(f"\nDiscovered {len(urls)} candidate page(s).")
    for u in urls:
        print(f"  - {u}")
    if degraded:
        print("\nDegraded tiers (loud, per spec §10.2 - coverage reduced):")
        for d_note in degraded:
            print(f"  ! {d_note[:160]}")

    print("\nFetching person sources + classifying...")
    from .acquisition import acquire, stamp_docs
    docs = acquire([u for u in urls if "wikipedia.org" not in u],
                   SourceClass.NEWS) if urls else []
    # §7.2 compounding: corpus answers before the network does.
    try:
        from . import store as _store2
        _c2 = _store2.connect()
        reused = _store2.corpus_search_docs(
            _c2, f"{name} {company or ''}".strip(), 6)
        _c2.close()
        fresh_ids = {d.doc_id for d in docs}
        reused = [d for d in reused if d.doc_id not in fresh_ids]
        if reused:
            print(f"Corpus hits (prior briefs, no fetch needed): {len(reused)}")
            docs = reused + docs
    except Exception:
        pass
    # Wikipedia edge blocks generic crawlers: pull page text through the
    # MediaWiki API instead (bot-intended path, same §5.4 record shape).
    from .discovery import (tier1_wikipedia_docs,
                               tier1_wikipedia_docs_for_urls)
    wiki_docs, wiki_note = tier1_wikipedia_docs(
        " ".join(x for x in (name, company, query) if x),
        subject=(name, company))
    if wiki_note:
        degraded.append(wiki_note)
    for wd in wiki_docs + tier1_wikipedia_docs_for_urls(urls):
        if wd.doc_id not in {d.doc_id for d in docs}:
            docs.append(wd)
            try:
                from .acquisition import queue_to_core, _MEM_QUEUE
                queue_to_core(wd, _MEM_QUEUE)
            except Exception:
                pass
    # Tier-1 enrichment docs (Wikidata/GLEIF/Companies House: pre-structured, api.py parity).
    if disc.docs:
        _have_ids = {d.doc_id for d in docs}
        for _ed in disc.docs:
            if _ed.doc_id not in _have_ids:
                _have_ids.add(_ed.doc_id)
                docs.append(_ed)
                try:
                    from .acquisition import queue_to_core, _MEM_QUEUE
                    queue_to_core(_ed, _MEM_QUEUE)
                except Exception:
                    pass
    print(f"Fetchable person documents: {len(docs)}")

    # Bare name? Infer likely companies from role-pattern evidence and ask
    # (§6.3: the human confirms the association before anything proceeds).
    if company.strip().lower() in ("", "unknown") and docs:
        from .passes import infer_companies
        for org, conf, ids in infer_companies(name, docs):
            ans = ask(f"\nIs this person related to {org}? "
                      f"(confidence {conf:.2f}, evidence: {', '.join(ids)}) [Y/n]: ")
            if ans.lower() in ("", "y", "yes"):
                company = org
                print(f"Company set to {org} (your confirmation).")
                break
            print(f"Noted - {org} rejected by you.")

    # Pass 1 (§6.3): candidates WITH evidence, human selects. Mandatory.
    cands = pass1_candidates(name, company or "unknown", docs)
    print("\n--- Pass 1: entity candidates (select one) ---")
    for i, c in enumerate(cands):
        print(f"[{i}] {c.full_name} @ {c.company} "
              f"(confidence {c.confidence:.2f})")
        for s in c.sources[:3]:
            print(f"      evidence: {s}")
    sel = ask(f"Select [0-{len(cands) - 1}] (Enter = 0, q to abort): ")
    if sel.lower() in ("q", "quit", "abort"):
        print("Aborted - no brief on unconfirmed identity.")
        return
    if sel == "":
        sel = "0"
    if not sel.isdigit() or not (0 <= int(sel) < len(cands)):
        print("Invalid selection - stopping. No brief on unconfirmed identity.")
        return
    person = pass1_confirm(PersonIdentity(
        full_name=cands[int(sel)].full_name, company=cands[int(sel)].company,
        title=cands[int(sel)].title, confidence=cands[int(sel)].confidence,
        sources=cands[int(sel)].sources, human_confirmed=True))

    # Company diet (§5.1/§6.3): recent press (strategy angle + hiring angle),
    # newsroom/careers/investor pages. This is what answers "what are they
    # trying to do this year" and feeds the pitch.
    co = person.company
    if expired():
        degraded.append("incomplete: wall-clock budget exceeded (§6.4)")
        incomplete = True
    from .segment import active as _seg
    from .segment import default_collateral
    seg = _seg()
    print(f"\nBuyer segment: {seg['label']}")
    if co.strip().lower() not in ("", "unknown") and not incomplete:
        from .discovery import company_diet
        from .passes import probe_company_pages
        print("\nFetching company diet (press + hiring + owned pages)...")
        company_diet(co, name, docs, degraded,
                     fetch_full=True, max_angles=4, deadline=deadline)
        try:
            owned = probe_company_pages(co, deadline=deadline)
        except Exception:
            owned = {}
        for role, url in owned.items():
            if url in {d.url_final or d.url for d in docs}:
                continue
            sc = (SourceClass.JOB_POSTING if role == "careers"
                  else SourceClass.PRESS_RELEASE if role == "newsroom"
                  else SourceClass.VENDOR_PAGE)
            docs.extend(acquire([url], sc))
        print(f"Company documents added; total fetchable: {len(docs)}")

    tools = ToolBoundary()
    incomplete = expired()  # §6.4: wall-clock breach → partial, marked
    if incomplete:
        degraded.append("incomplete: wall-clock budget exceeded (§6.4)")
    firmo = pass2_firmographic(person.company, {},
                                extra=disc.firmo_extra) if not incomplete else {
        "company": person.company, "registry": "unknown", "filings": [],
        "funding": "unknown"}
    if not incomplete:
        # Hunter-by-domain (free key, quota-guarded, api.py parity).
        _site = firmo.get("website_probe", "none") or "none"
        if _site == "none":
            _site = firmo.get("homepage", "") or "none"
        if _site != "none":
            try:
                from urllib.parse import urlparse as _up
                from . import enrich as _enrich
                from .acquisition import queue_to_core, _MEM_QUEUE
                from .passes import merge_firmo_extra
                _domain = (_up(_site).hostname or "").lower().removeprefix("www.")
                if _domain and "." in _domain:
                    _hdocs, _hfirmo, _hnotes = _enrich.enrich_company_by_domain(
                        _domain, person.company)
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
                pass
    from .passes import firmographic_unknowns  # §6.5 item 4 + §5.1 alerting
    from .passes import company_owned_hosts
    _fgaps, degraded = firmographic_unknowns(firmo, degraded)
    verified, gaps = ([], ["incomplete: wall-clock budget exceeded (§6.4)"]) \
        if incomplete else pass3_strategy(docs, tools, Verifier(),
                                          person.full_name, person.company,
                                          company_owned_hosts(firmo))
    gaps = _fgaps + gaps  # per-field unknowns first (§6.5 item 4)
    from .passes import extract_bio, pass_person_details
    person_details = [] if incomplete else pass_person_details(
        docs, Verifier(), person.full_name)
    bio = {} if incomplete else extract_bio(person.full_name, docs)
    brief = pass4_synthesize(Brief(person=person, firmographic=firmo,
                                   strategy_signals=verified, gaps=gaps,
                                   person_details=person_details, bio=bio,
                                   degraded=degraded),
                             default_collateral(), tools)
    brief = build_output_contract(brief, default_collateral(), tools)
    from .passes import build_profile_card  # Google-style card, §8.7-safe
    _roles, _refs = build_profile_card(person.full_name, docs, ref_hits, firmo)
    brief.current_roles = _roles
    brief.references = _refs
    if brief.degraded and not brief.gaps:
        # §10.2: degraded inputs mean coverage may be missing - say so.
        brief.gaps.append("coverage reduced by degraded tiers (see below)")

    print("\n" + "=" * 64)
    print(f"PROSPECT BRIEF: {brief.person.full_name} @ {brief.person.company}")
    print("=" * 64)
    print("\n[Q1 - WHO IS THIS PERSON? (human-confirmed, never automated)]")
    print(f"  Name: {brief.person.full_name}")
    print(f"  Company: {brief.person.company}")
    if brief.person.role or brief.person.title:
        print(f"  Role: {brief.person.role or brief.person.title}")
    print(f"  Confidence: {brief.person.confidence:.2f}")
    _uniq_src = list(dict.fromkeys(brief.person.sources))[:5]
    print(f"  Evidence: {', '.join(_uniq_src)}")
    if brief.current_roles:
        print("\n[PROFILE - current associations (evidence-bound)]")
        for r in brief.current_roles:
            print(f"  - {r.role + ' @ ' if r.role else ''}{r.company}")
            print(f"    evidence: {', '.join(r.evidence[:3])}")
    if brief.references:
        print("\n[REFERENCES - open manually]")
        for ref in brief.references[:6]:
            print(f"  - {ref.label}: {ref.url} ({ref.note})")
    print("\n[Q2 - WHAT DOES THEIR COMPANY LOOK LIKE? (deterministic lookups)]")
    for k, v in brief.firmographic.items():
        print(f"  {k}: {str(v)[:220]}")
    print("\n[Q3 - WHAT IS THEIR COMPANY TRYING TO DO THIS YEAR? (verified strategy)]")
    if brief.priorities:
        for p in brief.priorities:
            print(f"  - {p.statement[:220]}")
            print(f"    evidence={p.evidence} recency={p.recency} "
                  f"confidence={p.confidence:.2f}")
    else:
        print("  (none - see unknowns)")
    print("\n[Q4 - WHAT SHOULD WE PITCH THEM? (our collateral only, no internet)]")
    for m in brief.capability_map:
        print(f"  - ours: {m.our_capability[:120]}")
        print(f"    theirs: {m.their_priority_ref} | {m.rationale[:120]}")
    if not brief.capability_map:
        print("  (none)")
    print(f"\n[Pitch] {brief.pitch[:600]}")
    if brief.likely_objection.statement:
        print(f"\n[Likely objection] {brief.likely_objection.statement[:300]}")
        print(f"  basis: {brief.likely_objection.basis[:200]}")
    if brief.contradictions:
        print("\n[Conflicting sources - both shown, neither selected (§7.5)]")
        for c in brief.contradictions[:5]:
            print(f"  A ({c.recency_a}): {c.claim_a[:160]}")
            print(f"  B ({c.recency_b}): {c.claim_b[:160]}")
    if brief.opening_question:
        print(f"[Opening question] {brief.opening_question[:300]}")
    print("\n[Unknowns - required, never invented]")
    for g in brief.gaps or ["(none - full signal)"]:
        print(f"  - {g}")
    if brief.degraded:
        print("\n[Degraded inputs - read brief accordingly]")
        for d_note in brief.degraded:
            print(f"  ! {d_note[:200]}")

    from .export import brief_csv_path, export_brief, export_readable
    csv_path = export_brief(brief, {d.doc_id: d for d in docs},
                            brief_csv_path(f"{brief.person.full_name}-{brief.person.company}"))
    txt_path = csv_path.with_suffix(".txt")
    export_readable(brief, {d.doc_id: d for d in docs}, txt_path)
    print(f"\nSaved CSV: {csv_path}")
    print(f"Saved readable report: {txt_path}")


if __name__ == "__main__":
    main()
