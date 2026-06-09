# Contributing to Nexus Memory

Thanks for your interest in Nexus Memory — the universal memory layer for AI agents.

## 🧭 Project Scope

Nexus Memory is an MCP server that provides persistent memory for **any** AI agent (Hermes, Claude, Codex, Cursor, Cline, OpenClaw — any MCP-compatible client). One memory layer, one schema, no silos.

## 🚀 Dev Setup

```bash
# 1. Clone
git clone https://github.com/Neboy72/nexus-memory.git
cd nexus-memory

# 2. Virtual environment
python -m venv .venv
source .venv/bin/activate

# 3. Install in editable mode with dev deps
pip install -e ".[dev]"

# 4. Start Qdrant (choose one)
# Docker:
docker run -d -p 6333:6333 qdrant/qdrant
# Local binary: https://qdrant.tech/documentation/quick-start/
```

## 🔧 Environment

Copy `.env.example` to `.env` and set:

| Variable | Required | Description |
|----------|----------|-------------|
| `QDRANT_URL` | Yes | Qdrant endpoint (default: `http://localhost:6333`) |
| `VOYAGE_API_KEY` | Yes | Voyage AI API key for embeddings |
| `QDRANT_API_KEY` | No | If your Qdrant instance requires auth |

## 🧪 Running Tests

```bash
pytest tests/ -v
```

For test coverage:

```bash
pytest tests/ --cov=nexus_memory --cov-report=term-missing
```

## 📝 Pull Request Process

1. **Open an issue first** — discuss before building
2. **One PR = one feature/fix** — keep it focused
3. **Branch naming:** `feature/short-description` or `fix/short-description`
4. **Tests required** — new features need tests, fixes need a test that catches the regression
5. **Type hints** — all public APIs must have type annotations
6. **Changelog** — add an entry to `CHANGELOG.md` under `[Unreleased]`

### Before submitting

- [ ] Tests pass (`pytest tests/ -v`)
- [ ] Code is typed and formatted
- [ ] No `print()` debugging (use `logging`)
- [ ] Docs updated if API changed
- [ ] Changelog entry added

## 📐 Code Style

- **Python:** Follow [PEP 8](https://peps.python.org/pep-0008/). We use `ruff` for linting and formatting
- **Type hints:** Always annotate public functions. Use `from __future__ import annotations` for clean syntax
- **Logging** over print: use Python's `logging` module
- **Async:** We use `asyncio` + `httpx` for HTTP calls

Run linter before committing:

```bash
ruff check .
ruff format .
```

## 🗺️ Architecture Overview

```
AI Agent (Hermes / Claude / Codex / …)
        │
        ▼  MCP Protocol
┌───────────────┐
│  mcp-nexus    │  ← MCP server exposing memory tools
│  server.py    │
├───────────────┤
│  embedding.py │  ← Voyage AI / Ollama embeddings
├───────────────┤
│  qdrant.py    │  ← Vector storage & search
└───────────────┘
        │
        ▼
    ┌──────────┐
    │  Qdrant  │  ← Vector database
    └──────────┘
```

## ❓ Questions?

Open a [Discussion](https://github.com/Neboy72/nexus-memory/discussions) or reach out via the project's support channels.
