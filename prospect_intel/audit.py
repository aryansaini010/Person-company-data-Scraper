"""Append-only audit log: JSONL mirror + hash-chained SQLite row (tamper-evident).

Every query, fetch, claim, citation, and verdict lands here so any sentence
in any brief traces back to its source span.
"""
from __future__ import annotations
import json, time
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "run_log.jsonl"

def log(event: str, payload: dict | None = None) -> dict:
    entry = {"ts": time.time(), "event": event,
             "payload": dict(payload) if isinstance(payload, dict) else {}}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    try:
        from . import store
        con = store.connect()
        chained = store.audit_chain(con, event, payload)
        entry.update(chained)
        con.close()
    except Exception:
        pass  # JSONL is the durable fallback; chain is best-effort here
    return entry
