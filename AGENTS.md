# Nexus Memory — AGENTS.md

## Overview

Nexus Memory is a universal memory layer for AI agents. It runs as an MCP server on your local machine, backed by Qdrant (vector DB) and Voyage AI (embeddings).

### Key concepts

- **MCP server** — communicates over stdio, provides tools: `remember`, `recall`, `forget`, `health`
- **Access levels** — `public` (all agents), `trusted` (approved agents), `private` (owner only)
- **Qdrant collection** — `nexus`, 1024d vectors (voyage-3-large)
- **Auto .env loading** — reads `~/.hermes/.env` and `./.env` on startup

### Project structure

```
nexus-memory/
├── src/nexus_memory/
│   ├── mcp_server.py   # MCP server (main entry point)
│   └── __init__.py
├── tests/              # Not yet created
├── test_mcp.py         # Integration test
├── test_minimal.py     # Quick smoke test
├── pyproject.toml
├── README.md
└── AGENTS.md
```

### Architecture

```
MCP Client (Hermes/OpenClaw) ←→ MCP Server (nexus-memory) ←→ Qdrant (localhost:6333)
                                                                ↑
                                                            Voyage AI (embeddings)
```

## Building & Testing

```bash
pip install -e .                    # install in dev mode
python3 -m src.nexus_memory.mcp_server  # run server
python3 test_minimal.py             # run smoke test
```

## Config

Environment variables (auto-loaded from ~/.hermes/.env or ./.env):

- `VOYAGE_API_KEY` — required for embeddings
- `NEXUS_QDRANT_HOST` — default: localhost
- `NEXUS_QDRANT_PORT` — default: 6333
- `NEXUS_COLLECTION` — default: nexus
- `NEXUS_VOYAGE_MODEL` — default: voyage-3-large
- `NEXUS_EMBEDDING_DIM` — default: 1024

## Adding tools

1. Add tool definition in `handle_list_tools()`
2. Add handler in `handle_call_tool()`
3. Implement the logic (probably in `MemoryStore` class)
4. Test with `test_minimal.py`

## Release

1. Update version in `pyproject.toml`
2. Update `CHANGELOG.md`
3. Tag: `git tag v0.1.0 && git push --tags`
4. Publish: `pip install build && python3 -m build && python3 -m twine upload dist/*`
