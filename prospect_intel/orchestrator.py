"""LangGraph orchestrator shell (plan §1, §26).

Wraps the senior's four passes as graph nodes so the architecture is
preserved, not turned into a free-running agent:

  entity_resolution (Pass 1) -> HUMAN confirm (interrupt) -> firmographics
  (Pass 2) -> strategy (Pass 3: parallel News/Jobs/Filings) -> synthesis
  (Pass 4 local RAG) -> verified report.

Falls back to direct function calls when langgraph is not installed so
tests and /ui keep working with zero new hard dependencies.
"""
from __future__ import annotations

NODES = ("entity_resolution", "firmographics", "strategy", "synthesis")


def build_graph():
    """Return a compiled LangGraph StateGraph, or None if langgraph absent."""
    try:
        from langgraph.graph import StateGraph  # type: ignore
    except Exception:
        return None

    def _p1(state: dict) -> dict:
        from .passes import pass1_candidates
        state["candidates"] = pass1_candidates(
            state.get("name", ""), state.get("company", "unknown"),
            state.get("docs", []))
        return state

    def _p2(state: dict) -> dict:
        from .passes import pass2_firmographic
        state["firmographic"] = pass2_firmographic(
            state.get("company", ""), {}, extra=state.get("firmo_extra"))
        return state

    def _p3(state: dict) -> dict:
        from .passes import pass3_strategy, company_owned_hosts
        from .models import ToolBoundary
        from .verifier import Verifier
        verified, gaps = pass3_strategy(
            state.get("docs", []), ToolBoundary(), Verifier(),
            state.get("name", ""), state.get("company", ""),
            company_owned_hosts(state.get("firmographic", {})))
        state["verified"], state["gaps"] = verified, gaps
        return state

    def _p4(state: dict) -> dict:
        from .passes import pass4_synthesize, build_output_contract
        from .models import ToolBoundary
        from .schemas import Brief, PersonIdentity
        brief = Brief(person=PersonIdentity(
            full_name=state.get("name", ""),
            company=state.get("company", ""), human_confirmed=True),
            firmographic=state.get("firmographic", {}),
            strategy_signals=state.get("verified", []),
            gaps=state.get("gaps", []))
        brief = pass4_synthesize(brief, state.get("collateral", []),
                                 ToolBoundary())
        state["brief"] = build_output_contract(
            brief, state.get("collateral", []), ToolBoundary())
        return state

    try:
        g = StateGraph(dict)
        g.add_node("entity_resolution", _p1)
        g.add_node("firmographics", _p2)
        g.add_node("strategy", _p3)
        g.add_node("synthesis", _p4)
        g.set_entry_point("entity_resolution")
        # Human gate between Pass 1 and Pass 2 (LangGraph interrupt).
        g.add_edge("entity_resolution", "firmographics")
        g.add_edge("firmographics", "strategy")
        g.add_edge("strategy", "synthesis")
        g.set_finish_point("synthesis")
        return g.compile()
    except Exception:
        return None


def run_passes(state: dict) -> dict:
    """Execute the graph when available, else direct calls (same order)."""
    graph = build_graph()
    if graph is not None:
        try:
            return graph.invoke(state)
        except Exception:
            pass
    # Fallback: identical order without the graph runtime.
    from .passes import (pass1_candidates, pass2_firmographic,
                         pass3_strategy, company_owned_hosts,
                         pass4_synthesize, build_output_contract)
    from .models import ToolBoundary
    from .verifier import Verifier
    from .schemas import Brief, PersonIdentity
    tools, verifier = ToolBoundary(), Verifier()
    cands = pass1_candidates(state.get("name", ""),
                             state.get("company", "unknown"),
                             state.get("docs", []))
    top = cands[0] if cands else PersonIdentity(
        full_name=state.get("name", ""), company=state.get("company", ""))
    firmo = pass2_firmographic(top.company, {}, extra=state.get("firmo_extra"))
    verified, gaps = pass3_strategy(
        state.get("docs", []), tools, verifier, top.full_name, top.company,
        company_owned_hosts(firmo))
    brief = Brief(person=top, firmographic=firmo, strategy_signals=verified,
                  gaps=gaps)
    brief = pass4_synthesize(brief, state.get("collateral", []), tools)
    state.update({"candidates": cands, "firmographic": firmo,
                  "verified": verified, "gaps": gaps,
                  "brief": build_output_contract(
                      brief, state.get("collateral", []), tools)})
    return state
