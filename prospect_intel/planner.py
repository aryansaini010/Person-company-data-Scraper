"""Bounded LLM research planner (plan §3, §11, §23).

Replaces hardcoded queries with a typed, budgeted plan executed in parallel:
person/company/job/news/filing queries, max PLANNER_MAX_PER_CAT each,
max PLANNER_MAX_ROUNDS rounds with sufficiency-stop.

Mock-safe: uses AgentModel.plan() templates until Ollama/vLLM is wired.
Tier-3 routing rule preserved: person queries NEVER go to commercial search.
"""
from __future__ import annotations
import os


def planner_budget() -> int:
    try:
        return max(1, min(10, int(os.environ.get("PLANNER_MAX_PER_CAT", "5"))))
    except (ValueError, TypeError):
        return 5


def max_rounds() -> int:
    try:
        return max(1, min(3, int(os.environ.get("PLANNER_MAX_ROUNDS", "2"))))
    except (ValueError, TypeError):
        return 2


def build_plan(person: str, company: str, tools=None) -> dict:
    """Return {person_queries, company_queries, job_queries, news_queries,
    filing_queries} each capped at planner_budget(). Single-token companies
    (Bullet) cap at 3/cat: ambiguity burns quota on noise, and GNews costs
    1 req/query of 100/day."""
    try:
        from .models import ToolBoundary
        agent = (tools.agent if tools is not None
                 else ToolBoundary().agent)
        plan = agent.plan(person or "", company or "")
        if isinstance(plan, dict) and plan:
            cap = planner_budget()
            co = (company or "").strip()
            if co and " " not in co and len(co) <= 12:
                cap = min(cap, 3)
            return {k: list(v)[:cap] for k, v in plan.items()
                    if isinstance(v, list)}
    except Exception:
        pass
    return {}


def sufficient(verified, firmo: dict | None = None) -> bool:
    """Sufficiency check: stop after Round 1 when evidence is enough,
    else ONE targeted Round 2 (hard max in max_rounds). Never endless search."""
    try:
        n = len(verified or [])
    except TypeError:
        n = 0
    if n >= 3:
        return True
    if firmo and firmo.get("website_probe", "none") != "none" and n >= 1:
        return True
    return False


def execute_round(plan: dict, docs: list, degraded: list,
                  deadline=None, fetch_full: bool = False) -> int:
    """Parallel Round-1/2 fan-out: news + filings + jobs branches at once.

    Uses existing Tier-1 connectors (RSS/news, EDGAR, jobs) + company_diet
    helper; respects deadline and never raises. Returns docs added.
    Person queries NEVER touch Tier-3 (routing rule preserved — this round
    is Tier-1 only; breadth lives in discover_*).
    """
    import concurrent.futures as _cf
    from . import deadline as _dl
    added = 0
    if not plan or _dl.expired(deadline):
        return 0
    # Sufficiency skip: a corpus with ≥10 docs already covers Round-1 —
    # don't burn GNews quota + 45s on a redundant Round-2.
    try:
        if len(docs or []) >= 10:
            return 0
    except TypeError:
        pass

    def _news(qs):
        out = []
        try:
            from .discovery import tier1_google_news_rss, company_diet
            from .acquisition import acquire, stamp_docs
            from .schemas import SourceClass
            for q in (qs or [])[:planner_budget()]:
                if _dl.expired(deadline):
                    break
                try:
                    hits, meta = tier1_google_news_rss(q, 4)
                    fresh = [h for h in hits
                             if h.url not in {d.url_final or d.url
                                              for d in docs}]
                    if fetch_full and fresh:
                        got = acquire([h.url for h in fresh[:4]],
                                      SourceClass.NEWS, deadline=deadline,
                                      smart=False)
                        stamp_docs(got, meta)
                        out.extend(got)
                except Exception:
                    continue
        except Exception:
            pass
        return out

    def _filings(qs):
        out = []
        try:
            from .filings import edgar_search
            for q in (qs or [])[:2]:
                if _dl.expired(deadline):
                    break
                try:
                    co = q.replace(" annual report priorities", "").strip()
                    d, _, _ = edgar_search(co or q, max_results=2)
                    out.extend(d or [])
                except Exception:
                    continue
        except Exception:
            pass
        return out

    def _jobs(qs):
        out = []
        try:
            from .filings import jobs_board
            for q in (qs or [])[:2]:
                if _dl.expired(deadline):
                    break
                try:
                    co = q.replace(" AI jobs 2026", "").replace(
                        " hiring artificial intelligence", "").strip()
                    d, _, _ = jobs_board(co or q)
                    out.extend(d or [])
                except Exception:
                    continue
        except Exception:
            pass
        return out

    branches = [( _news, plan.get("news_queries", []) + plan.get(
        "company_queries", [])[:1]),
        (_filings, plan.get("filing_queries", [])),
        (_jobs, plan.get("job_queries", []))]
    try:
        ex = _cf.ThreadPoolExecutor(max_workers=3)
        try:
            futs = [ex.submit(fn, qs) for fn, qs in branches]
            for f in _cf.as_completed(futs, timeout=45):
                if _dl.expired(deadline):
                    break
                try:
                    got = f.result(timeout=5) or []
                except Exception:
                    continue
                have = {d.doc_id for d in docs}
                for d in got:
                    if d.doc_id not in have:
                        have.add(d.doc_id)
                        docs.append(d)
                        added += 1
                        try:
                            from .acquisition import queue_to_core, _MEM_QUEUE
                            queue_to_core(d, _MEM_QUEUE)
                        except Exception:
                            pass
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass
    return added
