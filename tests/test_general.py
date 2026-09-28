"""General-knowledge fallback: unverified lane, never evidence."""
from prospect_intel.general import _clean, get_general_background
from prospect_intel.schemas import Brief, PersonIdentity


def test_clean_strips_urls_phones_and_caps():
    out = _clean("JSC TV Centre is big https://x.example call +375123456789 " + "x" * 600)
    assert "https://" not in out and "+375" not in out and len(out) <= 500


def test_disabled_under_pytest_by_default():
    rows, note = get_general_background("JSC TV Centre", None)
    assert rows == []


def test_enabled_with_mocked_openwebui(monkeypatch):
    monkeypatch.setenv("GENERAL_KNOWLEDGE_TEST", "1")
    import prospect_intel.general as G
    import prospect_intel.openwebui as OW
    monkeypatch.setattr(OW, "chat_extract",
                        lambda messages, model, **k: ("JSC TV Centre is a major TV company. However, X.", None))
    rows, _ = G.get_general_background("JSC TV Centre", None)
    assert len(rows) == 1
    assert rows[0]["label"] == "GENERAL-KNOWLEDGE-UNVERIFIED"
    assert rows[0]["via"] == "openwebui"
    assert "JSC" in rows[0]["text"]
    monkeypatch.delenv("GENERAL_KNOWLEDGE_TEST", raising=False)


def test_brief_schema_accepts_general_knowledge():
    b = Brief(person=PersonIdentity(full_name="N", company="C"),
              general_knowledge=[{"text": "t", "label": "L",
                                  "model": "gpt-oss:20b", "via": "openwebui"}])
    assert b.general_knowledge[0]["model"] == "gpt-oss:20b"
    # Default stays empty for old briefs (backward compatible).
    b2 = Brief(person=PersonIdentity(full_name="N", company="C"))
    assert b2.general_knowledge == []
