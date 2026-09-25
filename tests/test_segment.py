"""Buyer segment: media relevance by default, switchable via PROSPECT_SEGMENT."""
from prospect_intel import segment
from prospect_intel.segment import MEDIA_OTT


def test_media_is_default():
    import os
    os.environ.pop("PROSPECT_SEGMENT", None)
    assert segment.active()["key"] == "media_ott"
    assert "advertising" in segment.relevance_query()

def test_generic_switch():
    import os
    os.environ["PROSPECT_SEGMENT"] = "generic"
    try:
        assert segment.active()["key"] == "generic"
        assert "advertising" not in segment.relevance_query()
    finally:
        del os.environ["PROSPECT_SEGMENT"]

def test_trade_outlet_boost():
    assert segment.outlet_boost("https://afaqs.com/news/x", "Brands") > 0
    assert segment.outlet_boost("https://random.example/x") == 0

def test_default_collateral_is_media_labeled():
    col = segment.default_collateral()
    assert len(col) == 3 and all("DEFAULT" in c for c in col)
    assert any("ad inventory" in c for c in col)

def test_media_rss_angles():
    assert any("advertising" in a for a in MEDIA_OTT["rss_angles"])
