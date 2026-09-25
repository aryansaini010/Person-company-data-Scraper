"""Open WebUI integration: all HTTP mocked, key never leaks."""
from prospect_intel import openwebui as ow


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload if payload is not None else {}

    def json(self):
        return self._p


class _Router:
    routes = []   # (method, substring, payload, status)
    calls = []    # (method, url, auth_header_present)

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _hit(self, method, url, **k):
        headers = k.get("headers", {}) or {}
        _Router.calls.append((method, url, "Authorization" in headers,
                              str(k.get("json", "") or "")))
        hay = url + " " + str(k.get("json", "") or "")
        for m, sub, payload, status in _Router.routes:
            if m == method and sub in hay:
                return _Resp(status, payload)
        return _Resp(404, {})

    def get(self, url, **k):
        return self._hit("GET", url, **k)

    def post(self, url, **k):
        return self._hit("POST", url, **k)


def _route(monkeypatch, routes):
    _Router.routes = routes
    _Router.calls = []
    monkeypatch.setattr(ow.httpx, "Client", _Router)


def _keyless(monkeypatch):
    monkeypatch.delenv("OPENWEBUI_API_KEY", raising=False)


def test_no_key_degraded_no_http(monkeypatch):
    _keyless(monkeypatch)
    _route(monkeypatch, [])
    models, note = ow.check()
    assert models is None and "no OPENWEBUI_API_KEY" in note
    content, note2 = ow.chat_extract([{"role": "user", "content": "hi"}],
                                     "llama3.1")
    assert content is None and "no OPENWEBUI_API_KEY" in note2
    fid, _ = ow.upload_brief_file("nope.txt")
    assert fid is None
    assert _Router.calls == []


def test_check_lists_models(monkeypatch):
    monkeypatch.setenv("OPENWEBUI_API_KEY", "k")
    _route(monkeypatch, [("GET", "/api/models",
                          {"data": [{"id": "llama3.1"}, {"id": "qwen"}]},
                          200)])
    models, note = ow.check()
    assert note is None and models == ["llama3.1", "qwen"]
    assert _Router.calls[0][2] is True  # auth header sent


def test_check_401_loud(monkeypatch):
    monkeypatch.setenv("OPENWEBUI_API_KEY", "bad")
    _route(monkeypatch, [("GET", "/api/models", {}, 401)])
    models, note = ow.check()
    assert models is None and "401" in note


def test_chat_extract_content_and_tools(monkeypatch):
    monkeypatch.setenv("OPENWEBUI_API_KEY", "k")
    _route(monkeypatch, [("POST", "/api/chat/completions",
                          {"choices": [{"message": {"content": "brief done"}}]},
                          200)])
    content, note = ow.chat_extract([{"role": "user", "content": "research"}],
                                    "llama3.1", tools=[{"id": "t1"}],
                                    tool_ids=["server:openapi:prospect"])
    assert note is None and content == "brief done"
    assert "t1" in _Router.calls[0][3]  # tool IDs travel in the body
    assert "prospect" in _Router.calls[0][3]
    assert _Router.calls[0][2] is True  # auth header sent


def test_chat_bad_shape_loud(monkeypatch):
    monkeypatch.setenv("OPENWEBUI_API_KEY", "k")
    _route(monkeypatch, [("POST", "/api/chat/completions", {"nope": 1}, 200)])
    content, note = ow.chat_extract([{"role": "user", "content": "x"}], "m")
    assert content is None and "unexpected shape" in note


def test_upload_brief_file(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENWEBUI_API_KEY", "k")
    p = tmp_path / "brief.txt"
    p.write_text("Acme brief", encoding="utf-8")
    _route(monkeypatch, [("POST", "/api/v1/files/", {"id": "f123"}, 200)])
    fid, note = ow.upload_brief_file(str(p))
    assert note is None and fid == "f123"


def test_upload_with_knowledge_attach(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENWEBUI_API_KEY", "k")
    p = tmp_path / "brief.txt"
    p.write_text("Acme brief", encoding="utf-8")
    _route(monkeypatch, [("POST", "/api/v1/files/", {"id": "f123"}, 200),
                         ("GET", "/process/status", {"status": "completed"},
                          200),
                         ("POST", "/file/add", {}, 200)])
    fid, note = ow.upload_brief_file(str(p), knowledge_id="kb1")
    assert note is None and fid == "f123"


def test_upload_missing_file_loud(monkeypatch):
    monkeypatch.setenv("OPENWEBUI_API_KEY", "k")
    _route(monkeypatch, [])
    fid, note = ow.upload_brief_file("/nonexistent/x.txt")
    assert fid is None and "unreadable" in note
    assert _Router.calls == []


def test_key_never_in_notes(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENWEBUI_API_KEY", "SECRETKEY123")
    _route(monkeypatch, [])
    for _, note in (ow.check(), ow.chat_extract([], ""),
                    ow.upload_brief_file("nope.txt")):
        assert note is not None and "SECRETKEY123" not in note
