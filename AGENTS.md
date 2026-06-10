# Nexus Memory — AGENTS.md (AI Installation Guide)

> Tell your AI agent: "Read https://raw.githubusercontent.com/Neboy72/nexus-memory/main/AGENTS.md and follow the installation instructions."

## Overview

Nexus Memory is a **universal memory layer** for AI agents. One memory for all your agents — Hermes, OpenClaw, Claude Code, Cursor, or any MCP-compatible agent.

## Quick Install

### Prerequisites

- Python 3.11+
- Qdrant running on localhost:6333
- At least **one** embedding provider (auto-detected in this order):

  | Provider | Type | Dimensions | How to get |
  |----------|------|-----------|------------|
  | **Voyage** ☁️ | Cloud | 1024d | `VOYAGE_API_KEY` |
  | **OpenAI** ☁️ | Cloud | 1536d | `OPENAI_API_KEY` |
  | **Google/Vertex AI** 💚 | Cloud | 768d | `GOOGLE_API_KEY` |
  | **Jina** 💜 | Cloud | 1024d | `JINA_API_KEY` |
  | **Ollama** 🦙 | Local | 768d | Auto-detected (`embed` model) |
  | **sentence-transformers** 🏠 | Local | 384d | `pip install sentence-transformers` |

  > **Zero-setup:** If you have Ollama running with an `embed` model (e.g. `nomic-embed-text`), it works out of the box — no API key needed.

### 1. Install

```bash
git clone https://github.com/Neboy72/nexus-memory.git ~/nexus-memory
cd ~/nexus-memory
pip install -e .
```

### 2. Configure

Set your preferred embedding provider's API key. Pick **one** of these options — the server auto-detects which provider is available:

**Option A — MCP `env:` block (recommended):**
```json
// OpenClaw — mcp.servers.<name>.env
// Hermes / Claude Code / Standard MCP — mcpServers.<name>.env
{
  "env": {
    "VOYAGE_API_KEY": "vo-your-key-here"
    // or: "OPENAI_API_KEY": "sk-...",
    // or: "GOOGLE_API_KEY": "AIza..."
  }
}
```

**Option B — `.env` file (auto-loaded from repo root or $NEXUS_ENV_FILE):**
```bash
echo 'VOYAGE_API_KEY="vo-your-key-here"' >> ~/nexus-memory/.env
```

> 💡 **No API key?** If you have Ollama running locally with an embed model (e.g. `nomic-embed-text`), skip config entirely — the server detects it automatically.

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

## Available Tools (7)

| Tool | Description | Parameters |
|------|-------------|------------|
| `remember` | Store a memory | `text` (req), `access_level`, `category`, `source`, `source_url`, `confidence` |
| `recall` | Hybrid search — returns results with `verification` status | `query` (req), `limit`, `filter_level` |
| `forget` | Delete a memory | `memory_id` (req) |
| `update` | Update in-place, preserve metadata | `memory_id` (req), `text`, `modified_by` |
| `health` | Check server status | — |
| `check_update` | Check for newer version on GitHub | — |
| `do_update` | Update + restart server | `confirm` (req, must be `true`) |

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

### Justification Check (Rung 2)

Each recall result includes a `verification` field:

| Status | Meaning |
|--------|---------|
| `verified` | Source URL is reachable (HTTP < 400) |
| `unreachable` | Source URL is unreachable or blocks HEAD requests |
| `unchecked` | No `source_url` was set when storing this memory |

Memory entries stored with `source_url` are checked via async HTTP HEAD on every recall. If a source becomes unreachable, the agent sees the downgrade and can treat the memory with lower confidence.

## Architecture

```
MCP Client ← stdio → nexus-memory (MCP Server)
                           │
                    ┌──────┴──────┐
                    │   Qdrant    │
                    │ localhost:6333 │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
           Voyage       OpenAI      Google
           (1024d)      (1536d)     (768d)
              │            │            │
            Jina        Ollama     sentence-
           (1024d)      (768d)    transformer
                                    (384d)
```

> **Auto-detection:** The server tries Voyage → OpenAI → Google → Jina → Ollama → sentence-transformers. First available wins. No manual selection needed.

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

## Data

All memories live in a single Qdrant collection called **`nexus`**:

- **12,700+ points** — memories, beliefs, events, paperless documents
- **Hybrid search** — BM25 full-text + vector similarity + RRF re-ranking
- **Access levels** — public / trusted / private (enforced by MCP tools)
- **Categories** — fact, belief, session, rule, preference, temp

> **Migration complete.** Old collections (`hermes-memory`, `openclaw-memory`, `nexus_beliefs`) have been consolidated into `nexus`. If you're migrating from a previous version, your data is already there — simply point your MCP client at the `nexus` collection.

## Release

```bash
git tag v0.2.1 && git push --tags
pip install build && python3 -m build && python3 -m twine upload dist/*
```
