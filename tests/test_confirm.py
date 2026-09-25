"""Two-step research gate: candidates first, brief only after human select."""
from fastapi.testclient import TestClient
from api import app

def test_research_needs_confirmation_then_brief():
    c = TestClient(app)
    r = c.post("/research", json={"name": "Example Test",
                                  "company": "ExampleCorp",
                                  "urls": ["https://example.com"],
                                  "max_results": 2})
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "needs_confirmation"
    assert d["candidates"] and "session_id" in d
    r2 = c.post("/research/confirm",
                json={"session_id": d["session_id"], "index": 0})
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["status"] == "brief"
    assert d2["brief"]["person"]["human_confirmed"] is True
    assert d2["brief"]["person"]["confidence"] >= 0

def test_confirm_bad_session_404():
    c = TestClient(app)
    assert c.post("/research/confirm",
                  json={"session_id": "nope", "index": 0}).status_code == 404

def test_confirm_bad_index_400_then_gone_404():
    c = TestClient(app)
    r = c.post("/research", json={"name": "Example Test",
                                  "company": "ExampleCorp",
                                  "urls": ["https://example.com"],
                                  "max_results": 2})
    sid = r.json()["session_id"]
    assert c.post("/research/confirm",
                  json={"session_id": sid, "index": 99}).status_code == 400
    r2 = c.post("/research/confirm", json={"session_id": sid, "index": 0})
    assert r2.status_code == 200  # session still alive after a bad index
    assert c.post("/research/confirm",
                  json={"session_id": sid, "index": 0}).status_code == 404
