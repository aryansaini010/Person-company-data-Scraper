"""Verifier calibration: sweep thresholds over eval/gold.jsonl.

Key metric per the plan ("fluent and wrong is worse than absent"):
HALLUCINATION ESCAPE = unsupported gold predicted as supported/partial.
That error loses deals; missing a supported claim only loses coverage.
Report picks the lowest escape rate, breaking ties by accuracy.
"""
from __future__ import annotations
import itertools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import prospect_intel.verifier as V
from prospect_intel.schemas import Claim


def load_gold():
    rows = []
    for line in (Path(__file__).parent / "gold.jsonl").read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def run_once(rows, support_t, partial_t):
    V.SUPPORT_T, V.PARTIAL_T = support_t, partial_t
    correct = escape = 0
    misses = []
    for i, r in enumerate(rows):
        span = r["span"]
        claim = Claim(claim_id=f"g{i}", text=r["claim"], doc_id="golddoc",
                      section_index=0, char_start=0, char_end=len(span))
        # bypass audit spam during sweep
        got = V.verify_claim.__wrapped__(claim, span) if hasattr(
            V.verify_claim, "__wrapped__") else _quiet(claim, span)
        if got == r["expected"]:
            correct += 1
        else:
            misses.append((r["expected"], got, r["claim"][:80]))
        if r["expected"] == "unsupported" and got in ("supported",
                                                      "partially_supported"):
            escape += 1
    return correct / len(rows), escape, misses


def _quiet(claim, span):
    import prospect_intel.audit as A
    real = A.log
    A.log = lambda e, p: {}
    try:
        return V.verify_claim(claim, span).verdict.value
    finally:
        A.log = real


if __name__ == "__main__":
    rows = load_gold()
    print(f"gold pairs: {len(rows)}")
    results = []
    for s, p in itertools.product([0.5, 0.6, 0.7], [0.2, 0.3, 0.4]):
        if p >= s:
            continue
        acc, esc, _ = run_once(rows, s, p)
        results.append((esc, -acc, s, p, acc))
        print(f"  support>={s} partial>={p}: accuracy={acc:.2f} escapes={esc}")
    results.sort()
    esc, neg_acc, s, p, acc = results[0]
    print(f"BEST: support>={s} partial>={p} accuracy={acc:.2f} escapes={esc}")
    _, _, misses = run_once(rows, s, p)
    for exp, got, txt in misses:
        print(f"  miss: expected={exp} got={got} :: {txt}")
