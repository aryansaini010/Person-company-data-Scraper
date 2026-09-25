"""Request budgets: everything settles inside ~3.5 minutes, partial + marked."""
import time

from prospect_intel import deadline as dl


def test_deadline_helpers():
    assert dl.api_deadline_s() >= 30.0
    assert dl.expired(time.time() - 1) is True
    assert dl.expired(time.time() + 100) is False
    assert dl.expired(None) is False
    assert dl.start() > time.time()


def test_deadline_env_clamped(monkeypatch):
    monkeypatch.setenv("PROSPECT_API_DEADLINE_S", "5")
    assert dl.api_deadline_s() == 30.0  # floor, never silly-low
    monkeypatch.setenv("PROSPECT_API_DEADLINE_S", "bogus")
    assert dl.api_deadline_s() == dl.DEFAULT_S


def test_acquire_deadline_returns_partial(monkeypatch):
    import prospect_intel.acquisition as A
    import prospect_intel.fetcher as F
    called = []

    def boom(u, **k):
        called.append(u)
        raise AssertionError("no fetch may start past the deadline")

    monkeypatch.setattr(F, "fetch_url_smart", boom)
    docs = A.acquire(["https://e.com/a", "https://e.com/b"],
                     deadline=time.time() - 1)
    assert docs == [] and called == []


def test_diet_plain_only(monkeypatch):
    import prospect_intel.fetcher as F
    from prospect_intel import discovery
    from prospect_intel.search import SearchHit
    seen = {}

    def fake_smart(url, render=True):
        seen[url] = render
        from prospect_intel.fetcher import FetchResult
        return FetchResult(url=url, url_final=url, status_code=200,
                           body_text="Acme Corp expands hiring now.",
                           raw_body=b"x", title="Acme Corp")

    monkeypatch.setattr(F, "fetch_url_smart", fake_smart)
    monkeypatch.setattr(discovery, "tier1_google_news_rss",
                        lambda q, n=4: (
                            [SearchHit(url="https://n.example/1",
                                       title="Acme Corp expands hiring",
                                       outlet="N")],
                            {"https://n.example/1": ("Acme Corp expands hiring",
                                                     "2026-01-01")}))
    docs = []
    discovery.company_diet("Acme Corp", "", docs, [], fetch_full=True,
                           max_angles=1)
    assert any(v is False for v in seen.values())  # plain-only diet fetch
    assert docs  # headlines still land


def test_probe_deadline_returns_fast(monkeypatch):
    import prospect_intel.passes as P
    import prospect_intel.fetcher as F

    def boom(*a, **k):
        raise AssertionError("no network past the deadline")

    monkeypatch.setattr(F, "fetch_url_smart", boom)
    assert P.probe_company_pages("Acme Corp",
                                 deadline=time.time() - 1) == {}
