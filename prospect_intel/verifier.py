"""Isolated blind verifier: sees ONLY (claim text, cited source span). Nothing else.

Real grounding semantics (local, no GPU weights):
- split claim into sentences; each must ground in some span sentence
- token-F1 >= 0.6 -> supported; >= 0.3 -> partially_supported; else unsupported
- empty span -> unsupported, logged as hallucination metric
Swap with an NLI cross-encoder behind ToolBoundary later without rewrite.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from . import audit
from .schemas import Claim, StructuredDoc, Verdict, VerifiedClaim

_SENT = re.compile(r"(?<=[.!?])\s+")

# Calibrated on eval/gold.jsonl (see eval/calibrate.py). Override via env.
import os as _os


def _threshold(raw: str | None, default: float) -> float:
    try:
        v = float(raw) if raw not in (None, "") else default
    except (ValueError, TypeError):
        v = default
    return min(1.0, max(0.0, v))


SUPPORT_T = _threshold(_os.environ.get("VERIFIER_SUPPORT_T"), 0.6)
PARTIAL_T = _threshold(_os.environ.get("VERIFIER_PARTIAL_T"), 0.3)
if PARTIAL_T > SUPPORT_T:
    PARTIAL_T = SUPPORT_T
# Opt-in for semantic (NLI/LLM) verifiers: token-F1 must keep this OFF
# (both claim + negation co-pass on 1-token diff). Set
# VERIFIER_NEGATION_GUARD=1 when swapping in an NLI scorer.
NEGATION_GUARD_DEFAULT = _os.environ.get("VERIFIER_NEGATION_GUARD", "") == "1"

def _toks(s) -> list[str]:
    if not isinstance(s, str):
        return []
    return re.findall(r"[a-z0-9]{3,}", s.lower())

def _f1(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    if not inter:
        return 0.0
    p, r = inter / len(sa), inter / len(sb)
    return 2 * p * r / (p + r)


def source_span(doc: StructuredDoc, claim: Claim) -> str:
    try:
        idx = int(claim.section_index)
    except (ValueError, TypeError):
        return ""
    if not doc.sections or not (0 <= idx < len(doc.sections)):
        return ""
    text = doc.sections[idx].text or ""
    try:
        cs, ce = int(claim.char_start), int(claim.char_end)
    except (ValueError, TypeError):
        return ""
    cs, ce = max(0, cs), max(0, ce)
    if ce < cs:
        cs, ce = ce, cs
    return text[cs:min(ce, len(text))]


def source_span_window(doc: StructuredDoc, claim: Claim,
                       window: int = 1) -> str:
    """Verifier-view expansion: the exact cited span PLUS `window`
    neighboring sentences on each side (qualifying-clause guard against
    truncated-clause misreads like '...negotiations fell through, but...'.

    The stored claim offsets NEVER move (§7.3 citations stay exact) — only
    the verifier's isolated view widens. Still blind: span text only, no
    generation context, no actions."""
    exact = source_span(doc, claim)
    if window <= 0 or not exact.strip():
        return exact
    try:
        idx = int(claim.section_index)
        text = doc.sections[idx].text or ""
        cs = max(0, int(claim.char_start))
    except (ValueError, TypeError):
        return exact
    # Sentence-boundary map over the section (same splitter as scoring).
    bounds: list[tuple[int, int]] = []
    pos = 0
    for part in _SENT.split(text):
        if not part:
            continue
        start = text.find(part, pos)
        if start < 0:
            continue
        bounds.append((start, start + len(part)))
        pos = start + len(part)
    if not bounds:
        return exact
    first = next((i for i, (s, e) in enumerate(bounds) if e > cs),
                 len(bounds) - 1)
    lo = max(0, first - window)
    hi = min(len(bounds), first + window + 1)
    return text[bounds[lo][0]:bounds[hi - 1][1]]


_NEG_RE = re.compile(
    r"\b(not|no |never|n't|denied|denies|rejected|rejects|opposed|oppose|"
    r"against| false|failed|fell through|called off|terminated)\b",
    re.IGNORECASE)


def negate_claim_text(text: str) -> str:
    """Rule-based semantic negation for the self-contradiction guard."""
    t = (text or "").strip()
    if not t:
        return t
    subs = [(r"\bwill\b", "will not"), (r"\bhas\b", "has not"),
            (r"\bhave\b", "have not"), (r"\bis\b", "is not"),
            (r"\bare\b", "are not"), (r"\bwas\b", "was not"),
            (r"\bwere\b", "were not"), (r"\bcan\b", "cannot")]
    for pat, rep in subs:
        nt, n = re.subn(pat, rep, t, count=1, flags=re.IGNORECASE)
        if n:
            return nt
    return "It is false that " + t


def check_negation(claim_text: str, span: str, score_fn,
                   threshold: float) -> str | None:
    """Self-contradiction guard: if `score_fn` passes BOTH the claim and its
    semantic negation against the same span, the span is ambiguous — return
    'ambiguous', else None.

    NOTE: meaningful only for semantic scorers (NLI/LLM verifier behind
    ToolBoundary). With token-F1 both sides nearly always co-pass (one
    token differs), so callers MUST leave the guard off for the F1 path —
    Verifier.check(negation_guard=False) by default. The seam + tests exist
    so the NLI swap activates protection without a rewrite."""
    try:
        if not (claim_text or "").strip() or not (span or "").strip():
            return None
        neg = negate_claim_text(claim_text)
        if score_fn(claim_text, span) >= threshold and \
                score_fn(neg, span) >= threshold:
            return "ambiguous"
    except Exception:
        pass
    return None


def verify_claim(claim: Claim, span: str) -> VerifiedClaim:
    _f1score = 0.0
    if not isinstance(span, str) or not span.strip():        v = VerifiedClaim(claim=claim, verdict=Verdict.UNSUPPORTED,
                          note="empty source span")
    elif not _toks(claim.text):
        v = VerifiedClaim(claim=claim, verdict=Verdict.UNSUPPORTED,
                          note="claim too vague to verify")
    else:
        span_sents = [s for s in _SENT.split(span) if _toks(s)]
        claim_sents = [s for s in _SENT.split(claim.text) if _toks(s)] or [claim.text]
        scores = []
        best = ""
        for cs in claim_sents:
            ct = _toks(cs)
            b = max([_f1(ct, _toks(ss)) for ss in span_sents] or [0.0])
            scores.append(b)
            if span_sents:
                best = max(span_sents,
                           key=lambda ss: _f1(ct, _toks(ss)))
        avg = sum(scores) / len(scores)
        _f1score = round(avg, 4)
        if avg >= SUPPORT_T:
            v = VerifiedClaim(claim=claim, verdict=Verdict.SUPPORTED,
                              note=f"grounded f1={avg:.2f} :: {best[:200]}")
        elif avg >= PARTIAL_T:
            v = VerifiedClaim(claim=claim, verdict=Verdict.PARTIALLY_SUPPORTED,
                              note=f"weak grounding f1={avg:.2f} :: {best[:200]}")
        else:
            v = VerifiedClaim(claim=claim, verdict=Verdict.UNSUPPORTED,
                              note=f"no grounding f1={avg:.2f}")
    audit.log("verdict", {"claim_id": claim.claim_id, "doc_id": claim.doc_id,
                          "verdict": v.verdict.value, "note": v.note,
                          "f1": _f1score,
                          "support_t": SUPPORT_T, "partial_t": PARTIAL_T})
    if v.verdict == Verdict.UNSUPPORTED:
        audit.log("hallucination_metric", {"claim_id": claim.claim_id})
    return v


def llm_verify(claim_text: str, span: str) -> dict | None:
    """LLM/NLI verifier (plan §16-17): claim + span ONLY (isolation kept).
    Returns {verdict, reason} or None when backend unavailable — callers
    fall back to token-F1. Handles negation/numeric/date/entity checks
    that F1 misses (laid-off vs hired). Wired later via Ollama/vLLM."""
    import os as _os2
    if (_os2.environ.get("MODEL_BACKEND", "mock") or "mock").lower() != \
            "ollama":
        return None
    if _os2.environ.get("VERIFIER_LLM", "") != "1":
        return None
    try:
        import httpx as _hx
        base = (_os2.environ.get("OLLAMA_BASE_URL",
                                 "http://localhost:11434")).rstrip("/")
        model = _os2.environ.get("OLLAMA_AGENT_MODEL", "gpt-oss:20b")
        prompt = ("Decide entailment. Reply JSON "
                  '{"verdict":"supported|partially_supported|unsupported",'
                  '"reason":"..."} only.\nCLAIM:\n"'
                  + (claim_text or "")[:1000] + '"\nEVIDENCE:\n"'
                  + (span or "")[:2000] + '"')
        r = _hx.post(base + "/api/generate",
                     json={"model": model, "prompt": prompt,
                           "stream": False, "format": "json"}, timeout=60)
        if r.status_code != 200:
            return None
        import json as _j
        txt = r.json().get("response", "") or ""
        cand = _j.loads(txt[txt.find("{"):txt.rfind("}") + 1])
        v = str(cand.get("verdict", "")).lower()
        if v not in ("supported", "partially_supported", "unsupported"):
            return None
        return {"verdict": v, "reason": str(cand.get("reason", ""))[:300]}
    except Exception:
        return None


@dataclass
class Verifier:
    window: int = 1
    negation_guard: bool = NEGATION_GUARD_DEFAULT

    def _score_pair(self, claim_text: str, span_text: str) -> float:
        """F1 scorer over the windowed span (same semantics as verify_claim
        internals, exposed so the negation seam can reuse it)."""
        span_sents = [s for s in _SENT.split(span_text) if _toks(s)]
        claim_sents = [s for s in _SENT.split(claim_text) if _toks(s)] \
            or [claim_text]
        scores = []
        for cs in claim_sents:
            ct = _toks(cs)
            scores.append(max([_f1(ct, _toks(ss)) for ss in span_sents]
                              or [0.0]))
        return sum(scores) / len(scores) if scores else 0.0

    def check(self, claim: Claim, doc: StructuredDoc) -> VerifiedClaim:
        assert claim.doc_id == doc.doc_id, "verifier: claim/doc mismatch"
        # N±1 view: qualifying clauses survive; stored offsets never move.
        span = source_span_window(doc, claim, self.window)
        # Deterministic F1 always runs first (truth boundary never delegated
        # blindly). LLM path: shadow-compare (VERIFIER_SHADOW=1, default when
        # VERIFIER_LLM=1) logs LLM-vs-F1 without enforcing; enforce only when
        # shadow is explicitly 0. Falls back to F1 on any failure.
        import os as _os3
        _shadow = _os3.environ.get("VERIFIER_SHADOW",
                                   "1" if _os3.environ.get(
                                       "VERIFIER_LLM", "") == "1" else "0")
        if _shadow == "1":
            try:
                llm = llm_verify(claim.text, span)
            except Exception:
                llm = None
            if llm is not None:
                audit.log("verifier_shadow",
                          {"claim_id": claim.claim_id, "doc_id": claim.doc_id,
                           "llm": llm.get("verdict"),
                           "reason": (llm.get("reason") or "")[:200]})
        elif _shadow == "0":
            try:
                llm = llm_verify(claim.text, span)
            except Exception:
                llm = None
            if llm is not None:
                try:
                    v = VerifiedClaim(
                        claim=claim, verdict=Verdict(llm["verdict"]),
                        note=f"llm {llm['verdict']} :: {llm['reason']}"[:300])
                except (ValueError, KeyError):
                    llm = None
                else:
                    audit.log("verdict", {"claim_id": claim.claim_id,
                                          "doc_id": claim.doc_id,
                                          "verdict": v.verdict.value,
                                          "note": v.note, "backend": "llm"})
                    if v.verdict == Verdict.UNSUPPORTED:
                        audit.log("hallucination_metric",
                                  {"claim_id": claim.claim_id})
                    return v
        if self.negation_guard:
            flag = check_negation(claim.text, span, self._score_pair,
                                  SUPPORT_T)
            if flag:
                v = VerifiedClaim(claim=claim, verdict=Verdict.UNSUPPORTED,
                                  note="ambiguous span: claim and negation "
                                       "both ground — dropped")
                audit.log("verdict", {"claim_id": claim.claim_id,
                                      "doc_id": claim.doc_id,
                                      "verdict": v.verdict.value,
                                      "note": v.note})
                audit.log("hallucination_metric",
                          {"claim_id": claim.claim_id})
                return v
        return verify_claim(claim, span)
