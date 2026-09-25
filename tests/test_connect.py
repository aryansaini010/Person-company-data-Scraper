"""Tier connections: DBpedia merge, NewsAPI loud failure, homepage-first probes."""
from prospect_intel import discovery
from prospect_intel.passes import pass2_firmographic


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload or {}

    def json(self):
        return self._p


class _Client:
    resp = _Resp()

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, *a, **k):
        return _Client.resp


DBP_FIXTURE = {
    "http://dbpedia.org/resource/Reliance_Industries": {
        "http://xmlns.com/foaf/0.1/homepage": [
            {"type": "uri", "value": "https://www.ril.com/"}],
        "http://dbpedia.org/ontology/numberOfEmployees": [
            {"type": "literal", "value": "347362"}],
        "http://dbpedia.org/ontology/locationCity": [
            {"type": "uri", "value": "http://dbpedia.org/resource/Mumbai"}],
        "http://dbpedia.org/ontology/industry": [
            {"type": "uri", "value": "http://dbpedia.org/resource/Conglomerate_(company)"}],
    }
}


def test_dbpedia_fields_parsed(monkeypatch):
    _Client.resp = _Resp(200, DBP_FIXTURE)
    monkeypatch.setattr(discovery.httpx, "Client", _Client)
    fields, note = discovery.dbpedia_company("Reliance Industries")
    assert note is None
    assert fields["homepage"] == "https://www.ril.com"
    assert fields["employees"] == "347362" and fields["hq"] == "Mumbai"
    assert fields["industry"] == "Conglomerate"


def test_homepage_glitch_sanitized(monkeypatch):
    glitch = dict(DBP_FIXTURE)
    glitch["http://dbpedia.org/resource/Reliance_Industries"] = dict(
        DBP_FIXTURE["http://dbpedia.org/resource/Reliance_Industries"])
    glitch["http://dbpedia.org/resource/Reliance_Industries"][
        "http://xmlns.com/foaf/0.1/homepage"] = [
            {"type": "uri", "value": "https://www.zee.com/%7Czee.com"}]
    _Client.resp = _Resp(200, glitch)
    monkeypatch.setattr(discovery.httpx, "Client", _Client)
    fields, _ = discovery.dbpedia_company("Reliance Industries")
    assert fields["homepage"] == "https://www.zee.com"


def test_newsapi_401_is_loud_not_silent(monkeypatch):
    import os
    os.environ["NEWSAPI_KEY"] = "bad-key"
    try:
        _Client.resp = _Resp(401, {"message": "apiKeyInvalid"})
        monkeypatch.setattr(discovery.httpx, "Client", _Client)
        r = discovery.tier1_news_api("Acme")
        assert not r.available and "401" in r.note
    finally:
        del os.environ["NEWSAPI_KEY"]


def test_pass2_merges_dbpedia(monkeypatch, tmp_path):
    from prospect_intel import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "p.db")
    import prospect_intel.passes as P
    monkeypatch.setattr(P, "_probe_company_website",
                        lambda c: {"company": c, "registry": "x",
                                   "filings": [], "funding": "unknown",
                                   "website_probe": "none"})
    monkeypatch.setattr(discovery, "dbpedia_company",
                        lambda c: ({"homepage": "https://www.ril.com",
                                    "employees": "347362"}, None))
    prof = pass2_firmographic("Reliance Industries", {})
    assert prof["website_probe"] == "https://www.ril.com"
    assert prof["employees"] == "347362" and prof["confidence"] >= 0.6
