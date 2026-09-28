"""Core fielded schemas. No field exists for inferred personal attributes by design.

Structuring follows spec §5.4 (doc_id, url_final, content_hash, fetched_at,
fetch_status, source_class, title, published_at, author, sections[] with
char_span, entities[]). Output contract follows §6.5 (person, company,
priorities[], capability_map[], likely_objection, opening_question,
unknowns[] — required).
"""
from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, Field


class SourceClass(str, Enum):
    FILING = "filing"
    REGISTRY = "registry"
    PRESS_RELEASE = "press_release"
    JOB_POSTING = "job_posting"
    NEWS = "news"
    VENDOR_PAGE = "vendor_page"
    TRANSCRIPT = "transcript"
    COURT_RECORD = "court_record"
    OTHER = "other"
    # Deprecated aliases kept for stored-data compatibility:
    EARNINGS_CALL = "earnings_call"
    EXEC_COMMENTARY = "exec_commentary"
    COMPANY_SITE = "company_site"


class FetchStatus(str, Enum):
    OK = "ok"
    BLOCKED = "blocked"
    PAYWALLED = "paywalled"
    EMPTY = "empty"
    ERROR = "error"
    ROBOTS_DENIED = "robots_denied"


class DocSection(BaseModel):
    section_id: str = ""
    heading: str = ""
    level: int = 0
    text: str
    char_start: int
    char_end: int


class EntityMention(BaseModel):
    type: str = ""  # person | org | role | money | date | other
    name: str
    confidence: float = 0.0


class StructuredDoc(BaseModel):
    doc_id: str
    url: str = ""                      # requested URL (legacy; prefer url_final)
    url_final: str = ""                # after redirects (§5.4)
    content_hash: str = ""             # sha256 of raw body (§5.2 snapshot key)
    fetched_at: str
    fetch_status: FetchStatus = FetchStatus.OK
    source_class: SourceClass
    title: str = ""
    published_at: str = ""
    author: str = ""
    sections: list[DocSection] = Field(default_factory=list)
    entities: list[EntityMention] = Field(default_factory=list)


class Claim(BaseModel):
    claim_id: str
    text: str
    doc_id: str
    section_id: str = ""
    section_index: int = 0
    char_start: int
    char_end: int
    recency: str = ""  # §7.5 recency tag, e.g. "2026-03" or "undated"


class Verdict(str, Enum):
    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    UNSUPPORTED = "unsupported"


class VerifiedClaim(BaseModel):
    claim: Claim
    verdict: Verdict
    note: str = ""


class PersonIdentity(BaseModel):
    """Pass 1 entity. Professional, publicly-stated info ONLY (§8.5)."""
    full_name: str
    company: str
    title: str = ""
    role: str = ""
    confidence: float = 0.0
    sources: list[str] = Field(default_factory=list)  # doc_ids §6.5
    human_confirmed: bool = False


class Priority(BaseModel):
    """§6.5 priorities[] — may be empty; never invented."""
    statement: str
    evidence: list[str] = Field(default_factory=list)  # claim_ids
    recency: str = ""
    confidence: float = 0.0


class CapabilityMap(BaseModel):
    our_capability: str
    their_priority_ref: str = ""
    rationale: str = ""


class Objection(BaseModel):
    """§6.5 likely_objection {statement, basis}."""
    statement: str = ""
    basis: str = ""


class Contradiction(BaseModel):
    """§7.5: conflicting sources surfaced with both claims and both dates."""
    claim_a: str = ""
    claim_b: str = ""
    recency_a: str = ""
    recency_b: str = ""
    note: str = ""


class RoleRef(BaseModel):
    """Current association: company + role, evidence-bound. LinkedIn-style
    'experience' without touching LinkedIn (§8.7)."""
    company: str
    role: str = ""
    evidence: list[str] = Field(default_factory=list)


class Reference(BaseModel):
    """Manual-check links. LinkedIn appears here ONLY as an un-fetched
    reference for the rep to open — never scraped (§8.7, §2.2)."""
    label: str
    url: str
    note: str = ""


class Brief(BaseModel):
    person: PersonIdentity
    firmographic: dict = Field(default_factory=dict)
    strategy_signals: list[VerifiedClaim] = Field(default_factory=list)
    pitch: str = ""
    gaps: list[str] = Field(default_factory=list)  # == unknowns[] (§6.5, required)
    # §6.5 output contract:
    priorities: list[Priority] = Field(default_factory=list)
    capability_map: list[CapabilityMap] = Field(default_factory=list)
    likely_objection: Objection = Field(default_factory=Objection)
    opening_question: str = ""
    contradictions: list[Contradiction] = Field(default_factory=list)
    current_roles: list[RoleRef] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    # Google-style person details: verified name-mention facts, NOT gated
    # by the strategy relevance_gate (bio/profile is the product here).
    person_details: list[VerifiedClaim] = Field(default_factory=list)
    # Structured biography: full bio, not just last 2-3 years. Deterministic
    # extractors over verified docs; each item is evidence-bound.
    bio: dict = Field(default_factory=dict)
    degraded: list[str] = Field(default_factory=list)  # §10.2 loud degradation
    # General background (UNVERIFIED, never evidence): LLM general knowledge
    # shown only when zero verified docs exist (e.g. unknown company).
    # Never enters strategy_signals/priorities/evidence/verdicts.
    general_knowledge: list[dict] = Field(default_factory=list)
