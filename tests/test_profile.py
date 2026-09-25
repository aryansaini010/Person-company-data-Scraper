"""Profile card: roles from evidence, LinkedIn as manual-check reference only."""
from prospect_intel.discovery import _filter_hits
from prospect_intel.passes import build_profile_card, roles_from_evidence
from prospect_intel.schemas import DocSection, SourceClass, StructuredDoc
from prospect_intel.search import SearchHit

def _doc(doc_id, title, text):
    return StructuredDoc(doc_id=doc_id, url="https://x", url_final="https://x",
        content_hash="h", fetched_at="2026-01-01T00:00:00Z",
        source_class=SourceClass.OTHER, title=title,
        sections=[DocSection(section_id=doc_id + "#s0", text=text,
                             char_start=0, char_end=len(text))])

def test_roles_capture_role_words():
    docs = [_doc("d1", "Elon Musk",
                 "Elon Musk is the CEO of Tesla and founder of SpaceX.")]
    roles = roles_from_evidence("Elon Musk", docs)
    tesla = [r for r in roles if r["company"] == "Tesla"][0]
    assert "CEO" in tesla["role"] and tesla["evidence"] == ["d1"]

def test_linkedin_partitioned_not_fetched():
    kept, refs = _filter_hits([SearchHit(url="https://in.linkedin.com/in/x"),
                               SearchHit(url="https://www.instagram.com/elonmusk/"),
                               SearchHit(url="https://example.com/a")])
    assert [h.url for h in kept] == ["https://example.com/a"]
    assert len(refs) == 1 and "linkedin" in refs[0].url

def test_profile_card_labels_linkedin_manual():
    docs = [_doc("d1", "Elon Musk",
                 "Elon Musk is the CEO of Tesla. Tesla builds cars.")]
    roles, refs = build_profile_card(
        "Elon Musk", docs,
        [SearchHit(url="https://in.linkedin.com/in/elonmusk", title="Elon Musk")],
        {"website_probe": "https://www.tesla.com"})
    assert any("never scraped" in r.note for r in refs)
    assert any(r.company == "Tesla" for r in roles)
    assert any("tesla.com" in r.url for r in refs)

def test_roles_require_same_sentence():
    # "co-founder" belongs to an Inflection sentence WITHOUT the person's
    # name: it must not leak onto the Microsoft role (old blob-wide bug).
    docs = [_doc("d1", "Satya Nadella",
                 "Satya Nadella is the CEO of Microsoft. "
                 "He was named co-founder of Inflection AI in 2022.")]
    roles = roles_from_evidence("Satya Nadella", docs)
    ms = [r for r in roles if r["company"] == "Microsoft"][0]
    assert "CEO" in ms["role"]
    assert "CO-FOUNDER" not in ms["role"] and "FOUNDER" not in ms["role"]
