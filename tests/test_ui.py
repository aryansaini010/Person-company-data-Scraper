"""Dashboard: serves clean brief form (no ops/log panels)."""
from fastapi.testclient import TestClient
from api import app

def test_ui_serves():
    c = TestClient(app)
    r = c.get("/ui")
    assert r.status_code == 200
    for marker in ("Run a brief", "What is their company trying",
                   "/research", "/research/confirm", "Show sources",
                   "Participants", "/participants/upload",
                   "REQ_TIMEOUT_MS", "timed out after 4 min"):
        assert marker in r.text
    for gone in ("Show extraction logs", "Ops metrics", "/audit/tail",
                 'value="Mukesh Ambani"', 'value="Reliance Industries"',
                 "Company brief (no person needed)", 'id="cocompany"'):
        assert gone not in r.text

def test_root_redirects_to_ui():
    c = TestClient(app)
    assert c.get("/", follow_redirects=False).status_code in (302, 307)
