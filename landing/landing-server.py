#!/usr/bin/env python3
"""Separate marketing landing page for Nexus Memory (Port 9121)."""
import os
import sys
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

app = FastAPI(title="Nexus Memory — Landing")

# Static assets from the main webui
static_dir = Path(__file__).parent.parent / "webui" / "static"
assert static_dir.exists(), f"Static dir not found: {static_dir}"

app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
app.mount("/landing-static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="landing-static")

LANDING_HTML = (Path(__file__).parent / "index.html").read_text()


@app.get("/", response_class=HTMLResponse)
async def landing():
    return LANDING_HTML


# ─── Demo Data ──────────────────────────────────────────────
DEMO_NODES = [
    # Facts (core concept)
    {"id": "demo-001", "title": "Nexus Memory", "text": "Universal memory layer for AI agents. MCP-native, self-hosted, open source.", "category": "fact", "access_level": "public", "drift": "fresh", "confidence": 1.0, "created_at": "2026-06-13T10:00:00Z", "source": "documentation", "group": 0},
    {"id": "demo-002", "title": "Hybrid Retrieval", "text": "BM25 full-text search + dense vector similarity + RRF re-ranking for optimal precision and recall.", "category": "fact", "access_level": "public", "drift": "fresh", "confidence": 0.98, "created_at": "2026-06-13T10:01:00Z", "source": "documentation", "group": 0},
    {"id": "demo-003", "title": "MCP Protocol Support", "text": "Native Model Context Protocol — works with Hermes, Claude Code, Cursor, Copilot, Cline, and any MCP-compatible agent.", "category": "fact", "access_level": "public", "drift": "fresh", "confidence": 1.0, "created_at": "2026-06-13T10:02:00Z", "source": "documentation", "group": 0},
    {"id": "demo-004", "title": "6 Embedding Providers", "text": "Voyage AI, OpenAI, Google/Vertex AI, Jina, Ollama (local), sentence-transformers (local) — auto-detected, zero config.", "category": "fact", "access_level": "public", "drift": "fresh", "confidence": 0.99, "created_at": "2026-06-13T10:03:00Z", "source": "documentation", "group": 0},
    {"id": "demo-005", "title": "Qdrant Vector Store", "text": "Self-hosted Qdrant for vector storage. 1024-dimensional embeddings, cosine similarity, hybrid search.", "category": "fact", "access_level": "public", "drift": "fresh", "confidence": 1.0, "created_at": "2026-06-13T10:04:00Z", "source": "documentation", "group": 0},
    {"id": "demo-006", "title": "Zero Trust Access Levels", "text": "Three access tiers: public (all agents), trusted (approved agents), private (owner only). Provenance tracking on every memory.", "category": "fact", "access_level": "public", "drift": "fresh", "confidence": 0.97, "created_at": "2026-06-13T10:05:00Z", "source": "documentation", "group": 0},
    {"id": "demo-007", "title": "Webhook Events", "text": "Subscribe to memory.remember, memory.update, memory.forget events with HTTP webhooks. Fire-and-forget delivery.", "category": "fact", "access_level": "public", "drift": "fresh", "confidence": 0.98, "created_at": "2026-06-13T10:06:00Z", "source": "documentation", "group": 0},
    {"id": "demo-008", "title": "Drift Detection", "text": "Automatically detects contradictory or outdated memories. Nightly drift-check job surfaces stale beliefs.", "category": "fact", "access_level": "public", "drift": "fresh", "confidence": 0.96, "created_at": "2026-06-13T10:07:00Z", "source": "documentation", "group": 0},

    # Sessions (example interactions)
    {"id": "demo-009", "title": "Session: Architecture Review", "text": "Analysed Nexus Memory architecture — MCP server, Qdrant integration, embedding pipeline. 93% recall@5 on test queries.", "category": "session", "access_level": "public", "drift": "fresh", "confidence": 0.92, "created_at": "2026-06-12T14:30:00Z", "source": "session", "group": 2},
    {"id": "demo-010", "title": "Session: Provider Plugin Test", "text": "E2E test of Nexus Memory provider plugin. Validated read/write cycle, recall accuracy, and category coersion.", "category": "session", "access_level": "public", "drift": "fresh", "confidence": 0.94, "created_at": "2026-06-15T09:00:00Z", "source": "session", "group": 2},
    {"id": "demo-011", "title": "Session: Graph Visualization", "text": "Built interactive D3.js force-directed graph for memory visualization. 500 nodes, 6 categories, zoom/pan/detail.", "category": "session", "access_level": "public", "drift": "fresh", "confidence": 0.96, "created_at": "2026-06-16T11:00:00Z", "source": "session", "group": 2},
    {"id": "demo-012", "title": "Session: Hybrid Search Tuning", "text": "Tuned BM25 + vector search weights. Optimal: 0.3 BM25 + 0.7 vector. RRF k=60 for re-ranking.", "category": "session", "access_level": "public", "drift": "drifting", "confidence": 0.91, "created_at": "2026-06-11T16:00:00Z", "source": "session", "group": 2},

    # Rules (operating rules)
    {"id": "demo-013", "title": "Nexus-First Rule", "text": "Before every user interaction, ALWAYS call Nexus Recall first — no exceptions. If empty → new content. If hit → context.", "category": "rule", "access_level": "public", "drift": "fresh", "confidence": 1.0, "created_at": "2026-06-10T08:00:00Z", "source": "configuration", "group": 3},
    {"id": "demo-014", "title": "Memory Categorization", "text": "All memories must have a category: fact (verified), belief (mutable), session (episodic), rule (stable), preference (user), temp (ephemeral).", "category": "rule", "access_level": "public", "drift": "fresh", "confidence": 1.0, "created_at": "2026-06-10T08:01:00Z", "source": "configuration", "group": 3},
    {"id": "demo-015", "title": "Provenance Standard", "text": "Every remember() call MUST include source_url and confidence. Enables Justification-Check at recall time.", "category": "rule", "access_level": "public", "drift": "fresh", "confidence": 1.0, "created_at": "2026-06-10T08:02:00Z", "source": "configuration", "group": 3},

    # Preferences
    {"id": "demo-016", "title": "Preferred Provider: Voyage", "text": "Voyage-3-large is the default embedding provider. 1024d, best quality for agent memory retrieval.", "category": "preference", "access_level": "public", "drift": "fresh", "confidence": 0.95, "created_at": "2026-06-01T12:00:00Z", "source": "configuration", "group": 4},
    {"id": "demo-017", "title": "Temperature: 0.2", "text": "Low temperature for consistent memory operations. Deterministic recall preferred over creative retrieval.", "category": "preference", "access_level": "public", "drift": "fresh", "confidence": 0.97, "created_at": "2026-06-01T12:01:00Z", "source": "configuration", "group": 4},
    {"id": "demo-018", "title": "Category Default: Fact", "text": "When category is omitted, default to 'fact' for backward compatibility. All legacy data inherits this scope.", "category": "preference", "access_level": "public", "drift": "fresh", "confidence": 0.99, "created_at": "2026-06-01T12:02:00Z", "source": "configuration", "group": 4},

    # Beliefs (mutable assumptions being tracked)
    {"id": "demo-019", "title": "Belief: Voice Interface Priority", "text": "Speculative: voice-controlled memory retrieval could become primary interaction mode for mobile agents.", "category": "belief", "access_level": "public", "drift": "drifting", "confidence": 0.45, "created_at": "2026-06-08T10:00:00Z", "source": "inference", "group": 1},
    {"id": "demo-020", "title": "Belief: Multi-Modal Memory", "text": "Hypothesis: adding image/audio embeddings would improve recall for visual/voice-first agents by 30%+.", "category": "belief", "access_level": "public", "drift": "drifting", "confidence": 0.55, "created_at": "2026-06-09T10:00:00Z", "source": "inference", "group": 1},
    {"id": "demo-021", "title": "Belief: Scaling Bottleneck", "text": "Current hypothesis: Qdrant sharding becomes bottleneck at 10M+ vectors. Need to evaluate alternatives.", "category": "belief", "access_level": "public", "drift": "drifted", "confidence": 0.5, "created_at": "2026-06-07T10:00:00Z", "source": "inference", "group": 1},

    # Temp (ephemeral notes)
    {"id": "demo-022", "title": "Temp: Release v0.2.5 Notes", "text": "Ship on 2026-06-13. Features: webhook subscriptions, provenance standard, state-prefixing, 379 tests, 10 tools.", "category": "temp", "access_level": "public", "drift": "fresh", "confidence": 0.9, "created_at": "2026-06-13T09:00:00Z", "source": "planning", "group": 5},
    {"id": "demo-023", "title": "Temp: v2.1.0 Roadmap Idea", "text": "Auto-discovery of memory patterns, graph analytics (PageRank for nodes), graph ranking for recall.", "category": "temp", "access_level": "public", "drift": "drifting", "confidence": 0.7, "created_at": "2026-06-10T09:00:00Z", "source": "planning", "group": 5},
    {"id": "demo-024", "title": "Temp: Marketing Page Draft", "text": "Crystal hero + AI agent ratings (Gemini 9.5, Perplexity 9.4, Grok 9/10) + MCP logos + feature grid + live graph demo.", "category": "temp", "access_level": "public", "drift": "fresh", "confidence": 0.95, "created_at": "2026-06-16T10:00:00Z", "source": "planning", "group": 5},
]

DEMO_EDGES = [
    # Core → features
    {"source": "demo-001", "target": "demo-002"},
    {"source": "demo-001", "target": "demo-003"},
    {"source": "demo-001", "target": "demo-004"},
    {"source": "demo-001", "target": "demo-005"},
    {"source": "demo-001", "target": "demo-006"},
    {"source": "demo-001", "target": "demo-007"},
    {"source": "demo-001", "target": "demo-008"},
    # Sessions → related facts
    {"source": "demo-009", "target": "demo-001"},
    {"source": "demo-010", "target": "demo-001"},
    {"source": "demo-011", "target": "demo-002"},
    {"source": "demo-012", "target": "demo-002"},
    # Rules → facts
    {"source": "demo-013", "target": "demo-001"},
    {"source": "demo-014", "target": "demo-006"},
    {"source": "demo-015", "target": "demo-006"},
    # Preferences → facts
    {"source": "demo-016", "target": "demo-004"},
    {"source": "demo-017", "target": "demo-001"},
    {"source": "demo-018", "target": "demo-014"},
    # Beliefs → facts
    {"source": "demo-019", "target": "demo-008"},
    {"source": "demo-020", "target": "demo-001"},
    {"source": "demo-021", "target": "demo-005"},
    # Temp → sessions
    {"source": "demo-022", "target": "demo-009"},
    {"source": "demo-023", "target": "demo-012"},
    {"source": "demo-024", "target": "demo-011"},
    # Cross connections
    {"source": "demo-002", "target": "demo-012"},
    {"source": "demo-013", "target": "demo-010"},
    {"source": "demo-019", "target": "demo-024"},
]


@app.get("/api/health")
async def demo_health():
    return {"status": "ok", "version": "demo"}


@app.get("/api/memories")
async def demo_memories(limit: int = 500, category: str = "all", access_level: str = "all", drift: str = "all"):
    nodes = DEMO_NODES
    if category != "all":
        nodes = [n for n in nodes if n.get("category") == category]
    if access_level != "all":
        nodes = [n for n in nodes if n.get("access_level") == access_level]
    if drift != "all":
        nodes = [n for n in nodes if n.get("drift") == drift]

    # Collect category counts from filtered set
    cat_counts = {}
    for n in nodes:
        cat = n.get("category", "fact")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    return {
        "memories": nodes[:limit],
        "edges": DEMO_EDGES,
        "category_counts": cat_counts
    }


@app.get("/api/memories/search")
async def demo_search(q: str = "", limit: int = 20):
    if not q:
        return {"memories": [], "edges": []}
    ql = q.lower()
    results = [n for n in DEMO_NODES if ql in n.get("title", "").lower() or ql in n.get("text", "").lower()]
    return {"memories": results[:limit], "edges": []}


@app.get("/api/memories/{memory_id}")
async def demo_memory(memory_id: str):
    for n in DEMO_NODES:
        if n["id"] == memory_id:
            return n
    return {"error": "not found"}, 404


@app.get("/api/stats")
async def demo_stats():
    drift = {"fresh": 0, "drifting": 0, "drifted": 0}
    cats = {}
    for n in DEMO_NODES:
        d = n.get("drift", "fresh")
        drift[d] = drift.get(d, 0) + 1
        c = n.get("category", "fact")
        cats[c] = cats.get(c, 0) + 1

    return {
        "total_memories": len(DEMO_NODES),
        "total_edges": len(DEMO_EDGES),
        "avg_confidence": sum(n.get("confidence", 0.5) for n in DEMO_NODES) / len(DEMO_NODES),
        "total_unique_sources": len(set(n.get("source", "") for n in DEMO_NODES)),
        "by_category": cats,
        "by_drift_status": drift,
    }


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9121
    print(f"🚀 Nexus Memory Landing Page → http://127.0.0.1:{port}")
    tailscale_ip = "100.69.110.5"
    print(f"   (Graph remains untouched on 9120)")
    print(f"   🌐 Local:  http://127.0.0.1:{port}")
    print(f"   🌐 Tailnet: http://{tailscale_ip}:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
