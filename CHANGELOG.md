# Changelog

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
- Auto .env loading (~/.hermes/.env and local .env)
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
- Automatic .env loading (`~/.hermes/.env` and `./.env`)
- Security: local-only server, no cloud dependencies
- Single collection for all agents (no per-agent silos)

### Known limitations

- No hybrid search yet (BM25 planned)
- No encryption at rest
- No Web UI
- Qdrant must be running separately
