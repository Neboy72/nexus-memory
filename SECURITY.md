# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.2.x | ✅ Active development |
| < 0.2 | ❌ No longer maintained |

## Reporting a Vulnerability

Nexus Memory runs entirely on your local machine. No data leaves your system
unless you configure a cloud embedding provider (Voyage, OpenAI, Google, Jina).

**If you discover a security issue:**

1. **Do not open a public GitHub issue.**
2. Email the maintainer at **neboy72@googlemail.com**
3. You will receive a response within 48 hours.

We will acknowledge receipt, assess the issue, and coordinate a fix and
disclosure timeline.

## Security Design

Nexus Memory is designed with a **security-first** approach:

- **Local by default** — Qdrant runs on localhost:6333. No remote access.
- **No outbound data** — memories stay on your machine. Only embedding API
  calls (configurable) leave your network.
- **Access control** — three levels: `public`, `trusted`, `private`,
  enforced by the MCP server before returning results.
- **No cloud dependency** — works fully offline with local embeddings
  (Ollama, sentence-transformers).

## Known Security Considerations

| Area | Risk | Mitigation |
|------|------|------------|
| Embedding providers | API keys in config/env | Use MCP `env:` block. Never commit keys to git. |
| Qdrant | No built-in auth | Qdrant listens on localhost only. Do not expose. |
| MCP stdio transport | Local only | MCP runs on stdio — no network socket. |
| Memory content | PII in stored data | Server logs a warning on email/phone patterns. |
| Third-party dependencies | Supply chain | Pinned via `pip install -e .` from git, not PyPI. |
