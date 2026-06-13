<p align="center">
  <img src="webui/static/assets/logo-transparent.png" width="72" height="72" alt=""/>&nbsp;
  <img src="docs/images/nexus-banner.svg" alt="Nexus Memory" height="72"/>
</p>

Your agents forget. Your context gets lost. Your setup knowledge is scattered across chats, tools and repos.

**Nexus Memory gives every MCP-compatible agent one persistent, self-hosted memory they all share.**

Hermes • OpenClaw • Claude Code • Codex • Cursor • Cline • Roo Code • GitHub Copilot • Pi • Continue • Odysseus • Kilo Code …and more!

[![Stars](https://img.shields.io/github/stars/Neboy72/nexus-memory?style=flat-square&logo=github)](https://github.com/Neboy72/nexus-memory)
[![License](https://img.shields.io/github/license/Neboy72/nexus-memory?style=flat-square)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue?style=flat-square&logo=python)](https://www.python.org/)
[![Qdrant](https://img.shields.io/badge/qdrant-v1.12+-purple?style=flat-square)](https://qdrant.tech/)
|[![Version](https://img.shields.io/badge/version-0.2.5-brightgreen?style=flat-square)](https://github.com/Neboy72/nexus-memory/releases)
|[![Tests](https://img.shields.io/badge/tests-379%20passing-brightgreen?style=flat-square)](tests/)
[![MCP](https://img.shields.io/badge/MCP-native-orange?style=flat-square)](https://modelcontextprotocol.io)

> **🤖 Bot Self-Install:** Tell your agent: *"Read AGENTS.md and install Nexus Memory."* It does the rest.

👉 [![Bot Self-Install](https://img.shields.io/badge/Bot%20Self--Install-blue?style=for-the-badge)](AGENTS.md)

👉 [![Star this repo](https://img.shields.io/badge/⭐%20Star%20this%20repo-323249?style=for-the-badge)](https://github.com/Neboy72/nexus-memory) &nbsp;&nbsp;&nbsp; [![Ko-fi](https://img.shields.io/badge/Ko--fi-323249?style=for-the-badge&logo=kofi&logoColor=white)](https://ko-fi.com/nexusmemory)

---

![Architecture](docs/images/architecture.png)

---

## 🤖 Quick Start

### Tell your agent to install it

Send this prompt to any MCP-compatible agent:

```
Read https://raw.githubusercontent.com/Neboy72/nexus-memory/main/AGENTS.md and follow the installation instructions.
```

Your agent will check prerequisites, install everything, configure the provider, and verify. Zero manual steps.

### 🛠️ Or install manually

```bash
git clone https://github.com/Neboy72/nexus-memory.git
cd nexus-memory
pip install -e .
```

Choose your embedding (auto-detected at runtime, you pick):

- **💚 Google / Vertex AI** — `GOOGLE_API_KEY` in `.env` (768d)
- **💜 Jina** — `JINA_API_KEY` in `.env` (1024d, best value)
- **🦙 Ollama** — `ollama pull nomic-embed-text`
- **☁️ Voyage** — `VOYAGE_API_KEY` in `NEXUS_ENV_FILE` or MCP `env:`-block (1024d, best quality)
- **☁️ OpenAI** — `OPENAI_API_KEY` in `NEXUS_ENV_FILE` or MCP `env:`-block (1536d)
- **🏠 Local (default)** — `pip install nexus-memory[local]` (sentence-transformers, no key)

Start the server:

```bash
nexus-memory
```

### 🌐 Web UI (optional)

Nexus Memory comes with a live graph visualization — your memories as an interactive force-directed graph.

```bash
pip install nexus-memory[webui]
nexus-memory webui
```

Opens a dashboard at `http://127.0.0.1:9120` — filter by category, search, click nodes to inspect details, and see drift status at a glance.

### 🔌 Platform Configuration

Choose your agent:

<details>
<summary>🔷 Hermes Agent</summary>

`~/.hermes/config.yaml`:

```yaml
mcp_servers:
  nexus:
    command: nexus-memory
```

Restart: `hermes gateway restart`
</details>

<details>
<summary>🔷 OpenClaw</summary>

`~/.openclaw/openclaw.json` (`mcp.servers.<name>.env` — nested, not top-level):

```json
{
  "mcp": {
    "servers": {
      "nexus-memory": {
        "command": "nexus-memory",
        "env": { "VOYAGE_API_KEY": "vo-your-key-here" }
      }
    }
  }
}
```
</details>

<details>
<summary>🔷 Claude Code</summary>

`~/.claude/settings.json` or `.mcp.json` in project root:

```json
{
  "mcpServers": {
    "nexus": {
      "command": "python3",
      "args": ["-m", "nexus_memory.mcp_server"]
    }
  }
}
```
</details>

<details>
<summary>🔷 Codex CLI</summary>

`~/.codex/config.toml`:

```toml
[mcp_servers.nexus]
command = "python3"
args = ["-m", "nexus_memory.mcp_server"]
```
</details>

<details>
<summary>🔷 GitHub Copilot (VS Code)</summary>

`.vscode/mcp.json` in your project:

```json
{
  "mcpServers": {
    "nexus": {
      "command": "python3",
      "args": ["-m", "nexus_memory.mcp_server"]
    }
  }
}
```
</details>

<details>
<summary>🔷 Cursor</summary>

Settings → Features → MCP Servers → Add:

- **Name:** nexus
- **Command:** `python3`
- **Arguments:** `-m nexus_memory.mcp_server`
</details>

<details>
<summary>🔷 Cline / Roo Code</summary>

MCP Server Config:

```json
{
  "mcpServers": {
    "nexus": {
      "command": "python3",
      "args": ["-m", "nexus_memory.mcp_server"]
    }
  }
}
```
</details>

<details>
<summary>🔷 Kilo Code</summary>

`.mcp.json` in your project:

```json
{
  "mcpServers": {
    "nexus": {
      "command": "python3",
      "args": ["-m", "nexus_memory.mcp_server"]
    }
  }
}
```
</details>

<details>
<summary>🔷 Pi Coding Agent</summary>

`~/.pi/config.json`:

```json
{
  "mcpServers": {
    "nexus": {
      "command": "python3",
      "args": ["-m", "nexus_memory.mcp_server"]
    }
  }
}
```
</details>

<details>
<summary>🔷 Continue.dev</summary>

`.mcp.json` or `~/.continue/config.json`:

```json
{
  "mcpServers": {
    "nexus": {
      "command": "python3",
      "args": ["-m", "nexus_memory.mcp_server"]
    }
  }
}
```
</details>

<details>
<summary>🔷 Odysseus (PewDiePie)</summary>

Settings → MCP Management → Add Server:

- **Name:** nexus
- **Command:** `python3`
- **Arguments:** `-m nexus_memory.mcp_server`
</details>

<details>
<summary>🔷 Any MCP-compatible agent</summary>

Standard MCP stdio config:

```json
{
  "mcpServers": {
    "nexus": {
      "command": "python3",
      "args": ["-m", "nexus_memory.mcp_server"]
    }
  }
}
```
</details>

---

## 🎯 MCP Tools

| Tool | Description | Parameters |
|------|-------------|------------|
| `remember` 💾 | Store a memory | `text` (req), `category` (req, default `fact`), `access_level`, `source`, `source_url`, `confidence` |
| `recall` 🔍 | Hybrid search (BM25 + Vector + RRF) | `query` (req), `limit`, `filter_level` |
| `forget` 🗑️ | Delete a memory | `memory_id` (req) |
| `update` ✏️ | Update in-place, preserve metadata | `memory_id` (req), `text`, `modified_by` |
| `subscribe` 🔔 | Register a webhook for memory events | `event_type` (req), `webhook_url` (req) |
| `unsubscribe` 🔕 | Remove a webhook subscription | `subscription_id` (req) |
| `list_subscriptions` 📋 | List all active webhooks | — |
| `health` ❤️ | Check server status | — |
| `check_update` 🔄 | Check for newer version on GitHub | — |
| `do_update` ⬆️ | Pull + install + restart server | `confirm` (req, must be `true`) |

### Memory Categories (State-Prefixing)

`category` is a **required** parameter on `remember`. The server applies `"fact"` as a backward-compatible default if a client omits it or sends an unknown value.

| Category | Scope | Use Case |
|----------|-------|----------|
| `fact` ✅ | Permanent | Verified facts, decisions (default) |
| `belief` 🤔 | Drift-prone | Assumptions that may change over time |
| `session` 🔄 | Ephemeral | Current conversation context |
| `rule` 📏 | Permanent | Operating rules, policies |
| `preference` ❤️ | Permanent | User likes, dislikes, habits |
| `temp` ⏳ | Temporary | Short-lived notes, TTL-managed |

### Access Levels 🛡️

| Level | Visible to | Example |
|-------|-----------|---------|
| 🟢 `public` | All agents | Project knowledge, technical info |
| 🟡 `trusted` | Approved agents only | Personal preferences, habits |
| 🔴 `private` | Owner only | Financial data, passwords, bills |

---

## ✨ Features

### Hybrid Retrieval 🛡️

Pure vector search is vulnerable to **RAG poisoning** — adversarial documents that rank high semantically but contain garbage. Nexus Memory blends **BM25 + Vector + Reciprocal Rank Fusion**:

```
Query → ┌─ BM25 Index ──────→ Keyword Rankings
         │                          │
         └─ Vector Embeddings ──→ Semantic Rankings
                                        │
                               RRF Fusion ───→ Combined Rankings
```

| Method | Strengths | Weaknesses |
|--------|----------|------------|
| **BM25** 🔤 | Keyword-exact, poison-resistant | Misses semantics |
| **Vector** 🧠 | Semantic matching, fuzzy queries | Vulnerable to poisoning |
| **Hybrid (RRF)** 🏆 | Best of both | — |

### Source-Tier Boosting 🏷️

| Tier | Sources | Boost |
|------|---------|-------|
| 🟢 Tier 1 | Agent, user, official docs | **1.2×** |
| 🟡 Tier 2 | Curated external | **1.0×** |
| 🔴 Tier 3 | Uncurated / unknown | **0.8×** |

### MemoryCategory Enum 🏷️

Six scopes from Agentic Design Patterns (Ch8): `fact`, `belief`, `session`, `rule`, `preference`, `temp`. Every memory knows its purpose.

### Provenance Tracking 📎

Every memory carries its origin: `source_url`, `confidence` (0.0–1.0), `modified_by`, timestamps. Full audit trail from creation to today.

### Guardrails 🛡️

Content-length warnings for entries >5,000 chars. PII detection hints for emails and phone numbers in non-private entries.

### Fact Lifecycle Model 🧬

Append-only state machine: `pending → canonical | deprecated | rolled_back`. Every revision is versioned with `fact_id`, `version_id`, `content_hash`, `supersedes`, and mandatory `decision_event`. **No silent overwrites. No zombie facts.**

### Staging + Rollback 🔄

| Operation | What it does |
|-----------|-------------|
| `create_pending()` | Stage new facts for review |
| `promote()` | Promote staged → canonical |
| `deprecate()` | Mark canonical as deprecated |
| `rollback()` | Restore previous canonical version |

### Auto-Discovery + Graph Analytics 🔄

Zero-token relation discovery between canonical facts via Qdrant (O(n·k)) + heuristic classification. Graph analytics: hub scores, isolation scores, knowledge gaps, connected components. **Facts connect themselves — no manual edges needed.**

### Skill Export 🎯

`export_skill()` searches canonical facts → clusters into Steps/Pitfalls/Prerequisites/Verification → generates complete `SKILL.md`. **Turn learned facts into reusable agent skills.**

### Belief Drift Detection 🔍

| Score | Status |
|-------|--------|
| 🟢 < 1 | Healthy |
| 🟡 1–3 | Attention needed |
| 🔴 > 3 | Action required |

Detects stale entries, old patterns (`"X running as fallback"` — but X was replaced), age thresholds. Weighted 0–10 scoring.

---

## 📊 vs Other Memory Solutions

| Feature | **Nexus Memory** 🦊 | Walrus Memory 🦭 | mem0 | Honcho | agentmemory | Holographic |
|---------|:-------------------:|:-----------------:|:----:|:------:|:-----------:|:-----------:|
| 🔍 Semantic search | ✅ local or cloud | ✅ via API | ✅ Cloud | ✅ pgvector | ✅ Gemini | ✅ HRR algebra |
| 🔀 **Hybrid retrieval** | **✅ BM25 + Vector + RRF** | ❌ | ✅ Multi-signal | ❌ | ❌ | ❌ |
| 🩺 **Drift detection** | **✅ Scored 0–10** | ❌ | ❌ * | ❌ | ❌ | ❌ |
| 🛡️ **Anti-poisoning** | **✅ Source tiers** | ❌ | ❌ | ❌ | ❌ | ❌ |
| 🔗 **Multi-Level Provenance** | **✅ Source + Corroboration + Dep.** | ✅ On-chain | ❌ | ❌ | ❌ | ❌ |
| 🏷️ **MemoryCategory Enum** | **✅ 6 scopes** | ❌ | ❌ | ❌ | ❌ | ❌ |
| 🧬 **Fact Lifecycle** | **✅ Append-only** | ❌ | ❌ | ❌ | ❌ | ❌ |
| 🔄 **Staging + Rollback** | **✅ Promote/Deprecate/Rollback** | ❌ | ❌ | ❌ | ❌ | ❌ |
| 🎯 **Skill Export** | **✅ Facts → SKILL.md** | ❌ | ❌ | ❌ | ❌ | ❌ |
| 🔗 **SkillGraph** | **✅ 5 relation types, BFS/DFS** | ❌ | ❌ | ❌ | ❌ | ❌ |
| 🔄 **Auto-Discovery** | **✅ 0 token cost** | ❌ | ❌ | ❌ | ❌ | ❌ |
| 📊 **Graph Analytics** | **✅ Hub scores, gaps** | ❌ | ❌ | ❌ | ❌ | ❌ |
| 🚀 **Graph Boost** | **✅ Search ranking boost** | ❌ | ❌ | ❌ | ❌ | ❌ |
| 🛡️ **Access Control** | **✅ public/trusted/private** | ✅ Permissions | ❌ | ❌ | ❌ | ❌ |
| 🏠 **Self-hosted** | **✅ Your machine** | ❌ Blockchain | ❌ Cloud | ❌ Cloud | ❌ Cloud | ✅ Local |
| 💰 **Cost** | **🆓 Free** | WAL token | Subscription | Subscription | API costs | Free |
| 📦 **Code size** | ~9.6K Python | Managed service | Managed service | Managed service | ~50K TS | ~1.5K Python |
| ⏱️ **Setup time** | **1 command** | Signup + SDK | API key + signup | Postgres + pgvector | 30+ min + OAuth | 1 command |

*\*Mem0 lists staleness as an "open problem" in their 2026 report but does not ship a solution.*

**Nexus Memory is the only solution with hybrid retrieval, drift detection, provenance, fact lifecycle, staging/rollback, auto-discovery, graph analytics, skill export, memory categories, and access control — all self-hosted, all in one package.**

---

## 🧩 Embedding Providers

One server. Multiple backends. Same API.

| Provider | Type | Setup | Dims | Quality |
|----------|------|-------|------|---------|
| **Voyage** ☁️ | Cloud API | `VOYAGE_API_KEY` in `.env` | **1024** | ⭐ Best |
| **OpenAI** ☁️ | Cloud API | `OPENAI_API_KEY` in `.env` | **1536** | ⭐ Great |
| **Ollama** 🦙 | Local | `ollama pull nomic-embed-text` | 768 | Better |
| **sentence-transformers** 🏠 | Local | `pip install sentence-transformers` | 384 | Good ✅ *(default)* |

---

## 🔧 Troubleshooting

| Symptom | Check | Fix |
|---------|-------|-----|
| `mcp_nexus_*` tools missing | `grep 'nexus' ~/.hermes/logs/agent.log` | Gateway restart |
| Qdrant not running | `curl http://127.0.0.1:6333/healthz` | `brew services start qdrant` |
| Hybrid search missing | `pip list \| grep bm25s` | `pip install bm25s` |
| Voyage embedding fails | `echo $VOYAGE_API_KEY` | Set in `~/.hermes/.env` |
| ModuleNotFoundError | Check PYTHONPATH | Set `PYTHONPATH=/path/to/nexus-memory` |

---

## 🧪 Tests

```bash
pytest tests/ -v   # 379 tests ✅
```

---

## 📋 Requirements

- Python 3.11+
- Qdrant v1.12+ running on `localhost:6333`
- One embedding provider (auto-detected):
  - **💚 Google / Vertex AI** — `GOOGLE_API_KEY` in `.env` (768d)
- **💜 Jina** — `JINA_API_KEY` in `.env` (1024d, best value)
- **🦙 Ollama** — `ollama pull nomic-embed-text`
  - **☁️ Voyage** — `VOYAGE_API_KEY` in `.env`
  - **☁️ OpenAI** — `OPENAI_API_KEY` in `.env`
  - **🏠 Local** — `pip install sentence-transformers`
- **Optional:** `bm25s` for hybrid search

---

## 📜 License

MIT — use it, modify it, ship it.

---

⭐️ Found it useful? [Give it a star on GitHub](https://github.com/Neboy72/nexus-memory) — it helps others find it!

<sub>Built by [Nebo](https://github.com/Neboy72) · June 2026 · v0.2.0 — One memory for all your agents</sub>
