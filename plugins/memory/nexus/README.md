# Nexus Memory — Hermes Agent Plugin

A Hermes Agent MemoryProvider plugin for **Nexus Memory** — a universal, self-hosted memory layer for AI agents powered by Qdrant (vector store) and Voyage AI (embeddings).

## What This Is

This plugin gives Hermes Agent native memory persistence. It speaks directly to Qdrant (`localhost:6333`) using the `qdrant_client` library — **not** through the MCP server. It reuses the same `nexus` collection and the same embedding logic, so Hermes and any MCP-connected agent (Claude Code, Cursor, etc.) see the same memories.

## How to Activate

```bash
hermes memory setup
```

Select **nexus** from the provider list. The wizard will guide you through configuration.

## Config Fields

| Field | Description | Required | Default |
|-------|-------------|----------|---------|
| `qdrant_url` | Qdrant server URL | No | `http://localhost:6333` |
| `voyage_api_key` | Voyage AI API key (secret, stored in `.env`) | No | — |
| `collection_name` | Qdrant collection name | No | `nexus` |

**Embedding auto-detection:** If `VOYAGE_API_KEY` is set (starts with `vo-` or `pa-`), the plugin uses Voyage AI's `voyage-3-large` (1024d, cloud). Otherwise it falls back to `sentence-transformers` with `all-MiniLM-L6-v2` (384d, local).

## Shared Store

This plugin and the Nexus Memory MCP server share the **same Qdrant collection**. Everything written by the plugin is visible to the MCP server, and vice versa. Claude Code, Cursor, and Hermes Agent all operate on one unified memory.

## Exposed Tools

- **nexus_recall** — Search past memories, facts, and context
- **nexus_remember** — Store a memory for future recall
- **nexus_forget** — Delete a memory by ID
