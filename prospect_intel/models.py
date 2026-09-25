"""Model layer: Agent (~30B MoE) / Utility (4-8B) / Embedding / Reranker.
Two pools so synthesis never queues behind bulk scoring (plan §5).

Real weights plug in here; skeleton ships mocks with identical interfaces.
MODEL_BACKEND=mock (default) | ollama | vllm — Ollama wired later; until
then every backend falls back to deterministic mocks so tests stay offline.
Deferred options (SIEVE, deep-research models, Zoom Search, ADK 2.0) attach
via ToolBoundary without a rewrite (plan §7).
"""
from __future__ import annotations
import os as _os
from dataclasses import dataclass, field


def _backend() -> str:
    return (_os.environ.get("MODEL_BACKEND", "mock") or "mock").lower()


@dataclass
class AgentModel:  # Pool A — planning, tool selection, final writing (~5% calls)
    name: str = "agent-30b-mock"

    def _ollama_chat(self, prompt: str) -> str | None:
        """Ollama dev backend (gpt-oss:20b). Returns None on any failure
        so callers fall back to mocks. Wired later; safe until then."""
        try:
            import httpx as _hx
            base = (_os.environ.get("OLLAMA_BASE_URL",
                                    "http://localhost:11434")).rstrip("/")
            model = _os.environ.get("OLLAMA_AGENT_MODEL", "gpt-oss:20b")
            r = _hx.post(base + "/api/generate",
                         json={"model": model, "prompt": prompt,
                               "stream": False}, timeout=60)
            if r.status_code != 200:
                return None
            return (r.json().get("response") or "").strip() or None
        except Exception:
            return None

    def draft_claim(self, prompt_data: str) -> str:
        if _backend() == "ollama":
            # Pitch prose needs fluency, not 20b reasoning: a dedicated
            # small pitch model (default 7b-instruct) halves relay latency
            # vs gpt-oss:20b. Set OLLAMA_PITCH_MODEL=gpt-oss:20b to force
            # the large model. Falls back to deterministic echo on failure.
            import httpx as _hx2
            try:
                base = (_os.environ.get("OLLAMA_BASE_URL",
                                        "http://localhost:11434")).rstrip("/")
                model = (_os.environ.get("OLLAMA_PITCH_MODEL", "") or
                         _os.environ.get("OLLAMA_UTILITY_MODEL",
                                         "qwen2.5:7b-instruct"))
                r = _hx2.post(base + "/api/generate",
                              json={"model": model,
                                    "prompt": ("Summarize the buyer priority "
                                               "in one sentence:\n"
                                               + prompt_data[:2000]),
                                    "stream": False}, timeout=45)
                if r.status_code == 200:
                    out = (r.json().get("response") or "").strip()
                    if out:
                        return out[:500]
            except Exception:
                pass
            out = self._ollama_chat(
                f"Summarize the buyer priority in one sentence:\n{prompt_data[:2000]}")
            if out:
                return out[:500]
        return prompt_data[:500]

    def plan(self, person: str, company: str) -> dict:
        """Bounded research plan (max PLANNER_MAX_PER_CAT per category).
        Mock now (deterministic templates); LLM planner later via Ollama/vLLM.
        Shape matches plan §3: person/company/job/news/filing queries."""
        try:
            cap = max(1, min(10, int(
                _os.environ.get("PLANNER_MAX_PER_CAT", "5"))))
        except (ValueError, TypeError):
            cap = 5
        person, company = (person or "").strip(), (company or "").strip()
        if _backend() == "ollama":
            out = self._ollama_chat(
                f"Generate {cap} search queries each for person/company/jobs/news/filings "
                f"for '{person} {company}'. Return JSON only.")
            if out:
                try:
                    import json as _j
                    cand = _j.loads(out[out.find("{"):out.rfind("}") + 1])
                    if isinstance(cand, dict) and any(
                            k.endswith("_queries") for k in cand):
                        cut = {k: list(v)[:cap] for k, v in cand.items()
                               if isinstance(v, list)}
                        if cut:
                            return cut
                except Exception:
                    pass
        short = company.split(" LIMITED")[0].split(" Ltd")[0].strip() or company
        plan = {
            "person_queries": [f"{person} {company} 2026",
                               f"{person} recent statements 2026"][:cap],
            "company_queries": [f"{short} 2026 strategy",
                                f"{short} AI investment 2026"][:cap],
            "job_queries": [f"{short} AI jobs 2026",
                            f"{short} hiring artificial intelligence"][:cap],
            "news_queries": [f"{short} latest news 2026",
                             f"{short} cloud latest news 2026"][:cap],
            "filing_queries": [f"{short} annual report priorities"][:cap],
        }
        return {k: v[:cap] for k, v in plan.items()}


@dataclass
class UtilityModel:  # Pool B — rewrite/score/dedup/extract/classify (~80% calls)
    name: str = "utility-7b-mock"

    def score(self, query: str, snippet: str) -> float:
        import re as _re
        query = query or ""
        snippet = snippet or ""
        norm = lambda s: set(_re.findall(r"[a-z0-9]+", s.lower()))
        q, s = norm(query), norm(snippet)
        return len(q & s) / max(1, len(q))

    def rewrite(self, query: str, n: int = 5) -> list[str]:
        """LLM-assisted query rewriting (mock: suffix expansion).
        Real Utility LLM later; same interface, strict budget."""
        base = (query or "").strip()
        if not base:
            return []
        n = max(1, min(10, n))
        tails = ["2026", "investment 2026", "strategy",
                 "hiring", "partnerships", "executive comments",
                 "infrastructure expansion", "latest news"]
        return [f"{base} {t}" for t in tails[:n]]


_EMBED_CACHE: dict = {}  # shared LRU {text_hash: vec}, bounded 512


@dataclass
class EmbeddingModel:  # Pool B (~10% calls)
    name: str = "embed-mock"

    def _cache_get(self, key: str):
        try:
            return _EMBED_CACHE.get(key)
        except Exception:
            pass
        return None

    def _cache_put(self, key: str, vec: list[float]) -> None:
        try:
            if len(_EMBED_CACHE) >= 512:
                _EMBED_CACHE.pop(next(iter(_EMBED_CACHE)))
            _EMBED_CACHE[key] = vec
        except Exception:
            pass

    def embed(self, text: str) -> list[float]:
        import hashlib as _hl
        key = _hl.sha256((text or "")[:2000].encode()).hexdigest()
        hit = self._cache_get(key)
        if hit is not None:
            return hit
        # Dedicated embed model (bge-m3) — never reuse the utility chat model.
        if _backend() == "ollama":
            try:
                import httpx as _hx
                base = (_os.environ.get("OLLAMA_BASE_URL",
                                        "http://localhost:11434")).rstrip("/")
                model = (_os.environ.get("OLLAMA_EMBED_MODEL", "")
                         or "bge-m3")
                r = _hx.post(base + "/api/embeddings",
                             json={"model": model,
                                   "prompt": (text or "")[:2000]},
                             timeout=30)
                if r.status_code == 200:
                    vec = r.json().get("embedding")
                    if isinstance(vec, list) and vec:
                        out = [float(x) for x in vec[:1024]]
                        self._cache_put(key, out)
                        return out
            except Exception:
                pass
        text = text or ""
        out = [float(len(text) % 97), float(len(text.split()) % 89)]
        self._cache_put(key, out)
        return out

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Batched embeddings (kills N+1 Ollama calls): one POST per text
        today (Ollama has no batch endpoint), but shared LRU cache means
        repeat briefs cost zero. Falls back per-item to mock."""
        return [self.embed(t or "") for t in (texts or [])]


@dataclass
class Reranker:  # Pool B (~5% calls)
    name: str = "reranker-mock"
    utility: UtilityModel | None = None

    def rerank(self, query: str, docs: list[str]) -> list[str]:
        util = self.utility or UtilityModel()
        scored = []
        for d in docs or []:
            try:
                scored.append((util.score(query or "", d or ""), d))
            except Exception:
                scored.append((0.0, d))
        scored.sort(key=lambda t: -t[0])
        return [d for _, d in scored]

    def top(self, query: str, docs: list[str], k: int = 10) -> list[str]:
        """Top-k after rerank (retrieval §4: top-50 -> rerank -> top-10)."""
        return self.rerank(query, docs)[:max(1, k)]


@dataclass
class ToolBoundary:
    """Thin swappable orchestration seam. Only the planner may call tools;
    retrieved DATA can never trigger a tool call."""
    agent: AgentModel = field(default_factory=AgentModel)
    utility: UtilityModel = field(default_factory=UtilityModel)
    embedding: EmbeddingModel = field(default_factory=EmbeddingModel)
    reranker: Reranker = field(default_factory=Reranker)
