"""Evidence fusion + freshness engine (plan §8, §12).

Fusion sits between retrieval and report generation: clusters verified
claims into signals {signal, evidence_count, sources, recency, confidence}.
News NEVER becomes a priority alone — it needs cross-source corroboration
(news + filing/job/exec) before promotion.

Freshness buckets: 0-7 Very Recent, 8-30 Recent, 31-90 Current,
3-12mo Background, >12mo Historical. Replaces binary 2yr gate with weights.
"""
from __future__ import annotations
import re

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def freshness_bucket(recency: str | None) -> str:
    """Map recency tag to freshness level. Undated -> Background (not Recent)."""
    import datetime as _dt
    r = (recency or "").strip()
    m = _YEAR_RE.search(r)
    if not m:
        return "Background"
    try:
        year = int(m.group(0))
        month = 6
        m2 = re.search(r"(\d{4})-(\d{2})", r)
        if m2:
            year, month = int(m2.group(1)), int(m2.group(2))
        now = _dt.date.today()
        age_days = (now - _dt.date(year, max(1, min(12, month)), 1)).days
    except Exception:
        return "Background"
    if age_days <= 7:
        return "Very Recent"
    if age_days <= 30:
        return "Recent"
    if age_days <= 90:
        return "Current"
    if age_days <= 365:
        return "Background"
    return "Historical"


def fuse_signals(verified) -> list[dict]:
    """Cluster verified claims by token overlap into fused signals.

    Each signal: {signal, evidence_count, sources, recency, confidence}.
    Confidence high only with >=2 sources from different docs; single-news
    signals stay low/medium — never auto-priority.
    """
    groups: list[dict] = []
    for v in verified or []:
        try:
            c = v.claim
            text, doc = c.text or "", c.doc_id or ""
            rec = getattr(c, "recency", "") or ""
        except AttributeError:
            continue
        toks = set(re.findall(r"[a-z0-9]{4,}", text.lower()))
        placed = False
        for g in groups:
            overlap = len(toks & g["_toks"])
            if overlap >= 3:
                g["evidence_count"] += 1
                if doc and doc not in g["sources"]:
                    g["sources"].append(doc)
                g["_toks"] |= toks
                # keep freshest recency label
                order = ["Historical", "Background", "Current",
                         "Recent", "Very Recent"]
                try:
                    if order.index(freshness_bucket(rec)) > order.index(
                            g["recency"]):
                        g["recency"] = freshness_bucket(rec)
                except ValueError:
                    pass
                placed = True
                break
        if not placed:
            groups.append({"signal": text[:200], "evidence_count": 1,
                           "sources": [doc] if doc else [],
                           "recency": freshness_bucket(rec),
                           "_toks": set(toks)})
    out = []
    for g in groups:
        n_src = len(g["sources"])
        conf = ("high" if g["evidence_count"] >= 3 and n_src >= 2
                else "medium" if n_src >= 2 or g["evidence_count"] >= 2
                else "low")
        out.append({"signal": g["signal"],
                    "evidence_count": g["evidence_count"],
                    "sources": g["sources"][:5], "recency": g["recency"],
                    "confidence": conf})
    out.sort(key=lambda s: (-s["evidence_count"], -len(s["sources"])))
    return out
