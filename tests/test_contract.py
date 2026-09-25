"""§6.4/§6.5/§7.2/§7.5 contract: objection object, contradictions, corpus
reuse, char budget, circuit breaker."""
from prospect_intel import acquisition, store
from prospect_intel.fetcher import FetchResult
from prospect_intel.passes import (build_output_contract, detect_contradictions)
from prospect_intel.schemas import (Brief, Claim, PersonIdentity, Verdict,
                                    VerifiedClaim)

def _vc(cid, doc, text, recency="", verdict=Verdict.SUPPORTED):
    return VerifiedClaim(
        claim=Claim(claim_id=cid, text=text, doc_id=doc, section_index=0,
                    char_start=0, char_end=len(text), recency=recency),
        verdict=verdict)

def test_objection_has_statement_and_basis():
    b = Brief(person=PersonIdentity(full_name="N", company="C",
                                    human_confirmed=True),
              firmographic={}, gaps=["no signal on AI strategy"])
    b = build_output_contract(b, [])
    assert b.likely_objection.statement and b.likely_objection.basis == "no signal on AI strategy"

def test_contradictions_surface_both_sides():
    vs = [_vc("a", "d1", "Reliance profits grew strongly in 2024", "2024"),
          _vc("b", "d2", "Reliance denied profits grew in 2024 as false", "2024")]
    out = detect_contradictions(vs)
    assert len(out) == 1 and out[0].recency_a == "2024"

def test_contradictions_ignore_same_doc_and_unrelated():
    vs = [_vc("a", "d1", "Reliance profits grew in 2024", "2024"),
          _vc("b", "d1", "Reliance profits grew in 2024", "2024"),
          _vc("c", "d2", "Quantum zebras orbit nightly", "")]
    assert detect_contradictions(vs) == []

def test_finance_headlines_are_not_contradictions():
    vs = [_vc("a", "d1", "Mukesh Ambani unveils 5-way roadmap to propel growth", "2026"),
          _vc("b", "d2", "Reliance share price holds firm despite profit fall", "2026")]
    assert detect_contradictions(vs) == []

def test_corpus_reuse_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "c.db")
    con = store.connect()
    store.doc_save(con, "dx", "https://e.com", "2026-01-01T00:00:00Z",
                   "news", "Reliance Industries expands refining capacity")
    got = store.corpus_search_docs(con, "Reliance refining", 5)
    assert [d.doc_id for d in got] == ["dx"]
    assert got[0].sections[0].char_end == len(got[0].sections[0].text)
    con.close()

def test_acquire_budget_and_circuit(monkeypatch):
    big = FetchResult(url="https://e.com/a", url_final="https://e.com/a",
                      status_code=200, body_text="Acme expands. " * 5000,
                      raw_body=b"x", title="T")
    import prospect_intel.acquisition as A
    import prospect_intel.fetcher as F
    monkeypatch.setattr(F, "fetch_url_smart", lambda u, **k: big)
    monkeypatch.setenv("PROSPECT_CHAR_BUDGET", "1000")  # ignore real .env
    docs = A.acquire(["https://e.com/a", "https://e.com/b"], char_budget=1000)
    assert len(docs) == 1  # budget stops intake after the first doc
    assert docs[0].url == "https://e.com/a"  # order preserved, no merge
    monkeypatch.undo()  # real fetcher: loopbacks refuse via SSRF guard
    bad = A.acquire([f"http://127.0.0.{i}/x" for i in range(1, 7)])
    assert bad == []  # circuit: all refused, nothing invented
