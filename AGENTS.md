# Nexus Memory — AGENTS.md (AI Installation Guide)

> Tell your AI agent: "Install Nexus Memory from https://github.com/Neboy72/nexus-memory"

## Overview

Nexus Memory is a **universal memory layer** for AI agents. One memory for all your agents — Hermes, OpenClaw, Claude Code, Cursor, or any MCP-compatible agent.

## Quick Install

### Prerequisites

- Python 3.11+
- Qdrant running on localhost:6333
- Voyage AI API key (or other embedding provider)

### 1. Install

```bash
git clone https://github.com/Neboy72/nexus-memory.git ~/nexus-memory
cd ~/nexus-memory
pip install -e .
```

### 2. Configure

Set your Voyage API key. Pick **one** of these — they all work:

**Option A — MCP `env:` block (recommended):**
```json
// OpenClaw — mcp.servers.<name>.env
// Hermes / Claude Code / Standard MCP — mcpServers.<name>.env
{
  "env": {
    "VOYAGE_API_KEY": "vo-your-key-here"
  }
}
```

**Option B — `.env` file:**
```bash
echo 'VOYAGE_API_KEY="vo-your-key-here"' >> ~/nexus-memory/.env
# or anywhere else, point to it via NEXUS_ENV_FILE
```

`~/.hermes/.env` is no longer read by default (removed in v0.2.1).

### 3. Run MCP Server

```bash
nexus-memory
```

The server starts on stdio and auto-creates a `nexus` collection in Qdrant.

### 4. Connect your Agent

**Hermes Agent** — add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  nexus:
    command: /path/to/venv/bin/python3
    args: ["-m", "nexus_memory.mcp_server"]
    env:
      PYTHONPATH: /Users/you/nexus-memory
```

Restart gateway. Tools appear as `mcp_nexus_remember`, `mcp_nexus_recall`, etc.

**Any MCP-compatible agent** — configure to launch:

```json
{
  "mcpServers": {
    "nexus": { "command": "nexus-memory" }
  }
}
```

## Available Tools (5)

| Tool | Description | Parameters |
|------|-------------|------------|
| `remember` | Store a memory | `text` (req), `access_level`, `category`, `source`, `source_url`, `confidence` |
| `recall` | Hybrid search (BM25 + Vector + RRF) | `query` (req), `limit`, `filter_level` |
| `forget` | Delete a memory | `memory_id` (req) |
| `update` | Update in-place, preserve metadata | `memory_id` (req), `text`, `modified_by` |
| `health` | Check server status | — |

## Memory Categories

Use `category` parameter to classify memories:
- `fact` — verified facts (default)
- `belief` — drift-prone assumptions (nightly drift detection)
- `session` — session-scoped episodic memory
- `rule` — operating rules
- `preference` — user preferences
- `temp` — ephemeral entries

## Access Levels

| Level | Description | Visible to |
|-------|-------------|-----------|
| `public` | General knowledge | All agents |
| `trusted` | Personal data | Trusted agents (e.g. Kiosha) |
| `private` | Sensitive data | Owner only (Nebo) |

## Provenance

```python
# Store with source tracking
await mcp_nexus_remember(
    text="...",
    source_url="https://example.com",
    confidence=0.95,
    category="fact"
)
```

## Architecture

```
MCP Client ← stdio → MCP Server (nexus-memory) ← HTTP → Qdrant (localhost:6333)
                                                            ↑
                                                        Voyage AI (embeddings)
```

### Key Components

- **nexus/** — core library (MemoryCategory, HybridRetriever, DriftDetector, Provenance, Lifecycle, Graph, Discovery, Export, ...)
- **src/nexus_memory/mcp_server.py** — MCP server (5 tools, guardrails, access control)
- **tests/** — 224 tests (pytest)

## Testing

```bash
cd ~/nexus-memory
pip install pytest
pytest tests/ -v
```

## Migration from hermes-nexus-memory / openclaw-nexus-memory

1. Install nexus-memory (above)
2. The MCP server connects to the same Qdrant instance
3. Your old memories are still in `hermes-memory` collection
4. New memories go to `nexus` collection
5. Optional: run `nexus/scripts/migrate.py` to copy old → new

## Release

```bash
git tag v0.2.0 && git push --tags
pip install build && python3 -m build && python3 -m twine upload dist/*
```
