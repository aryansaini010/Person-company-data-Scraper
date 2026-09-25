"""Ops metrics that matter (§10.1), computed from the hash-chained run log.

- fetch classification distribution (leading indicator of degradation)
- verifier rejection rate (model/retrieval/corpus drift signal)
- wall-clock per brief (unit economics)
- human override rate at Pass 1 (entity-resolution quality)
"""
from __future__ import annotations
from collections import Counter


def _zeroed() -> dict:
    return {"fetch_classification": {}, "verdicts": {},
            "verifier_rejection_rate": 0.0,
            "relevance_gate_rejection_rate": 0.0, "tier1_alerts": 0,
            "briefs_created": 0, "pass1_proposed": 0, "pass1_confirmed": 0,
            "hallucination_drops": 0, "verifier_shadow_checks": 0,
            "ambiguous_drops": 0,
            "verifier_thresholds": {"support_t": 0.6, "partial_t": 0.3},
            "verifier_f1_avg": 0.0}


def compute() -> dict:
    try:
        from . import store
        con = store.connect()
        rows = con.execute("SELECT event, payload, ts FROM audit_log").fetchall()
        con.close()
    except Exception:
        return _zeroed()
    ev = Counter(e for e, _, _ in rows)
    fetch_dist = Counter()
    for e, p, _ in rows:
        if e == "fetch_rejected":
            import json
            try:
                fetch_dist[str(json.loads(p).get("status", "?")).lower()] += 1
            except Exception:
                pass
        if e == "duplicate_content":
            fetch_dist["near_duplicate"] += 1
        if e == "fetched":
            fetch_dist["ok"] += 1
    verdicts = Counter()
    _f1s: list[float] = []
    _support_t = 0.6
    _partial_t = 0.3
    for e, p, _ in rows:
        if e == "verdict":
            import json
            try:
                _pj = json.loads(p)
                verdicts[str(_pj.get("verdict", "?")).lower()] += 1
                if isinstance(_pj.get("f1"), (int, float)):
                    _f1s.append(float(_pj["f1"]))
                if isinstance(_pj.get("support_t"), (int, float)):
                    _support_t = float(_pj["support_t"])
                if isinstance(_pj.get("partial_t"), (int, float)):
                    _partial_t = float(_pj["partial_t"])
            except Exception:
                pass
    total_v = sum(verdicts.values())
    brief_times = [t for e, _, t in rows if e == "brief_created"]
    rel_rej = sum(1 for e, p, _ in rows
                  if e == "relevance_gate" and '"rejected"' in p)
    rel_tot = sum(1 for e, _, _ in rows if e == "relevance_gate")
    tier1_alerts = sum(1 for e, _, _ in rows if e in ("tier_down", "tier_refused"))
    ambiguous = sum(1 for e, p, _ in rows
                    if e == "verdict" and "ambiguous span" in p)
    return {
        "fetch_classification": dict(fetch_dist),
        "verdicts": dict(verdicts),
        "verifier_rejection_rate": (
            (verdicts.get("unsupported", 0) / total_v) if total_v else 0.0),
        "relevance_gate_rejection_rate": (rel_rej / rel_tot) if rel_tot else 0.0,
        "tier1_alerts": tier1_alerts,
        "briefs_created": ev.get("brief_created", 0),
        "pass1_proposed": ev.get("pass1_proposed", 0) + ev.get("pass1_candidates", 0),
        "pass1_confirmed": ev.get("pass1_confirmed", 0),
        "hallucination_drops": ev.get("hallucination_metric", 0),
        "verifier_shadow_checks": ev.get("verifier_shadow", 0),
        "ambiguous_drops": ambiguous,
        "verifier_thresholds": {"support_t": _support_t,
                                "partial_t": _partial_t},
        "verifier_f1_avg": (sum(_f1s) / len(_f1s)) if _f1s else 0.0,
    }
