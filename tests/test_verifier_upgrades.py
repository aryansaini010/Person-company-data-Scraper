"""Verifier N±1 window, negation seam, temporal-intent labeling."""
from prospect_intel.schemas import (Claim, DocSection, FetchStatus, SourceClass,
                                    StructuredDoc, Verdict)
from prospect_intel.verifier import (Verifier, check_negation, negate_claim_text,
                                     source_span_window)


def _doc(text):
    return StructuredDoc(doc_id="d1", url="https://x/1", url_final="https://x/1",
                         content_hash="h", fetched_at="2026-01-01T00:00:00Z",
                         fetch_status=FetchStatus.OK,
                         source_class=SourceClass.NEWS,
                         sections=[DocSection(section_id="d1#s0", text=text,
                                              char_start=0, char_end=len(text))])


def test_window_keeps_offsets_but_adds_context():
    text = "Negotiations fell through, but the company acquired Startup X. Shares rose."
    doc = _doc(text)
    start = text.index("the company acquired")
    c = Claim(claim_id="c1", text="the company acquired Startup X.",
              doc_id="d1", section_index=0, char_start=start,
              char_end=start + len("the company acquired Startup X."))
    from prospect_intel.verifier import source_span
    assert source_span(doc, c) == "the company acquired Startup X."
    wide = source_span_window(doc, c, 1)
    assert "Negotiations fell through" in wide  # qualifying clause visible
    assert c.char_start == start  # stored offsets never move


def test_negation_seam_off_by_default_on_for_nli():
    assert "not" in negate_claim_text("Revenue will grow.").lower()
    # F1-style scorer co-passes both sides: guard must stay OFF by default.
    v = Verifier().check(
        Claim(claim_id="c1", text="Revenue grew 30 percent.", doc_id="d1",
              section_index=0, char_start=0, char_end=24),
        _doc("Revenue grew 30 percent year over year."))
    assert v.verdict == Verdict.SUPPORTED  # default path unaffected
    # With a semantic scorer, double-pass flags ambiguity.
    flag = check_negation("Revenue grew.", "Revenue grew 30 percent.",
                          lambda claim, span: 0.9, 0.6)
    assert flag == "ambiguous"
    # Discriminating scorer (negation fails) → no flag.
    assert check_negation("Revenue grew.", "Revenue grew 30 percent.",
                          lambda claim, span: 0.9 if "false" not in claim
                          else 0.1, 0.6) is None
    # Negation-guard path drops on ambiguity.
    vn = Verifier(negation_guard=True)
    import prospect_intel.verifier as V
    orig = V.check_negation
    try:
        V.check_negation = lambda *a, **k: "ambiguous"
        v2 = vn.check(
            Claim(claim_id="c1", text="Revenue grew.", doc_id="d1",
                  section_index=0, char_start=0, char_end=13),
            _doc("Revenue grew."))
        assert v2.verdict == Verdict.UNSUPPORTED and "ambiguous" in v2.note
    finally:
        V.check_negation = orig


def test_historical_intent_capped(monkeypatch):
    import prospect_intel.audit as _A
    monkeypatch.setattr(_A, "log", lambda e, p: {})
    from prospect_intel.models import ToolBoundary
    from prospect_intel.passes import pass3_strategy
    text = ("Zee Entertainment will launch its European service by Q3 2021 "
            "to expand platform reach.")
    doc = _doc(text)
    doc.published_at = "2021-06-01"
    verified, _ = pass3_strategy([doc], ToolBoundary(), Verifier(),
                                 "", "Zee Entertainment")
    assert verified, "intent claim must survive as labeled, not vanish"
    assert verified[0].verdict == Verdict.PARTIALLY_SUPPORTED
    assert "historical intent" in verified[0].claim.recency
