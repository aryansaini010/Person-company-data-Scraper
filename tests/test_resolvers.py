"""Pass 0 resolvers + scoper + governance ordering (mocked network)."""
import prospect_intel.resolvers as R
from prospect_intel import discovery


def _gleif_item(name, lei, status="ACTIVE", cin="L92132MH1982PLC028767",
                city="Mumbai", country="IN"):
    return {"attributes": {
        "lei": lei,
        "entity": {
            "legalName": {"name": name},
            "status": status,
            "jurisdiction": country,
            "legalAddress": {"city": city, "country": country,
                             "addressLines": ["NM Joshi Marg"]},
            "headquartersAddress": {"city": city, "country": country,
                                    "addressLines": ["NM Joshi Marg"]},
            "registeredAs": cin},
        "registration": {"status": "ISSUED"}}}


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


class _Client:
    """Fake httpx.Client recording calls; routes by URL."""
    calls: list = []

    def __init__(self, handler, *a, **k):
        self._h = handler

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, **k):
        _Client.calls.append(url)
        return self._h(url, params)


def test_gleif_accepts_exact_zee(monkeypatch, tmp_path):
    from prospect_intel import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "r.db")
    _Client.calls = []
    items = [_gleif_item("ZEE ENTERTAINMENT ENTERPRISES LIMITED",
                         "254900EQIYPXZEO10B94"),
             _gleif_item("ZEE LEARN LIMITED", "335800G5Q44L3J8Y6L99"),
             _gleif_item("ZEE MEDIA CORPORATION LIMITED", "254900EQIYPXZEO77B11")]

    def handler(url, params):
        assert "gleif" in url
        return _Resp({"data": items})

    import httpx
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _Client(handler, *a, **k))
    firmo, docs, note, ok = R.query_gleif("Zee Entertainment Enterprises")
    assert ok and note is None
    assert firmo["lei"] == "254900EQIYPXZEO10B94"  # wrong LEI must not match
    assert firmo["cin"] == "L92132MH1982PLC028767"
    assert firmo["legal_name"] == "ZEE ENTERTAINMENT ENTERPRISES LIMITED"
    assert docs and docs[0].source_class.value == "registry"


def test_gleif_namesake_ambiguity_falls_back(monkeypatch, tmp_path):
    from prospect_intel import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "r2.db")
    # Two ACTIVE records tie on substring tokens with no exact equality:
    # must refuse to guess (alias fallback downstream), loudly.
    items = [_gleif_item("ZEE ENTERTAINMENT HOLDINGS", "254900EQIYPXZEO10B94"),
             _gleif_item("ZEE MEDIA HOLDINGS", "254900EQIYPXZEO77B11")]

    def handler(url, params):
        return _Resp({"data": items})

    import httpx
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _Client(handler, *a, **k))
    firmo, docs, note, ok = R.query_gleif("Zee Holdings")
    assert not ok and firmo == {} and docs == []
    assert note is not None and "mbiguity" in note


def test_gleif_cache_hit_no_second_call(monkeypatch, tmp_path):
    from prospect_intel import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "r3.db")
    items = [_gleif_item("ZEE ENTERTAINMENT ENTERPRISES LIMITED",
                         "254900EQIYPXZEO10B94")]
    calls = {"n": 0}

    def handler(url, params):
        calls["n"] += 1
        return _Resp({"data": items})

    import httpx
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _Client(handler, *a, **k))
    R.query_gleif("Zee Entertainment Enterprises")
    R.query_gleif("Zee Entertainment Enterprises")
    assert calls["n"] == 1  # second served from 30d SQLite cache


def test_wikidata_sparql_fallback(monkeypatch, tmp_path):
    from prospect_intel import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "r4.db")
    import prospect_intel.enrich as E
    monkeypatch.setattr(E, "wikidata_company", lambda c: ([], {}, "no match"))

    def handler(url, params):
        assert "sparql" in url
        return _Resp({"results": {"bindings": [{
            "item": {"value": "http://www.wikidata.org/entity/Q12428554"},
            "itemLabel": {"value": "Zee Entertainment Enterprises"},
            "inception": {"value": "1991-01-01T00:00:00Z"},
            "hqLabel": {"value": "Mumbai"},
            "website": {"value": "https://www.zee.com"}}]}})

    import httpx
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _Client(handler, *a, **k))
    firmo, docs, note = R.query_wikidata("Zee Entertainment Enterprises")
    assert firmo.get("founded") == "1991"
    assert firmo.get("homepage") == "https://www.zee.com"
    assert docs


def test_scoper_keeps_governance_filings_and_segment():
    from prospect_intel.search_scoper import build_scoped_queries
    seg = {"rss_angles": ("{co} strategy expansion earnings",
                          "{co} hiring jobs careers")}
    qs = build_scoped_queries(
        {"legal_name": "ZEE ENTERTAINMENT ENTERPRISES LIMITED",
         "official_website": "https://www.zee.com"}, seg)
    assert qs[0].startswith("site:zee.com")
    assert any("MCA" in q for q in qs)  # filings query
    assert any("hiring jobs" in q for q in qs)  # segment preserved
    qs2 = build_scoped_queries({"legal_name": "Acme Corp"}, {})
    assert qs2[0].startswith('"Acme Corp"')  # no domain → quoted fallback


def test_prioritize_governance_first():
    from prospect_intel.firecrawl import prioritize_governance_urls
    urls = ["https://x.com/post", "https://co.example/about",
            "https://co.example/investors/annual-report-2025",
            "https://co.example/newsroom/launches-ott"]
    out = prioritize_governance_urls(urls + ["https://co.example/about"])
    assert out[0] == "https://co.example/investors/annual-report-2025"
    assert len(out) == 4  # deduped


def test_ground_truth_contract_keys(monkeypatch, tmp_path):
    from prospect_intel import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "r5.db")
    monkeypatch.setattr(R, "query_gleif", lambda q, deadline=None: (
        {"lei": "254900EQIYPXZEO10B94", "legal_name": "ZEE ENTERTAINMENT "
         "ENTERPRISES LIMITED", "sources": ["https://search.gleif.org/"]},
        [], None, True))
    monkeypatch.setattr(R, "query_wikidata", lambda q, deadline=None: ({}, [], None))
    firmo, docs, degraded, verified, resolved, web = R.get_ground_truth("Zee")
    assert verified and resolved.startswith("ZEE ENTERTAINMENT")
    assert "verified_identity" not in firmo  # contract keys only
    assert firmo.get("lei") == "254900EQIYPXZEO10B94"
    assert isinstance(degraded, list)
