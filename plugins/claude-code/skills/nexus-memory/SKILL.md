---
name: nexus-memory
description: >
  Persistent memory for Claude Code powered by Qdrant. Auto-Recall injects
  relevant memories before each prompt. Auto-Capture stores facts after
  each turn. Self-hosted, private, works alongside Hermes and OpenClaw
  with the same Qdrant collection. Configure via NEXUS_* environment
  variables. Use when the user asks to "remember", "recall", "search
  memory", or when project context from past sessions is needed.
---

# Nexus Memory

Nexus Memory gives Claude Code persistent memory across sessions using a
local Qdrant instance. The same memory store shared with Hermes Agent and
OpenClaw - one brain, many agents.

## How it works

- **Auto-Recall** (UserPromptSubmit hook): Before each prompt, searches
  Qdrant for relevant memories and injects them as context.
- **Auto-Capture** (Stop hook): After each turn, extracts notable facts
  from the transcript and stores them in Qdrant.
- **Session Start** (SessionStart hook): Loads project-related memories
  when a session begins or resumes.

## Configuration

Environment variables (set in `.env` or shell):

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_QDRANT_URL` | `http://localhost:6333` | Qdrant server URL |
| `NEXUS_COLLECTION` | `nexus` | Qdrant collection name |
| `NEXUS_EMBEDDING_PROVIDER` | `voyage` | Embedding provider (voyage/ollama) |
| `VOYAGE_API_KEY` | - | Voyage AI API key |
| `NEXUS_EMBEDDING_MODEL` | `voyage-3-large` | Embedding model |
| `NEXUS_MAX_RECALL` | `5` | Max memories to inject per prompt |
| `NEXUS_OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Ollama embed model |

## Manual Tools

The MCP server (`nexus-memory`) provides explicit tools:
- `nexus_remember` - Store a memory
- `nexus_recall` - Search memories
- `nexus_forget` - Delete a memory

## Shared Store

Same Qdrant collection as:
- Hermes Agent (native plugin)
- OpenClaw (native plugin)
- Any MCP-compatible agent

A memory stored by Claude Code is immediately visible to Hermes and vice
versa. One brain, many agents.