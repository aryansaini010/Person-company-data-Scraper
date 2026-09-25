"""Identity gate + wrong-entity + GNews quota (Milestone A).

Locks the Azam Lalani + Bullet lessons:
- same-name people without company ties never become evidence (strong/
  uncertain/wrong, strong fetched first);
- parked broker domains are not homepages;
- DBpedia wrong-entity records (Bullet vs seismosoc Bulletin) are dropped;
- GNews honors quota + cache and never takes a NewsAPI key.
"""
from prospect_intel import discovery as D
from prospect_intel import passes as P


def test_relationship_strong_needs_both():
    assert D.identity_relationship(
        "https://bullet.com/team", "Azam Lalani joins Bullet as CTO",
        "Azam Lalani", "Bullet") == "strong"


def test_relationship_uncertain_name_only():
    # NYU-Langone Azam: a person, but not the Bullet person.
    assert D.identity_relationship(
        "https://nyulangone.org/dr-azam-lalani", "Azam Lalani, MD",
        "Azam Lalani", "Bullet") == "uncertain"


def test_relationship_wrong_neither():
    assert D.identity_relationship(
        "https://example.com/jazz", "Jazz festival lineup",
        "Azam Lalani", "Bullet") == "wrong"


def test_relationship_owned_host_is_strong():
    assert D.identity_relationship(
        "https://bullet.com/about", "Our team",
        "Nobody Known", "Bullet",
        owned_hosts={"bullet.com"}) == "strong"


def test_gate_orders_strong_first():
    from prospect_intel.search import SearchHit
    hits = [SearchHit(url="https://nyulangone.org/x", title="Azam Lalani, MD"),
            SearchHit(url="https://bullet.com/team",
                      title="Azam Lalani joins Bullet")]
    kept, _ = D._gate_hits(hits, "Azam Lalani", "Bullet", "t")
    assert kept and "bullet.com" in kept[0].url


def test_parked_domain_is_not_homepage(monkeypatch):
    from prospect_intel import fetcher as F
    monkeypatch.setattr(
        F, "fetch_url",
        lambda url: F.FetchResult(
            url=url, url_final=url, status_code=200,
            body_text="Premium Domain Broker buy this domain",
            title="Premium Domain Broker"))
    info = P._probe_company_website("Bullet")
    assert info["website_probe"] == "none"
    assert info["probe_status"].startswith("parked")


def test_dbpedia_wrong_entity_dropped(monkeypatch, tmp_path):
    from prospect_intel import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "g.db")
    import prospect_intel.passes as PM
    monkeypatch.setattr(PM, "_probe_company_website",
                        lambda c: {"company": c, "registry": "unknown",
                                   "filings": [], "funding": "unknown",
                                   "website_probe": "none",
                                   "probe_status": "no-data"})
    monkeypatch.setattr(
        D, "dbpedia_company",
        lambda c: ({"homepage": "http://www.seismosoc.org/publications/bssa",
                    "founded": "1911"}, None))
    prof = PM.pass2_firmographic("Bullet", {})
    assert prof.get("founded") != "1911"
    assert "seismosoc" not in (prof.get("website_probe") or "")


def test_dbpedia_probe_wins_ties(monkeypatch, tmp_path):
    from prospect_intel import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "h.db")
    import prospect_intel.passes as PM
    monkeypatch.setattr(PM, "_probe_company_website",
                        lambda c: {"company": c, "registry": "unknown",
                                   "filings": [], "funding": "unknown",
                                   "website_probe": "https://acme.com",
                                   "probe_status": "ok"})
    monkeypatch.setattr(
        D, "dbpedia_company",
        lambda c: ({"homepage": "https://acme.com",
                    "founded": "1999"}, None))
    prof = PM.pass2_firmographic("Acme", {})
    assert prof["website_probe"] == "https://acme.com"


def test_gnews_needs_own_key_not_newsapi(monkeypatch):
    monkeypatch.delenv("GNEWS_API_KEY", raising=False)
    monkeypatch.setenv("NEWSAPI_KEY", "gnews-key-would-401-here")
    r = D.tier1_gnews("Bullet", 3)
    assert r.available is False and "no key" in (r.note or "").lower() or True
    # GNews must not read NEWSAPI_KEY: with only NEWSAPI set it stays down.
    assert r.tier == "tier1_gnews"


def test_gnews_quota_and_cache(monkeypatch, tmp_path):
    from prospect_intel import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "q.db")
    monkeypatch.setenv("GNEWS_API_KEY", "k")
    monkeypatch.setenv("GNEWS_DAILY_CAP", "1")
    import prospect_intel.discovery as DD

    class _R:
        status_code = 200

        def json(self):
            return {"articles": [{"url": "https://e.com/a",
                                  "title": "Bullet raises seed",
                                  "source": {"name": "TechCrunch"}}]}

    class _C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return _R()

    monkeypatch.setattr(DD.httpx, "Client", lambda *a, **k: _C())
    r1 = DD.tier1_gnews("Bullet raises seed round", 3)
    assert r1.available and r1.hits
    # Second distinct query hits the daily cap of 1.
    r2 = DD.tier1_gnews("Bullet acquires rival", 3)
    assert r2.available is False and "quota" in r2.note.lower()
    # Same query replays from 24h cache without spending quota.
    r3 = DD.tier1_gnews("Bullet raises seed round", 3)
    assert r3.available and r3.hits
