"""Hiring sub-schema/velocity + transcript Q&A scoping (deterministic)."""
from prospect_intel.passes import (
    attach_hiring_velocity, job_velocity, parse_job_posting,
    transcript_qa_boost, transcript_zones)
from prospect_intel.schemas import (DocSection, FetchStatus, SourceClass,
                                    StructuredDoc)


def _doc(doc_id, text, sc=SourceClass.JOB_POSTING):
    return StructuredDoc(doc_id=doc_id, url=f"https://x/{doc_id}",
                         url_final=f"https://x/{doc_id}", content_hash="h",
                         fetched_at="2026-01-01T00:00:00Z",
                         fetch_status=FetchStatus.OK, source_class=sc,
                         sections=[DocSection(section_id=doc_id + "#s0",
                                              text=text, char_start=0,
                                              char_end=len(text))])


def test_parse_job_posting_fields():
    p = parse_job_posting("Senior Backend Engineer (Python, AWS) in Bengaluru. "
                          "Build streaming platform. Apply now.")
    assert p["role_department"] == "engineering"
    assert p["seniority_level"] == "senior"
    assert p["geo_location"] in ("bengaluru", "bangalore")
    assert "python" in p["tech_stack_or_specialty"]


def test_parse_job_posting_boilerplate_only():
    assert parse_job_posting("We are an equal opportunity employer. "
                             "Benefits include health.") == {}


def test_job_velocity_counts_with_evidence():
    docs = [_doc("j1", "Senior Python Engineer in Bengaluru, build OTT stack."),
            _doc("j2", "Junior Data Analyst in Mumbai, SQL dashboards."),
            _doc("n1", "Zee launches new channel.", SourceClass.NEWS)]
    v = job_velocity(docs)
    assert v and v["postings"] == 2 and len(v["evidence"]) == 2
    assert "2 open postings" in v["line"]
    assert job_velocity(docs[:1]) is None  # single posting: anecdote


def test_attach_adds_firmo_key():
    docs = [_doc("j1", "Senior Python Engineer in Bengaluru."),
            _doc("j2", "Staff Go Engineer in Mumbai.")]
    firmo = attach_hiring_velocity(docs, {"company": "Acme"})
    assert "hiring_velocity" in firmo and "evidence:" in firmo["hiring_velocity"]
    assert attach_hiring_velocity([], {"company": "Acme"}) == {"company": "Acme"}


def test_transcript_zones_split_and_boost():
    body = ("Safe harbor statement. CEO: revenue grew. "
            "We will now take questions. Operator: first question. "
            "Analyst: what drove margins? CEO: pricing power.")
    zones = transcript_zones(body)
    assert [z for _, _, z in zones] == ["prepared", "qa"]
    d = _doc("t1", body, SourceClass.TRANSCRIPT)
    d.title = "Q2 earnings call transcript"
    assert transcript_qa_boost(d, len(body) - 10) == 0.15
    assert transcript_qa_boost(d, 5) == 0.0
    assert transcript_qa_boost(
        _doc("g1", "Generic page text."), 5) == 0.0
