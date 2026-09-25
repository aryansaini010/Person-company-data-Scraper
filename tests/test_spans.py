"""Span integrity: chrome pruning, normalization lock, SimHash dedup."""
from prospect_intel.fetcher import html_to_text
from prospect_intel.security import normalize_ingest_text, scrub_retrieved_text
from prospect_intel.acquisition import simhash64, simhash_distance


def test_nav_cookie_related_stripped_article_kept():
    html = ("<html><body><nav>India World Sport Videos Podcast</nav>"
            "<div id='cookie-banner'>Accept cookies</div>"
            "<article><h1>Zee bets on digital growth</h1>"
            "<p>Zee Entertainment bets on digital growth in 2026.</p></article>"
            "<aside class='related-stories'>Read more: diet tips</aside>"
            "<footer>All rights reserved. Terms of use.</footer>"
            "</body></html>")
    t = html_to_text(html)
    assert "Zee bets on digital growth" in t
    assert "India World Sport" not in t
    assert "Accept cookies" not in t
    assert "diet tips" not in t
    assert "All rights reserved" not in t


def test_header_footer_kept_without_article():
    html = ("<html><body><header>Zee Media Corporation</header>"
            "<p>Zee Media runs news channels.</p></body></html>")
    assert "Zee Media Corporation" in html_to_text(html)


def test_normalize_is_1to1_and_idempotent():
    s = "A B “quote” — dash … end"  # NBSP, smart quotes, em dash, ellipsis
    n = normalize_ingest_text(s)
    assert len(n) == len(s)  # offsets preserved
    assert " " not in n and "“" not in n
    assert scrub_retrieved_text(s) == scrub_retrieved_text(
        scrub_retrieved_text(s))


def test_reslice_invariant(monkeypatch, tmp_path):
    from prospect_intel import store, audit
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "s.db")
    monkeypatch.setattr(audit, "log", lambda e, p: {})
    from prospect_intel.acquisition import structure_fetch
    from prospect_intel.passes import _candidate_sentences
    from prospect_intel.schemas import SourceClass
    body = ("Zee Entertainment “bets” on digital growth in 2026. "
            "ZEE5 turns profitable — a first.")
    doc = structure_fetch("https://example.com/a", 200, body, SourceClass.NEWS)
    assert doc is not None
    text = doc.sections[0].text
    for sent, cs, ce in _candidate_sentences(doc):
        assert text[cs:ce].strip().startswith(sent[:20].strip()[:20])
        assert sent in text  # claim text slices from the same string


def test_simhash_identical_zero_distant_large():
    a = "Zee Entertainment bets on digital growth in twenty twenty six"
    assert simhash_distance(simhash64(a), simhash64(a)) == 0
    b = "Quantum zebras orbit Jupiter nightly over silent harbors"
    assert simhash_distance(simhash64(a), simhash64(b)) > 10


def test_simhash_near_dup_small():
    a = ("Zee Entertainment bets on digital growth and ZEE5 turns "
         "profitable in FY26 with regional content expansion")
    b = ("Zee Entertainment bets on digital growth and ZEE5 turns "
         "profitable in FY26 with regional content push")
    # One-word rewrite: survives intake (Pass 3 story-dedup owns these),
    # but scores far below unrelated text.
    assert simhash_distance(simhash64(a), simhash64(b)) <= 12
    # Whitespace/case-only copies are intake-level duplicates (distance 0).
    assert simhash64("  Zee   GROWTH\n2026 ") == simhash64("zee growth 2026")
