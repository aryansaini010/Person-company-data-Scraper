"""Snapshot + index mirrors (Phase 2: MinIO + OpenSearch, best-effort).

FS/SQLite stay the source of truth — these mirrors never block ingestion.
All functions swallow errors and return False/None so offline tests pass
with zero services running. Wire real credentials via .env when ready.
"""
from __future__ import annotations


def mirror_snapshot(content_hash: str, raw: bytes) -> bool:
    """PUT snapshots/<hash> to MinIO bucket. Returns True on success."""
    import os
    if not os.environ.get("MINIO_ENDPOINT", ""):
        return False
    try:
        from minio import Minio  # type: ignore
        import io as _io
        client = Minio(os.environ["MINIO_ENDPOINT"],
                       access_key=os.environ.get("MINIO_ROOT_USER",
                                                 "minioadmin"),
                       secret_key=os.environ.get("MINIO_ROOT_PASSWORD",
                                                 "minioadmin"),
                       secure=os.environ.get("MINIO_SECURE", "") == "1")
        bucket = os.environ.get("MINIO_BUCKET", "prospect-snapshots")
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
        client.put_object(bucket, content_hash, _io.BytesIO(raw), len(raw))
        return True
    except Exception:
        return False


def index_doc(doc_id: str, url: str, fetched_at: str, source_class: str,
              text: str, embedding: list[float] | None = None) -> bool:
    """Index a doc into OpenSearch (BM25 + kNN). Returns True on success."""
    import os
    base = (os.environ.get("OPENSEARCH_URL", "") or "").rstrip("/")
    if not base:
        return False
    try:
        from opensearchpy import OpenSearch  # type: ignore
        client = OpenSearch(base)
        body = {"doc_id": doc_id, "url": url, "fetched_at": fetched_at,
                "source_class": source_class,
                "sections": [{"text": (text or "")[:20000]}]}
        if embedding:
            body["embedding"] = list(embedding)[:384]
        client.index(index="prospect_docs", id=doc_id, body=body,
                     refresh=False)
        return True
    except Exception:
        return False


def pg_available() -> bool:
    """True when DATABASE_URL is set and psycopg can connect."""
    import os
    if not os.environ.get("DATABASE_URL", ""):
        return False
    try:
        import psycopg  # type: ignore
        with psycopg.connect(os.environ["DATABASE_URL"],
                             connect_timeout=3) as _:
            pass
        return True
    except Exception:
        return False
