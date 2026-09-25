from prospect_intel.schemas import Claim, StructuredDoc, SourceClass, DocSection
from prospect_intel.verifier import Verifier

def _doc(text):
    return StructuredDoc(doc_id="doc_abc", url="https://example.com",
        fetched_at="2026-01-01T00:00:00Z", source_class=SourceClass.NEWS,
        sections=[DocSection(text=text, char_start=0, char_end=len(text))])

def test_supported():
    text = "Acme will expand platform engineering hiring in Berlin"
    doc = _doc(text)
    c = Claim(claim_id="c1", text="Acme will expand platform engineering hiring",
              doc_id="doc_abc", section_index=0, char_start=0, char_end=len(text))
    assert Verifier().check(c, doc).verdict.value == "supported"

def test_unsupported_dropped():
    doc = _doc("Acme sells muffins")
    c = Claim(claim_id="c2", text="Acme acquired a semiconductor fab",
              doc_id="doc_abc", section_index=0, char_start=0, char_end=len(doc.sections[0].text))
    assert Verifier().check(c, doc).verdict.value == "unsupported"
