"""Wiki-text quality: markup stripping, surname-first ranking, max-year recency."""
from prospect_intel.discovery import clean_wiki_text, rank_wiki_titles
from prospect_intel.passes import _recency_of
from prospect_intel.schemas import DocSection, SourceClass, StructuredDoc

def _doc(text):
    return StructuredDoc(doc_id="d", url="u", url_final="u", content_hash="h",
        fetched_at="2026-01-01T00:00:00Z", source_class=SourceClass.OTHER,
        sections=[DocSection(section_id="d#s0", text=text,
                             char_start=0, char_end=len(text))])

def test_wiki_markup_lines_dropped():
    t = clean_wiki_text("Title\n== Career ==\nHe founded X.\n== See also ==\n\n")
    assert "==" not in t and "He founded X." in t

def test_wiki_citation_artifacts_stripped():
    t = clean_wiki_text("It is the largest retailer.[\\[85\\]](https://x.com/y) Truly.[86]")
    assert "[85]" not in t and "http" not in t and "largest retailer" in t

def test_surname_titles_rank_first():
    ranked = rank_wiki_titles(["Antilia (building)", "Mukesh Ambani",
                               "Nita Ambani"], "Mukesh Ambani")
    assert ranked[0] == "Mukesh Ambani" and ranked[-1] == "Antilia (building)"

def test_recency_from_published_at_only():
    # v1.1 §5.4: recency is read from published_at, never text-mined.
    d = _doc("born 1957, expanded refinery in 2010 with record 2024 profits")
    assert _recency_of(d, d.sections[0].text) == "undated"
