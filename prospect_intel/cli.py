"""CLI: python -m prospect_intel.cli --name 'Jane Doe' --company Acme --urls https://..."""
from __future__ import annotations
import argparse
import json

from .acquisition import acquire
from .models import ToolBoundary
from .passes import (build_output_contract, extract_bio, firmographic_unknowns,
                     merge_firmo_extra, pass1_candidates, pass1_confirm,
                     pass2_firmographic, pass3_strategy, pass4_synthesize,
                     pass_person_details)
from .schemas import PersonIdentity, SourceClass
from .verifier import Verifier


def main() -> None:
    ap = argparse.ArgumentParser(description="Prospect intelligence brief")
    ap.add_argument("--name", required=True)
    ap.add_argument("--company", required=True)
    ap.add_argument("--urls", nargs="*", default=[])
    ap.add_argument("--query", default="")
    ap.add_argument("--max-results", type=int, default=5)
    ap.add_argument("--source-class", default="other",
                    choices=[s.value for s in SourceClass])
    ap.add_argument("--collateral", nargs="*", default=[])
    ap.add_argument("--upload-knowledge", action="store_true",
                    help="push the readable report to Open WebUI Files "
                         "(needs OPENWEBUI_API_KEY in .env)")
    args = ap.parse_args()

    urls = list(args.urls)
    if not urls and args.query:
        from .search import SearxngProvider
        urls = [h.url for h in SearxngProvider().search(args.query,
                                                        args.max_results)]
    docs = acquire(urls[: args.max_results],
                   SourceClass(args.source_class)) if urls else []
    tools, verifier = ToolBoundary(), Verifier()
    # Ranked candidates first (api parity); the typed name+company counts as
    # the human confirmation of the top match.
    cands = pass1_candidates(args.name, args.company or "unknown", docs)
    top = cands[0] if cands else PersonIdentity(full_name=args.name,
                                                company=args.company or "unknown")
    person = pass1_confirm(PersonIdentity(
        full_name=top.full_name, company=top.company, title=top.title,
        confidence=top.confidence, sources=top.sources, human_confirmed=True))
    try:
        from . import enrich as _enrich
        _, _extra, _ = _enrich.enrich_company(person.company)
    except Exception:
        _extra = {}
    firmo = pass2_firmographic(person.company, {}, extra=_extra)
    try:
        from . import enrich as _enrich2
        from urllib.parse import urlparse as _up
        _site = firmo.get("website_probe", "none") or "none"
        if _site == "none":
            _site = firmo.get("homepage", "") or "none"
        _domain = (_up(_site).hostname or "").lower().removeprefix("www.")
        if _domain and "." in _domain:
            _hdocs, _hfirmo, _ = _enrich2.enrich_company_by_domain(
                _domain, person.company)
            _have = {d.doc_id for d in docs}
            for _hd in _hdocs:
                if _hd.doc_id not in _have:
                    docs.append(_hd)
            firmo = merge_firmo_extra(firmo, _hfirmo)
    except Exception:
        pass
    _fgaps, _degraded = firmographic_unknowns(firmo, [])
    from prospect_intel.passes import company_owned_hosts
    verified, gaps = pass3_strategy(docs, tools, verifier, person.full_name,
                                    person.company,
                                    company_owned_hosts(firmo))
    gaps = _fgaps + gaps
    from .schemas import Brief
    brief = Brief(person=person, firmographic=firmo,
                  strategy_signals=verified, gaps=gaps,
                  person_details=pass_person_details(
                      docs, verifier, person.full_name),
                  bio={}, degraded=_degraded)
    try:
        brief.bio = extract_bio(person.full_name, docs)
    except Exception:
        pass
    brief = pass4_synthesize(brief, args.collateral or
                             ["platform scaling case study"], tools)
    brief = build_output_contract(brief, args.collateral or
                                  ["platform scaling case study"], tools)
    from .passes import build_profile_card
    _roles, _refs = build_profile_card(person.full_name, docs, [], firmo)
    brief.current_roles = _roles
    brief.references = _refs
    print(json.dumps({"fetched": len(docs), "brief": brief.model_dump()},
                     indent=2))
    if args.upload_knowledge:
        from .export import brief_csv_path, export_readable as _er
        from . import openwebui as _ow
        _tp = brief_csv_path(
            f"{person.full_name}-{person.company}").with_suffix(".txt")
        _er(brief, {d.doc_id: d for d in docs}, _tp)
        _fid, _note = _ow.upload_brief_file(str(_tp))
        print(f"Knowledge upload: {_fid or _note}")


if __name__ == "__main__":
    main()
