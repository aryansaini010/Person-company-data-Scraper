from fastapi.testclient import TestClient
from api import app
from prospect_intel.acquisition import structure_fetch
from prospect_intel.schemas import SourceClass

c = TestClient(app)

print("1. CAPTCHA page (HTTP 200 but blocked):")
bad = structure_fetch(
    "https://example.com/x", 200,
    "<html>Enable cookies captcha are you a robot</html>",
    SourceClass.NEWS,
)
print("   ->", bad, "(None = correctly rejected, never fed to model)")

print("2. Good fetch:")
doc = structure_fetch(
    "https://example.com/strat", 200,
    "Acme will expand platform engineering hiring in Berlin to support enterprise growth",
    SourceClass.JOB_POSTING,
)
print("   ->", doc.doc_id if doc else None)

print("3. Entity resolve (Pass 1 proposes, human confirms inside POST /briefs):")
print("   ->", c.post("/entity-resolve", json={"name": "Jane Doe", "company": "Acme"}).json())

print("4. Create brief:")
r = c.post("/briefs", json={
    "name": "Jane Doe",
    "company": "Acme",
    "docs": [doc.model_dump()] if doc else [],
    "collateral": ["platform scaling case study"],
})
print("   status:", r.status_code)
d = r.json()
if d["brief"]["strategy_signals"]:
    print("   verdict:", d["brief"]["strategy_signals"][0]["verdict"])
else:
    print("   gaps:", d["brief"]["gaps"])
print("   pitch:", d["brief"]["pitch"])

print("5. GET brief back:")
print("   status:", c.get(f"/briefs/{d['id']}").status_code)
