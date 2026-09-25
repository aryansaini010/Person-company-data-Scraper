"""Classified unknowns (cause codes) + confidence-decay labels."""
from prospect_intel.passes import (GAP_CODES, firmographic_unknowns, gap,
                                   staleness_label)


def test_gap_format_and_fallback():
    g = gap("company.funding", "absent_data", "no public data found")
    assert g == "company.funding: no public data found [absent_data]"
    assert gap("x", "bogus-code", "d").endswith("[absent_data]")
    assert set(GAP_CODES) >= {"absent_data", "source_unreachable",
                              "budget_exhausted", "contradiction_unresolved",
                              "filtered"}


def test_firmographic_gaps_carry_codes():
    gaps, _ = firmographic_unknowns(
        {"registry": "unknown", "filings": [], "funding": "unknown",
         "website_probe": "none", "probe_status": "unreachable: ValueError"},
        [])
    assert any(g.startswith("company.registry:") and g.endswith("[absent_data]")
               for g in gaps)
    assert any(g.startswith("company.website_probe:") and
               g.endswith("[source_unreachable]") for g in gaps)
    gaps2, _ = firmographic_unknowns(
        {"registry": "unknown", "filings": [], "funding": "unknown",
         "website_probe": "none", "probe_status": "no-data"}, [])
    assert any(g.endswith("[absent_data]") for g in gaps2)


def test_staleness_labels():
    assert staleness_label("2026") == ""
    assert staleness_label("undated") == ""
    assert staleness_label("") == ""
    assert "2021" in staleness_label("2021")
    assert staleness_label("2021 · historical intent") == ""
    assert staleness_label(None) == ""
