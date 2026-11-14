# Nexus Memory — OpenClaw Plugin

Long-term memory for [OpenClaw](https://openclaw.ai), powered by [Qdrant](https://qdrant.tech). Automatically recalls relevant context before every AI turn and captures conversations after each turn — all through Qdrant's vector search, with no Python dependency.

> **Thin TypeScript plugin** that reads from Qdrant REST directly and writes
> through the local Nexus service's shared write gate (`POST /write`).
> Embeddings via the Nexus service, OpenAI, Ollama, Google, or Jina — all over
> HTTP.

## Install

### From ClawHub (published package)

```bash
openclaw plugins install clawhub:@neboy72/nexus-memory
```

The installer places the plugin and sets the memory slot
(`plugins.slots.memory = "nexus-memory"`) itself — you do not edit that by hand.

Then allow the conversation hooks. OpenClaw blocks conversation access for
plugins it did not ship with, so the plugin loads but stays silent: **without
`allowConversationAccess` there is no Auto-Recall and no Auto-Capture**, and
`openclaw plugins doctor` shows the block. Add both lines to
`~/.openclaw/openclaw.json`:

```json
{ "plugins": { "entries": { "nexus-memory": {
    "enabled": true,
    "hooks": { "allowPromptInjection": true, "allowConversationAccess": true } } } } }
```

Restart the gateway afterwards: `openclaw gateway restart`.

### One-command install

```bash
cd ~/nexus-memory/plugins/openclaw
./scripts/install_openclaw_plugin.sh
openclaw gateway restart
```

The install script symlinks the plugin into `~/.openclaw/plugins/nexus-memory/` and prints configuration instructions.

### Manual install

```bash
# Symlink the plugin
ln -s ~/nexus-memory/plugins/openclaw ~/.openclaw/plugins/nexus-memory

# Or copy it
cp -r ~/nexus-memory/plugins/openclaw ~/.openclaw/plugins/nexus-memory
```

## Prerequisites

1. **Qdrant** running on `localhost:6333`

   ```bash
   docker run -d -p 6333:6333 qdrant/qdrant
   ```

2. **Embedding source** — pick one. Every Recall and every Capture embeds
   through it, so the plugin has nothing to recall or capture without one.

   **a. Ollama — local, free, recommended for a fresh install**

   ```bash
   ollama pull qwen3-embedding:0.6b
   ```

   Then name the provider in the plugin config (see
   [Configuration](#configuration)): `"embedding": { "provider": "ollama" }`.
   Default model `qwen3-embedding:0.6b`, 1024 dimensions. Exporting
   `OLLAMA_HOST` or `OLLAMA_BASE_URL` also selects Ollama — and that endpoint —
   when the config names no provider.

   **b. A cloud provider with your own key** — name the provider in the config
   (`"provider": "openai"` etc.) and export its key:

   | Provider | Env Var | Default Model | Dimensions |
   |----------|---------|---------------|------------|
   | **OpenAI** | `OPENAI_API_KEY` | `text-embedding-3-small` | 1536 |
   | **Google** | `GOOGLE_API_KEY` | `text-embedding-004` | 768 |
   | **Jina** | `JINA_API_KEY` | `jina-embeddings-v3` | 1024 |

   ```bash
   export OPENAI_API_KEY="<dein-schluessel>"
   ```

   **c. The local Nexus service** — only if you run Nexus Memory yourself:

   ```bash
   # Install from the repository — NOT from PyPI (see note below).
   git clone https://github.com/Neboy72/nexus-memory.git ~/nexus-memory
   pip install -e ~/nexus-memory
   nexus-memory serve   # http://127.0.0.1:9122
   ```

   > **The PyPI package named `nexus-memory` is not this project.** PyPI serves
   > version 1.0.2 with the note "DEPRECATED: renamed to smriti-memcore", so
   > `pip install nexus-memory` installs someone else's package. Install from
   > the repository (clone above, or
   > `pip install "git+https://github.com/Neboy72/nexus-memory.git"` if your pip
   > may install from Git).

   This is our in-house setup, not the default for a ClawHub install: the
   plugin does not ship that service. Point the plugin elsewhere with
   `embedding.baseUrl`; the service picks its own embedding backend through the
   engine's `NEXUS_EMBEDDING_PROVIDER`.

   > **Without one of these three sources** the plugin still loads, but there is
   > no Recall and no Capture. `openclaw plugins doctor` and the plugin's
   > self-report show it.

3. **The Nexus service for writes** — since 10.11.2026 every write goes to
   `POST /write` on `writeGate.url`, where the shared write gate checks it
   before anything reaches Qdrant. Run it from a repository checkout with
   `nexus-memory serve` (http://127.0.0.1:9122) and put its per-installation
   key (`~/.nexus-memory/service_auth.key`) in `writeGate.key`. Reads still talk
   to Qdrant directly; without this service the plugin **refuses to write**
   rather than bypassing the gate.

## Configuration

Add to `~/.openclaw/openclaw.json`:

```json
{
  "plugins": {
    "slots": {
      "memory": "nexus-memory"
    },
    "entries": {
      "nexus-memory": {
        "enabled": true,
        "hooks": {
          "allowPromptInjection": true,
          "allowConversationAccess": true
        },
        "config": {
          "qdrantUrl": "http://localhost:6333",
          "collection": "nexus",
          "writeGate": {
            "url": "http://127.0.0.1:9122",
            "key": "${NEXUS_SERVICE_AUTH_KEY}"
          },
          "embedding": {
            "provider": "openai",
            "model": "text-embedding-3-small",
            "apiKey": "${OPENAI_API_KEY}"
          },
          "autoRecall": true,
          "autoCapture": true,
          "maxRecallResults": 10,
          "accessLevel": "private",
          "debug": false,
          "cronFormGate": {
            "enabled": false
          }
        }
      }
    }
  }
}
```

### Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `qdrantUrl` | `string` | `http://localhost:6333` | Qdrant REST API endpoint (reads) |
| `collection` | `string` | `nexus` | Qdrant collection name |
| `writeGate.url` | `string` | — (required) | Nexus service base URL; writes go to `POST /write` there, never straight to Qdrant |
| `writeGate.key` | `string` | — (required) | Per-installation service key (`<data-dir>/service_auth.key`, same header as `/embed`); supports `${ENV_VAR}` |
| `embedding.provider` | `string` | auto-detected | `openai`, `ollama`, `google`, or `jina` |
| `embedding.model` | `string` | provider default | Embedding model name |
| `embedding.apiKey` | `string` | env var | API key (supports `${ENV_VAR}` interpolation) |
| `embedding.baseUrl` | `string` | provider default | Base URL (mainly for Ollama) |
| `embedding.dimensions` | `number` | provider default | Vector dimensions |
| `autoRecall` | `boolean` | `true` | Inject relevant memories before every AI turn |
| `autoCapture` | `boolean` | `true` | Store conversation turns to Qdrant after each AI turn |
| `maxRecallResults` | `number` | `10` | Max memories injected per turn (1–20) |
| `accessLevel` | `string` | `private` | Agent access level: `public`, `trusted`, or `private` |
| `ownerUserIds` | `string[]` | `[]` | Owner identities (the human), one per platform. The only owner source besides an already existing `owner.json`; the plugin never claims an owner itself. Empty and no file = owner unknown, all reads `public` (see "Owner") |
| `privateRooms` | `string[]` | `[]` | Rooms explicitly released for memories above `public`. Empty = every group/channel turn reads `public` (see "Released rooms") |
| `debug` | `boolean` | `false` | Verbose debug logs |
| `cronFormGate.enabled` | `boolean` | `false` | Master switch for the unattended-send gate (cron/heartbeat sessions) |
| `cronFormGate.titles` | `string[]` | `[]` | Exact allowed first-line titles; `enabled` with an empty list keeps the gate off |
| `cronFormGate.maxLines` | `number` | `6` | Max lines of an accepted form (1–50) |
| `cronFormGate.maxChars` | `number` | `900` | Max characters of an accepted form (50–5000) |
| `planGate.enabled` | `boolean` | `false` | Level-3 plan lock: non-trivial commands require a plan lock file. Level-1 guardrails stay active either way |
| `planGate.lockPath` | `string` | `<os tmpdir>/nexus-plan-gate.lock` | Where the plan lock is expected (portable per-user default) |
| `planGate.maxAgeSeconds` | `number` | `300` | Lock validity in seconds (30–3600) |

> **Writes require `writeGate`.** Since 10.11.2026 the plugin does not write
> to Qdrant directly. Every store/capture/guardrail-override sends the text to
> the Nexus service, which runs it through the shared write gate (empty content,
> `fact` labels without a statement, missing provenance, access level from the
> source, and `rule` only from a manual origin or a deliberate call — the store
> tool sets `explicit: true`, auto-capture never may, and quoted document text
> is refused either way). Without `writeGate.url` and `writeGate.key` the write
> fails loudly instead of bypassing the checks. A rejection (HTTP 4xx) is final:
> it is never put into the capture retry queue and never retried.
> Reads (recall/search/scroll) still talk to Qdrant directly.

### Unattended send gate (`cronFormGate`)

Off by default. When enabled, messages from unattended sessions (`:cron:`,
`:heartbeat`) are delivered only if they match a fixed form: an exact
first-line title from `titles`, at most `maxLines` lines / `maxChars`
characters, and no reasoning traces. Anything else is cancelled and recorded
as one line in the workspace daily note. Interactive sessions (DMs, groups)
are never affected. This is what lets a cron job alert you without opening a
free-text channel for unattended runs.

> **Titles are deployment-specific data, not code.** Every installation picks
> its own titles in its local config; the repo ships none, and the test-suite
> enforces that (see `test-deploy-literals.ts`-style contract in
> `test-cron-form-gate.mjs`). Never add your deployment's real titles to this
> repository — a foreign user's cron sends must never be shaped by literals
> that only make sense in one deployment.

```json
"cronFormGate": {
  "enabled": true,
  "titles": ["⚠️ Alert", "🚀 Release"]
}
```

### Plan gate (`planGate`)

Level 1 (the destructive-command guardrails: recursive deletes on protected
paths, killing the embedding runtime, full config replaces) is **always
active** and cannot be switched off — it is a documented safety feature.
`planGate.*` controls **level 3 only**: the plan coercion that makes
non-trivial actions require a plan lock file first.

Off by default. When enabled, an allowed action must be preceded by a lock
file whose content starts with `plan:` (an exact prefix — a `# Plan:` heading
does **not** count), written to `planGate.lockPath` (default:
`<os tmpdir>/nexus-plan-gate.lock`) and not older than `maxAgeSeconds`.
Invalid or expired locks are deleted. Without a valid lock the call is blocked
with a message naming the path, the contract and the validity.

```json
"planGate": {
  "enabled": true
}
```

> Migration: if you relied on the plan lock before this release, set
> `planGate.enabled: true` — the default is off so no existing deployment is
> suddenly blocked. The lock path moved from a hard-coded developer path to
> the portable OS temp dir; a previous `lockPath` override is honoured as-is.

### Access Levels

Nexus Memory supports three access levels with hierarchical filtering:

- **`public`** — agent sees only `public` memories
- **`trusted`** — agent sees `public` + `trusted` memories
- **`private`** — agent sees all memories (`public` + `trusted` + `private`)

This lets you run multiple agents with different trust levels against the same Qdrant collection.

The level is enforced everywhere the plugin reads: semantic search, delete
(by ID and by query) **and the graph tools** (`nexus_find_entities`,
`nexus_graph_traverse`, `nexus_get_subgraph`, `nexus_get_related`). In the
graph, a memory outside the agent's level is not just hidden itself — its
neighbours are hidden too, and a traversal does not pass *through* it. Asking
for a hidden fact by ID answers exactly like asking for one that does not
exist.

A stored point without an `access_level` counts as `private` (fail-closed), so
legacy or foreign writers can never accidentally publish data. Auto-Capture
writes every new memory at the configured `accessLevel` (default `private`).

### Owner (`ownerUserIds`)

The plugin never decides on its own who its owner is. There are exactly two
sources — configuration, or an owner file that already exists:

```json
"ownerUserIds": ["123456789", "987654321"]
```

One ID per platform, because the same person has a different ID on Telegram and
on Discord. A non-empty list always wins and the file is then not read.

Without a list, the plugin reads `<OPENCLAW_STATE_DIR>/nexus/owner.json`
(default `~/.openclaw`), if that file exists:

```json
{"version":2,"owners":{"telegram":"5763330319","discord":"373816693522432001"}}
```

The key is the `channelId` of the hook context (`telegram`, `discord`, …). Room
reads compare the author against the owners of **all** platforms, so the owner
stays the owner on every one of them.

**The plugin never writes this file.** Up to 1.22.3 it did: the first human who
wrote on a platform became that platform's owner, which meant that a stranger
who wrote first owned the private memories — ClawHub's audit raised this as
MCP07-1, and since 1.22.4 the automatic claim is gone. A file left over from
those versions keeps working, so an existing installation does not lose its
state after the update; it is simply never extended or rewritten.

To set an owner, do one of these and restart the gateway:

- put the IDs in `ownerUserIds` (preferred — no file to protect), or
- place the file above yourself (mode `0600`).

To reset the owner, delete the file — reads fall back to `public` at once,
because nothing is cached:

```bash
rm ~/.openclaw/nexus/owner.json    # or $OPENCLAW_STATE_DIR/nexus/owner.json
```

A `version: 1` file holds a single id without a platform, so it cannot be
attributed to one; it is ignored (not deleted) with one log line, and reads stay
`public` until you set an owner the current way. A corrupt file is reported once
and treated as absent.

An owner counts as known when either `ownerUserIds` is non-empty or the file
yields at least one entry. **With no known owner every read is `public`** —
direct chats included, not only groups: the plugin cannot tell its owner's chat
from a stranger's, so it delivers nothing above `public` anywhere. That is the
fail-closed direction on purpose. Auto-Capture stays blocked in every group
regardless.

### Released rooms (`privateRooms`)

Group and channel turns are `public` unless the room is explicitly released:

```json
"privateRooms": ["-1001234567890", "session:agent:main:discord:channel:42"]
```

Default: empty — every group turn reads `public`. A bare chat ID means
`chat:<id>`; use the `session:` form for a turn that carries no chat id at all.
Only these rooms may see memories above `public`.

This list exists because the plugin used to infer privacy from silence: a room
counted as private as long as no stranger had written in it. Silent readers,
members who joined later, foreign bots and threads are all invisible to that
observation, so "nobody foreign has spoken" was never evidence of a private
audience — ClawHub's audit raised this as MCP07-2. The plugin still *observes*
who writes where, but that observation can now only tighten the verdict: a room
a stranger has written in is `public` even when it is released.

Auto-Capture stays blocked in every group regardless.

A turn counts as a group turn when the hook payload carries a `groupId`, including an
empty one. The `message_received` payload has no explicit "is this a DM" flag; it may
carry bot indicators under several field names (`isBot`, `senderIsBot`, `fromBot`, …),
which the plugin checks when present, and no indicator simply means "treat as human".

The room key is taken from the first identifier that is present, in this order:
`conversationId`, `chatId`, `groupId`, then `sessionKey`. The observer only ever passes
`chatId`, `groupId` and `sessionKey`, so in practice the chat id decides and the
session key is used only for a turn that carries neither of the other two. A turn that
carries no identifier at all has no room, and reads on it are capped to the public
level, the same as in a room marked shared.

Auto-Capture is skipped whenever the payload carries a `groupId`, including an empty
one; only a payload without a `groupId` can be captured.

### Tool level is bound per session

The AI tools (`nexus_search`, `nexus_forget`, the graph tools, `nexus_store`) get
no turn context from the host, so the plugin records the turn's decision when it
builds the prompt and hands it to the call: `before_tool_call` ties each tool call
to the session it runs in, and the tool reads the level of *that* session only.
A call that cannot be tied to a session — no session key in the hook context, an
expired entry (older than 120 s), or a call that never went through
`before_tool_call` — reads `public`. It never inherits "the level that was seen
last", which is what made two parallel conversations able to mix up their
permissions (ClawHub audit MCP07-3).

## How it works

Once installed and configured, the plugin works automatically:

- **Auto-Recall** (`before_prompt_build` event) — Before every AI turn, the user's message is embedded and used to search Qdrant for semantically similar memories. Results are injected as context wrapped in `<nexus-context>` tags. Memories are filtered by the agent's access level.

- **Auto-Capture** (`agent_end` event) — After every successful AI turn, the last conversation turn (user + assistant messages) is embedded and stored in Qdrant. Automated triggers (exec-event, cron-event, heartbeat) are skipped. Previously injected context tags are cleaned from the capture.

## AI Tools

The AI uses these tools autonomously:

| Tool | Description | Parameters |
|------|-------------|------------|
| `nexus_store` | Save information to memory | `text` (req), `category`, `access_level` |
| `nexus_search` | Search memories by semantic query | `query` (req), `limit` |
| `nexus_forget` | Delete a memory by ID or query | `memoryId` or `query` |

## Shared Store

This plugin reads/writes the **same Qdrant collection** as the Nexus Memory MCP server and the Hermes native plugin. A memory stored by OpenClaw is immediately visible to Hermes, Claude Code, Cursor, or any MCP-compatible agent — and vice versa. One brain, many agents.

```
┌─────────────┐   ┌──────────────┐   ┌─────────────────┐
│   OpenClaw  │   │ Hermes Agent │   │ Claude Code /   │
│  (this plugin)│  │ (native plugin)│  │  Cursor (MCP)  │
└──────┬──────┘   └──────┬───────┘   └──────┬──────────┘
       │ Qdrant REST     │ qdrant_client     │ stdio
       └────────┬────────┴───────────────────┘
                ▼
        ┌──────────────────┐
        │  Qdrant :6333    │
        │  Collection: nexus│
        └──────────────────┘
```

## Development

```bash
# Install dev dependencies
cd ~/nexus-memory/plugins/openclaw
npm install

# Type-check
npm run check-types

# Build (esbuild → dist/index.js)
npm run build
```

## Architecture

```
plugins/openclaw/
├── openclaw.plugin.json   # Plugin manifest
├── package.json           # npm metadata
├── tsconfig.json          # TypeScript config
├── index.ts               # Entry point — register(api)
├── logger.ts              # Logger (prefixes "nexus:")
├── runtime.ts             # Memory runtime + prompt section builder
├── types/
│   └── openclaw.d.ts      # OpenClaw SDK type declarations
├── lib/
│   ├── config.ts          # Config parser + schema
│   ├── embedder.ts        # Embedding provider (OpenAI/Ollama/Google/Jina)
│   └── qdrant-client.ts   # Qdrant REST API client
├── hooks/
│   ├── trigger.ts         # isInteractiveTrigger helper
│   ├── recall.ts          # Auto-Recall (before_prompt_build)
│   └── capture.ts         # Auto-Capture (agent_end)
├── tools/
│   ├── store.ts           # nexus_store tool
│   ├── search.ts          # nexus_search tool
│   └── forget.ts          # nexus_forget tool
├── scripts/
│   └── install_openclaw_plugin.sh
└── README.md
```

## License

MIT