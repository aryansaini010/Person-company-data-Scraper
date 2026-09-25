"""Quality gates: no unrelated data in briefs (Imperial Milestone lesson)."""
from prospect_intel.schemas import DocSection, FetchStatus, SourceClass, StructuredDoc


def _doc(doc_id, title, text, url="https://example.com/x"):
    return StructuredDoc(doc_id=doc_id, url=url, url_final=url,
                         content_hash="h", fetched_at="2026-01-01T00:00:00Z",
                         fetch_status=FetchStatus.OK,
                         source_class=SourceClass.OTHER, title=title,
                         sections=[DocSection(section_id=doc_id + "#s0",
                                              text=text, char_start=0,
                                              char_end=len(text))])


def test_wiki_title_gate():
    from prospect_intel.discovery import _wiki_title_relevant
    # company query: org page about the company kept, person page dropped
    assert _wiki_title_relevant("Imperial Milestone", "", "Imperial Milestone Private Limited")
    assert not _wiki_title_relevant("Elon Musk", "", "Tesla")
    assert not _wiki_title_relevant("Bank of New York Mellon", "",
                                    "Imperial Milestone Private Limited")
    # person flow: own page kept, other's dropped, org context kept
    assert _wiki_title_relevant("Elon Musk", "Elon Musk", "Tesla")
    assert not _wiki_title_relevant("Errol Musk", "Elon Musk", "Tesla")


def test_core_tokens():
    from prospect_intel.discovery import _core_tokens_mentioned
    lead = "Imperial Milestone Pvt Ltd | Technology & Business Solutions"
    assert _core_tokens_mentioned(lead, "Imperial Milestone Private Limited")
    assert not _core_tokens_mentioned("Justdial local listings Jaipur",
                                      "Imperial Milestone Private Limited")


def test_pass3_drops_foreign_company_sentence():
    from prospect_intel.models import ToolBoundary
    from prospect_intel.passes import pass3_strategy
    from prospect_intel.verifier import Verifier
    stray = _doc(
        "d1", "Finance News",
        "(now Bank of New York Mellon) of Pittsburgh PA expanded pension "
        "and custody business in 1997 with strong earnings growth.",
        url="https://news.example.com/finance")
    own = _doc(
        "d2", "Imperial Milestone",
        "Imperial Milestone Private Limited will expand platform engineering "
        "hiring in Jaipur to support enterprise growth in 2026.",
        url="https://news.example.com/imperial")
    verified, gaps = pass3_strategy([stray, own], ToolBoundary(), Verifier(),
                                    "", "Imperial Milestone Private Limited",
                                    {"www.imperialmilestone.com"})
    texts = [v.claim.text for v in verified]
    assert any("Imperial Milestone" in t for t in texts)
    assert not any("Mellon" in t for t in texts)
    assert any("not about" in g for g in gaps)


def test_pass3_owned_doc_sentence_without_name_kept():
    from prospect_intel.models import ToolBoundary
    from prospect_intel.passes import pass3_strategy
    from prospect_intel.verifier import Verifier
    owned = _doc(
        "d3", "Careers",
        "We will hire 500 engineers this year to support enterprise growth "
        "and platform expansion across all regions.",
        url="https://www.imperialmilestone.com/careers")
    verified, _ = pass3_strategy([owned], ToolBoundary(), Verifier(),
                                 "", "Imperial Milestone Private Limited",
                                 {"www.imperialmilestone.com"})
    assert len(verified) >= 1  # nameless but owned: recall preserved


def test_probe_accepts_token_title(monkeypatch):
    import prospect_intel.passes as P
    monkeypatch.setattr(P, "_company_domains", lambda c: [])
    monkeypatch.setattr("prospect_intel.discovery.dbpedia_company",
                        lambda c: ({}, None))
    from prospect_intel import firecrawl as fc
    monkeypatch.setattr(fc, "search", lambda q, n=5: [
        ("https://www.imperialmilestone.com/", "Imperial Milestone Pvt Ltd", "")])
    from prospect_intel.fetcher import FetchResult
    long_body = ("Imperial Milestone Pvt Ltd builds software in Jaipur. "
                 "Careers open. " + "Enterprise platforms and hiring. " * 30)
    monkeypatch.setattr(
        "prospect_intel.fetcher.fetch_url_smart",
        lambda u, **k: FetchResult(url=u, url_final=u, status_code=200,
                              body_text=long_body,
                              raw_body=b"x", title="Imperial Milestone"))
    found = P.probe_company_pages("Imperial Milestone Private Limited")
    assert found  # token-title homepage accepted, not only full legal name
