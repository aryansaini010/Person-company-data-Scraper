"""Acquisition Zone (DMZ): search & fetch only. No customer data, no CRM, no weights.
Outputs StructuredDoc records (§5.4) across the one-way queue into the Secure Core.

Spec MUSTs enforced here: snapshot every raw body content-addressed (§5.2),
classify before any model sees bytes (§5.3), robots_denied/paywalled/blocked
never reach a model."""
from __future__ import annotations
import hashlib
import os
import re
import time
from pathlib import Path
from . import audit
from .fetcher import FetchResult
from .schemas import (DocSection, EntityMention, FetchStatus, SourceClass,
                      StructuredDoc)
from .security import (assert_no_ssrf, classify_fetch,
                       scrub_retrieved_text,
                       strip_instruction_shaped_text)

SNAPSHOT_DIR = Path(os.environ.get("PROSPECT_SNAPSHOTS",
                    Path(__file__).resolve().parent.parent / "snapshots"))

_MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s?(?:million|billion|trillion|M|B|K)?",
                    re.IGNORECASE)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def snapshot_raw(raw: bytes) -> str:
    """Immutable snapshot, content-addressed (§5.2). Returns hex sha256.
    Atomic under threads (O_CREAT|O_EXCL); disk-full raises OSError.
    Phase 2: mirrors to MinIO (best-effort) when MINIO_ENDPOINT is set;
    FS stays the source of truth so tests/offline keep working."""
    import os as _os
    h = hashlib.sha256(raw).hexdigest()
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    p = SNAPSHOT_DIR / h
    if not p.exists():
        try:
            fd = _os.open(str(p), _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL,
                          0o644)
        except FileExistsError:
            pass
        else:
            try:
                with _os.fdopen(fd, "wb") as f:
                    f.write(raw)
            except BaseException:
                try:
                    p.unlink()
                except OSError:
                    pass
                raise
    try:
        from .snapshot_store import mirror_snapshot
        mirror_snapshot(h, bytes(raw))
    except Exception:
        pass
    return h


def extract_entities(text: str) -> list[EntityMention]:
    """Deterministic extractors only (money, dates). No model dependency."""
    ents: list[EntityMention] = []
    for m in _MONEY.finditer(text[:50_000]):
        ents.append(EntityMention(type="money", name=m.group(0)[:60],
                                  confidence=0.9))
    for m in set(_YEAR.findall(text[:50_000])):
        ents.append(EntityMention(type="date", name=m, confidence=0.9))
    return ents[:100]


def structure_fetch(url: str, status_code: int, raw_body: str,
                    source_class: SourceClass = SourceClass.OTHER) -> StructuredDoc | None:
    """Classify first; on non-ok NEVER feed to a model — alert via audit log."""
    from .security import resolve_and_assert_no_ssrf
    try:
        resolve_and_assert_no_ssrf(url)
    except ValueError as e:
        audit.log("fetch_rejected", {"url": url, "status": "ssrf_blocked",
                                     "detail": str(e)[:200]})
        return None
    verdict = classify_fetch(status_code, raw_body)
    if verdict != "ok":
        audit.log("fetch_rejected", {"url": url, "status": verdict,
                                     "http": status_code})
        return None
    try:
        content_hash = snapshot_raw(raw_body.encode())
    except OSError as e:
        audit.log("fetch_rejected", {"url": url, "status": "snapshot_failed",
                                     "detail": f"{type(e).__name__}"})
        return None
    clean = scrub_retrieved_text(raw_body)[:200_000]
    doc_id = "doc_" + hashlib.sha256(
        (url + content_hash).encode()).hexdigest()[:16]
    section = DocSection(section_id=f"{doc_id}#s0", heading="", level=0,
                         text=clean, char_start=0, char_end=len(clean))
    doc = StructuredDoc(doc_id=doc_id, url=url, url_final=url,
                        content_hash=hashlib.sha256(
                            raw_body.encode()).hexdigest(),
                        fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 time.gmtime()),
                        fetch_status=FetchStatus.OK,
                        source_class=source_class, sections=[section],
                        entities=extract_entities(clean))
    audit.log("doc_structured", {"doc_id": doc_id, "url": url,
                                 "source_class": source_class.value})
    return doc


def structure_result(fr: FetchResult,
                     source_class: SourceClass = SourceClass.OTHER,
                     ) -> StructuredDoc | None:
    """Full §5.4 path: snapshot raw bytes, classify extracted text, field record."""
    assert_no_ssrf(fr.url_final or fr.url)
    if fr.robots_denied:
        return None  # already audit-logged in fetcher
    verdict = classify_fetch(fr.status_code, fr.body_text)
    if verdict != "ok":
        audit.log("fetch_rejected", {"url": fr.url_final, "status": verdict,
                                     "http": fr.status_code})
        return None
    content_hash = snapshot_raw(fr.raw_body) if fr.raw_body else hashlib.sha256(
        fr.body_text.encode()).hexdigest()
    clean = scrub_retrieved_text(fr.body_text)[:200_000]
    # doc_id binds URL + content: identical bodies at different URLs stay
    # distinct records (no wrong merge); content_hash pins the raw bytes.
    doc_id = "doc_" + hashlib.sha256(
        ((fr.url_final or fr.url) + content_hash).encode()).hexdigest()[:16]
    section = DocSection(section_id=f"{doc_id}#s0", heading="", level=0,
                         text=clean, char_start=0, char_end=len(clean))
    doc = StructuredDoc(
        doc_id=doc_id, url=fr.url, url_final=fr.url_final or fr.url,
        content_hash=content_hash,
        fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        fetch_status=FetchStatus(verdict), title=fr.title,
        published_at=fr.published or "",
        source_class=source_class, sections=[section],
        entities=extract_entities(clean))
    audit.log("doc_structured", {"doc_id": doc_id, "url": doc.url_final,
                                 "snapshot": content_hash,
                                 "source_class": source_class.value})
    return doc


def simhash64(text: str, width: int = 4) -> int:
    """64-bit SimHash over char-n-gram shingles (stdlib only, no new deps).

    Secondary dedup signal: catches syndicated copies across sister domains
    and reissued press releases that sha256 (exact bytes) misses. Primary
    identity stays content_hash; intake suppresses Hamming <= 3 (exact /
    whitespace-level copies — template-similar but distinct reports must
    survive to Pass 3's story-level dedup, never die here).
    """
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not t:
        return 0
    k = max(2, int(width or 4))
    shingles = [t[i:i + k] for i in range(max(1, len(t) - k + 1))]
    acc = [0] * 64
    for sh in shingles:
        h = int.from_bytes(hashlib.blake2b(sh.encode(), digest_size=8).digest(),
                           "big")
        for b in range(64):
            acc[b] += 1 if (h >> b) & 1 else -1
    out = 0
    for b in range(64):
        if acc[b] > 0:
            out |= (1 << b)
    return out


def simhash_distance(a: int, b: int) -> int:
    """Hamming distance between two SimHashes."""
    return bin((a or 0) ^ (b or 0)).count("1")


def stamp_docs(docs: list[StructuredDoc],
               meta: dict[str, tuple[str, str]]) -> list[StructuredDoc]:
    """Stamp feed metadata (title, published_at) onto structured docs,
    matched by requested URL. Feed titles beat bare <title> extraction."""
    for d in docs:
        key = d.url if d.url in meta else (d.url_final or "")
        if key in meta:
            title, pub = meta[key]
            if title:
                d.title = title
            if pub:
                d.published_at = pub
    return docs


def structure_rss_items(items: list[tuple[str, str, str, str]],
                        source_class: SourceClass = SourceClass.NEWS,
                        ) -> list[StructuredDoc]:
    """Structure aggregator headlines as citable NEWS docs.

    items: (url, title, published_at, outlet). The headline + outlet + date
    is stored verbatim as the span — weaker provenance than full text, but
    labeled as such (title carries the outlet). Never expanded beyond the
    headline: no invented detail, recency exact (§7.5)."""
    docs: list[StructuredDoc] = []
    for url, title, pub, outlet in items:
        if not title.strip():
            continue  # headline is the substance; nothing to cite without it
        body = scrub_retrieved_text(
            f"{title}\nOutlet: {outlet}\nPublished: {pub}".strip())[:2000]
        content_hash = snapshot_raw(body.encode())
        doc_id = "doc_" + content_hash[:16]
        docs.append(StructuredDoc(
            doc_id=doc_id, url=url, url_final=url, content_hash=content_hash,
            fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            fetch_status=FetchStatus.OK, source_class=source_class,
            title=f"{title[:200]} [{outlet}]", published_at=pub,
            sections=[DocSection(section_id=doc_id + "#s0", text=body,
                                 char_start=0, char_end=len(body))],
            entities=extract_entities(body)))
    if docs:
        audit.log("rss_structured", {"docs": len(docs)})
    return docs


def queue_to_core(doc: StructuredDoc, queue: list) -> None:
    """One-way queue: structured documents only cross into the core."""
    if not isinstance(doc, StructuredDoc):
        raise TypeError("only StructuredDoc may cross into Secure Core")
    queue.append(doc)
    body = "\n".join(s.text for s in doc.sections)
    try:
        from . import store
        con = store.connect()
        store.doc_save(con, doc.doc_id, doc.url_final or doc.url,
                       doc.fetched_at, doc.source_class.value, body)
        store.queue_push(con, doc.doc_id)
        con.close()
    except Exception as e:
        audit.log("queue_store_failed", {"doc_id": doc.doc_id,
                                         "detail": f"{type(e).__name__}"})
    # Phase 2 mirrors (best-effort, never blocking): OpenSearch BM25+kNN.
    try:
        from .snapshot_store import index_doc
        from .models import EmbeddingModel
        vec = None
        try:
            vec = EmbeddingModel().embed(body[:2000])
        except Exception:
            vec = None
        index_doc(doc.doc_id, doc.url_final or doc.url, doc.fetched_at,
                  doc.source_class.value, body, vec)
    except Exception:
        pass
    audit.log("queued_to_core", {"doc_id": doc.doc_id})


def acquire(urls: list[str],
            source_class: SourceClass = SourceClass.OTHER,
            max_workers: int = 4,
            char_budget: int = 400_000,
            deadline: float | None = None,
            smart: bool = True) -> list[StructuredDoc]:
    """Full DMZ path for real URLs: fetch -> classify -> structure -> queue.
    URLs fetch in parallel (order preserved); classification/structuring stay
    sequential per doc. §6.4 guardrails: char budget stops intake (partial,
    marked downstream); batches circuit-break when errors dominate.
    Blocked/paywalled/empty/robots_denied never reach a model.
    deadline: stop intake and return partial when the request budget is
    spent. smart=False uses plain HTTP only (no render fallback) — for
    high-volume diet fetching where headlines already cover."""
    import concurrent.futures
    import os as _os
    from . import deadline as _dl
    from .fetcher import fetch_url_smart
    from .fetcher import fetch_url_smart

    try:
        char_budget = int(_os.environ.get("PROSPECT_CHAR_BUDGET", char_budget))
    except ValueError:
        pass

    def _one(url: str):
        try:
            return ("ok", fetch_url_smart(url, render=smart))
        except ValueError as e:  # SSRF / scheme refusal / redirects
            return ("ssrf", url, str(e))
        except Exception as e:  # DNS/connect/TLS/timeout: evidence absent
            return ("error", url, f"{type(e).__name__}: {e}")

    docs: list[StructuredDoc] = []
    used_chars, errors, ssrf_blocks = 0, 0, 0
    _batch_hashes: list[int] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for i in range(0, len(urls), max_workers):
            if _dl.expired(deadline):
                audit.log("budget_exhausted",
                          {"reason": "deadline", "docs": len(docs)})
                return docs
            batch = urls[i:i + max_workers]
            futs = {ex.submit(_one, u): u for u in batch}
            for fut in futs:
                try:
                    res = fut.result(timeout=60)  # one stuck fetch can't hang the batch
                except Exception as e:
                    audit.log("fetch_rejected", {"url": futs[fut],
                                                 "status": "timeout",
                                                 "detail": f"{type(e).__name__}"})
                    errors += 1
                    continue
                if res[0] != "ok":
                    _, url, detail = res
                    status = "ssrf_blocked" if res[0] == "ssrf" else "error"
                    if res[0] == "ssrf":
                        ssrf_blocks += 1
                    else:
                        errors += 1
                    audit.log("fetch_rejected", {"url": url, "status": status,
                                                 "detail": detail})
                    continue
                fr = res[1]
                if fr.robots_denied:
                    continue  # already audit-logged in fetcher
                try:
                    doc = structure_result(fr, source_class)
                except OSError as e:  # disk-full etc: per-URL failure, not a crash
                    audit.log("fetch_rejected",
                              {"url": fr.url_final or fr.url,
                               "status": "structure_failed",
                               "detail": f"{type(e).__name__}"})
                    errors += 1
                    continue
                if doc is not None:
                    # Near-dup intake suppression (syndication/sister domains):
                    # Hamming <= 3 over SimHash means near-identical body —
                    # drop loudly so duplicate extraction cycles can't bias
                    # the verifier. Exact identity stays content_hash-based.
                    try:
                        _body = "\n".join(s.text for s in doc.sections)
                        _sh = simhash64(_body)
                        _dup = any(simhash_distance(_sh, _h) <= 3
                                   for _h in _batch_hashes)
                        if _dup:
                            audit.log("duplicate_content",
                                      {"url": fr.url_final or fr.url,
                                       "doc_id": doc.doc_id,
                                       "reason": "near-identical body already in batch"})
                            continue
                        _batch_hashes.append(_sh)
                    except Exception:
                        pass
                    used_chars += sum(len(s.text) for s in doc.sections)
                    queue_to_core(doc, _MEM_QUEUE)
                    docs.append(doc)
                    if used_chars >= char_budget:
                        audit.log("budget_exhausted",
                                  {"chars": used_chars, "docs": len(docs)})
                        return docs
            # Breaker counts real errors only: SSRF refusals are policy
            # outcomes, not outages — they must not DoS the rest (§6.4).
            if errors >= 3 and errors / max(1, len(docs) + errors) > 0.6:
                audit.log("circuit_open",
                          {"errors": errors, "docs": len(docs)})
                break
    return docs


_MEM_QUEUE: list = []
