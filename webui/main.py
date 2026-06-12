#!/usr/bin/env python3
"""Nexus Memory Web UI — FastAPI Backend"""

import json
import os
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
HERE = Path(__file__).parent
STATIC = HERE / "static"

app = FastAPI(
    title="Nexus Memory",
    description="Universal Memory Layer for AI Agents",
)

# Mount static files (CSS, JS, assets)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# ---------------------------------------------------------------------------
# Demo Data — realistic memories that showcase Nexus Memory
# ---------------------------------------------------------------------------
DEMO_MEMORIES = [
    # ── Core Identity ──
    {"id": "mem-001", "category": "fact",      "access_level": "public",  "confidence": 0.97, "text": "Nexus Memory is a universal memory layer for AI agents — self-hosted, secure, and MCP-based.", "source": "system", "drift": "fresh"},
    {"id": "mem-002", "category": "rule",      "access_level": "public",  "confidence": 0.95, "text": "All agents share one memory. What Kiosha learns, Miosha can recall — no silos.", "source": "system", "drift": "fresh"},
    {"id": "mem-003", "category": "fact",      "access_level": "public",  "confidence": 0.93, "text": "Hybrid retrieval combines BM25 full-text search + vector similarity + RRF re-ranking.", "source": "system", "drift": "fresh"},
    {"id": "mem-004", "category": "fact",      "access_level": "public",  "confidence": 0.91, "text": "Drift Detection automatically flags contradictory memories for nightly review.", "source": "system", "drift": "fresh"},
    {"id": "mem-005", "category": "fact",      "access_level": "public",  "confidence": 0.96, "text": "Built on Qdrant vector database with SQLite for metadata and BM25 for keyword search.", "source": "system", "drift": "fresh"},
    {"id": "mem-006", "category": "fact",      "access_level": "public",  "confidence": 0.89, "text": "Self-install via agents.md — any MCP agent can set up Nexus Memory automatically.", "source": "system", "drift": "fresh"},

    # ── Technical Details ──
    {"id": "mem-007", "category": "fact",      "access_level": "public",  "confidence": 0.92, "text": "Supports 6 embedding providers: Voyage AI (1024d), OpenAI (1536d), Google (768d), Jina (1024d), Ollama (768d), sentence-transformers (384d).", "source": "docs", "drift": "fresh"},
    {"id": "mem-008", "category": "fact",      "access_level": "public",  "confidence": 0.88, "text": "Access levels: public (all agents), trusted (approved agents), private (owner only).", "source": "docs", "drift": "fresh"},
    {"id": "mem-009", "category": "fact",      "access_level": "public",  "confidence": 0.86, "text": "Memory categories: fact, belief, session, rule, preference, temp — each with different lifecycle.", "source": "docs", "drift": "fresh"},
    {"id": "mem-010", "category": "fact",      "access_level": "public",  "confidence": 0.84, "text": "Provenance tracking: every memory stores source_url, confidence, and verification status.", "source": "docs", "drift": "fresh"},
    {"id": "mem-011", "category": "fact",      "access_level": "public",  "confidence": 0.82, "text": "Lifecycle management: memories age through fresh → active → stale → archived states.", "source": "docs", "drift": "fresh"},
    {"id": "mem-012", "category": "preference", "access_level": "public",  "confidence": 0.79, "text": "Confidence threshold alerts when memory confidence drops below 0.6.", "source": "config", "drift": "fresh"},

    # ── Agent Ecosystem ──
    {"id": "mem-013", "category": "fact",      "access_level": "public",  "confidence": 0.90, "text": "Native Hermes Agent integration — tools appear as mcp_nexus_remember, mcp_nexus_recall, etc.", "source": "integration", "drift": "fresh"},
    {"id": "mem-014", "category": "fact",      "access_level": "public",  "confidence": 0.87, "text": "OpenClaw compatible — plug-and-play via MCP stdio protocol.", "source": "integration", "drift": "fresh"},
    {"id": "mem-015", "category": "fact",      "access_level": "public",  "confidence": 0.83, "text": "Claude Code support via standard MCP server configuration.", "source": "integration", "drift": "fresh"},
    {"id": "mem-016", "category": "belief",    "access_level": "public",  "confidence": 0.72, "text": "Cursor IDE integration planned — memory-aware coding assistance.", "source": "roadmap", "drift": "fresh"},
    {"id": "mem-017", "category": "belief",    "access_level": "public",  "confidence": 0.68, "text": "VS Code extension with inline memory suggestions in development.", "source": "roadmap", "drift": "fresh"},

    # ── External Reviews ──
    {"id": "mem-018", "category": "fact",      "access_level": "public",  "confidence": 0.85, "text": "Grok review (June 2026): 9/10 — 'Brilliant architecture, best for productive agent setups.'", "source": "external-review", "drift": "fresh"},
    {"id": "mem-019", "category": "fact",      "access_level": "public",  "confidence": 0.84, "text": "Perplexity review: 9.4/10 — 'Comprehensive memory solution with superior retrieval.'", "source": "external-review", "drift": "fresh"},
    {"id": "mem-020", "category": "fact",      "access_level": "public",  "confidence": 0.83, "text": "Gemini review: 9.5/10 — 'Sets a new standard for agent memory management.'", "source": "external-review", "drift": "fresh"},
    {"id": "mem-021", "category": "fact",      "access_level": "public",  "confidence": 0.78, "text": "Claude review: 7.5/10 (up from 6/10) — Lauded hybrid search, suggested web UI.", "source": "external-review", "drift": "fresh"},

    # ── Competitors (Memory) ──
    {"id": "mem-022", "category": "fact",      "access_level": "public",  "confidence": 0.88, "text": "MEM0: Open-source memory layer with vector-graph support. Strong community but cloud-dependent.", "source": "competitive-analysis", "drift": "fresh"},
    {"id": "mem-023", "category": "fact",      "access_level": "public",  "confidence": 0.87, "text": "ZEP: Long-term memory for AI agents. Good API but lacks hybrid retrieval.", "source": "competitive-analysis", "drift": "fresh"},
    {"id": "mem-024", "category": "fact",      "access_level": "public",  "confidence": 0.86, "text": "Letta (formerly MemGPT): OS-level agent memory. Powerful but complex setup.", "source": "competitive-analysis", "drift": "fresh"},
    {"id": "mem-025", "category": "fact",      "access_level": "public",  "confidence": 0.85, "text": "LangChain Memory: Good for LCEL chains but not truly agent-agnostic.", "source": "competitive-analysis", "drift": "fresh"},

    # ── User Preferences (trusted) ──
    {"id": "mem-026", "category": "preference", "access_level": "trusted", "confidence": 0.96, "text": "Nebo prefers DeepSeek V4 Flash for all routine/background agent tasks.", "source": "conversation", "drift": "fresh"},
    {"id": "mem-027", "category": "preference", "access_level": "trusted", "confidence": 0.94, "text": "Kiosha and Miosha are sisters — two different systems, one unit.", "source": "conversation", "drift": "fresh"},
    {"id": "mem-028", "category": "preference", "access_level": "trusted", "confidence": 0.92, "text": "Nebo's vision: 'Build something truly strong — money will follow quality.'", "source": "conversation", "drift": "fresh"},
    {"id": "mem-029", "category": "rule",      "access_level": "trusted",  "confidence": 0.95, "text": "Green Zone rule: routine maintenance is always autonomous — no permission needed.", "source": "system", "drift": "fresh"},
    {"id": "mem-030", "category": "rule",      "access_level": "trusted",  "confidence": 0.93, "text": "Before any API task >1€: STOP and calculate. >2€: ask Nebo first.", "source": "system", "drift": "fresh"},

    # ── Private ──
    {"id": "mem-031", "category": "preference", "access_level": "private", "confidence": 0.98, "text": "Paperless-ngx multi-instance planned: Nebo, Luka, Juri, Maja.", "source": "conversation", "drift": "fresh"},
    {"id": "mem-032", "category": "preference", "access_level": "private", "confidence": 0.97, "text": "Expense tracking via SQLite + Telegram parsing — all finances local.", "source": "conversation", "drift": "fresh"},

    # ── Beliefs / Drift Candidates ──
    {"id": "mem-033", "category": "belief",    "access_level": "public",  "confidence": 0.65, "text": "Web UI with graph visualization could increase adoption by 3x.", "source": "hypothesis", "drift": "drifting"},
    {"id": "mem-034", "category": "belief",    "access_level": "public",  "confidence": 0.60, "text": "Mem0 will add hybrid retrieval within 6 months.", "source": "hypothesis", "drift": "drifting"},
    {"id": "mem-035", "category": "belief",    "access_level": "public",  "confidence": 0.55, "text": "Multi-tenancy is the #1 requested feature from enterprise users.", "source": "hypothesis", "drift": "drifted"},
    {"id": "mem-036", "category": "belief",    "access_level": "public",  "confidence": 0.50, "text": "Nexus Memory should support blockchain-based provenance.", "source": "hypothesis", "drift": "drifted"},

    # ── Stats & Metrics ──
    {"id": "mem-037", "category": "fact",      "access_level": "public",  "confidence": 0.91, "text": "Repository: 12,700+ memory points across all categories.", "source": "system", "drift": "fresh"},
    {"id": "mem-038", "category": "fact",      "access_level": "public",  "confidence": 0.90, "text": "Zero external API dependencies for core functionality — fully self-hosted.", "source": "system", "drift": "fresh"},
    {"id": "mem-039", "category": "fact",      "access_level": "public",  "confidence": 0.89, "text": "224 passing tests with continuous integration.", "source": "system", "drift": "fresh"},

    # ── Technical Architecture ──
    {"id": "mem-040", "category": "fact",      "access_level": "public",  "confidence": 0.87, "text": "MCP server architecture: stdio-based JSON-RPC with 7 tools for memory CRUD operations.", "source": "docs", "drift": "fresh"},
    {"id": "mem-041", "category": "fact",      "access_level": "public",  "confidence": 0.86, "text": "Qdrant collection 'nexus' stores 12,700+ points with 1024d Voyage embeddings.", "source": "system", "drift": "fresh"},
    {"id": "mem-042", "category": "fact",      "access_level": "public",  "confidence": 0.85, "text": "RRF (Reciprocal Rank Fusion) combines BM25 and vector scores for hybrid ranking.", "source": "docs", "drift": "fresh"},
    {"id": "mem-043", "category": "fact",      "access_level": "public",  "confidence": 0.84, "text": "SQLite metadata store: access levels, categories, provenance, confidence scores.", "source": "docs", "drift": "fresh"},
    {"id": "mem-044", "category": "fact",      "access_level": "public",  "confidence": 0.81, "text": "Drift detection runs nightly: compares belief clusters for semantic contradictions.", "source": "system", "drift": "fresh"},
    {"id": "mem-045", "category": "fact",      "access_level": "public",  "confidence": 0.80, "text": "Lifecycle states: fresh → active → stale → archived. Automatic transition based on access recency.", "source": "docs", "drift": "fresh"},

    # ── Use Cases & Applications ──
    {"id": "mem-046", "category": "fact",      "access_level": "public",  "confidence": 0.88, "text": "Personal AI assistant memory: Kiosha remembers everything across sessions.", "source": "use-case", "drift": "fresh"},
    {"id": "mem-047", "category": "fact",      "access_level": "public",  "confidence": 0.86, "text": "Cross-agent handoff: Hermes learns, OpenClaw recalls — seamless knowledge sharing.", "source": "use-case", "drift": "fresh"},
    {"id": "mem-048", "category": "fact",      "access_level": "public",  "confidence": 0.83, "text": "Document intelligence: Paperless-ngx integration auto-tags and stores document memories.", "source": "use-case", "drift": "fresh"},
    {"id": "mem-049", "category": "fact",      "access_level": "public",  "confidence": 0.82, "text": "Conversation memory: Every chat with Nebo is stored with full provenance tracking.", "source": "use-case", "drift": "fresh"},
    {"id": "mem-050", "category": "fact",      "access_level": "public",  "confidence": 0.79, "text": "Expense tracking via Telegram: parse → categorize → store in Nexus Memory.", "source": "use-case", "drift": "fresh"},
    {"id": "mem-051", "category": "fact",      "access_level": "public",  "confidence": 0.77, "text": "GitHub repo context: Nexus tracks issues, PRs, and code changes as memories.", "source": "use-case", "drift": "fresh"},

    # ── Design Philosophy ──
    {"id": "mem-052", "category": "rule",      "access_level": "public",  "confidence": 0.96, "text": "Simplicity over complexity: fewer features, better execution. No feature bloat.", "source": "philosophy", "drift": "fresh"},
    {"id": "mem-053", "category": "rule",      "access_level": "public",  "confidence": 0.94, "text": "Privacy by design: all data stays on your infrastructure. Zero telemetry.", "source": "philosophy", "drift": "fresh"},
    {"id": "mem-054", "category": "rule",      "access_level": "public",  "confidence": 0.92, "text": "Agent-agnostic: no vendor lock-in. Any MCP agent can use Nexus Memory.", "source": "philosophy", "drift": "fresh"},
    {"id": "mem-055", "category": "belief",    "access_level": "public",  "confidence": 0.75, "text": "Memory should be as natural for agents as breathing. Zero friction, maximum value.", "source": "philosophy", "drift": "fresh"},
    {"id": "mem-056", "category": "belief",    "access_level": "public",  "confidence": 0.70, "text": "Open source is not just code — it's a community. Contributors shape the roadmap.", "source": "philosophy", "drift": "fresh"},

    # ── Community & Growth ──
    {"id": "mem-057", "category": "fact",      "access_level": "public",  "confidence": 0.82, "text": "Ko-fi supporters get early access to new features and direct roadmap influence.", "source": "community", "drift": "fresh"},
    {"id": "mem-058", "category": "fact",      "access_level": "public",  "confidence": 0.78, "text": "GitHub Sponsors integration: sponsor-only channels for beta features.", "source": "community", "drift": "fresh"},
    {"id": "mem-059", "category": "belief",    "access_level": "public",  "confidence": 0.72, "text": "A thriving plugin ecosystem could 10x Nexus Memory adoption within a year.", "source": "community", "drift": "drifting"},
    {"id": "mem-060", "category": "belief",    "access_level": "public",  "confidence": 0.68, "text": "Enterprise adoption requires SSO, audit logging, and team management features.", "source": "community", "drift": "drifting"},
    {"id": "mem-061", "category": "fact",      "access_level": "public",  "confidence": 0.76, "text": "Twitter/X presence growing: technical deep-dives, release notes, architecture posts.", "source": "community", "drift": "fresh"},
    {"id": "mem-062", "category": "fact",      "access_level": "public",  "confidence": 0.74, "text": "Reddit discussions in r/AI, r/MachineLearning — positive sentiment, active threads.", "source": "community", "drift": "fresh"},
]

# Add timestamps
base = datetime(2026, 6, 10, 12, 0, 0)
for i, m in enumerate(DEMO_MEMORIES):
    created = base - timedelta(days=random.randint(0, 14), hours=random.randint(0, 23))
    m["created_at"] = created.isoformat()
    m["updated_at"] = (created + timedelta(hours=random.randint(1, 48))).isoformat()

# ---------------------------------------------------------------------------
# Helper: edges (connections between related memories)
# ---------------------------------------------------------------------------
def _build_edges(memories):
    """Create edges from shared source or category relationship."""
    edges = []
    source_groups = {}
    for m in memories:
        src = m.get("source", "unknown")
        source_groups.setdefault(src, []).append(m["id"])
    for src, ids in source_groups.items():
        if len(ids) > 1:
            for i in range(len(ids) - 1):
                edges.append({"source": ids[i], "target": ids[i + 1], "type": src})

    # Cross-category edges (belief ↔ fact if same topic)
    cat_edges = [
        ("mem-033", "mem-001"),  # Web UI belief → Identity
        ("mem-034", "mem-022"),  # Mem0 belief → Competitor
        ("mem-035", "mem-032"),  # Multi-tenancy → Private
        ("mem-036", "mem-038"),  # Blockchain → Self-hosted
        ("mem-021", "mem-018"),  # Claude → Grok (reviews)
        ("mem-020", "mem-019"),  # Gemini → Perplexity (reviews)
        ("mem-016", "mem-015"),  # Cursor → Claude Code (integrations)
        ("mem-013", "mem-014"),  # Hermes → OpenClaw (ecosystem)
        ("mem-046", "mem-047"),  # Kiosha → Cross-agent (use cases)
        ("mem-048", "mem-050"),  # Paperless → Expenses (documents)
        ("mem-049", "mem-046"),  # Conversation → Kiosha (personal)
        ("mem-052", "mem-053"),  # Simplicity → Privacy (philosophy)
        ("mem-054", "mem-013"),  # Agnostic → Hermes (integration)
        ("mem-057", "mem-058"),  # Ko-fi → Sponsors (community)
        ("mem-061", "mem-062"),  # Twitter → Reddit (social)
        ("mem-040", "mem-041"),  # MCP → Qdrant (architecture)
        ("mem-042", "mem-043"),  # RRF → SQLite (retrieval)
        ("mem-044", "mem-045"),  # Drift → Lifecycle (maintenance)
        ("mem-055", "mem-056"),  # Memory natural → Open source (vision)
        ("mem-059", "mem-060"),  # Plugin ecosystem → Enterprise (growth)
    ]
    for s, t in cat_edges:
        edges.append({"source": s, "target": t, "type": "cross-reference"})

    return edges

EDGES = _build_edges(DEMO_MEMORIES)

# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "version": "0.2.3",
        "provider": "Demo Data (MCP server not connected)",
        "memories": len(DEMO_MEMORIES),
    }

@app.get("/api/stats")
async def stats():
    cat_counts = {}
    level_counts = {}
    drift_counts = {}
    for m in DEMO_MEMORIES:
        cat_counts[m["category"]] = cat_counts.get(m["category"], 0) + 1
        level_counts[m["access_level"]] = level_counts.get(m["access_level"], 0) + 1
        drift_counts[m["drift"]] = drift_counts.get(m["drift"], 0) + 1

    return {
        "total_memories": len(DEMO_MEMORIES),
        "total_edges": len(EDGES),
        "by_category": cat_counts,
        "by_access_level": level_counts,
        "by_drift_status": drift_counts,
        "total_unique_sources": len({m["source"] for m in DEMO_MEMORIES}),
        "avg_confidence": round(sum(m["confidence"] for m in DEMO_MEMORIES) / len(DEMO_MEMORIES), 2),
    }

@app.get("/api/memories")
async def get_memories(
    category: Optional[str] = Query(None),
    access_level: Optional[str] = Query(None),
    drift: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
):
    results = list(DEMO_MEMORIES)
    if category:
        results = [m for m in results if m["category"] == category]
    if access_level:
        results = [m for m in results if m["access_level"] == access_level]
    if drift:
        results = [m for m in results if m["drift"] == drift]
    if source:
        results = [m for m in results if m["source"] == source]
    return {
        "memories": results[:limit],
        "total": len(results),
        "edges": EDGES,
    }

@app.get("/api/memories/search")
async def search_memories(q: str = Query(""), limit: int = Query(20, le=50)):
    ql = q.lower()
    results = [
        m for m in DEMO_MEMORIES
        if ql in m["text"].lower() or ql in m["category"] or ql in m["source"]
    ]
    return {
        "query": q,
        "memories": results[:limit],
        "total": len(results),
    }

@app.get("/api/memories/{memory_id}")
async def get_memory(memory_id: str):
    for m in DEMO_MEMORIES:
        if m["id"] == memory_id:
            return m
    return JSONResponse({"error": "Memory not found"}, status_code=404)

# ---------------------------------------------------------------------------
# SPA catch-all — serve index.html for any non-API, non-static route
# ---------------------------------------------------------------------------
@app.get("/{path:path}")
async def spa(path: str):
    if path.startswith("api/") or path.startswith("static/"):
        return JSONResponse({"error": "Not found"}, status_code=404)
    index_path = STATIC / "index.html"
    if not index_path.exists():
        return JSONResponse({"error": "Frontend not built"}, status_code=500)
    return HTMLResponse(index_path.read_text(encoding="utf-8"))

# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=9120,
        reload=True,
        log_level="info",
    )
