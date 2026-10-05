# Nexus Memory — Hermes Agent Plugin

A Hermes Agent MemoryProvider plugin for **Nexus Memory** — a self-hosted memory
layer for AI agents built on Qdrant (vector store).

## What This Is

This plugin gives Hermes Agent native memory persistence. It speaks directly to
Qdrant (default `http://localhost:6333`) using the `qdrant_client` library —
**not** through the MCP server. It reuses the same `nexus` collection and the
same embedding logic, so Hermes and any MCP-connected agent (Claude Code,
Cursor, etc.) see the same memories.

## What Leaves Your Machine

**Nothing, out of the box.** A fresh install runs entirely on the machine:
embeddings use a **local** model (Ollama, or `sentence-transformers` as
fallback), query rewriting and paid stations are off, and Qdrant plus all state
stay local under `~/.nexus-memory`.

The picture changes as soon as a **cloud key** is added — that is the operator's
choice, and the paths below are what it enables. Read this list before adding
one.

### Egress paths, per setting

| Setting | What leaves the machine | Destination |
|---|---|---|
| `VOYAGE_API_KEY` set | Turn text (user + assistant), for embedding | Voyage AI (`api.voyageai.com`) |
| `OPENAI_API_KEY` set | Turn text, for embedding | OpenAI (`api.openai.com`) |
| `GOOGLE_API_KEY` set | Turn text, for embedding | Google (`generativelanguage.googleapis.com`) |
| `JINA_API_KEY` set | Turn text, for embedding | Jina (`api.jina.ai`) |
| `NEXUS_REWRITE=1` | The search query, for rewriting | The rewrite station (Ollama, local first) |
| `NEXUS_FUEL_PAID=1` + provider keys | Consolidation prompts | The configured fuel stations |
| Session end / entity extraction | Conversation text, for fact extraction | The model endpoint in the Hermes `config.yaml` |

Embedding is **auto-detected**: cloud keys first (Voyage → OpenAI → Google →
Jina), then Ollama with `qwen3-embedding` (1024d, preferred local), then
`bge-m3` (1024d). The local path is the recommended default — it is the only
one that needs no key at all.

### Files this plugin reads

- `~/.hermes/config.yaml` — the model endpoint for session-end extraction.
- `~/.hermes/.env` — provider API keys, read from the **process environment**
  first (Hermes loads this file at startup). A key is only ever paired with the
  endpoint it belongs to: an `OPENAI_API_KEY` is never sent to a non-OpenAI
  `base_url` (see `src/nexus_memory/llm_endpoint_config.py`).

### Files this plugin writes

- `~/.nexus-memory/` — memory state, backups, toggle files, spend records.

## Opt-In Network Calls

Both are **off by default** and only run when explicitly enabled:

- **Query rewriting** (`NEXUS_REWRITE=1`): rewrites a search query before
  embedding so keyword+vector search finds it. Sends the query to the rewrite
  station. Under the provider this is opt-in only; the MCP server keeps its own
  default, so the two deployments can differ.
- **Paid fuel stations** (`NEXUS_FUEL_PAID=1`, or the dashboard toggle): lets
  consolidation use providers that cost money. Until opted in, only free/local
  stations are used — provider keys are never spent on their own, and no file is
  written during import.

## How to Activate

```bash
hermes memory setup
```

Select **nexus** from the provider list. The wizard will guide you through
configuration.

## Config Fields

| Field | Description | Required | Default |
|-------|-------------|----------|---------|
| `qdrant_url` | Qdrant server URL | No | `http://localhost:6333` |
| `voyage_api_key` | Voyage AI API key — opt-in cloud embedding. Supply it through the environment (`VOYAGE_API_KEY`); the plugin reads it from there and it is not copied into the plugin config | No | — |
| `collection_name` | Qdrant collection name | No | `nexus` |

## Shared Store

This plugin and the Nexus Memory MCP server share the **same Qdrant
collection**. Everything written by the plugin is visible to the MCP server,
and vice versa. Claude Code, Cursor, and Hermes Agent all operate on one
unified memory.

## Exposed Tools

- **nexus_recall** — Search past memories, facts, and context
- **nexus_remember** — Store a memory for future recall
- **nexus_forget** — Delete a memory by ID
