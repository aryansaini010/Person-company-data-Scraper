"""Security guards: retrieved bytes are attacker-controllable DATA, never instructions."""
from __future__ import annotations
import ipaddress
import re
from urllib.parse import urlparse

_INSTRUCTION_SHAPED = re.compile(
    r"(ignore\s+(all\s+)?previous\s+instructions|system\s*prompt|execute\s+tool\s*:|<\|?tool_call)",
    re.IGNORECASE,
)

CAPTCHA_MARKERS = ("captcha", "are you a robot", "enable cookies", "cookie-wall", "access denied")

PAYWALL_MARKERS = ("paywall", "subscribe to continue", "sign in to read",
                   "continue reading", "metered paywall", "premium content")

# §8.3: cloud metadata endpoints explicitly denied (belt over suspenders —
# link-local/range checks already cover the IPs, hostnames need names too).
METADATA_HOSTS = {"metadata.google.internal", "metadata.goog",
                  "instance-data", "instance-data-compute",
                  "169.254.169.254", "100.100.100.200", "fd00:ec2::254"}

_ZERO_WIDTH = re.compile(r"[\u200b-\u200d\ufeff\ufffc\u00ad]")


def _effective_ip(ip):
    """Unwrap carrier encodings to the address that actually gets packets:
    NAT64/DNS64 synthesis (64:ff9b::/96 embeds a plain IPv4) and IPv4-mapped
    forms. Without this, public sites on NAT64 networks (e.g. a GoDaddy
    homepage resolving to 64:ff9b::/96) are misclassified as internal and
    every probe of them fails."""
    try:
        if isinstance(ip, ipaddress.IPv6Address):
            if ip in ipaddress.ip_network("64:ff9b::/96"):
                return ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
            if ip.ipv4_mapped is not None:
                return ip.ipv4_mapped
    except Exception:
        pass
    return ip


def _ip_blocked(ip) -> bool:
    ip = _effective_ip(ip)
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _parse_numeric_host(h: str):
    """Integer/hex/octal IPv4 forms (http://2130706433/ == 127.0.0.1)."""
    import socket as _socket
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    try:  # dotted int/hex/octal quads via inet_aton
        packed = _socket.inet_aton(h)
        return ipaddress.ip_address(packed)
    except Exception:
        pass
    try:  # single decimal integer
        if h.isdigit():
            return ipaddress.ip_address(int(h))
    except Exception:
        pass
    return None


def is_internal_host(host: str) -> bool:
    h = host.lower().strip().rstrip(".")
    if h in METADATA_HOSTS:
        return True
    ip = _parse_numeric_host(h)
    if ip is not None:
        return _ip_blocked(ip)
    return h in ("localhost",) or h.endswith(".internal") or h.endswith(".local")


def assert_no_ssrf(url: str) -> None:
    """Literal check (fast path). Hostnames that resolve internally are
    checked by resolve_and_assert_no_ssrf before any socket connects."""
    host = urlparse(url).hostname or ""
    if is_internal_host(host):
        raise ValueError(f"SSRF blocked: internal host {host!r}")


def resolve_and_assert_no_ssrf(url: str, dns_timeout_s: float = 8.0) -> list[str]:
    """DNS check: resolve host, reject if ANY addr is internal.
    Returns resolved IP strings. Raises ValueError on block / DNS failure.
    getaddrinfo has no built-in timeout, so it runs in a worker thread."""
    import concurrent.futures
    import socket
    host = urlparse(url).hostname or ""
    assert_no_ssrf(url)  # literal fast path first
    # NB: no `with` block — Executor.__exit__ waits for the worker, which
    # would re-hang on stuck DNS. Shutdown without waiting instead.
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        infos = ex.submit(socket.getaddrinfo, host, None).result(
            timeout=dns_timeout_s)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"SSRF blocked: DNS resolution failed for {host!r}: {e}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    ips = sorted({info[4][0] for info in infos})
    for ip_str in ips:
        try:
            ip = ipaddress.ip_address(ip_str.split("%")[0])  # strip IPv6 zone
        except ValueError:
            raise ValueError(f"SSRF blocked: unparseable DNS answer {ip_str!r}")
        if _ip_blocked(ip):
            raise ValueError(f"SSRF blocked: {host!r} resolves to internal {ip_str!r}")
    return ips


def strip_instruction_shaped_text(text: str) -> str:
    return _INSTRUCTION_SHAPED.sub("[removed-instruction-shaped-text]", text)


# Span-integrity lock (§5.4): normalize_ingest_text is the FIRST and ONLY
# offset-preserving pre-span transform. All mappings are 1 char → 1 char,
# so char offsets assigned downstream never drift. Nothing after span
# assignment may touch section text — enforced by tests/test_spans.py
# (re-slice invariant + scrub idempotence).
_NORMALIZE_1TO1 = (
    (" ", " "),  # NBSP -> space
    (""", "'"), (""", "'"), ("„", '"'), ("“", '"'), ("”", '"'),
    ("–", "-"), ("—", "-"), ("…", "."),
)


def normalize_ingest_text(text: str) -> str:
    """Offset-preserving cleanup: 1:1 char maps only, never deletions."""
    if not isinstance(text, str) or not text:
        return text
    for src, dst in _NORMALIZE_1TO1:
        if src in text:
            text = text.replace(src, dst)
    return text


def scrub_retrieved_text(text: str) -> str:
    """§8.2 pre-ingest scrub: normalize once, then zero-width/hidden chars
    + instruction shapes. Single normalization point for all span paths."""
    text = normalize_ingest_text(text)
    text = _ZERO_WIDTH.sub("", text)
    return strip_instruction_shaped_text(text)


def classify_fetch(status_code: int, body: str) -> str:
    """HTTP 200 != success: challenge / cookie-wall / paywall stubs return 200.
    Returns ok | blocked | paywalled | empty | error (§5.3)."""
    lowered = body.lower()
    if any(m in lowered for m in CAPTCHA_MARKERS):
        return "blocked"
    if any(m in lowered for m in PAYWALL_MARKERS):
        return "paywalled"
    if not body.strip():
        return "empty"
    if status_code == 200:
        return "ok"
    if status_code in (401, 402, 403):
        return "blocked"
    return "error"


def wrap_as_data(content: str) -> str:
    """Delimited channel: retrieved content is DATA, never mixed into instructions."""
    return "<RETRIEVED_DATA>\n" + content + "\n</RETRIEVED_DATA>"
