"""Participant upload: CSV/XLSX parse, Phone/Mobile exclusion, API roundtrip."""
import io

from fastapi.testclient import TestClient

from api import app
from prospect_intel.participants import normalize_rows, parse_csv, parse_upload


CSV_SAMPLE = (
    "Last Name,First Name,Position,Company,Corporative Email,Country,"
    "Company Phone,Phone,Mobile,E-mail,Participant Type,Activity\n"
    "Bahatka,Andrei,\"Head of the YASNAe TV Channel Development Department, Beltelecom\","
    "\"BELTELECOM, YASNAe TV\",bogatko.ag@main.beltelecom.by,Belarus,"
    "80172171340,+375298513431,+375298513431,bogatko.ag@main.beltelecom.by,"
    "Visitor,TV Channel (all platforms)\n"
)


def test_parse_csv_drops_phone_mobile():
    rows, notes = parse_csv(CSV_SAMPLE.encode("utf-8"))
    assert len(rows) == 1
    r = rows[0]
    assert r["full_name"] == "Andrei Bahatka"
    assert r["company_primary"] == "BELTELECOM"
    assert r["company_raw"] == "BELTELECOM, YASNAe TV"
    assert r["corp_email"] == "bogatko.ag@main.beltelecom.by"
    assert r["company_phone"] == "80172171340"
    assert "phone" not in r and "mobile" not in r
    assert "+375298513431" not in str(r.values())


def test_parse_upload_rejects_extension():
    rows, notes = parse_upload("list.txt", b"a,b\n1,2")
    assert rows == [] and any("unsupported" in n for n in notes)


def test_normalize_skips_empty_rows():
    rows, notes = normalize_rows([{"Last Name": "", "First Name": "",
                                   "Company": ""}])
    assert rows == [] and notes


def _client():
    return TestClient(app)


def test_upload_list_get_roundtrip():
    c = _client()
    r = c.post("/participants/upload",
               files={"file": ("people.csv", CSV_SAMPLE.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["count"] == 1 and d["upload_id"].startswith("upl_")
    assert d["people"][0]["full_name"] == "Andrei Bahatka"
    pid = d["people"][0]["pid"]

    l = c.get(f"/participants/{d['upload_id']}")
    assert l.status_code == 200 and l.json()["count"] == 1

    g = c.get(f"/participants/{d['upload_id']}/{pid}")
    assert g.status_code == 200
    person = g.json()["person"]
    assert person["position"].startswith("Head of the YASNAe")
    assert "phone" not in person and "mobile" not in person
    assert "+375298513431" not in str(person.values())


def test_upload_bad_extension_400():
    c = _client()
    r = c.post("/participants/upload",
               files={"file": ("x.txt", b"hi", "text/plain")})
    assert r.status_code == 400


def test_user_supplied_flows_to_brief(monkeypatch, tmp_path):
    """Position -> person.title, official contacts -> firmo, Phone/Mobile absent."""
    from prospect_intel import store
    import api as A
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "p.db")
    monkeypatch.setattr(A, "uploads", {})
    c = TestClient(app)
    # Offline: stub network-heavy discovery/acquire like other suites do.
    import prospect_intel.discovery as D
    monkeypatch.setattr(D, "tier1_gnews", lambda *a, **k: ([], {}))
    monkeypatch.setattr(A, "_hunter_enrich", lambda f, co, d, dg: f)
    r = c.post("/briefs", json={
        "name": "Andrei Bahatka", "company": "BELTELECOM", "docs": [],
        "user_supplied": {
            "position": "Head of the YASNAe TV Channel Development Department",
            "corp_email": "bogatko.ag@main.beltelecom.by",
            "company_phone": "80172171340",
            "phone": "+375298513431",  # must be dropped
            "mobile": "+375298513431",  # must be dropped
            "country": "Belarus",
        }})
    assert r.status_code == 200, r.text
    b = r.json()["brief"]
    assert b["person"]["title"].startswith("Head of the YASNAe")
    firmo = b["firmographic"]
    assert firmo["contact_supplied"]["corp_email"] == "bogatko.ag@main.beltelecom.by"
    assert "+375298513431" not in str(firmo)
    assert any("user-supplied" in g for g in b["degraded"])
