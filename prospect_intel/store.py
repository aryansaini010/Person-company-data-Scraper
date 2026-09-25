"""Durable core store: SQLite by default (zero-ops laptop run).

NOTE: Postgres/OpenSearch attach is aspirational — this module always uses
SQLite (same logical schema as storage/schema.sql). Set PROSPECT_DB to point
at a file; a Postgres URL will NOT work (it would become a filename).

Tables: persons, firmographics (90-day TTL ~= quarterly refresh), docs
(+ FTS5 index as the OpenSearch stand-in), claims, queue (one-way DMZ->core),
collateral, audit_log (hash-chained: each row links prev_hash, tamper-evident).
"""
from __future__ import annotations
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("PROSPECT_DB",
               Path(__file__).resolve().parent.parent / "prospect.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS persons(id TEXT PRIMARY KEY, full_name TEXT NOT NULL,
 company TEXT NOT NULL, title TEXT DEFAULT '', human_confirmed INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS firmographics(company TEXT PRIMARY KEY, profile TEXT NOT NULL,
 refreshed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS docs(doc_id TEXT PRIMARY KEY, url TEXT NOT NULL,
 fetched_at TEXT NOT NULL, source_class TEXT NOT NULL, text TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(doc_id, text);
CREATE TABLE IF NOT EXISTS claims(claim_id TEXT PRIMARY KEY, brief_id TEXT NOT NULL,
 text TEXT NOT NULL, doc_id TEXT NOT NULL, section_index INT NOT NULL,
 char_start INT NOT NULL, char_end INT NOT NULL, verdict TEXT NOT NULL, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS queue(seq INTEGER PRIMARY KEY AUTOINCREMENT,
 doc_id TEXT NOT NULL, received INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS collateral(id TEXT PRIMARY KEY, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS briefs(id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, data TEXT NOT NULL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log(seq INTEGER PRIMARY KEY AUTOINCREMENT,
 ts REAL NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL,
 prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
DELETE FROM queue WHERE seq NOT IN
 (SELECT MIN(seq) FROM queue GROUP BY doc_id, received);
CREATE UNIQUE INDEX IF NOT EXISTS queue_doc_uniq ON queue(doc_id, received);
CREATE INDEX IF NOT EXISTS audit_event_idx ON audit_log(event);
CREATE INDEX IF NOT EXISTS queue_received_idx ON queue(received);
CREATE INDEX IF NOT EXISTS briefs_created_idx ON briefs(created_at);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions(expires_at);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), timeout=30.0)
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    con.executescript(SCHEMA)
    return con


def doc_save(con: sqlite3.Connection, doc_id: str, url: str, fetched_at: str,
             source_class: str, text: str) -> None:
    con.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?)",
                (doc_id, url, fetched_at, source_class, text))
    con.execute("DELETE FROM docs_fts WHERE doc_id=?", (doc_id,))
    con.execute("INSERT INTO docs_fts(doc_id, text) VALUES (?,?)",
                (doc_id, text))
    con.commit()


def _fts_escape(query: str) -> str:
    """AND of quoted tokens: raw user text (C++, quotes, colons) must never
    raise OperationalError (callers swallow it as 'no results'), while
    multi-term queries still match non-adjacent terms."""
    import re as _re
    toks = [t for t in _re.findall(r"[^\s]+", query or "") if t.strip('"')]
    if not toks:
        return '""'
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in toks)


def doc_search(con: sqlite3.Connection, query: str, limit: int = 8) -> list[str]:
    try:
        rows = con.execute(
            "SELECT doc_id FROM docs_fts WHERE docs_fts MATCH ? "
            "ORDER BY bm25(docs_fts) LIMIT ?",
            (_fts_escape(query), limit)).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def hybrid_search_docs(con: sqlite3.Connection, query: str,
                       limit: int = 10, overfetch: int = 50) -> list:
    """Hybrid retrieval (plan §10): BM25 + vector + boolean -> RRF -> rerank.

    - BM25: doc_search overfetch (boolean AND via _fts_escape).
    - Vector: EmbeddingModel cosine over the same candidate texts.
    - Merge: reciprocal-rank fusion (k=60), then Reranker.top(k=limit).
    Returns StructuredDocs (same AS-SAVED guarantee as corpus_search_docs).
    Falls back to BM25-only when embeddings fail so offline tests pass.
    """
    from .schemas import DocSection, FetchStatus, SourceClass, StructuredDoc
    try:
        bm25_ids = doc_search(con, query, overfetch)
    except Exception:
        bm25_ids = []
    if not bm25_ids:
        return []
    texts: dict[str, str] = {}
    meta: dict[str, tuple] = {}
    for doc_id in bm25_ids:
        try:
            row = con.execute("SELECT url, fetched_at, source_class, text"
                              " FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
        except Exception:
            continue
        if not row:
            continue
        url, fetched_at, sc, text = row
        texts[doc_id] = text or ""
        meta[doc_id] = (url, fetched_at, sc)
    if not texts:
        return []
    # Vector rank over candidates (cosine; bge-m3 when Ollama on).
    # Batched via embed_many + shared LRU: repeat briefs cost zero calls.
    vec_rank: list[str] = []
    try:
        from .models import EmbeddingModel
        emb = EmbeddingModel()
        ids = list(texts.keys())
        vecs = emb.embed_many([query or ""] + [texts[d][:2000] for d in ids])
        qv, dvs = vecs[0], vecs[1:]
        import math as _m

        def _cos(a, b):
            try:
                dot = sum(x * y for x, y in zip(a, b))
                na = _m.sqrt(sum(x * x for x in a)) or 1.0
                nb = _m.sqrt(sum(y * y for y in b)) or 1.0
                return dot / (na * nb)
            except Exception:
                return 0.0

        scored = [(_cos(qv, v), d) for v, d in zip(dvs, ids)]
        scored.sort(key=lambda t: -t[0])
        vec_rank = [d for _, d in scored]
    except Exception:
        vec_rank = []
    # RRF fuse (k=60) of BM25 order + vector order.
    rrf: dict[str, float] = {}
    for rank, doc_id in enumerate(bm25_ids):
        rrf[doc_id] = rrf.get(doc_id, 0.0) + 1.0 / (60 + rank + 1)
    for rank, doc_id in enumerate(vec_rank):
        rrf[doc_id] = rrf.get(doc_id, 0.0) + 1.0 / (60 + rank + 1)
    fused = sorted(rrf.items(), key=lambda kv: -kv[1])
    fused_ids = [d for d, _ in fused][:max(overfetch, limit)]
    # Rerank top-50 -> top-k via Pool-B utility (same interface, LLM later).
    try:
        from .models import Reranker
        order_texts = [texts[d] for d in fused_ids]
        ranked_texts = Reranker().top(query or "", order_texts, k=limit)
        # map back preserving duplicates-safe order
        seen: set[str] = set()
        out_ids: list[str] = []
        for t in ranked_texts:
            for d in fused_ids:
                if d not in seen and texts[d] == t:
                    seen.add(d)
                    out_ids.append(d)
                    break
        fused_ids = out_ids or fused_ids[:limit]
    except Exception:
        fused_ids = fused_ids[:limit]
    docs = []
    for doc_id in fused_ids[:limit]:
        url, fetched_at, sc = meta[doc_id]
        try:
            source_class = SourceClass(sc)
        except ValueError:
            source_class = SourceClass.OTHER
        text = texts[doc_id]
        docs.append(StructuredDoc(
            doc_id=doc_id, url=url, url_final=url, content_hash="",
            fetched_at=fetched_at, fetch_status=FetchStatus.OK,
            source_class=source_class,
            sections=[DocSection(section_id=doc_id + "#s0", text=text,
                                 char_start=0, char_end=len(text))]))
    return docs


def corpus_search_docs(con: sqlite3.Connection, query: str,
                       limit: int = 6) -> list:
    """§7.2 compounding: answer from the corpus before hitting the network.
    Returns StructuredDocs rebuilt from stored rows. Stored text is served
    AS SAVED (spans stay valid); no re-cleaning on load, so saved claim
    offsets keep matching the served sections."""
    from .schemas import DocSection, FetchStatus, SourceClass, StructuredDoc
    docs = []
    for doc_id in doc_search(con, query, limit):
        row = con.execute("SELECT url, fetched_at, source_class, text FROM docs"
                          " WHERE doc_id=?", (doc_id,)).fetchone()
        if not row:
            continue
        url, fetched_at, sc, text = row
        try:
            source_class = SourceClass(sc)
        except ValueError:
            source_class = SourceClass.OTHER
        text = text or ""
        docs.append(StructuredDoc(
            doc_id=doc_id, url=url, url_final=url, content_hash="",
            fetched_at=fetched_at, fetch_status=FetchStatus.OK,
            source_class=source_class,
            sections=[DocSection(section_id=doc_id + "#s0", text=text,
                                 char_start=0, char_end=len(text))]))
    return docs


def briefs_today(con: sqlite3.Connection) -> int:
    """Count briefs created since local midnight (§6.4 quota input)."""
    import datetime
    start = datetime.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    row = con.execute("SELECT COUNT(*) FROM briefs WHERE created_at>=?",
                      (start,)).fetchone()
    return row[0] if row else 0


def queue_push(con: sqlite3.Connection, doc_id: str) -> None:
    """One-way: DMZ inserts; core consumes via queue_pop. No path back.
    Re-pushes are ignored (unique doc_id per unreceived batch)."""
    con.execute("INSERT OR IGNORE INTO queue(doc_id) VALUES (?)", (doc_id,))
    con.commit()


def queue_pop(con: sqlite3.Connection, limit: int = 50) -> list[str]:
    con.execute("BEGIN IMMEDIATE")
    try:
        rows = con.execute(
            "SELECT seq, doc_id FROM queue WHERE received=0 ORDER BY seq LIMIT ?",
            (limit,)).fetchall()
        for seq, _ in rows:
            con.execute("UPDATE queue SET received=1 WHERE seq=?", (seq,))
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    return [doc_id for _, doc_id in rows]


def _norm_key(company: str) -> str:
    return (company or "").strip().casefold()


def firmo_get(con: sqlite3.Connection, company: str, ttl_days: int = 90) -> dict | None:
    row = con.execute("SELECT profile, refreshed_at FROM firmographics WHERE company=?",
                      (_norm_key(company),)).fetchone()
    if not row:
        return None
    try:
        profile = json.loads(row[0])
    except (ValueError, TypeError):
        return None  # corrupted row: treat as miss, re-probe
    refreshed = row[1]
    if time.time() - refreshed > ttl_days * 86400:
        return None  # stale -> re-probe
    return profile


def firmo_put(con: sqlite3.Connection, company: str, profile: dict) -> None:
    try:
        json.dumps(profile)
    except (ValueError, TypeError):
        profile = {"company": company, "registry": "unknown",
                   "note": "unserializable profile replaced"}
    con.execute("INSERT OR REPLACE INTO firmographics VALUES (?,?,?)",
                (_norm_key(company), json.dumps(profile), time.time()))
    con.commit()


def audit_chain(con: sqlite3.Connection, event: str, payload: dict) -> dict:
    try:
        safe = json.dumps(payload)
    except (ValueError, TypeError):
        safe = json.dumps({"unserializable": True})
        payload = {"unserializable": True}
    con.execute("BEGIN IMMEDIATE")  # single-writer: no chain forks
    try:
        prev = con.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = prev[0] if prev else "GENESIS"
        ts = time.time()
        body = json.dumps({"ts": ts, "event": event, "payload": payload,
                           "prev": prev_hash}, sort_keys=True)
        h = hashlib.sha256(body.encode()).hexdigest()
        con.execute("INSERT INTO audit_log(ts,event,payload,prev_hash,hash) VALUES (?,?,?,?,?)",
                    (ts, event, safe, prev_hash, h))
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    return {"ts": ts, "event": event, "hash": h, "prev": prev_hash}


def _session_encode(sess: dict) -> str:
    """Sessions hold StructuredDocs (pydantic) — persist as plain JSON.

    Docs are stored via model_dump; dates/bytes coerced to str. Anything
    unserializable falls back to repr so a session never blocks the gate.
    """
    def _default(o):
        try:
            if hasattr(o, "model_dump"):
                return o.model_dump()
            return str(o)
        except Exception:
            return repr(o)
    try:
        return json.dumps(sess, default=_default)
    except Exception:
        return json.dumps({"unserializable": True})


def session_save(con: sqlite3.Connection, sid: str, sess: dict,
                 ttl_s: float = 1800) -> None:
    now = time.time()
    con.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",
                (sid, _session_encode(sess), now, now + ttl_s))
    con.commit()


def session_load(con: sqlite3.Connection, sid: str):
    from .schemas import StructuredDoc
    row = con.execute("SELECT data, expires_at FROM sessions WHERE id=?",
                      (sid,)).fetchone()
    if not row:
        return None
    data, expires_at = row
    if time.time() > (expires_at or 0):
        try:
            con.execute("DELETE FROM sessions WHERE id=?", (sid,))
            con.commit()
        except Exception:
            pass
        return None
    try:
        sess = json.loads(data)
    except Exception:
        return None
    # Rehydrate docs: dicts -> StructuredDoc so passes keep working.
    try:
        docs = sess.get("docs")
        if isinstance(docs, list) and docs and isinstance(docs[0], dict):
            sess["docs"] = [StructuredDoc.model_validate(d) for d in docs]
    except Exception:
        pass
    return sess


def session_delete(con: sqlite3.Connection, sid: str) -> None:
    try:
        con.execute("DELETE FROM sessions WHERE id=?", (sid,))
        con.commit()
    except Exception:
        pass


def session_sweep(con: sqlite3.Connection, now: float | None = None) -> int:
    now = now if now is not None else time.time()
    try:
        cur = con.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        con.commit()
        return cur.rowcount or 0
    except Exception:
        return 0
