# On-Prem Prospect Intelligence — MVP skeleton

Derived from `prospect-intelligence-plan.md` (simplified walkthrough of v1.0 spec).
Guiding rule: **fluent and wrong is worse than absent.**

## Layout

- `prospect_intel/schemas.py` — StructuredDoc, Claim, Verdict, Brief. No inferred personal attributes by construction.
- `prospect_intel/acquisition.py` — DMZ side: fetch classification (200 != success), structuring with char_span.
- `prospect_intel/verifier.py` — isolated blind check: sees ONLY (claim, source span).
- `prospect_intel/passes.py` — Pass 1 (human-gated) → Pass 2 → Pass 3 (bounded) → Pass 4 (no network).
- `prospect_intel/security.py` — SSRF guard, instruction-strip, delimited DATA channels.
- `prospect_intel/audit.py` — append-only JSONL run log.
- `prospect_intel/models.py` — Agent/Utility/Embedding/Reranker interfaces + mocks (pools A/B).
- `api.py` — FastAPI: briefs + human confirm gate.
- `storage/` — Postgres schema + OpenSearch mapping.
- `docker-compose.yml` — postgres + opensearch (core backing services).

## Run (no Docker/models needed for skeleton)

```powershell
pip install -r requirements.txt
pytest -q
python -m uvicorn api:app --reload
```

## Live run (real fetch -> verify -> brief)

```powershell
python demo_live.py
python -m prospect_intel.cli --name "Jane Doe" --company Acme --urls https://example.com
```

`POST /research {"name","company","urls"|"query"}` runs the full
DMZ->core pipeline. Persistence is SQLite (`prospect.db`, same schema as
`storage/schema.sql` + FTS5 as the OpenSearch stand-in); Postgres/OpenSearch
attach via env when available. Docker daemon not required.

## Company-first briefing

Person lookup by bare name is fragile (namesakes, no footprint); company
lookup resolves deterministically. Two paths use the company as the backbone:

- `POST /company-research {"company"}` — company name in, company brief
  out. No person, no Pass-1 identity gate. Pure-company queries may use
  Tier-3 breadth (no person intent, §5.1).
- Thin-person fallback (automatic): when identity evidence is weak (top
  candidate < 0.6 and < 2 name-matched docs), the person flow pivots to a
  company-depth brief — person facts stay unknown instead of padded.

## Open WebUI loop (chat + agents over briefs)

1. Start this backend (note the port), then in Open WebUI add an OpenAPI
   tool server: Settings → Integrations → Tools → `+` →
   `http://localhost:<port>/openapi.json` (use `host.docker.internal`
   instead of `localhost` if Open WebUI runs in Docker).
2. Agent-friendly surface: every brief response carries flat `evidence[]`
   (statement, quote, URL, recency, confidence) and `unknowns[]`
   (field, code, detail) plus `doc_urls` — no multi-hop joins needed.
3. `GET /briefs/{id}/readable.txt` renders any brief as plain text;
   `POST /openwebui/push {"brief_id", "knowledge_id"}` uploads it to
   Open WebUI Files/Knowledge server-side (needs `OPENWEBUI_API_KEY`).
   CLI equivalent: `--upload-knowledge`.
4. Headless extraction: `prospect_intel.openwebui.chat_extract()` drives
   chat completions with `tool_ids` (your registered `:8003` tools).
5. Human gate stays default: `/research` → you pick → `/research/confirm`.
   `/company-research` is the single-call agent path.

## Connecting tiers (clears the [Degraded inputs] lines)

```powershell
Copy-Item .env.example .env   # then edit .env in VS Code
```

| Notice | Fix | What unlocks |
|---|---|---|
| `Tier-1 news connector down (no key)` | Free key at newsapi.org/register → `NEWSAPI_KEY=` in `.env` | Full-text news reach via NewsAPI |
| `SearXNG not configured` | Start Docker Desktop → `docker compose up -d searxng` → `SEARXNG_URL=http://localhost:8080` in `.env` | Self-hosted breadth search (queries never leave your box) |
| `Tier-3 refused by routing rule` | Nothing — permanent by design (§5.1 confidentiality) | — |
| `Tier-1 registry lookup empty` | Nothing to install: GLEIF registry proof (legal name, jurisdiction, LEI status) runs keyless already — small private companies often carry no LEI, so this gap is honest coverage, not missing setup | Verified registry record when one exists |
| `Tier-1 enrichment connector down (no key)` | Free key at hunter.io → `HUNTER_API_KEY=` in `.env` (free monthly credits) | Company profile by domain (industry, size, location) once a homepage is probed |

Keyless and always on: Wikipedia reference, Google News RSS headlines,
DBpedia firmographics, Wikidata person/company facts, GLEIF registry proof,
local Firecrawl render (`localhost:3002`). LinkedIn stays manual-check links
only (§8.7) — person/company enrichment comes from official free APIs
(Wikidata, GLEIF, Hunter) via `prospect_intel/enrich.py`,
never scraping.

## MVP scope decisions (from walkthrough §7)

Deferred, behind `MCP tool boundary` (`prospect_intel/models.py:ToolBoundary`): SIEVE, deep-research models, Zoom Search, ADK 2.0, LinkedIn scraping, Apple Silicon serving.
