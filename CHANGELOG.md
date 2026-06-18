# Changelog

All notable changes to **Nexus Memory** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.4.0] — 2026-06-19

### Added

- **OpenClaw native plugin** — Auto-Recall (memories injected before every turn) and Auto-Capture (facts extracted after every turn) powered by local Qdrant
- **`install_openclaw_plugin.sh`** — one-command install script that detects OpenClaw, configures `plugins.load.paths`, sets `plugins.slots.memory`, auto-detects embedding provider, and restarts the gateway
- **3-way architecture** — Hermes Plugin · OpenClaw Plugin · MCP Server, all sharing the same Qdrant collection
- **"Which path should I use?" table** in AGENTS.md and README.md

### Changed

- README.md completely rewritten — 3-way architecture diagram, all 3 install paths, release history table, GitHub Sponsors badge
- Version badge updated to v0.4.0

### Notes

- No breaking changes — same Qdrant collection, same API, same tools
- OpenClaw plugin uses Qdrant REST via `fetch()` (no Python dependency on the OpenClaw side)

---

## [v0.3.0] — 2026-06-18

### Added

- **Hermes native MemoryProvider plugin** — direct Qdrant access with zero MCP overhead
  - Auto-prefetch: relevant memories injected into context before every turn
  - Auto-sync: user + assistant turns saved as memories automatically
  - 3 manual tools: `nexus_recall`, `nexus_remember`, `nexus_forget`
  - Dimension-mismatch protection warns if embedding provider changed
- **`install_hermes_plugin.sh`** — one-command install: symlinks plugin, sets `memory.provider`, verifies
- **Embedding Provider Selection wizard** — `nexus-memory-init` interactive CLI
  - Scans system for all 6 providers
  - Shows quality ranking (excellent / good / basic)
  - Auto-selects best available as default
  - API key URL hints for cloud providers
- **Separate landing page server** + marketing assets (poster, references)

### Changed

- `pyproject.toml` version bumped to 0.3.0
- AGENTS.md restructured with Hermes Plugin and OpenClaw Plugin sections

### Notes

- Hermes plugin shares the same Qdrant collection with the MCP server
- No breaking changes to the MCP server API

---

## [v0.2.5] — 2026-06-13

### Fixed

- **`is_success()` helper** replaces raw `status_code == 200` across 29 sites in 10 files — Qdrant 201/204 responses no longer falsely treated as errors
  - `apply.py` (9 sites), `events.py` (7 sites), `staging.py` (3 sites), `nexus/__init__.py` (2 sites), `provenance/__init__.py` (3 sites), `cli.py` (1 site), `mcp_server.py` (1 site), `retrieval/__init__.py` (1 site), examples (2 sites)
- **5 bugs from Verifier audit** — Google async, try/except handlers, missing if-condition, deps, version drift

### Changed

- **TRUST_EPSILON consolidated** — same value (0.01) in `recompute_trust` + `recompute_all` (previously 0.01 vs 1e-9)
- **EVENT_TYPES derived from Enum** — single source of truth instead of duplicate
- **Deprecated `asyncio.get_event_loop()`** replaced with `get_running_loop()`
- **Re-embedding on hybrid fallback eliminated** — one API call instead of two
- **Unused imports removed** (json, Any, datetime, timezone, sys locales)
- **Unused constants removed** (STATUS_CONTESTED, RETRACTED, HISTORICAL, VALID_STATUSES)
- **`config.py` docstring corrected** — says "nexus" instead of "hermes-memory"

### Added

- **Audit GitHub Action** — automatic check on every push:
  - Collection-name check (finds `openclaw-memory`, `hermes-memory-1024d` etc.)
  - Status-code check (finds raw `== 200`)
  - Python compile check
  - pytest
- **SECURITY.md** — contact, supported versions, reporting process
- **Webhook subscriptions** — 3 new tools: `subscribe`, `unsubscribe`, `list_subscriptions`
  - Fire-and-forget HTTP POST to registered URLs on memory events
  - Persisted in `~/.nexus-webhooks.json` (no Qdrant, no SQLite, no new dependency)
  - Event types: `memory.remember`, `memory.update`, `memory.forget`
  - 27 new tests (379 total, all passing)

### Notes

- No breaking changes — same Qdrant collection, same API

---

## [v0.2.4] — 2026-06-12

### Added

- **Web UI** with live D3.js v7 force-directed graph
  - Interactive node graph of all memories
  - Clustering and category-mapping
  - Detail view on node click
  - Drift ampel (traffic light) for belief health
  - Stats cards with tooltips
  - Filter by category, full-text search
- **`nexus-memory webui` CLI command** — launches dashboard at `http://127.0.0.1:9120`
- **Ko-fi integration** in Web UI header and footer

### Fixed

- Graph.js crash on `d.full` → `fullText` property
- Safari reader mode prevention, marked as web app
- Cache-bust all assets (`?v=20260612`)
- Graph edges visibility (4px / 75% opacity, hover 5px / 100%)
- Node sizing (7 + 15×confidence), thicker edges, larger labels

### Changed

- WebUI refactored to graph-only landing page, removed marketing clutter
- `cli()` cleaned up after patch damage, proper argparse restored

---

## [v0.2.3] — 2026-06-08

### Added

- **`check_update` tool** — checks if a newer version is available on GitHub. Returns local vs latest version, release URL, and whether an update is available
- **`do_update` tool** — pulls latest version from GitHub, reinstalls via pip, and restarts the server. Requires `confirm: true` as safety guard
- **Self-restart** — after successful `do_update`, the server exits cleanly; the MCP client automatically reconnects with the new version

### Fixed

- **macOS setup.sh** — `grep -oP` → `-oE` compatibility fix
- **uv --system** flag added for venv creation

### Notes

- Agent workflow: `check_update` → ask user → `do_update(confirm: true)` → automatic reconnect
- Language-neutral — agent communicates in whatever language the user speaks

---

## [v0.2.2] — 2026-06-08

### Added

- **Justification Check (Rung 2)** — source URL verification on recall
  - `verification` field in recall results: `verified`, `unreachable`, or `unchecked`
  - `_check_sources()` async method — parallel HTTP HEAD checks on all source URLs
  - Payload enrichment — hybrid search results now include `source_url`, `access_level`, `category`, `source`, `created_at`, `provenance`

### Fixed

- **Score key** — `rrf_score` instead of `score` in HybridRetriever
- **Score normalization** — relative instead of fixed `/10`
- **Hybrid search embedding pass-through** + shim correction
- **HybridRetriever.search() shim** — resolves recall crash (`AttributeError`)
- **Default collection** → `nexus` (was: `hermes-memory`)
- **Voyage API key detection** — support both `pa-` and `vo-` prefix
- **Health check** — `model_name` property added to EmbeddingProvider
- **pyproject.toml** — `where=['src', '.']` finds both `nexus/` (root) and `nexus_memory/` (src/)
- **CLI sync** — `cli()` wrapper for async `main()` (entrypoint bug)

### Changed

- **Privacy** — author name `Nebojsa Kacavenda` → `Nebo` in all public files
- **Headline** — "One brain for all your agents" (pain-first positioning)

### Removed

- **Hardcoded `~/.hermes/.env` path** — replaced with generic MCP `env:` block, `NEXUS_ENV_FILE`, or `cwd/.env` fallback

---

## [v0.2.0] — 2026-06-07

### Added

- **MemoryCategory Enum** — 6 scopes: `fact`, `belief`, `session`, `rule`, `preference`, `temp`
- **Provenance tracking** — `source_url`, `confidence`, `attach_source()`
- **Guardrails** — content-length warnings (>5,000 chars), PII detection hints
- **Access Control** — `public` / `trusted` / `private` levels
- **Hybrid Search** — BM25 + Vector + Reciprocal Rank Fusion
- **Health monitoring** — Qdrant + embedding provider health checks
- **Drift detection** — scored 0–10 with healthy/attention/action thresholds
- **Auto-Discovery** — zero-token relation discovery between canonical facts
- **Graph Analytics** — hub scores, isolation scores, knowledge gaps, connected components
- **Skill Export** — `export_skill()` generates `SKILL.md` from canonical facts
- **`update` tool** — in-place metadata-preserving memory updates
- **5 MCP tools** — `remember`, `recall`, `forget`, `update`, `health`

### Changed

- Full v2.8.0 feature parity ported from `hermes-nexus-memory`
- 224 tests passing
- Single collection for all agents (no per-agent silos)

### Notes

- Backward-compatible with `hermes-nexus-memory` data
- All existing memories preserved in Qdrant

---

## [v0.1.0] — 2026-06-07

### Added

- **Initial release** — Universal Memory Layer for AI Agents
- **MCP Server** with 4 tools: `remember`, `recall`, `forget`, `health`
- **Access control** — `public` / `trusted` / `private` levels
- **Qdrant-backed vector storage** (1024d, voyage-3-large)
- **Automatic `.env` loading** — `~/.hermes/.env` [deprecated since v0.2.1] and `./.env`
- **Security** — local-only server, no cloud dependencies
- **Single collection** for all agents (no per-agent silos)

### Known Limitations

- No hybrid search yet (BM25 planned)
- No encryption at rest
- No Web UI
- Qdrant must be running separately

---

[v0.4.0]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.4.0
[v0.3.0]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.3.0
[v0.2.5]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.2.5
[v0.2.4]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.2.4
[v0.2.3]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.2.3
[v0.2.2]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.2.2
[v0.2.0]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.2.0
[v0.1.0]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.1.0