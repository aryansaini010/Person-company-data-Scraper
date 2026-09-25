"""CSV export of a verified brief (§6.5 output contract → rows).

One file per brief: person block, firmographics, priorities (each bound to
its evidence claim + source doc + span), capability map, unknowns, degraded.
Only verified-or-flagged claims ever appear — unsupported stays dropped.
"""
from __future__ import annotations
import csv
import re
import time
from pathlib import Path


def brief_csv_path(name: str, outdir: Path | str = "briefs") -> Path:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "brief").lower()).strip("-") or "brief"
    base = out / f"brief_{slug}_{time.strftime('%Y%m%d-%H%M%S')}"
    path = base.with_suffix(".csv")
    n = 1
    while path.exists():  # same-second collision: never overwrite
        n += 1
        path = out / f"{base.name}-{n}.csv"
    return path


def _flat(s, n: int = 500) -> str:
    """Excel-safe cell: no embedded newlines, capped length, no 'None'."""
    if s is None:
        return ""
    if isinstance(s, (list, tuple)):
        s = "; ".join("" if x is None else str(x) for x in s)
    return re.sub(r"\s+", " ", str(s)).strip()[:n]


def _num(x) -> str:
    try:
        return f"{float(x):.2f}"
    except (ValueError, TypeError):
        return ""


def _verdict(v) -> str:
    return getattr(getattr(v, "verdict", None), "value", "") or ""


def export_brief(brief, docs_by_id: dict, path: Path | str) -> Path:
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8-sig") as f:  # BOM: Excel
        w = csv.writer(f)
        w.writerow(["section", "field", "value", "evidence", "recency",
                    "confidence", "source_url", "span"])
        p = brief.person
        w.writerow(["person", "name", _flat(p.full_name, 200),
                    _flat(p.sources or [], 200), "",
                    _num(p.confidence), "", ""])
        w.writerow(["person", "company", _flat(p.company, 200), "", "", "", "", ""])
        w.writerow(["person", "role", _flat(p.role or p.title, 200), "", "", "", "", ""])
        for v in getattr(brief, "person_details", []) or []:
            d = docs_by_id.get(v.claim.doc_id, {})
            url = getattr(d, "url_final", "") or getattr(d, "url", "")
            w.writerow(["person_detail", _verdict(v), _flat(v.claim.text),
                        v.claim.claim_id, v.claim.recency, "", url,
                        f"{v.claim.char_start}-{v.claim.char_end}"])
        for k, v in (brief.firmographic or {}).items():
            w.writerow(["company", _flat(k, 100), _flat(v), "", "", "", "", ""])
        for pr in brief.priorities or []:
            cid = pr.evidence[0] if pr.evidence else ""
            url, span, verdict = "", "", ""
            for v in brief.strategy_signals or []:
                if v.claim.claim_id == cid:
                    d = docs_by_id.get(v.claim.doc_id, {})
                    url = getattr(d, "url_final", "") or getattr(d, "url", "")
                    span = f"{v.claim.char_start}-{v.claim.char_end}"
                    verdict = _verdict(v)
            w.writerow(["priority", verdict or "priority", _flat(pr.statement),
                        cid, pr.recency, _num(pr.confidence), url, span])
        for m in brief.capability_map or []:
            w.writerow(["capability_map", _flat(m.their_priority_ref, 100),
                        _flat(m.our_capability), "", "", "", "", ""])
        if getattr(brief.likely_objection, "statement", ""):
            w.writerow(["objection", _flat(brief.likely_objection.basis, 200),
                        _flat(brief.likely_objection.statement), "", "", "",
                        "", ""])
        for c in brief.contradictions or []:
            w.writerow(["contradiction", f"{c.recency_a} vs {c.recency_b}",
                        _flat(c.claim_a, 300), "", c.recency_a, "", "",
                        ""])
            w.writerow(["contradiction", f"{c.recency_a} vs {c.recency_b}",
                        _flat(c.claim_b, 300), "", c.recency_b, "", "",
                        ""])
        if brief.opening_question:
            w.writerow(["opening_question", "",
                        _flat(brief.opening_question), "", "", "", "", ""])
        for g in brief.gaps or []:
            w.writerow(["unknown", "", _flat(g), "", "", "", "", ""])
        for d_note in brief.degraded or []:
            w.writerow(["degraded", "", _flat(d_note), "", "", "", "", ""])
        for r in brief.current_roles or []:
            w.writerow(["role", _flat(r.role), _flat(r.company),
                        _flat(r.evidence), "", "", "", ""])
        for ref in brief.references or []:
            w.writerow(["reference", _flat(ref.label), _flat(ref.url), "", "",
                        "", "", _flat(ref.note)])
    return path


def export_readable(brief, docs_by_id: dict, path: Path | str) -> Path:
    """Plain-text brief report — open in Notepad, no spreadsheet needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from .passes import staleness_label as _stale
    except Exception:
        def _stale(r):  # fallback: no decay labels, never crash export
            return ""
    L: list[str] = []
    p = brief.person
    L.append(f"BRIEF: {p.full_name} @ {p.company} "
             f"(confidence {_num(p.confidence)})")
    L.append("=" * 60)
    L.append("\n[Company facts]")
    for k, v in (brief.firmographic or {}).items():
        L.append(f"- {k}: {_flat(v, 300)}")
    bio = getattr(brief, "bio", {}) or {}
    if bio:
        L.append("\n[Profile - full bio, not recency-capped]")
        for k in ("full_name", "birth", "parents", "spouse",
                  "children", "education", "career"):
            for val in bio.get(k, []) or []:
                L.append(f"- {k}: {_flat(val, 300)}")
    details = getattr(brief, "person_details", []) or []
    if details:
        L.append("\n[Person details - Google-style verified facts]")
        for i, v in enumerate(details, 1):
            d = docs_by_id.get(v.claim.doc_id, {})
            url = getattr(d, "url_final", "") or getattr(d, "url", "")
            L.append(f"{i}. {_flat(v.claim.text, 400)}")
            L.append(f"   evidence={v.claim.claim_id} "
                     f"recency={v.claim.recency}{_stale(v.claim.recency)} "
                     f"verdict={_verdict(v)}")
            if url:
                L.append(f"   source: {url}")
    L.append("\n[Priorities - each verified against its source]")
    if brief.priorities or []:
        for i, pr in enumerate(brief.priorities or [], 1):
            url = ""
            for v in brief.strategy_signals:
                if pr.evidence and v.claim.claim_id == pr.evidence[0]:
                    d = docs_by_id.get(v.claim.doc_id, {})
                    url = getattr(d, "url_final", "") or getattr(d, "url", "")
            L.append(f"{i}. {_flat(pr.statement, 400)}")
            L.append(f"   evidence={pr.evidence} "
                     f"recency={pr.recency}{_stale(pr.recency)} "
                     f"confidence={_num(pr.confidence)}")
            if url:
                L.append(f"   source: {url}")
    else:
        L.append("(none - see unknowns)")
    L.append("\n[Capability map]")
    for m in brief.capability_map or []:
        L.append(f"- ours: {_flat(m.our_capability, 200)}")
        L.append(f"  theirs: {m.their_priority_ref} ({_flat(m.rationale, 150)})")
    L.append(f"\n[Pitch]\n{_flat(brief.pitch, 1500)}")
    if getattr(brief.likely_objection, "statement", ""):
        L.append(f"\n[Likely objection] {_flat(brief.likely_objection.statement, 400)}")
        L.append(f"  basis: {_flat(brief.likely_objection.basis, 300)}")
    if brief.contradictions or []:
        L.append("\n[Conflicting sources - both shown, neither selected]")
        for c in brief.contradictions[:5]:
            L.append(f"  A ({c.recency_a}): {_flat(c.claim_a, 250)}")
            L.append(f"  B ({c.recency_b}): {_flat(c.claim_b, 250)}")
    if brief.opening_question:
        L.append(f"[Opening question] {_flat(brief.opening_question, 400)}")
    L.append("\n[Unknowns]")
    for g in brief.gaps or ["(none)"]:
        L.append(f"- {_flat(g, 300)}")
    if brief.references:
        L.append("\n[References - open manually]")
        for ref in brief.references[:8]:
            L.append(f"- {ref.label}: {ref.url} ({ref.note})")
    if brief.degraded:
        L.append("\n[Degraded inputs]")
        for d_note in brief.degraded:
            L.append(f"! {_flat(d_note, 300)}")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path
