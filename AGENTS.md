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

### 5. 🌐 Web UI (optional)

```bash
pip install nexus-memory[webui]
nexus-memory webui
```

Opens a live graph dashboard at `http://127.0.0.1:9120` — explore your memory network visually.

## Available Tools (10)

| Tool | Description | Parameters |
|------|-------------|------------|
| `remember` | Store a memory | `text` (req), `category` (req, default `fact`), `access_level`, `source`, `source_url`, `confidence` |
| `recall` | Hybrid search — returns results with `verification` status | `query` (req), `limit`, `filter_level` |
| `forget` | Delete a memory | `memory_id` (req) |
| `update` | Update in-place, preserve metadata | `memory_id` (req), `text`, `modified_by` |
| `health` | Check server status | — |
| `check_update` | Check for newer version on GitHub | — |
| `do_update` | Update + restart server | `confirm` (req, must be `true`) |
| `subscribe` | Register a webhook for a memory event | `event_type` (req), `webhook_url` (req) |
| `unsubscribe` | Remove a webhook subscription | `subscription_id` (req) |
| `list_subscriptions` | List all registered webhook subscriptions | — |

## Webhook Subscriptions

MCP clients can register a webhook URL to be notified when the memory store
changes. The server POSTs a small JSON payload to every subscriber that
matches the event type — useful for keeping external systems (CRMs, audit
logs, notification bots, second-brain tools) in sync with your memory.

### Event types

| Event | Fires when |
|-------|-----------|
| `memory.remember` | A new memory has been stored (`remember` tool succeeded) |
| `memory.update`   | An existing memory was updated in place (`update` tool succeeded) |
| `memory.forget`   | A memory was deleted (`forget` tool succeeded) |

### Tools

```python
# Register a webhook
await mcp_nexus_subscribe(
    event_type="memory.remember",
    webhook_url="https://example.com/hooks/nexus"
)
# → { "status": "subscribed", "subscription": { "id": "<uuid>", ... } }

# List all subscriptions
await mcp_nexus_list_subscriptions()
# → { "subscriptions": [...], "count": 1 }

# Remove one
await mcp_nexus_unsubscribe(subscription_id="<uuid-from-subscribe>")
```

### Payload shape

The server POSTs a JSON body to your URL:

```json
{ "event": "memory.remember", "memory_id": "<uuid>", "timestamp": "2026-06-13T12:34:56+00:00" }
```

### Storage

Subscriptions are persisted in `~/.nexus-webhooks.json` (a single
human-readable JSON file). They survive server restarts. No new Qdrant
collection or database is required.

### Delivery semantics

* **Fire-and-forget** — the tool call returns immediately; HTTP delivery
  happens in a background task. A slow or unresponsive endpoint does not
  block `remember` / `update` / `forget`.
* **Best-effort, 5s timeout** — POSTs time out after 5 seconds. Failures
  (timeouts, 4xx, 5xx, DNS errors) are logged at WARNING and never
  crash the server.
* **No retries** — webhook delivery is at-most-once. Subscribers should
  tolerate gaps; use `recall` to reconcile state if needed.

### Validation

* `event_type` must be one of `memory.remember`, `memory.update`,
  `memory.forget`. Unknown values are rejected with an error envelope.
* `webhook_url` must be a non-empty `http://` or `https://` URL. Other
  schemes (`ftp://`, `javascript:`, …) are rejected.

## Memory Categories (State-Prefixing)

`category` is a **required** parameter on `remember` (declared in the tool schema).
The server applies `"fact"` as a backward-compatible default if a client omits it
or sends an unknown value, so older clients keep working. The six scopes map to
the State-Prefixing pattern from Agentic Design Patterns (Ch8):

| Category | Scope | Lifetime / Notes |
|----------|-------|------------------|
| `fact` | Permanent verified facts | Default. No TTL, no drift check. |
| `belief` | Mutable assumptions | Drift-detection candidates (nightly job). |
| `session` | Session-specific | Episodic memory, scoped to a single session. |
| `rule` | Operating rules | Stable, rarely changed. |
| `preference` | User preferences | Per-user, durable. |
| `temp` | Ephemeral | Expires after a TTL. |

> **Legacy data:** memories stored before `category` was required are returned
> with `category: "fact"` on read so consumers always see a valid enum value.

## Access Levels

| Level | Description | Visible to |
|-------|-------------|-----------|
| `public` | General knowledge | All agents |
| `trusted` | Personal data | Trusted agents (e.g. Kiosha) |
| `private` | Sensitive data | Owner only (Nebo) |

## Provenance

Provenance is **strongly recommended** for every memory you store. It lets the
server verify sources at recall time (Rung 2: Justification Check) so you
always know whether a memory's evidence is still alive.

### Recommended call

```python
# Best practice: include source_url + confidence on every remember()
await mcp_nexus_remember(
    text="Voyage-3-large produces 1024-dim embeddings",
    source_url="https://docs.voyageai.com/docs/embeddings",
    confidence=0.95,
    source="documentation",
    category="fact",
)
```

### The three parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `source_url` | string | `""` | **Activates verification.** When set, the server runs an async HTTP HEAD against this URL on every recall. Result is returned as the `verification` field. Omit to disable verification. |
| `confidence` | number 0.0-1.0 | `0.7` | Your self-assessed confidence in the fact. Use 0.9+ for verified facts, 0.5-0.8 for beliefs / inferences, <0.5 for speculative notes. |
| `source` | string | `""` | Free-form origin label — `"conversation"`, `"document"`, `"cron"`, etc. Stored alongside, but does not trigger verification. |

> **All three stay optional.** Older clients that omit them keep working — they
> just get `verification: "unchecked"` on recall and `confidence: 0.7` stored.

### Justification Check (Rung 2) — what recall returns

Each recall result includes a `verification` field derived from `source_url`:

| Status | When | Meaning |
|--------|------|---------|
| `verified` | `source_url` set, HTTP HEAD returned `< 400` | The source is alive — treat the memory as trustworthy. |
| `unreachable` | `source_url` set, HEAD failed (timeout, DNS, 4xx/5xx, bot block) | The source is gone or inaccessible — downgrade the memory's trust and consider re-fetching. |
| `unchecked` | `source_url` was not set at storage time | No provenance was attached — the server did not verify anything. |

Source URLs are checked in parallel via async HTTP HEAD on every recall. If a
URL becomes unreachable, the agent sees the downgrade immediately and can
decide whether to keep, refresh, or drop the memory.

### Why this matters

- **Spivakovsky's Ladder of Checks, Rung 2:** *schema-valid is not
  answer-correct.* A memory that was true yesterday may be stale today.
  Justification verification keeps evidence alive without re-running the
  original ingest.
- **Trust degrades visibly:** a fact with `verification: "unreachable"` is a
  signal to the agent to either re-check the source or treat the claim as
  provisional.
- **Zero-config default:** when you cannot supply a `source_url`, omit it —
  no error, no warning, just `verification: "unchecked"`.

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
