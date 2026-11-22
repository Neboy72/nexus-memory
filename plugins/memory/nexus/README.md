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

**Nothing, out of the box — but "out of the box" means *no key present*.** A
fresh install on a machine with no embedding key runs entirely locally:
embeddings use a **local** model — HuggingFace via `sentence-transformers`
(installed with the package; default model `Qwen/Qwen3-Embedding-0.6B`).
Ollama is used only when it is chosen explicitly; it is never required.
Query rewriting and paid stations are off, and Qdrant plus all state
stay local under `~/.nexus-memory`.

Cloud embedding is **opt-in and needs two things**: an explicit choice —
`NEXUS_EMBEDDING_PROVIDER=openai|google|jina` or `embedding_provider` in
the Nexus config — **and** the matching API key in the environment. A key that
is merely present (for example `OPENAI_API_KEY`, which many setups carry for
other tools) does **not** select a cloud provider on its own, and turn text is
never sent to one until you have made that choice.

Read the table below before opting in — it lists what each setting enables.

### Egress paths, per setting

| Setting | What leaves the machine | Destination |
|---|---|---|
| `NEXUS_EMBEDDING_PROVIDER=openai` **and** `OPENAI_API_KEY` | Turn text (user + assistant), for embedding | OpenAI (`api.openai.com`) |
| `NEXUS_EMBEDDING_PROVIDER=openai` **and** `OPENAI_API_KEY` | Turn text, for embedding | OpenAI (`api.openai.com`) |
| `NEXUS_EMBEDDING_PROVIDER=google` **and** `GOOGLE_API_KEY` | Turn text, for embedding | Google (`generativelanguage.googleapis.com`) |
| `NEXUS_EMBEDDING_PROVIDER=jina` **and** `JINA_API_KEY` | Turn text, for embedding | Jina (`api.jina.ai`) |
| `NEXUS_REWRITE=1` | The search query, for rewriting | The rewrite station (Ollama, local first) |
| `NEXUS_FUEL_PAID=1` + provider keys | Consolidation prompts | The configured fuel stations |
| Session end / entity extraction | Conversation text, for fact extraction | The model endpoint in the Hermes `config.yaml` |

Embedding is **auto-detected, and the automatic path is local**: HuggingFace via
`sentence-transformers` (ships with the package, model `Qwen/Qwen3-Embedding-0.6B`,
1024d) — no key and no extra software, and no Ollama needed. Ollama is used only
when it is chosen explicitly (`NEXUS_EMBEDDING_PROVIDER=ollama`); its model
remains `qwen3-embedding` (1024d). Cloud providers (OpenAI → Google →
Jina) are **never** picked automatically — they need `NEXUS_EMBEDDING_PROVIDER`
plus the matching key.

### Files this plugin reads

- `~/.hermes/config.yaml` — the model endpoint for session-end extraction.
- `~/.hermes/.env` — provider API keys, read from the **process environment**
  first (Hermes loads this file at startup). A key is only ever paired with the
  endpoint it belongs to: an `OPENAI_API_KEY` is never sent to a non-OpenAI
  `base_url` (see `src/nexus_memory/llm_endpoint_config.py`).

### Files this plugin writes

- `~/.nexus-memory/` — memory state, backups, toggle files, spend records.
- `~/.nexus-memory/logs/` — the service log and, per day, `read-YYYY-MM-DD.jsonl`:
  one line per recall with the ids of the entries served and a checksum of the
  query, never its text (90-day rotation).

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

| `collection_name` | Qdrant collection name | No | `nexus` |

## Shared Store

This plugin and the Nexus Memory MCP server share the **same Qdrant
collection**. Everything written by the plugin is visible to the MCP server,
and vice versa. Claude Code, Cursor, and Hermes Agent all operate on one
unified memory.

## Who wrote a memory (`agent`)

Every stored memory names its author in the payload field `agent` — resolved from
the context of the call (the plugin's own `NEXUS_AGENT_ID`, or the agent key the
service names), never from a field a caller can invent. Since 22.11.2026 the field
is mandatory: a write without an identity is refused instead of storing a point
nobody owns. The maintenance passes that write directly (consolidation, dreaming)
carry `writer: consolidation` / `writer: dreaming` instead and leave `agent` empty,
because a consolidation merges several agents' statements whose originals are
deleted; older points without either field stay as they are — no migration.

## Exposed Tools

- **nexus_recall** — Search past memories, facts, and context
- **nexus_remember** — Store a memory for future recall
- **nexus_forget** — Delete a memory by ID

## Failed tool calls, reported at session end (0.22.64)

The nightly learning in Nexus reads no harness session store. Instead every
agent reports its own failed tool calls to `POST /events`, and Nexus decides
what becomes a lesson. This provider does that at the end of a session: it
takes the tool results Hermes itself marked as failed (`exit_code != 0` or a
set `error`), and sends tool name, error text and session id — never the
conversation.

`NEXUS_REPORT_INCIDENTS=0` turns it off. If the service is not reachable, the
report is skipped silently; a session end never waits on it.
