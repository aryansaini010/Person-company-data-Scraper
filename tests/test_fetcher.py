from prospect_intel import security
from prospect_intel.fetcher import html_to_text

def test_dns_ssrf_blocked_localhost():
    try:
        security.resolve_and_assert_no_ssrf("http://localhost/x")
    except ValueError:
        return
    raise AssertionError("localhost should be blocked")

def test_literal_ssrf_blocked():
    for u in ["http://127.0.0.1/", "http://10.0.0.5/a", "http://192.168.1.1/"]:
        try:
            security.resolve_and_assert_no_ssrf(u)
        except ValueError:
            continue
        raise AssertionError(f"{u} should be blocked")

def test_html_extract_drops_script():
    t = html_to_text("<html><head><script>evil()</script></head>"
                     "<body><h1>Acme expands</h1><p>Hiring engineers.</p></body></html>")
    assert "Acme expands" in t and "evil" not in t
