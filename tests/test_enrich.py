"""Tier-1 enrichment: Wikidata / GLEIF / Hunter. All HTTP mocked."""
from prospect_intel import enrich
from prospect_intel.schemas import SourceClass


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload if payload is not None else {}

    def json(self):
        return self._p


class _Router:
    routes = []
    calls = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, **k):
        _Router.calls.append(url + " " + str(params or ""))
        hay = url + " " + str(params or "")
        for sub, payload, status in _Router.routes:
            if sub in hay:
                return _Resp(status, payload)
        return _Resp(404, {})


def _route(monkeypatch, routes):
    _Router.routes = routes
    _Router.calls = []
    monkeypatch.setattr(enrich.httpx, "Client", _Router)


def _fresh_db(monkeypatch, tmp_path):
    from prospect_intel import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "e.db")


WD_PERSON_SEARCH = {"search": [
    {"id": "Q123", "label": "Ada Lovelace",
     "description": "English mathematician"}]}
WD_PERSON_ENTITY = {"entities": {"Q123": {
    "labels": {"en": {"value": "Ada Lovelace"}},
    "descriptions": {"en": {"value": "English mathematician"}},
    "claims": {
        "P108": [{"mainsnak": {"snaktype": "value", "datavalue": {
            "type": "wikibase-entityid",
            "value": {"entity-type": "item", "id": "Q999"}}}}],
        "P69": [{"mainsnak": {"snaktype": "value", "datavalue": {
            "type": "wikibase-entityid",
            "value": {"entity-type": "item", "id": "Q888"}}}}]},
    "sitelinks": {"enwiki": {"title": "Ada Lovelace"}}}}}
WD_LABELS = {"entities": {
    "Q999": {"labels": {"en": {"value": "Analytical Engines Ltd"}}},
    "Q888": {"labels": {"en": {"value": "Some College"}}},
    "Q777": {"labels": {"en": {"value": "Software"}}},
    "Q778": {"labels": {"en": {"value": "Berlin"}}}}}
WD_CO_SEARCH = {"search": [
    {"id": "Q456", "label": "Acme Corp", "description": "Fictional company"}]}
WD_CO_ENTITY = {"entities": {"Q456": {
    "labels": {"en": {"value": "Acme Corp"}},
    "descriptions": {"en": {"value": "Fictional company"}},
    "claims": {
        "P1278": [{"mainsnak": {"snaktype": "value", "datavalue": {
            "type": "string", "value": "549300ABCDEF123456"}}}],
        "P452": [{"mainsnak": {"snaktype": "value", "datavalue": {
            "type": "wikibase-entityid",
            "value": {"entity-type": "item", "id": "Q777"}}}}],
        "P159": [{"mainsnak": {"snaktype": "value", "datavalue": {
            "type": "wikibase-entityid",
            "value": {"entity-type": "item", "id": "Q778"}}}}],
        "P1128": [{"mainsnak": {"snaktype": "value", "datavalue": {
            "type": "quantity", "value": {"amount": "+5000"}}}}],
        "P571": [{"mainsnak": {"snaktype": "value", "datavalue": {
            "type": "time",
            "value": {"time": "+1999-00-00T00:00:00Z"}}}}],
        "P856": [{"mainsnak": {"snaktype": "value", "datavalue": {
            "type": "string", "value": "https://acme.example"}}}]},
    "sitelinks": {}}}}
GLEIF_RECORD = {"data": [{
    "id": "549300ABCDEF123456",
    "attributes": {
        "lei": "549300ABCDEF123456",
        "entity": {
            "legalName": {"name": "Acme Corp", "language": "en"},
            "jurisdiction": "US",
            "status": "ACTIVE",
            "registeredAs": "12-3456789",
            "legalAddress": {"city": "Boston", "country": "US",
                             "addressLines": []},
            "headquartersAddress": {"city": "Berlin", "country": "DE",
                                    "addressLines": []}},
        "registration": {"status": "ISSUED"}}}]}
HUNTER_CO = {"data": {"name": "Acme Corp", "description": "We build things",
                      "industry": "Software", "size": "51-200",
                      "location": "Berlin, Germany", "tech": ["python"]}}
HUNTER_PERSON = {"data": {"name": {"fullName": "Ada Lovelace"},
                          "employment": {"title": "CTO",
                                         "name": "Acme Corp"},
                          "location": "London"}}


def test_wikidata_person_facts_with_spans(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    _route(monkeypatch, [("wbsearchentities", WD_PERSON_SEARCH, 200),
                         ("Special:EntityData", WD_PERSON_ENTITY, 200),
                         ("wbgetentities", WD_LABELS, 200)])
    docs, firmo, note = enrich.wikidata_person("Ada Lovelace")
    assert note is None and firmo == {} and len(docs) == 1
    text = docs[0].sections[0].text
    assert "employer: Analytical Engines Ltd" in text
    assert "educated at: Some College" in text
    sec = docs[0].sections[0]
    assert text[sec.char_start:sec.char_end] == text  # exact span


def test_wikidata_namesake_rejected_loudly(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    _route(monkeypatch, [("wbsearchentities", {"search": [
        {"id": "Q9", "label": "Aryan Gupta"}]}, 200)])
    docs, _, note = enrich.wikidata_person("Aryan Saini")
    assert docs == [] and note is not None and "no confident match" in note


def test_wikidata_company_fields_and_lei(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    _route(monkeypatch, [("wbsearchentities", WD_CO_SEARCH, 200),
                         ("Special:EntityData", WD_CO_ENTITY, 200),
                         ("wbgetentities", WD_LABELS, 200)])
    docs, firmo, note = enrich.wikidata_company("Acme Corp")
    assert note is None and len(docs) == 1
    assert firmo["industry"] == "Software" and firmo["hq"] == "Berlin"
    assert firmo["employees"] == "5000" and firmo["founded"] == "1999"
    assert firmo["homepage"] == "https://acme.example"
    assert firmo["lei"] == "549300ABCDEF123456"


def test_wikidata_company_rejects_person_entity(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    _route(monkeypatch, [("wbsearchentities", {"search": [
        {"id": "Q7", "label": "Ford Family"}]}, 200),
        ("Special:EntityData", {"entities": {"Q7": {
            "labels": {"en": {"value": "Ford Family"}},
            "claims": {"P108": [{"mainsnak": {
                "snaktype": "value",
                "datavalue": {"type": "wikibase-entityid",
                              "value": {"entity-type": "item",
                                        "id": "Q1"}}}}]}}}}, 200)])
    docs, _, note = enrich.wikidata_company("Ford")
    assert docs == [] and note is not None and "person record" in note


def test_wikidata_labels_failure_notes_partial(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    _route(monkeypatch, [("wbsearchentities", WD_PERSON_SEARCH, 200),
                         ("Special:EntityData", WD_PERSON_ENTITY, 200),
                         ("wbgetentities", {}, 500)])
    docs, _, note = enrich.wikidata_person("Ada Lovelace")
    assert len(docs) == 1 and note is not None
    assert "labels unresolved" in note


def test_wikidata_entity_cached(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    _route(monkeypatch, [("wbsearchentities", WD_CO_SEARCH, 200),
                         ("Special:EntityData", WD_CO_ENTITY, 200),
                         ("wbgetentities", WD_LABELS, 200)])
    enrich.wikidata_company("Acme Corp")
    enrich.wikidata_company("Acme Corp")
    assert sum("Special:EntityData" in u for u in _Router.calls) == 1


def test_gleif_registry_proof(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    _route(monkeypatch, [("lei-records", GLEIF_RECORD, 200)])
    docs, firmo, note = enrich.gleif_by_lei("549300abcdef123456", "Acme Corp")
    assert note is None and len(docs) == 1
    assert docs[0].source_class == SourceClass.REGISTRY
    assert firmo["registry"] == "LEI 549300ABCDEF123456 (US) (verified via GLEIF)"
    assert firmo["hq"] == "Berlin, DE"
    for ln in docs[0].sections[0].text.splitlines():
        if ln.startswith("Provenance:"):
            continue
        assert ln.rstrip().endswith("(GLEIF).")  # every line tagged (M3)


def test_gleif_namesake_rejected(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    _route(monkeypatch, [("lei-records", GLEIF_RECORD, 200)])
    docs, firmo, note = enrich.gleif_by_lei("549300ABCDEF123456", "Globex")
    assert docs == [] and firmo == {}
    assert note is not None and "rejected" in note


def test_gleif_no_record_loud(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    _route(monkeypatch, [("lei-records", {"data": []}, 200)])
    docs, _, note = enrich.gleif_by_lei("000000000000000000", "Acme Corp")
    assert docs == [] and note is not None and "no record" in note


def test_enrich_company_chains_lei(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    _route(monkeypatch, [("wbsearchentities", WD_CO_SEARCH, 200),
                         ("Special:EntityData", WD_CO_ENTITY, 200),
                         ("wbgetentities", WD_LABELS, 200),
                         ("lei-records", GLEIF_RECORD, 200)])
    docs, firmo, notes = enrich.enrich_company("Acme Corp")
    assert any("lei-records" in u for u in _Router.calls)
    assert firmo["registry"].startswith("LEI 549300ABCDEF123456")
    assert len(docs) == 2  # wikidata + gleif, no other calls needed


def test_enrich_company_no_lei_notes_gleif_skip(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    nolei = {"entities": {"Q456": dict(
        WD_CO_ENTITY["entities"]["Q456"], claims={"P452": []})}}
    _route(monkeypatch, [("wbsearchentities", WD_CO_SEARCH, 200),
                         ("Special:EntityData", nolei, 200)])
    docs, firmo, notes = enrich.enrich_company("Acme Corp")
    assert not any("lei-records" in u for u in _Router.calls)
    assert any("no LEI" in n for n in notes)
    assert "registry" not in firmo
    assert len(docs) == 1  # wikidata only


def test_hunter_no_key_is_loud_no_http(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    _route(monkeypatch, [])
    docs, _, note = enrich.hunter_company("acme.example")
    assert docs == [] and note is not None and "no key" in note
    assert _Router.calls == []


def test_hunter_company_fields(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setenv("HUNTER_API_KEY", "key")
    _route(monkeypatch, [("api.hunter.io", HUNTER_CO, 200)])
    docs, firmo, note = enrich.hunter_company("acme.example", "Acme Corp")
    assert note is None and len(docs) == 1
    assert firmo["industry"] == "Software" and firmo["hq"] == "Berlin, Germany"
    assert "We build things" in docs[0].sections[0].text


def test_hunter_dict_values_ignored(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setenv("HUNTER_API_KEY", "key")
    weird = {"data": {"name": "Acme Corp", "size": {"min": 1, "max": 9},
                      "tech": ["python", {"x": 1}]}}
    _route(monkeypatch, [("api.hunter.io", weird, 200)])
    docs, firmo, _ = enrich.hunter_company("acme.example")
    assert "employees" not in firmo  # dict size never stringified (M2)
    assert "{'min'" not in docs[0].sections[0].text


def test_hunter_monthly_cap_blocks_http(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setenv("HUNTER_API_KEY", "key")
    monkeypatch.setenv("HUNTER_MONTHLY_CAP", "1")
    _route(monkeypatch, [("api.hunter.io", HUNTER_CO, 200)])
    enrich.hunter_company("acme.example")
    n = len(_Router.calls)
    docs, _, note = enrich.hunter_company("other.example")
    assert docs == [] and note is not None and "quota" in note
    assert len(_Router.calls) == n


def test_hunter_person_title_and_safe_url(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setenv("HUNTER_API_KEY", "key")
    _route(monkeypatch, [("api.hunter.io", HUNTER_PERSON, 200)])
    docs, _, note = enrich.hunter_person(email="ada@acme.example")
    assert note is None and len(docs) == 1
    assert "CTO at Acme Corp" in docs[0].sections[0].text
    assert " " not in docs[0].url  # slug-quoted (L4)


def test_enrich_disable_kills_all_http(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setenv("ENRICH_DISABLE", "1")
    monkeypatch.setenv("HUNTER_API_KEY", "key")
    _route(monkeypatch, [])
    assert enrich.wikidata_person("Ada Lovelace") == ([], {}, None)
    assert enrich.wikidata_company("Acme Corp") == ([], {}, None)
    assert enrich.gleif_by_lei("549300ABCDEF123456", "Acme") == ([], {}, None)
    assert enrich.hunter_company("acme.example") == ([], {}, None)
    assert _Router.calls == []


def test_merge_firmo_extra_fill_and_registry_upgrade():
    from prospect_intel.passes import merge_firmo_extra
    base = {"company": "Acme", "registry": "unknown", "industry": "Old",
            "sources": ["https://dbpedia.org/page/Acme"]}
    out = merge_firmo_extra(base, {"registry": "LEI 123 (verified via GLEIF)",
                                   "industry": "New", "hq": "London",
                                   "sources": ["https://search.gleif.org"]})
    assert out["registry"].startswith("LEI 123")
    assert out["industry"] == "Old"  # never clobbers
    assert out["hq"] == "London"
    assert out["sources"] == ["https://dbpedia.org/page/Acme",
                              "https://search.gleif.org"]


def test_pass2_merges_and_persists_extra(monkeypatch, tmp_path):
    from prospect_intel import discovery
    from prospect_intel import store
    from prospect_intel.passes import pass2_firmographic
    import prospect_intel.passes as P
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "p.db")
    monkeypatch.setattr(P, "_probe_company_website",
                        lambda c: {"company": c, "registry": "unknown",
                                   "filings": [], "funding": "unknown",
                                   "website_probe": "none"})
    monkeypatch.setattr(discovery, "dbpedia_company", lambda c: ({}, None))
    prof = pass2_firmographic("Acme Corp", {},
                              extra={"registry": "LEI 1 (verified via GLEIF)",
                                     "sources": ["https://search.gleif.org"]})
    assert prof["registry"].startswith("LEI 1")
    con = store.connect()
    reread = store.firmo_get(con, "Acme Corp")  # L5: merged profile persisted
    con.close()
    assert reread["registry"].startswith("LEI 1")


def test_discover_person_wires_enrich(monkeypatch, tmp_path):
    from prospect_intel import discovery
    from prospect_intel.schemas import (DocSection, FetchStatus, SourceClass,
                                        StructuredDoc)
    _fresh_db(monkeypatch, tmp_path)
    doc = StructuredDoc(doc_id="doc_e1", url="https://www.wikidata.org/wiki/Q1",
                        url_final="https://www.wikidata.org/wiki/Q1",
                        content_hash="h", fetched_at="2026-01-01T00:00:00Z",
                        fetch_status=FetchStatus.OK,
                        source_class=SourceClass.OTHER,
                        sections=[DocSection(section_id="doc_e1#s0",
                                             text="Acme Corp — industry: Software (Wikidata).",
                                             char_start=0, char_end=44)])
    gdoc = StructuredDoc(doc_id="doc_g1", url="https://search.gleif.org/",
                         url_final="https://search.gleif.org/",
                         content_hash="g", fetched_at="2026-01-01T00:00:00Z",
                         fetch_status=FetchStatus.OK,
                         source_class=SourceClass.REGISTRY,
                         sections=[DocSection(section_id="doc_g1#s0",
                                              text="Acme Corp — LEI proof (GLEIF).",
                                              char_start=0, char_end=30)])
    monkeypatch.setattr(discovery, "tier1_wikipedia",
                        lambda q, n=4: discovery.TierResult(tier="tier1_wiki"))
    monkeypatch.setattr(discovery, "tier1_gnews", lambda q, n=8:
                        discovery.TierResult(tier="tier1_gnews"))
    monkeypatch.setattr(discovery, "tier2_searxng", lambda q, n=8:
                        discovery.TierResult(tier="tier2_searxng",
                                             available=False, note="off"))
    import prospect_intel.enrich as E
    monkeypatch.setattr(E, "enrich_person", lambda n: ([], {}, []))
    monkeypatch.setattr(E, "enrich_company",
                        lambda c: ([doc, gdoc],
                                   {"industry": "Software",
                                    "registry": "LEI 1 (verified via GLEIF)"},
                                   ["CH skipped (no key)"]))
    d = discovery.discover_person("Ada Lovelace", "Acme Corp", "Ada Acme")
    assert "tier1_enrich" in d.tiers_used
    assert [x.doc_id for x in d.docs] == ["doc_e1", "doc_g1"]
    assert d.firmo_extra["registry"].startswith("LEI 1")
    assert any("CH skipped" in n for n in d.degraded)


def test_discover_person_disabled_hides_tier(monkeypatch, tmp_path):
    from prospect_intel import discovery
    _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setenv("ENRICH_DISABLE", "1")
    monkeypatch.setattr(discovery, "tier1_wikipedia",
                        lambda q, n=4: discovery.TierResult(tier="tier1_wiki"))
    monkeypatch.setattr(discovery, "tier1_gnews", lambda q, n=8:
                        discovery.TierResult(tier="tier1_gnews"))
    monkeypatch.setattr(discovery, "tier2_searxng", lambda q, n=8:
                        discovery.TierResult(tier="tier2_searxng",
                                             available=False, note="off"))
    d = discovery.discover_person("Ada Lovelace", "Acme Corp", "Ada Acme")
    assert "tier1_enrich" not in d.tiers_used  # L2: no phantom tier
    assert d.docs == [] and d.firmo_extra == {}


def test_hit_relevant_gates():
    from prospect_intel.discovery import hit_relevant
    assert hit_relevant("https://x/1", "Ada Lovelace wins award",
                        "Ada Lovelace", "Acme Corp")
    assert hit_relevant("https://x/2", "Acme Corp expands hiring",
                        "Ada Lovelace", "Acme Corp")
    assert not hit_relevant("https://x/3", "Aryan Gupta wins award",
                            "Aryan Saini", "Acme Corp")
    assert not hit_relevant("https://x/4", "Tech giant cuts jobs",
                            "Ada Lovelace", "Acme Corp")  # strict: drop
    assert not hit_relevant("https://x/5", "Random story", "", "")


def test_discover_person_drops_irrelevant_hits(monkeypatch, tmp_path):
    from prospect_intel import discovery
    from prospect_intel.search import SearchHit
    _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setattr(discovery, "tier1_news_api", lambda q, n=8:
                        discovery.TierResult(
                            tier="tier1_news",
                            hits=[SearchHit(url="https://n/1",
                                            title="Acme Corp expands hiring"),
                                  SearchHit(url="https://n/2",
                                            title="Unrelated sports story")]))
    monkeypatch.setattr(discovery, "tier1_wikipedia",
                        lambda q, n=4: discovery.TierResult(tier="tier1_wiki"))
    monkeypatch.setattr(discovery, "tier1_gnews", lambda q, n=8:
                        discovery.TierResult(tier="tier1_gnews"))
    monkeypatch.setattr(discovery, "tier2_searxng", lambda q, n=8:
                        discovery.TierResult(tier="tier2_searxng",
                                             available=False, note="off"))
    import prospect_intel.enrich as E
    monkeypatch.setattr(E, "enrich_person", lambda n: ([], {}, []))
    monkeypatch.setattr(E, "enrich_company", lambda c: ([], {}, []))
    d = discovery.discover_person("Ada Lovelace", "Acme Corp", "Ada Acme")
    assert [h.url for h in d.hits] == ["https://n/1"]
    assert any("none about" in n for n in d.degraded) is False  # one kept
    # all miss -> loud note
    monkeypatch.setattr(discovery, "tier1_news_api", lambda q, n=8:
                        discovery.TierResult(
                            tier="tier1_news",
                            hits=[SearchHit(url="https://n/9",
                                            title="Unrelated sports story")]))
    d2 = discovery.discover_person("Ada Lovelace", "Acme Corp", "Ada Acme")
    assert d2.hits == []
    assert any("none about" in n for n in d2.degraded)


def test_hunter_wrong_company_rejected(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setenv("HUNTER_API_KEY", "key")
    _route(monkeypatch, [("api.hunter.io", HUNTER_CO, 200)])
    docs, firmo, note = enrich.hunter_company("acme.example", "Globex")
    assert docs == [] and firmo == {}
    assert note is not None and "rejected" in note
