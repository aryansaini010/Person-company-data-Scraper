"""v1.1 Phase 1.5 acceptance (§9.5): Appendix A trivia dies at the relevance
gate, genuine priorities survive; recency from published_at; no placeholder
in capability_map; pitch untruncated; unknowns per-field."""
from prospect_intel.passes import (build_output_contract, firmographic_unknowns,
                                   relevance_gate, synthesize_pitch,
                                   _gist, _recency_of)
from prospect_intel.schemas import (Brief, DocSection, PersonIdentity,
                                    SourceClass, StructuredDoc)

TRIVIA = [
    'Sharpe and Man Mohan Sharma because they are "the kind of professors '
    'who made you think out of the box."',
    "By Securities and Exchange Board of India directive, RIL carried out an "
    "organised operation to obtain unauthorised profits from trading of RPL.",
    'Launching for Indian women, Her Circle will provide a "joyful and safe '
    "space for interaction, engagement and collaboration.",
    "Ambani directed the creation of the world's largest grassroots "
    "petroleum refinery in Jamnagar producing 660,000 barrels per day.",
    "Mukesh Ambani set up Reliance Infocomm Limited, focused on information "
    "and communications technology initiatives.",
]
GENUINE = [
    "Mukesh Ambani unveils 5-way roadmap to propel RIL's growth ahead.",
    "AI Summit: AI won't kill work, says Mukesh Ambani amid job market panic.",
    "JioMart's Brand Strategy: Integrating Online and Offline Retail.",
    "RIL climbs 3 spots ranking 85 in Fortune Global 500.",
]

def test_trivia_rejected_genuine_kept():
    for t in TRIVIA:
        keep, _ = relevance_gate(t)
        assert not keep, f"trivia passed gate: {t[:60]}"
    for g in GENUINE:
        keep, _ = relevance_gate(g)
        assert keep, f"genuine dropped: {g[:60]}"
    keep, _ = relevance_gate(
        'Named among "India\'s Best CEOs" by Fortune India 2025 list.')
    assert not keep, "awards-listicle passed gate"
    from prospect_intel.firecrawl import clean_markdown
    assert "--------------------" not in clean_markdown("Awards\n----\nReal text.")
    assert "Vslide" not in clean_markdown("**Vslide:** slide6: podium\nReal line.")
    assert "**" not in clean_markdown("### **Head**\nBody.")
    keep, _ = relevance_gate("What role does digital play in strategy?")
    assert not keep, "SEO question passed gate"
    keep, _ = relevance_gate("Mukesh Ambani standing at the podium as applause "
                             "filled the transcript of the slides ceremony.")
    assert not keep, "transcript narration passed gate"
    from prospect_intel.discovery import _filter_hits
    from prospect_intel.search import SearchHit
    kept, _refs = _filter_hits([SearchHit(url="https://in.linkedin.com/in/x"),
                                SearchHit(url="https://example.com/a")])
    assert [h.url for h in kept] == ["https://example.com/a"]

def _doc(doc_id, text, published=""):
    return StructuredDoc(doc_id=doc_id, url=f"https://x/{doc_id}",
        url_final=f"https://x/{doc_id}",
        content_hash="h", fetched_at="2026-01-01T00:00:00Z",
        fetch_status="ok", source_class=SourceClass.NEWS, published_at=published,
        sections=[DocSection(section_id=doc_id + "#s0", text=text,
                             char_start=0, char_end=len(text))])

def test_recency_from_published_at_never_mined():
    assert _recency_of(_doc("d", "born 1957, expanded in 2010",
                            "Sat, 20 Jun 2026 07:00:00 GMT")) == "2026"
    assert _recency_of(_doc("d", "born 1957, expanded in 2010", "")) == "undated"

def test_placeholder_collateral_fails_closed():
    b = Brief(person=PersonIdentity(full_name="N", company="C",
                                    human_confirmed=True),
              firmographic={})
    from prospect_intel.schemas import Claim, VerifiedClaim, Verdict
    v = VerifiedClaim(
        claim=Claim(claim_id="c1", text="RIL plans 5-way growth roadmap",
                    doc_id="d", section_index=0, char_start=0, char_end=10,
                    recency="2026"), verdict=Verdict.SUPPORTED)
    b.strategy_signals = [v]
    b = build_output_contract(b, ["[DEFAULT - replace] OTT ad inventory"])
    assert b.capability_map == []
    assert any("no mapped capability" in g for g in b.gaps)

def test_pitch_and_question_are_clean():
    assert "Outlet:" not in _gist("Roadmap to growth - economictimes.com\n"
                                   "Outlet: ET\nPublished: 2026")
    assert '"' not in _gist("Roadmap to growth - economictimes.com")
    from prospect_intel.schemas import Claim, VerifiedClaim, Verdict
    vs = [VerifiedClaim(
        claim=Claim(claim_id=f"c{i}", text=t, doc_id="d", section_index=0,
                    char_start=0, char_end=len(t), recency="2026"),
        verdict=Verdict.SUPPORTED) for i, t in enumerate(GENUINE[:3])]
    pitch = synthesize_pitch(vs)
    assert len(pitch) <= 600 and not pitch.rstrip(".…").endswith("communit")
    assert pitch.endswith((".", "…"))

def test_unknowns_per_field_and_tier1_distinction():
    gaps, degraded = firmographic_unknowns(
        {"company": "C", "registry": "unverified-manual-check", "filings": [],
         "funding": "unknown", "website_probe": "none",
         "probe_status": "unreachable: ConnectTimeout"}, [])
    fields = [g.split(":")[0] for g in gaps]
    assert fields == ["company.registry", "company.filings",
                      "company.funding", "company.website_probe"]
    assert any("connector failure" in d for d in degraded)

def test_boilerplate_disclaimer_rejected():
    keep, _ = relevance_gate(
        "The content of this webpage is not investment advice and does not "
        "constitute any offer or solicitation of any investment product.")
    assert not keep, "legal disclaimer passed gate"


def test_pass3_end_to_end_kills_trivia(monkeypatch):
    import prospect_intel.audit as _A
    monkeypatch.setattr(_A, "log", lambda e, p: {})
    from prospect_intel.models import ToolBoundary
    from prospect_intel.passes import pass3_strategy
    from prospect_intel.verifier import Verifier
    docs = [_doc(f"d{i}", t, "Sat, 20 Jun 2026 07:00:00 GMT")
            for i, t in enumerate(TRIVIA + GENUINE)]
    verified, _ = pass3_strategy(docs, ToolBoundary(), Verifier(), "Mukesh Ambani")
    texts = " ".join(v.claim.text for v in verified)
    assert "professor" not in texts and "Her Circle" not in texts
    assert "refinery" not in texts and "Infocomm" not in texts
    assert "roadmap" in texts  # genuine coverage kept
