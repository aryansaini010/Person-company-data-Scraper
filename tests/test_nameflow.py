"""Bare-name flow: org inference + CSV export."""
from prospect_intel.export import brief_csv_path, export_brief
from prospect_intel.passes import infer_companies
from prospect_intel.schemas import (Brief, DocSection, PersonIdentity,
                                    SourceClass, StructuredDoc)

def _doc(doc_id, title, text):
    return StructuredDoc(doc_id=doc_id, url="https://x", url_final="https://x",
        content_hash="h", fetched_at="2026-01-01T00:00:00Z",
        source_class=SourceClass.OTHER, title=title,
        sections=[DocSection(section_id=doc_id + "#s0", text=text,
                             char_start=0, char_end=len(text))])

def test_infer_companies_from_role_evidence():
    docs = [_doc("d1", "Elon Musk", "Elon Musk is the CEO of Tesla and founder of SpaceX. "
                 "Elon Musk said Tesla will expand."),
            _doc("d2", "Other", "Nothing relevant here.")]
    got = infer_companies("Elon Musk", docs)
    orgs = [o for o, _, _ in got]
    assert "Tesla" in orgs and "SpaceX" in orgs
    assert all(c > 0 for _, c, _ in got)

def test_infer_companies_empty_without_evidence():
    assert infer_companies("Nobody Here", [_doc("d9", "T", "Unrelated text.")]) == []

def test_csv_export_has_contract_rows(tmp_path):
    b = Brief(person=PersonIdentity(full_name="N", company="C",
                                    human_confirmed=True),
              firmographic={"company": "C"}, gaps=["no strategy signal found"])
    from prospect_intel.passes import build_output_contract
    b = build_output_contract(b, ["cap A"])
    p = brief_csv_path("N-C", outdir=tmp_path)
    export_brief(b, {}, p)
    text = p.read_text(encoding="utf-8-sig")
    assert "section,field,value,evidence,recency,confidence,source_url,span" in text
    assert "unknown" in text and "opening_question" in text

def test_csv_cells_have_no_newlines_and_readable_report(tmp_path):
    from prospect_intel.export import export_readable
    b = Brief(person=PersonIdentity(full_name="N", company="C",
                                    human_confirmed=True),
              firmographic={"k": "a\nb"}, gaps=["g1"])
    p = brief_csv_path("N-C", outdir=tmp_path)
    export_brief(b, {}, p)
    rows = list(__import__("csv").reader(
        p.read_text(encoding="utf-8-sig").splitlines()))
    assert all("\n" not in c for r in rows for c in r)
    t = export_readable(b, {}, tmp_path / "r.txt")
    txt = t.read_text(encoding="utf-8")
    assert txt.startswith("BRIEF: N @ C") and "[Unknowns]" in txt
