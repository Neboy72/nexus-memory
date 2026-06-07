# Changelog

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
