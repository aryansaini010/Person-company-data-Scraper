"""Wall-clock budgets for API flows (3-4 minute target).

Interactive mode has its own PROSPECT_DEADLINE_S (default 600s). API flows
use PROSPECT_API_DEADLINE_S (default 210s): every stage checks `expired()`
and stops intake on breach, returning a partial brief with a loud
`incomplete` note instead of hanging the request (§6.4).
"""
from __future__ import annotations
import os
import time

DEFAULT_S = 210.0
INCOMPLETE_NOTE = "incomplete: wall-clock budget exceeded (§6.4)"


def api_deadline_s() -> float:
    try:
        return max(30.0, float(os.environ.get("PROSPECT_API_DEADLINE_S",
                                              DEFAULT_S) or DEFAULT_S))
    except (ValueError, TypeError):
        return DEFAULT_S


def start() -> float:
    """Absolute deadline timestamp for a new request."""
    return time.time() + api_deadline_s()


def expired(deadline: float | None) -> bool:
    return deadline is not None and time.time() > deadline
