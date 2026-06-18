# Nexus Memory — OpenClaw Plugin

Long-term memory for [OpenClaw](https://openclaw.ai), powered by [Qdrant](https://qdrant.tech). Automatically recalls relevant context before every AI turn and captures conversations after each turn — all through Qdrant's vector search, with no Python dependency.

> **Thin TypeScript plugin** that talks to Qdrant REST API directly. Embeddings via Voyage, OpenAI, Ollama, Google, or Jina — all over HTTP.

## Install

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

2. **Embedding provider** — set one API key (auto-detected):

   | Provider | Env Var | Default Model | Dimensions |
   |----------|---------|---------------|------------|
   | **Voyage** | `VOYAGE_API_KEY` | `voyage-3-large` | 1024 |
   | **OpenAI** | `OPENAI_API_KEY` | `text-embedding-3-small` | 1536 |
   | **Google** | `GOOGLE_API_KEY` | `text-embedding-004` | 768 |
   | **Jina** | `JINA_API_KEY` | `jina-embeddings-v3` | 1024 |
   | **Ollama** | — (no key needed) | `nomic-embed-text` | 768 |

   ```bash
   export VOYAGE_API_KEY="vo-your-key-here"
   ```

   > **No API key?** If you have Ollama running locally with `nomic-embed-text`, it works out of the box.

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
          "embedding": {
            "provider": "voyage",
            "model": "voyage-3-large",
            "apiKey": "${VOYAGE_API_KEY}"
          },
          "autoRecall": true,
          "autoCapture": true,
          "maxRecallResults": 10,
          "accessLevel": "public",
          "debug": false
        }
      }
    }
  }
}
```

### Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `qdrantUrl` | `string` | `http://localhost:6333` | Qdrant REST API endpoint |
| `collection` | `string` | `nexus` | Qdrant collection name |
| `embedding.provider` | `string` | auto-detected | `voyage`, `openai`, `ollama`, `google`, or `jina` |
| `embedding.model` | `string` | provider default | Embedding model name |
| `embedding.apiKey` | `string` | env var | API key (supports `${ENV_VAR}` interpolation) |
| `embedding.baseUrl` | `string` | provider default | Base URL (mainly for Ollama) |
| `embedding.dimensions` | `number` | provider default | Vector dimensions |
| `autoRecall` | `boolean` | `true` | Inject relevant memories before every AI turn |
| `autoCapture` | `boolean` | `true` | Store conversation turns to Qdrant after each AI turn |
| `maxRecallResults` | `number` | `10` | Max memories injected per turn (1–20) |
| `accessLevel` | `string` | `public` | Agent access level: `public`, `trusted`, or `private` |
| `debug` | `boolean` | `false` | Verbose debug logs |

### Access Levels

Nexus Memory supports three access levels with hierarchical filtering:

- **`public`** — agent sees only `public` memories
- **`trusted`** — agent sees `public` + `trusted` memories
- **`private`** — agent sees all memories (`public` + `trusted` + `private`)

This lets you run multiple agents with different trust levels against the same Qdrant collection.

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
│   ├── embedder.ts        # Embedding provider (Voyage/OpenAI/Ollama/Google/Jina)
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