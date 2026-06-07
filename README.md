# 🦊 Nexus Memory

**One memory for all your agents. Install what you want. Switch when you want. Nexus Memory is always there.**

Hermes • OpenClaw • Claude Code • Codex • Cursor • Cline • Roo Code • GitHub Copilot • Pi • Continue • Odysseus • Kilo Code …and more!

---

## Why Nexus Memory?

AI agents forget everything when a session ends. Nexus Memory is the **memory layer** that bridges the gap:

- **Persistent** — agents remember across sessions, restarts, and providers
- **Universal** — works with any MCP-compatible agent (Hermes, OpenClaw, Claude Code, Cursor, Cline, ...)
- **Self-hosted** — your data stays on your machine. No cloud, no blockchain, no token costs
- **Secure** — access levels control exactly who sees what (public / trusted / private)

### vs. Alternatives

| Feature | Nexus Memory 🦊 | Walrus Memory 🦭 | mem0 |
|---------|----------------|------------------|------|
| **Hosting** | Local (your machine) | Blockchain/Sui | Cloud |
| **Cost** | Free | WAL token fees | Subscription |
| **Data control** | You own everything | Encrypted on-chain | Shared infra |
| **Search** | Hybrid (vector + filters) | Vector only | Vector only |
| **MCP native** | ✅ | ✅ | ❌ |
| **Access control** | public / trusted / private | Permissions model | Basic |
| **Multi-agent** | All agents share one memory | Shared spaces | Per-user |

---

## Quick Start

### Prerequisites

- Python 3.11+
- [Qdrant](https://qdrant.tech) (vector database) running on localhost:6333
- [Voyage AI](https://voyageai.com) API key (for embeddings)

### 1. Install

```bash
pip install nexus-memory
```

Or from source:

```bash
git clone https://github.com/Neboy72/nexus-memory.git
cd nexus-memory
pip install -e .
```

### 2. Configure

Set your Voyage API key:

```bash
export VOYAGE_API_KEY="vo-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

Or add it to `~/.hermes/.env` (auto-loaded):

```
VOYAGE_API_KEY=vo-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### 3. Run the MCP Server

```bash
nexus-memory
```

The server starts and listens on **stdio** for MCP connections. It auto-creates a `nexus` collection in Qdrant on first run.

### 4. Connect your Agent

#### Hermes Agent

Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  nexus:
    command: /path/to/venv/bin/python3
    args: ["-m", "nexus_memory.mcp_server"]
```

Restart the gateway (`hermes gateway restart`). Tools appear as `mcp_nexus_remember`, `mcp_nexus_recall`, etc.

#### Any MCP-compatible Agent

Configure your agent to launch the server as a stdio subprocess:

```json
{
  "mcpServers": {
    "nexus": {
      "command": "nexus-memory"
    }
  }
}
```

---

## Tools

| Tool | Description | Parameters |
|------|-------------|------------|
| `remember` | Store a memory | `text` (required), `access_level`, `category`, `source` |
| `recall` | Search memories | `query` (required), `limit`, `filter_level` |
| `forget` | Delete a memory | `memory_id` (required) |
| `health` | Check server status | — |

### Access Levels

| Level | Description | Visible to |
|-------|-------------|-----------|
| `public` | General knowledge, project info | All agents |
| `trusted` | Personal data, preferences | Agents you trust (e.g. Kiosha) |
| `private` | Sensitive data (bills, passwords) | Only you (owner) |

---

## Security Model

Nexus Memory runs **entirely on your machine**. Data never leaves your computer. The MCP server listens on `localhost` only — no external access possible.

Access control is enforced at the **server level**, not the agent level. An agent cannot request memories above its authorized level. You decide which agents get which access level via their MCP configuration.

---

## Architecture

```
┌──────────────────────────────────────┐
│         Your Machine                 │
│                                      │
│  ┌────────────────────────────────┐  │
│  │   Nexus Memory (MCP Server)    │  │
│  │   ┌──────┐ ┌──────┐ ┌──────┐  │  │
│  │   │Hermes│ │OpenCl│ │Claude│  │  │
│  │   │      │ │aw    │ │Code  │  │  │
│  │   └──────┘ └──────┘ └──────┘  │  │
│  │        └───┐  ┌───┘            │  │
│  │           ▼  ▼                 │  │
│  │        ┌──────────┐            │  │
│  │        │  Qdrant  │            │  │
│  │        │  (local) │            │  │
│  │        └──────────┘            │  │
│  └────────────────────────────────┘  │
└──────────────────────────────────────┘
```

All agents share the same Qdrant database. Access control is per-memory, not per-database.

---

## Migrating from hermes-nexus-memory / openclaw-nexus-memory

If you're using the old per-agent memory packages:

1. Install `nexus-memory`
2. Start the MCP server — it uses the same Qdrant instance
3. The server reads your existing `hermes-memory` or `openclaw-memory` collections
4. Uninstall the old packages once everything works

No data migration needed — Qdrant stores all vectors locally.

---

## Roadmap

- [x] MCP Server with remember/recall/forget
- [x] Access control (public/trusted/private)
- [x] Auto .env loading
- [ ] Hybrid search (BM25 + Vector + RRF)
- [ ] Encryption at rest (optional)
- [ ] Multi-collection support
- [ ] Web UI dashboard
- [ ] Backup/restore CLI

---

## License

MIT
