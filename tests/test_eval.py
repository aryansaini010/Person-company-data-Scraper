"""Gold-set regression: pins calibration results from eval/calibrate.py.

Plan rule ("fluent and wrong is worse than absent") encoded as tests:
- ZERO unsupported gold may verify as fully supported (silent escape).
- accuracy floor 0.70 on the 24-pair set.
"""
import json
import pytest
from pathlib import Path
from prospect_intel.schemas import Claim
from prospect_intel.verifier import Verifier
import prospect_intel.audit as _A


@pytest.fixture(autouse=True)
def _quiet_audit(monkeypatch):
    monkeypatch.setattr(_A, "log", lambda e, p: {})  # keep sweep out of run_log

def _gold():
    rows = []
    for line in (Path(__file__).parent.parent / "eval" / "gold.jsonl").read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows

def _verdict(r, i):
    span = r["span"]
    c = Claim(claim_id=f"g{i}", text=r["claim"], doc_id="golddoc",
              section_index=0, char_start=0, char_end=len(span))
    import prospect_intel.schemas as S
    doc = S.StructuredDoc(doc_id="golddoc", url="https://example.com",
        fetched_at="2026-01-01T00:00:00Z", source_class=S.SourceClass.NEWS,
        sections=[S.DocSection(text=span, char_start=0, char_end=len(span))])
    return Verifier().check(c, doc).verdict.value

def test_no_silent_escapes():
    bad = [(r["claim"][:60], v) for i, r in enumerate(_gold())
           if r["expected"] == "unsupported"
           for v in [_verdict(r, i)] if v == "supported"]
    assert bad == [], f"silent hallucination escapes: {bad}"

def test_accuracy_floor():
    rows = _gold()
    hits = sum(1 for i, r in enumerate(rows) if _verdict(r, i) == r["expected"])
    assert hits / len(rows) >= 0.70, f"accuracy {hits}/{len(rows)}"
