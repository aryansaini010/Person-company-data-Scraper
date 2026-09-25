import sqlite3
from prospect_intel import store
from prospect_intel.schemas import Claim, StructuredDoc, SourceClass, DocSection
from prospect_intel.verifier import Verifier

def test_partial_support_fires():
    text = ("Acme will expand platform engineering hiring in Berlin. "
            "The weather in Berlin is mild and pleasant today.")
    doc = StructuredDoc(doc_id="doc_p", url="https://example.com",
        fetched_at="2026-01-01T00:00:00Z", source_class=SourceClass.NEWS,
        sections=[DocSection(text=text, char_start=0, char_end=len(text))])
    c = Claim(claim_id="c9", text="Acme will expand platform engineering hiring in Berlin. "
              "Quantum zebras orbit Jupiter nightly.",
              doc_id="doc_p", section_index=0, char_start=0, char_end=len(text))
    assert Verifier().check(c, doc).verdict.value == "partially_supported"

def test_store_queue_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    con = store.connect()
    store.doc_save(con, "d1", "https://example.com", "2026-01-01T00:00:00Z",
                   "news", "Acme expands hiring")
    store.queue_push(con, "d1")
    assert store.queue_pop(con) == ["d1"]
    assert store.queue_pop(con) == []
    h1 = store.audit_chain(con, "e1", {"a": 1})["hash"]
    h2 = store.audit_chain(con, "e2", {"b": 2})
    assert h2["prev"] == h1
    con.close()

def test_acquire_rejects_ssrf(monkeypatch):
    from prospect_intel import acquisition
    docs = acquisition.acquire(["http://127.0.0.1/secret"])
    assert docs == []

def test_legacy_duplicate_queue_migrates(tmp_path, monkeypatch):
    import sqlite3
    from prospect_intel import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "m.db")
    # legacy DB: table without the unique index, holding duplicates
    raw = sqlite3.connect(str(tmp_path / "m.db"))
    raw.execute("CREATE TABLE queue(seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                " doc_id TEXT NOT NULL, received INT DEFAULT 0)")
    raw.execute("INSERT INTO queue(doc_id) VALUES ('d1')")
    raw.execute("INSERT INTO queue(doc_id) VALUES ('d1')")
    raw.commit()
    raw.close()
    con = store.connect()  # migration must not raise on legacy duplicates
    rows = con.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
    assert rows == 1
    con.close()
