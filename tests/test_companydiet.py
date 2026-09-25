"""Company diet: RSS stamping, domain/probe builders, source weighting."""
from prospect_intel.acquisition import stamp_docs, structure_fetch
from prospect_intel.passes import _company_domains
from prospect_intel.schemas import SourceClass

def test_stamp_docs_sets_title_and_date():
    doc = structure_fetch("https://example.com/a", 200, "Acme expands hiring.")
    assert doc is not None
    stamp_docs([doc], {"https://example.com/a": ("Feed Title", "Mon, 01 Sep 2026")})
    assert doc.title == "Feed Title" and "2026" in doc.published_at

def test_company_domains_skips_unknown():
    assert _company_domains("unknown") == []
    assert _company_domains("Tesla") == ["https://tesla.com", "https://www.tesla.com"]

def test_news_outranks_generic(monkeypatch):
    from prospect_intel.models import ToolBoundary
    from prospect_intel.passes import pass3_strategy
    from prospect_intel.schemas import DocSection, StructuredDoc
    from prospect_intel.verifier import Verifier
    import prospect_intel.audit as _A
    monkeypatch.setattr(_A, "log", lambda e, p: {})
    def doc(i, sc, text):
        return StructuredDoc(doc_id=f"dx{i}", url=f"https://x/{i}",
            url_final=f"https://x/{i}", content_hash="h",
            fetched_at="2026-01-01T00:00:00Z", source_class=sc,
            sections=[DocSection(section_id=f"dx{i}#s0", text=text,
                                 char_start=0, char_end=len(text))])
    docs = [doc(1, SourceClass.OTHER, "Acme will expand platform hiring now."),
            doc(2, SourceClass.JOB_POSTING, "Acme will expand platform hiring now.")]
    v, _ = pass3_strategy(docs, ToolBoundary(), Verifier())
    assert v and v[0].claim.doc_id == "dx2"  # job posting wins ties
