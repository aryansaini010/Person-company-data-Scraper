"""Plain-first policy: robots gate in front, plain primary, render fallback
for blocked/paywalled/empty/error, robots denial final (aggregator
redirects excepted), SSRF never bypassed or masked."""
import os
from prospect_intel import fetcher
from prospect_intel.fetcher import FetchResult
from prospect_intel import firecrawl as fc

def _plain(status=200, text="ok body", robots=False, url="https://example.com/a"):
    return FetchResult(url=url, url_final=url, status_code=status,
                       body_text=text, raw_body=text.encode(),
                       robots_denied=robots)

def test_bypass_policy():
    assert fc.bypass_allowed("https://x.com", "blocked")
    assert fc.bypass_allowed("https://x.com", "paywalled")
    assert fc.bypass_allowed("https://x.com", "empty")
    assert not fc.bypass_allowed("https://x.com", "robots_denied")
    assert fc.bypass_allowed("https://news.google.com/rss/articles/X", "robots_denied")

def test_plain_primary(monkeypatch):
    monkeypatch.setattr(fetcher, "_robots_ok", lambda u: True)
    def boom(u):
        raise AssertionError("render must not be called when plain is ok")
    monkeypatch.setattr(fc, "scrape", boom)
    monkeypatch.setattr(fetcher, "fetch_url", lambda u: _plain())
    fr = fetcher.fetch_url_smart("https://example.com/a")
    assert fr.rendered_via == "" and fr.body_text == "ok body"

def test_render_fallback_on_blocked(monkeypatch):
    monkeypatch.setattr(fetcher, "_robots_ok", lambda u: True)
    monkeypatch.setattr(fc, "available", lambda: True)
    monkeypatch.setattr(fc, "scrape", lambda u: ("# T\nRendered.", "T", "2026-01-01"))
    monkeypatch.setattr(fetcher, "fetch_url",
                        lambda u: _plain(status=403, text="forbidden"))
    fr = fetcher.fetch_url_smart("https://example.com/a")
    assert fr.rendered_via == "firecrawl" and "Rendered" in fr.body_text

def test_plain_fallback_on_render_failure(monkeypatch):
    monkeypatch.setattr(fetcher, "_robots_ok", lambda u: True)
    monkeypatch.setattr(fc, "available", lambda: True)
    def boom(u):
        raise RuntimeError("render down")
    monkeypatch.setattr(fc, "scrape", boom)
    monkeypatch.setattr(fetcher, "fetch_url", lambda u: _plain())
    assert fetcher.fetch_url_smart("https://example.com/a").rendered_via == ""

def test_robots_denial_stands(monkeypatch):
    monkeypatch.setattr(fetcher, "_robots_ok", lambda u: False)
    def boom(u):
        raise AssertionError("robots denial must stand")
    monkeypatch.setattr(fc, "scrape", boom)
    assert fetcher.fetch_url_smart("https://example.com/a").robots_denied

def test_ssrf_never_rendered(monkeypatch):
    import pytest
    with monkeypatch.context() as m:
        m.setattr("prospect_intel.fetcher._robots_ok",
                  lambda u: (_ for _ in ()).throw(ValueError("SSRF blocked")))
        with pytest.raises(ValueError):
            fetcher.fetch_url_smart("http://127.0.0.1/x")
