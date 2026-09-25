from prospect_intel.acquisition import structure_fetch
from prospect_intel.schemas import SourceClass

def test_captcha_200_is_rejected():
    assert structure_fetch("https://example.com/x", 200,
        "<html>Enable cookies to prove you are you a robot captcha</html>",
        SourceClass.NEWS) is None

def test_empty_is_rejected():
    assert structure_fetch("https://example.com/y", 200, "   ") is None

def test_ok_structured_with_span():
    doc = structure_fetch("https://example.com/z", 200,
        "We plan to expand hiring for platform engineers.", SourceClass.JOB_POSTING)
    assert doc and doc.sections[0].char_end == len(doc.sections[0].text)
