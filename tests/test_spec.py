"""Spec MUSTs (§§5,6,8): structuring, paywall, metadata SSRF, scrub,
Tier-3 routing refusal, Pass-1 evidence, §6.5 output contract."""
from prospect_intel import acquisition, security
from prospect_intel.discovery import tier3_commercial
from prospect_intel.fetcher import FetchResult
from prospect_intel.passes import build_output_contract, pass1_candidates
from prospect_intel.schemas import (Brief, PersonIdentity, SourceClass,
                                    StructuredDoc, DocSection)

def _fr(url="https://example.com/a", status=200,
        text="Acme will expand hiring in 2026."):
    return FetchResult(url=url, url_final=url, status_code=status,
                       body_text=text, raw_body=text.encode(), title="T")

def test_structured_record_has_spec_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(acquisition, "SNAPSHOT_DIR", tmp_path)
    doc = acquisition.structure_result(_fr())
    assert doc is not None
    assert doc.url_final == "https://example.com/a"
    assert len(doc.content_hash) == 64
    assert (tmp_path / doc.content_hash).exists()  # §5.2 snapshot
    assert doc.fetch_status.value == "ok"
    assert doc.sections[0].section_id.endswith("#s0")  # §7.3 citable
    assert doc.title == "T"

def test_paywalled_is_distinct_status():
    assert security.classify_fetch(200, "Subscribe to continue reading") == "paywalled"
    assert acquisition.structure_result(
        _fr(text="Subscribe to continue reading")) is None

def test_metadata_endpoints_blocked():
    for u in ["http://169.254.169.254/latest/meta-data",
              "http://metadata.google.internal/",
              "http://100.100.100.200/"]:
        try:
            security.resolve_and_assert_no_ssrf(u)
        except ValueError:
            continue
        raise AssertionError(f"{u} must be blocked")

def test_zero_width_scrubbed():
    assert "​" not in security.scrub_retrieved_text("Acme​ hiring")

def test_tier3_refuses_person_queries():
    r = tier3_commercial("Jane Doe Acme", person_query=True)
    assert r.hits == [] and not r.available

def _doc(doc_id, title, text):
    return StructuredDoc(doc_id=doc_id, url="https://x", url_final="https://x",
        content_hash="h", fetched_at="2026-01-01T00:00:00Z",
        source_class=SourceClass.NEWS, title=title,
        sections=[DocSection(section_id=doc_id+"#s0", text=text,
                             char_start=0, char_end=len(text))])

def test_pass1_candidates_carry_evidence():
    docs = [_doc("d1", "Jane Doe joins Acme", "Jane Doe joins Acme as CTO."),
            _doc("d2", "Acme news", "Acme reported profits.")]
    cands = pass1_candidates("Jane Doe", "Acme", docs)
    assert len(cands) >= 2
    assert cands[0].sources and cands[0].confidence > cands[-1].confidence

def test_output_contract_unknowns_required():
    b = Brief(person=PersonIdentity(full_name="N", company="C",
                                    human_confirmed=True),
              firmographic={}, gaps=["no strategy signal found"])
    b = build_output_contract(b, ["cap A"])
    assert b.gaps  # unknowns[] required §6.5
    assert b.opening_question  # generated even with no signal
    assert b.likely_objection
