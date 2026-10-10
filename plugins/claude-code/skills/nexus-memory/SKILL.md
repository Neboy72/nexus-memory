---
name: nexus-memory
description: >
  Persistent memory for Claude Code backed by Qdrant. Auto-Recall injects
  relevant memories before each prompt. Auto-Capture stores facts after each
  turn, automatically. Storage is a local Qdrant instance; embeddings default
  to a fully local Ollama model, so nothing leaves your machine out of the box.
  The same Qdrant collection can be shared with Hermes and OpenClaw. Read the
  "What gets stored" section before installing. Configure via NEXUS_*
  environment variables. Use when the user asks to "remember", "recall",
  "search memory", or when project context from past sessions is needed.
---

# Nexus Memory

Nexus Memory gives Claude Code persistent memory across sessions using a
Qdrant instance. The same memory store can be shared with Hermes Agent and
OpenClaw - one brain, many agents.

## How it works

- **Auto-Recall** (UserPromptSubmit hook): Before each prompt, searches
  Qdrant for relevant memories and injects them as context.
- **Auto-Capture** (Stop hook): After each turn, extracts notable facts
  from the transcript and stores them in Qdrant.
- **Session Start** (SessionStart hook): Loads project-related memories
  when a session begins or resumes.

## Read this before you install: what gets stored, and where it goes

**This skill stores to persistent memory automatically.** Facts are
extracted from your conversations after every turn and written to Qdrant,
then re-injected into later prompts without asking again. Two things
deserve a deliberate decision:

**1. Conversation content is persisted.** Anything said in a session can
end up as a stored memory, including details you did not intend to keep.
Do not run this around credentials, secrets, regulated data, or client
material you are not permitted to retain - unless you have decided how to
handle review, deletion and disabling (see "Disabling and controlling it").
Stored memories also resurface later, where they can be mistaken for
something just said.

**2. Where the text goes depends on your embedding provider - and the
default is local.** Out of the box the plugin embeds through the local
Nexus service (`nexus-memory serve`), which runs the engine with its local
HuggingFace model: nothing has to be installed and memory text does not
leave your machine. Only if you deliberately name a cloud provider
(Voyage, OpenAI, Google, Jina) is the text of your memories sent to that
provider's API. If you switch providers later, re-embed, because vectors
from different models are not comparable.

**3. A shared collection is readable across agents.** If Claude Code,
Hermes Agent and OpenClaw point at the same Qdrant collection (the common
setup), a memory written by one agent is visible to the others. That
crosses tool and trust boundaries - use a separate `NEXUS_COLLECTION` per
project when contexts must not mix.

### Disabling and controlling it

- **Turn off automatic storage:** remove or disable the `Stop` hook in the
  plugin's hook configuration (`hooks/nexus-hooks.json`, or Claude Code's
  hooks settings) - that hook is what captures facts after each turn. The
  manual `remember` / `recall` / `forget` tools keep working without it.
- **Keep memory local:** this is already the default (the local Nexus
  service, on your machine) - just do not set a cloud provider.
- **Keep memory separate:** set a distinct `NEXUS_COLLECTION` per project.
- **Remove something:** use the `forget` tool on the memory you do not want.

## Configuration

Environment variables (set in `.env` or shell):

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_QDRANT_URL` | `http://localhost:6333` | Qdrant server URL |
| `NEXUS_COLLECTION` | `nexus` | Qdrant collection name |
| `NEXUS_EMBEDDING_PROVIDER` | `auto` | `auto` (local Nexus service, default), `ollama` (local), or a cloud provider (`voyage`, `openai`, `google`, `jina`) |
| `NEXUS_EMBEDDING_MODEL` | `voyage-4` | Model used when the provider is `voyage` |
| `VOYAGE_API_KEY` | - | Required only if you choose the Voyage provider |
| `NEXUS_OLLAMA_EMBED_MODEL` | `qwen3-embedding:0.6b` | Model used when the provider is `ollama` |
| `NEXUS_MAX_RECALL` | `5` | Max memories to inject per prompt |

## Embeddings

The plugin defaults to the **local Nexus service** - no API key, no account,
nothing leaving your machine, and nothing to install:

| Provider | Where | Setup | Default model |
|----------|-------|-------|---------------|
| *(default)* `auto` | **Local** - the service embeds with the engine's model | run `nexus-memory serve` (the installer registers it as a service) | local HuggingFace, e.g. `Qwen/Qwen3-Embedding-0.6B` (1024d) |
| `ollama` | **Local** (needs Ollama running) | `NEXUS_EMBEDDING_PROVIDER=ollama` + `ollama pull qwen3-embedding:0.6b` | `qwen3-embedding:0.6b` (1024d) |
| `voyage` | Cloud (text is sent to Voyage) | set `NEXUS_EMBEDDING_PROVIDER=voyage` and `VOYAGE_API_KEY` | `voyage-4` |

**Setup (once):** run the service - the wizard/installer already registers it:

```bash
nexus-memory serve        # http://127.0.0.1:9122, installed as a user service
```

Point the plugin elsewhere with `NEXUS_SERVE_URL` if the service listens
somewhere else. If it is not reachable, the hooks say so on stderr and skip
their work instead of storing or recalling nothing in silence.

The explicit Ollama path is the whole setup then: `qwen3-embedding:0.6b` is
multilingual and instruction-aware, which matters if your memory is not
English-only.

`bge-m3` (1.2 GB, 1024d, multilingual) is a heavier local alternative.
Use one model consistently - mixing models inside a single collection
produces incomparable vectors.

## Manual Tools

The MCP server (`nexus-memory`) provides explicit tools:
- `remember` - Store a memory
- `recall` - Search memories
- `forget` - Delete a memory

## Shared Store

Same Qdrant collection as:
- Hermes Agent (native plugin)
- OpenClaw (native plugin)
- Any MCP-compatible agent

A memory stored by Claude Code is immediately visible to Hermes and vice
versa - one brain, many agents, as long as you want them to share.
