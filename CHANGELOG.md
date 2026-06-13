# Changelog

## v0.2.5 (2026-06-13)

**Bugfix: `is_success()` statt rohem `status_code == 200` — Qdrant 201/204 werden nicht mehr fälschlich als Fehler gewertet.**

### Bugfixes (29 Stellen in 10 Dateien)

- **is_success() Helper** in `nexus/config.py` — zentrale Prüfung `200 <= code < 300` statt überall `== 200`
- **apply.py** (9 Stellen) — Batch-Operationen sicher
- **events.py** (7 Stellen) — Event-CRUD sicher
- **staging.py** (3 Stellen) — Stage/Promote sicher
- **nexus/__init__.py** (2 Stellen) — API-Interface sicher
- **provenance/__init__.py** (3 Stellen) — Provenance-Tracking sicher
- **cli.py** (1 Stelle) — CLI-verify sicher
- **mcp_server.py** (1 Stelle) — Embedding-Detection sicher
- **retrieval/__init__.py** (1 Stelle) — Rerank-Abbruch sicher
- **examples/nexus-sica-analyzer.py + nexus_search.py** (2 Stellen)

### Code-Qualität (simplify-code)

- **TRUST_EPSILON konsolidiert** — gleicher Wert (0.01) in recompute_trust + recompute_all (vorher 0.01 vs 1e-9)
- **EVENT_TYPES aus Enum abgeleitet** — single source of truth statt Duplikat
- **Deprecated `asyncio.get_event_loop()`** durch `get_running_loop()` ersetzt
- **Re-Embedding bei Hybrid-Fallback eliminiert** — ein API-Call statt zwei
- **Unused imports entfernt** (json, Any, datetime, timezone, sys locales)
- **Unused constants entfernt** (STATUS_CONTESTED, RETRACTED, HISTORICAL, VALID_STATUSES)

### CI / Qualitätssicherung

- **Audit GitHub Action** — automatischer Check bei jedem Push:
  - Collection-Name-Check (findet `openclaw-memory`, `hermes-memory-1024d` etc.)
  - Status-Code-Check (findet rohe `== 200`)
  - Python Compile-Check (alle Dateien kompilierbar)
  - pytest

### config.py

- **Docstring korrigiert** — sagt "nexus" statt "hermes-memory" (Code war bereits korrekt)

### Migration

No change — same Qdrant collection, same API. Zero breaking changes.

## v0.2.3 (2026-06-08)

**Auto-Update — Agent managed: check, ask, update, restart.**

### New Tools (2)

- **`check_update`** — Check if a newer version is available on GitHub. Returns local vs latest version, release URL, and whether an update is available.
- **`do_update`** — Pull the latest version from GitHub, reinstall via pip, and restart the server. Requires `confirm: true` as safety guard. The server self-terminates after a successful update; the MCP client automatically reconnects.

### Self-Restart

- After a successful `do_update`, the server exits cleanly. The MCP client (Hermes gateway, Claude Code, Cursor, etc.) detects the disconnection and restarts the server with the new version — zero manual steps for the user.

### Agent Workflow (Language-Neutral)

1. Agent calls `check_update` → sees `update_available: true`
2. Agent asks user in their language: "Update available. Install?"
3. User says "yes"
4. Agent calls `do_update(confirm: true)` → git pull + pip install + server restart
5. Client reconnects automatically — new version is live

## v0.2.2 (2026-06-08)

**Justification Check (Rung 2) — Source URL Verification on Recall.**

### New Feature

- **`verification` field in recall results** — each result now includes a `verification` status:
  - `verified` — source URL is reachable (HTTP HEAD < 400)
  - `unreachable` — source URL unreachable or blocks HEAD requests
  - `unchecked` — no `source_url` was set
- **`_check_sources()` async method** — parallel HTTP HEAD checks on all source URLs in result set
- **Payload enrichment** — hybrid search results now include `source_url`, `access_level`, `category`, `source`, `created_at`, `provenance` from Qdrant payload (previously only score + text)

### Documentation

- **AGENTS.md:** Justification Check section, recall tool description updated

### Implementation Notes

Follows Rung 2 (Justification Verification) from Spivakovsky's Ladder of Checks: schema-valid is not answer-correct. Memory sources are verified at recall time, not just at storage time.

## v0.2.1 (2026-06-08)

**Breaking: hardcoded `~/.hermes/.env` removed for generic MCP compatibility.**

### Breaking Changes

- **Removed hardcoded `~/.hermes/.env` path** from `src/nexus_memory/mcp_server.py`. The server no longer assumes Hermes Agent. Use one of:
  - MCP config `env:` block (recommended) — works with every agent
  - `NEXUS_ENV_FILE` env var pointing to your `.env` file
  - `cwd/.env` fallback (unchanged)

### Migration

If you relied on `~/.hermes/.env`:

| Old | New |
|-----|-----|
| Keys in `~/.hermes/.env` | Move keys to MCP config `env:` block or set `NEXUS_ENV_FILE` in agent config |
| No explicit env config | Add `env: { VOYAGE_API_KEY: "..." }` to your MCP server config |

### Upgraded Config Documentation

- **AGENTS.md:** Configure section rewritten — three options (`env:` block, NEXUS_ENV_FILE, `.env` file), `~/.hermes/.env` usage removed
- **README.md:** Embedding provider table no longer references `~/.hermes/.env`; OpenClaw config corrected to JSON + `mcp.servers` schema
- **CHANGELOG.md:** Deprecation notices added to v0.2.0 and v0.1.0 entries

## v0.2.0 (2026-06-07)

**Full v2.8.0 integration — all features ported.**

### Features from v2.8.0

- MemoryCategory Enum: fact, belief, session, rule, preference, temp
- Provenance tracking: source_url, confidence, attach_source()
- Guardrails: content-length warnings, PII detection hints
- Access Control: public / trusted / private levels
- Hybrid Search: BM25 + Vector + Reciprocal Rank Fusion
- Health monitoring: Qdrant + Voyage health checks
- Drift detection, AutoDiscovery, Graph Analytics, Export API
- nexus_update — in-place metadata-preserving updates

### MCP Server

- 5 tools: remember, recall, forget, update, health
- Hybrid search with automatic vector fallback
- Auto .env loading (`~/.hermes/.env` [deprecated since v0.2.1] and local .env)
- Single collection for all agents (no per-agent silos)

### Quality

- 224 tests passing (ported from hermes-nexus-memory v2.8.0)
- All existing memories preserved in Qdrant
- Backward-compatible with hermes-nexus-memory data

## v0.1.0 (2026-06-07)

**Initial release — Universal Memory Layer for AI Agents**

### Features

- MCP Server with 4 tools: `remember`, `recall`, `forget`, `health`
- Access control: `public` / `trusted` / `private` levels
- Qdrant-backed vector storage (1024d, voyage-3-large)
- Automatic .env loading (`~/.hermes/.env` [deprecated since v0.2.1] and `./.env`)
- Security: local-only server, no cloud dependencies
- Single collection for all agents (no per-agent silos)

### Known limitations

- No hybrid search yet (BM25 planned)
- No encryption at rest
- No Web UI
- Qdrant must be running separately
