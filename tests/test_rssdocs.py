"""RSS-headline docs: citable, dated, never expanded beyond the headline."""
from prospect_intel.acquisition import structure_rss_items

def test_rss_items_become_dated_news_docs():
    docs = structure_rss_items([("https://n.example/1", "RIL beats estimates - Moneycontrol.com",
                                 "Fri, 17 Jul 2026 07:00:00 GMT", "Moneycontrol.com")])
    assert len(docs) == 1
    d = docs[0]
    assert d.source_class.value == "news"
    assert "2026" in d.published_at and "Moneycontrol" in d.title
    assert "beats estimates" in d.sections[0].text  # verbatim, nothing added

def test_rss_items_skip_empties():
    assert structure_rss_items([("u", "", "", "")]) == []
