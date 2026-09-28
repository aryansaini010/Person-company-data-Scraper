"""General-knowledge company overview (UNVERIFIED, never evidence).

Used ONLY when zero verified company data exists after the full
company-depth fallback (no registry, no probe/homepage, no strategy
signals). Produces TV-Centre-style background + disambiguation via the
user's local model THROUGH Open Web UI chat:

  Open Web UI (:8081, OPENWEBUI_API_KEY) -> local Ollama (gpt-oss:20b)

Primary route is Open Web UI (model id as shown in its picker). If Open
Web UI is down, falls back to the same weights direct via Ollama
(localhost:11434, same model id) so availability doesn't depend on the
gateway. Both routes are server-side; the browser never sees the key.

Safety contract (senior-grade):
- Pure text, max ~500 chars, no URLs/phones/emails/revenue invented.
- Never returns VerifiedClaim/Priority/evidence; caller stores it in
  Brief.general_knowledge only (see schemas.py).
- Never raises: (items, note) with items==[] on any failure/skip.
- Disabled under pytest unless explicitly enabled (deterministic tests).
- Deadline-aware: caller passes remaining budget; we cap our own spend.
"""
from __future__ import annotations

import os
import re
import time

LABEL = "GENERAL-KNOWLEDGE-UNVERIFIED"
MAX_CHARS = 500
SPEND_S = 25.0  # own cap inside the caller's 120s budget


def _chat_model() -> str:
    return (os.environ.get("OPENWEBUI_CHAT_MODEL", "") or "gpt-oss:20b").strip()


def _prompt(company: str) -> str:
    co = (company or "").strip()[:120]
    return (
        f'Give only widely-known general background for "{co}". '
        "State uncertainty in the first words. "
        "Then list 2-3 possible disambiguations as short bullets "
        '(e.g. "However, depending on context, you might mean..."). '
        "Do NOT invent revenue, employee counts, URLs, emails, or phone "
        "numbers. Do NOT claim verification. Max 120 words."
    )


def _clean(text: str) -> str:
    s = re.sub(r"\s+", " ", (text or "")).strip()
    # Strip any URLs/phones the model may add despite instructions.
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"\+?\d[\d\s\-()]{7,}\d", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s[:MAX_CHARS]


def get_general_background(company: str,
                           deadline: float | None = None) -> tuple[list[dict], str | None]:
    """Return ([{text,label,model,via}], note). Never raises."""
    import sys as _sys
    co = (company or "").strip()
    if not co or co.lower() == "unknown":
        return [], None
    if ("PYTEST_CURRENT_TEST" in os.environ) or ("pytest" in _sys.modules):
        if os.environ.get("GENERAL_KNOWLEDGE_TEST", "") != "1":
            return [], None
    if deadline is not None and (deadline - time.time() < 20):
        return [], None
    model = _chat_model()
    messages = [{"role": "user", "content": _prompt(co)}]

    # Route 1 (primary): local model THROUGH Open Web UI chat API.
    # Bounded by chat_extract's own 60s timeout; slow cold 20B models
    # return [] rather than hanging the 120s brief.
    try:
        from . import openwebui as _ow
        content, note = _ow.chat_extract(messages, model)
        if content and _clean(content):
            return [{"text": _clean(content), "label": LABEL,
                     "model": model, "via": "openwebui"}], None
    except Exception:
        pass

    # Route 2 (backup): same weights direct via local Ollama.
    # Skipped when the request budget is nearly spent.
    try:
        if deadline is not None and (deadline - time.time() < 35):
            return [], None
        import httpx as _hx
        base = (os.environ.get("OLLAMA_BASE_URL",
                               "http://localhost:11434") or "").rstrip("/") \
            or "http://localhost:11434"
        prompt = _prompt(co) + " Reply in plain text, no markdown links."
        with _hx.Client(timeout=30.0) as _c:
            _r = _c.post(base + "/api/generate",
                         json={"model": model, "prompt": prompt,
                               "stream": False})
            if _r.status_code == 200:
                try:
                    txt = _r.json().get("response", "")
                except Exception:
                    txt = ""
                if txt and _clean(txt):
                    return [{"text": _clean(txt), "label": LABEL,
                             "model": model, "via": "ollama-direct"}], None
    except Exception:
        pass
    return [], None
