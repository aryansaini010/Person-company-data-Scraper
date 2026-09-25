"""Open WebUI integration (server-side only — never browser-exposed).

Two flows, both optional and both loud when unavailable:
- chat_extract(): headless chat-completions call (optionally with Open WebUI
  tool IDs, e.g. our own :8003 OpenAPI tools) for scripted extractions.
- upload_brief_file(): push an exported readable brief (.txt) into Open WebUI
  Files (optionally attached to a Knowledge collection) for cross-brief RAG.

Contract (mirrors enrich.py): every public function returns (result, note);
missing key / unreachable server / API drift report down loudly and never
raise, so briefs never depend on chat being up. The key lives ONLY in env
(OPENWEBUI_API_KEY) and NEVER appears in notes, logs, or return values.
"""
from __future__ import annotations
import os

import httpx

from . import audit

_TIMEOUT = 60


def _creds(base: str | None = None,
           key: str | None = None) -> tuple[str, str]:
    base = (base if base is not None
            else os.environ.get("OPENWEBUI_BASE_URL",
                                "http://localhost:3000")).rstrip("/")
    key = (key if key is not None
           else os.environ.get("OPENWEBUI_API_KEY", "")).strip()
    return base, key


def _down(reason: str) -> tuple[None, str]:
    audit.log("tier_down", {"tier": "openwebui", "reason": reason})
    return None, (f"Open WebUI connector down ({reason}): continuing "
                  "without chat/RAG features")


def check(base: str | None = None,
          key: str | None = None) -> tuple[list | None, str | None]:
    """Verify connectivity + key. Returns (model_ids, note)."""
    base, key = _creds(base, key)
    if not key:
        return _down("no OPENWEBUI_API_KEY")
    try:
        with httpx.Client(timeout=20) as c:
            r = c.get(base + "/api/models",
                      headers={"Authorization": f"Bearer {key}"})
            if r.status_code != 200:
                return None, (f"Open WebUI models check failed "
                              f"(HTTP {r.status_code}): key or base URL wrong?")
            try:
                data = r.json().get("data", []) or []
            except Exception:
                return None, "Open WebUI models check returned invalid JSON"
            models = [m.get("id", "?") for m in data
                      if isinstance(m, dict)]
            audit.log("openwebui_check", {"base": base, "models": len(models)})
            return models, None
    except Exception as e:
        return _down(f"{type(e).__name__}: {e}")


def chat_extract(messages: list[dict], model: str,
                 tools: list | None = None,
                 tool_ids: list[str] | None = None,
                 base: str | None = None,
                 key: str | None = None) -> tuple[str | None, str | None]:
    """Headless extraction: run messages through an Open WebUI-hosted model.

    tool_ids selects server-side tools (e.g. our :8003 OpenAPI tool server
    once registered in Open WebUI); plain `tools` arrays are forwarded to
    the model instead. Returns (content, note)."""
    base, key = _creds(base, key)
    if not key:
        return _down("no OPENWEBUI_API_KEY")
    if not model.strip() or not messages:
        return None, "Open WebUI chat_extract needs a model and messages"
    body: dict = {"model": model.strip(), "messages": messages}
    if tool_ids:
        body["tool_ids"] = list(tool_ids)
    if tools:
        body["tools"] = tools
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(base + "/api/chat/completions",
                       headers={"Authorization": f"Bearer {key}",
                                "Content-Type": "application/json"},
                       json=body)
            if r.status_code != 200:
                return None, (f"Open WebUI chat failed (HTTP {r.status_code}): "
                              "extraction skipped")
            try:
                content = r.json()["choices"][0]["message"]["content"]
            except Exception:
                return None, ("Open WebUI chat returned unexpected shape: "
                              "extraction skipped")
            audit.log("openwebui_chat", {"model": model,
                                         "chars": len(str(content or ""))})
            return str(content or ""), None
    except Exception as e:
        return _down(f"{type(e).__name__}: {e}")


def _wait_processed(c: httpx.Client, base: str, key: str, fid: str,
                    timeout_s: float = 120.0) -> bool:
    """Poll file processing until completed (attaching early 400s)."""
    import time as _t
    url = f"{base}/api/v1/files/{fid}/process/status"
    headers = {"Authorization": f"Bearer {key}"}
    start = _t.time()
    while _t.time() - start < timeout_s:
        try:
            r = c.get(url, headers=headers, timeout=20)
            if r.status_code == 200 and r.json().get("status") == "completed":
                return True
            if r.status_code == 200 and r.json().get("status") == "failed":
                return False
        except Exception:
            pass
        _t.sleep(2.0)
    return False


def upload_brief_file(path: str, knowledge_id: str = "",
                      base: str | None = None,
                      key: str | None = None) -> tuple[str | None, str | None]:
    """Upload an exported brief file for RAG. Returns (file_id, note).
    knowledge_id attaches it to a collection when provided; the bare file
    alone is already usable in chat via # references."""
    from pathlib import Path
    base, key = _creds(base, key)
    if not key:
        return _down("no OPENWEBUI_API_KEY")
    try:
        data = Path(path).read_bytes()
    except Exception as e:
        return None, f"brief file unreadable ({e}): upload skipped"
    if not data:
        return None, "brief file empty: upload skipped"
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(base + "/api/v1/files/",
                       headers={"Authorization": f"Bearer {key}"},
                       files={"file": (Path(path).name, data,
                                       "text/plain")})
            if r.status_code not in (200, 201):
                return None, (f"Open WebUI upload failed "
                              f"(HTTP {r.status_code}): upload skipped")
            try:
                fid = r.json().get("id", "")
            except Exception:
                return None, ("Open WebUI upload returned unexpected shape: "
                              "upload skipped")
            if not fid:
                return None, ("Open WebUI upload returned no file id: "
                              "upload skipped")
            if (knowledge_id or "").strip():
                if not _wait_processed(c, base, key, fid):
                    return fid, (f"file uploaded ({fid}) but processing did "
                                 f"not complete: attach to knowledge manually")
                r2 = c.post(
                    base + "/api/v1/knowledge/"
                    + (knowledge_id or "").strip() + "/file/add",
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"},
                    json={"file_id": fid})
                if r2.status_code not in (200, 201):
                    return fid, (f"file uploaded ({fid}) but knowledge attach "
                                 f"failed (HTTP {r2.status_code}): attach manually")
            audit.log("openwebui_upload", {"file": Path(path).name,
                                           "file_id": fid})
            return fid, None
    except Exception as e:
        return _down(f"{type(e).__name__}: {e}")
