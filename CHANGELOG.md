# Changelog

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
