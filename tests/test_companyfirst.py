"""Company-first briefing: company-only discovery + brief, thin-person fallback."""
from fastapi.testclient import TestClient

import api
from prospect_intel.schemas import DocSection, FetchStatus, SourceClass, StructuredDoc


def _doc(doc_id, title, text, url="https://example.com/x"):
    return StructuredDoc(doc_id=doc_id, url=url, url_final=url,
                         content_hash="h", fetched_at="2026-01-01T00:00:00Z",
                         fetch_status=FetchStatus.OK,
                         source_class=SourceClass.OTHER, title=title,
                         sections=[DocSection(section_id=doc_id + "#s0",
                                              text=text, char_start=0,
                                              char_end=len(text))])


def _offline_pass2(monkeypatch, tmp_path):
    from prospect_intel import store
    import prospect_intel.passes as P
    from prospect_intel import discovery
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "p.db")
    # Offline determinism: no live planner fan-out or GNews quota burn.
    monkeypatch.setenv("PLANNER_ENABLE", "")
    monkeypatch.setattr(discovery, "tier1_gnews", lambda q, n=8:
                        discovery.TierResult(tier="tier1_gnews"))
    monkeypatch.setattr(P, "_probe_company_website",
                        lambda c: {"company": c, "registry": "unknown",
                                   "filings": [], "funding": "unknown",
                                   "website_probe": "none"})
    monkeypatch.setattr(P, "probe_company_pages", lambda c: {})
    monkeypatch.setattr(discovery, "dbpedia_company", lambda c: ({}, None))
    monkeypatch.setattr(P, "probe_company_pages", lambda c: {})


def test_discover_company_allows_tier3(monkeypatch, tmp_path):
    from prospect_intel import discovery
    from prospect_intel.search import SearchHit
    from prospect_intel import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "e.db")
    monkeypatch.setattr(discovery, "tier1_news_api", lambda q, n=8:
                        discovery.TierResult(tier="tier1_news"))
    monkeypatch.setattr(discovery, "tier2_searxng", lambda q, n=8:
                        discovery.TierResult(tier="tier2_searxng",
                                             available=False, note="off"))
    monkeypatch.setattr(discovery, "tier1_wikipedia", lambda q, n=4:
                        discovery.TierResult(tier="tier1_wiki"))
    # Predates the GNews tier: keep deterministic (offline) like other tiers.
    monkeypatch.setattr(discovery, "tier1_gnews", lambda q, n=8:
                        discovery.TierResult(tier="tier1_gnews"))
    monkeypatch.setattr(discovery, "tier3_commercial",
                        lambda q, n=8, person_query=False: discovery.TierResult(
                            tier="tier3",
                            hits=[SearchHit(url="https://t/1",
                                            title="Acme Corp opens plant"),
                                  SearchHit(url="https://t/2",
                                            title="Unrelated sports")])
                        if not person_query
                        else discovery.TierResult(tier="tier3",
                                                  available=False, note="no"))
    import prospect_intel.enrich as E
    monkeypatch.setattr(E, "enrich_company", lambda c: ([], {}, []))
    import prospect_intel.resolvers as R
    monkeypatch.setattr(R, "get_ground_truth",
                        lambda q, deadline=None: ({}, [], [], False, q, "unknown"))
    d = discovery.discover_company("Acme Corp")
    assert "tier3" in d.tiers_used
    assert [h.url for h in d.hits] == ["https://t/1"]  # gated, tier3 allowed
    assert d.firmo_extra == {}


def test_company_research_brief_no_person(monkeypatch, tmp_path):
    from prospect_intel import discovery
    _offline_pass2(monkeypatch, tmp_path)
    doc = _doc("doc_c1", "Acme Corp",
               "Acme Corp will expand platform engineering hiring in Berlin "
               "to support enterprise growth in 2026.")
    disc = discovery.Discovery(
        hits=[], degraded=[], tiers_used=["tier1_enrich"], references=[],
        docs=[doc], firmo_extra={"industry": "Software"})
    monkeypatch.setattr(api, "discover_company", lambda c, n=8, **k: disc)
    monkeypatch.setattr(api, "acquire", lambda urls, sc=None, **k: [])
    monkeypatch.setattr(discovery, "tier1_wikipedia_docs",
                        lambda q, subject=None: ([], None))
    monkeypatch.setattr(discovery, "tier1_wikipedia_docs_for_urls",
                        lambda urls: [])
    monkeypatch.setattr(discovery, "tier1_google_news_rss",
                        lambda q, n=4: ([], {}))
    c = TestClient(api.app)
    r = c.post("/company-research", json={"company": "Acme Corp"})
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "brief"
    b = d["brief"]
    assert b["person"]["full_name"] == ""  # no person invented
    assert b["person"]["company"] == "Acme Corp"
    assert b["firmographic"]["industry"] == "Software"
    assert any("person.unknown" in g for g in b["gaps"])
    assert b["person_details"] == [] and b["bio"] == {}


def test_company_research_requires_company():
    c = TestClient(api.app)
    assert c.post("/company-research",
                  json={"company": "  "}).status_code == 400


def test_person_strength_gates():
    from prospect_intel.passes import person_evidence_strength
    from prospect_intel.passes import pass1_candidates
    docs = [_doc("d1", "Ada Lovelace",
                 "Ada Lovelace is the CTO of Acme Corp."),
            _doc("d2", "Ada Lovelace",
                 "Ada Lovelace joined Acme Corp in 2024.")]
    cands = pass1_candidates("Ada Lovelace", "Acme Corp", docs)
    strong, note = person_evidence_strength("Ada Lovelace", "Acme Corp",
                                            docs, cands)
    assert strong and note == ""
    thin_docs = [_doc("d9", "Weather", "Rain in Berlin today, mild and calm.")]
    cands2 = pass1_candidates("Zzx Nobody", "UnknownCo", thin_docs)
    strong2, note2 = person_evidence_strength("Zzx Nobody", "UnknownCo",
                                              thin_docs, cands2)
    assert not strong2 and "thin" in note2


def test_run_brief_thin_person_stays_unknown(monkeypatch, tmp_path):
    _offline_pass2(monkeypatch, tmp_path)
    thin_docs = [_doc("d9", "Weather", "Rain in Berlin today, mild and calm.")]
    bid, brief = api._run_brief("Zzx Nobody", "UnknownCo", thin_docs, [],
                                ["note"])
    assert any("thin" in g for g in brief.degraded)
    assert brief.person_details == [] and brief.bio == {}
    assert any("person.role" in g for g in brief.gaps)
    assert bid.startswith("brief_")


def test_run_brief_strong_person_keeps_bio(monkeypatch, tmp_path):
    _offline_pass2(monkeypatch, tmp_path)
    docs = [_doc("d1", "Ada Lovelace",
                 "Ada Lovelace graduated from Some College in 2020. "
                 "Ada Lovelace is the CTO of Acme Corp."),
            _doc("d2", "Ada Lovelace",
                 "Ada Lovelace joined Acme Corp in 2024 to lead engineering.")]
    _, brief = api._run_brief("Ada Lovelace", "Acme Corp", docs, [], [])
    assert not any("thin" in g for g in brief.degraded)
    assert brief.bio.get("education")  # bio extracted, not blanked
