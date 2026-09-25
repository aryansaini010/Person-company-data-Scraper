"""Live end-to-end: real HTTP fetch -> DMZ -> core passes -> brief + audit tail."""
from fastapi.testclient import TestClient
from api import app

c = TestClient(app)
print("== LIVE /research against https://example.com ==")
r = c.post("/research", json={
    "name": "Demo Prospect", "company": "ExampleCorp",
    "urls": ["https://example.com"], "max_results": 3,
    "collateral": ["platform scaling case study",
                   "earnings-call intelligence brief"],
})
print("status:", r.status_code)
d = r.json()
print("fetched:", d.get("fetched"))
if d.get("status") == "needs_confirmation":
    print("candidates:", [(c_["full_name"], c_["company"])
                          for c_ in d.get("candidates", [])])
    r = c.post("/research/confirm",
               json={"session_id": d["session_id"], "index": 0})
    print("confirm status:", r.status_code)
    d = r.json()
b = d["brief"]
print("signals:", len(b["strategy_signals"]))
for v in b["strategy_signals"][:3]:
    print(f"  [{v['verdict']}] {v['claim']['text'][:120]}")
print("pitch:", b["pitch"][:300])
print("gaps:", b["gaps"])
print("== SSRF refusal ==")
r2 = c.post("/research", json={"name": "X", "company": "Y",
                               "urls": ["http://127.0.0.1/secret"]})
print("status:", r2.status_code, "->", r2.json().get("warning", "brief"))
print("== audit tail ==")
for e in c.get("/audit/tail", params={"n": 6}).json():
    print(f"  {e['event']} hash={e['hash'][:10]}")
