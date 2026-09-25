"""Agent surface: readable.txt, openwebui push, evidence/unknowns blocks."""
from fastapi.testclient import TestClient

import api


def _brief_id(monkeypatch, tmp_path):
    from prospect_intel import store
    import prospect_intel.passes as P
    from prospect_intel import discovery
    import api as _api
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "a.db")
    monkeypatch.setattr(P, "_probe_company_website",
                        lambda c: {"company": c, "registry": "unknown",
                                   "filings": [], "funding": "unknown",
                                   "website_probe": "none"})
    monkeypatch.setattr(P, "probe_company_pages",
                        lambda c, deadline=None: {})
    monkeypatch.setattr(discovery, "dbpedia_company", lambda c: ({}, None))
    monkeypatch.setattr(discovery, "tier1_google_news_rss",
                        lambda q, n=4: ([], {}))
    # P0: isolate from live network (GLEIF/Wikidata/Wikipedia/SearXNG/DuckDuckGo).
    # company-research is an agent-surface test, not an integration test.
    monkeypatch.setenv("PLANNER_ENABLE", "")
    monkeypatch.setattr(discovery, "tier1_gnews", lambda q, n=8:
                        discovery.TierResult(tier="tier1_gnews"))
    from prospect_intel.discovery import Discovery
    monkeypatch.setattr(_api, "discover_company",
                        lambda company, max_results=8, deadline=None: Discovery())
    monkeypatch.setattr(_api, "acquire", lambda urls, *a, **k: [])
    monkeypatch.setattr(discovery, "tier1_wikipedia_docs",
                        lambda q, max_results=4, subject=None: ([], None))
    monkeypatch.setattr(discovery, "tier1_wikipedia_docs_for_urls",
                        lambda urls: [])
    monkeypatch.setattr(discovery, "company_diet",
                        lambda *a, **k: [])
    monkeypatch.setattr(_api, "_hunter_enrich",
                        lambda firmo, company, docs, degraded: firmo)
    c = TestClient(api.app)
    r = c.post("/company-research", json={"company": "Acme Corp"})
    assert r.status_code == 200
    d = r.json()
    assert "evidence" in d and "unknowns" in d  # agent joins, no hopping
    assert "doc_urls" in d
    return c, d["id"]


def test_readable_txt(monkeypatch, tmp_path):
    c, bid = _brief_id(monkeypatch, tmp_path)
    r = c.get(f"/briefs/{bid}/readable.txt")
    assert r.status_code == 200
    assert "BRIEF" in r.text and "Acme Corp" in r.text
    assert c.get("/briefs/nope/readable.txt").status_code == 404


def test_push_without_key_502(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENWEBUI_API_KEY", raising=False)
    c, bid = _brief_id(monkeypatch, tmp_path)
    r = c.post("/openwebui/push", json={"brief_id": bid})
    assert r.status_code == 502  # loud, brief unaffected
    assert "no OPENWEBUI_API_KEY" in r.json()["detail"]
    assert c.post("/openwebui/push",
                  json={"brief_id": "nope"}).status_code == 404


def test_entity_resolve_json_body():
    c = TestClient(api.app)
    r = c.post("/entity-resolve",
               json={"name": "Jane Doe", "company": "Acme"})
    assert r.status_code == 200
    assert r.json()[0]["full_name"] == "Jane Doe"


def test_briefs_path_has_profile_card(monkeypatch, tmp_path):
    from prospect_intel import store
    import prospect_intel.passes as P
    from prospect_intel import discovery
    import prospect_intel.resolvers as R
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "b.db")
    monkeypatch.setattr(P, "_probe_company_website",
                        lambda c: {"company": c, "registry": "unknown",
                                   "filings": [], "funding": "unknown",
                                   "website_probe": "none"})
    monkeypatch.setattr(P, "probe_company_pages",
                        lambda c, deadline=None: {})
    monkeypatch.setattr(discovery, "dbpedia_company", lambda c: ({}, None))
    monkeypatch.setattr(R, "get_ground_truth",
                        lambda q, deadline=None: ({}, [], [], False, q, "unknown"))
    c = TestClient(api.app)
    r = c.post("/briefs", json={"name": "Elon Musk", "company": "Tesla",
                                "docs": [], "collateral": []})
    assert r.status_code == 200
    b = r.json()["brief"]
    assert "current_roles" in b and "references" in b
