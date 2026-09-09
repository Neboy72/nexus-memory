## [0.18.7] - 2026-09-09

### Changed

- **Update notifications now re-check GitHub every 24h** (previously: a single
  check at server startup — a long-running server never saw releases published
  after it booted). The docstring's "cached 24h" promise is now real.
- **Update nudge repeats every 7 days** instead of once per server lifetime:
  a user who misses the first notice gets reminded weekly, without spam.
  Failure paths unchanged: network errors fail open silently.

## v0.18.7 (unreleased)

### Removed
- **Legacy `webui/` graph dashboard deleted** (the original DeepSeek-era UI). `nexus-memory webui` now starts the current dashboard (`dashboard/`): connected agents, memory graph with filters, memory inspector. Same command, now on port 9121 — no stale UI left behind.

### Changed
- README / AGENTS.md / install scripts point agents to the current dashboard. `pip install nexus-memory[webui]` extra removed from pyproject (fastapi/uvicorn needed only when running the dashboard from a checkout).

# v0.18.6 — Auto-Scoping Parity: All Three Plugins

**Auto-scoping now works identically on every integration path** — Hermes native plugin, OpenClaw TS plugin, and Claude Code hooks. v0.18.5 shipped self-organizing memory server-side (auto-tagging in the MCP server worked for all agents) but the client-side pieces (auto-recall gating, auto-capture tagging, store-tool tagging) lived only in the Hermes plugin. Now every plugin infers areas from scoped centroids with the same clear-match rule — no user config anywhere (Nebo law: full automation or useless; no release without plugin parity).

## New
- **`plugins/openclaw/lib/scope-auto.ts`** — TypeScript port of `scope_auto.py`: centroid cache with TTL + inflight dedup, conservative `inferScope()` (clear-closest only: ≥0.72 absolute AND ≥0.05 margin over runner-up), `prefetchFilterScopes()` with manual-scope-as-addition semantics. Wired into auto-recall gating, auto-capture tagging, and the `nexus_store` tool (explicit scope param wins → cfg scope → auto-infer → default).
- **`plugins/claude-code/scripts/scope_auto.py`** — shared lib for the Claude Code hooks (short-lived processes: one scroll per call, fail-open). `auto_recall.py` now infers the allowed scope set from the prompt itself; `auto_capture.py` tags captures via `_resolve_capture_scope()`.
- **Parity tests**: 13 new tests — 12 Python (clear-match, ambiguous fail-open, absolute threshold, manual-scope union, fetch fail-open, capture resolution) + 1 cross-language parity test that runs the actual TypeScript module via node and asserts identical decisions to the Python implementation.

## Tests
- 1076 passed (1063 previous + 13 parity tests)

**The memory now assigns its own areas — full automation, zero user setup (Nebo law: automate or it's useless).** Scope labels exist since v0.18.4, but they required a config value per agent. Now the memory infers the area itself: when a new memory is stored, it compares the content vector against the centroids of existing scoped areas and inherits the matching scope automatically. And at recall time, a question that clearly belongs to one area gets that area's memories plus the shared ones — no configuration anywhere in the loop.

## New
- **`scope_auto.py` — scope centroids + conservative inference**: centroid per scope from canonical scoped points (60s TTL cache, fail-open to "no areas"). `infer_scope()`: only tags a memory when its vector is CLEARLY closest to one area (margin ≥ 0.05 to runner-up AND ≥ 0.72 absolute similarity) — under-tagging is harmless, over-tagging is what we avoid. Zero LLM cost: pure vector math.
- **Auto-tagging at `remember()`**: caller leaves `scope` at `default` → server inherits the inferred area automatically. Explicit non-default scopes are never overridden. Fail-open: any inference error → stays `default`.
- **Query-side auto-filtering (Hermes plugin prefetch)**: a prompt that clearly belongs to one area surfaces only `default` + that area's memories; ambiguous prompts change nothing (old behavior). No `NEXUS_SCOPE` needed — the query steers itself.
- **Fully backward compatible**: with no scoped memories in the store, centroids are empty → `default` everywhere → byte-for-byte old behavior. Users never see the word "scope".

## Tests
- 1063 passed (1048 previous + 15 new auto-scoping tests: centroid math, margin logic, dimension-mismatch skip, fail-open paths, remember-integration, prefetch integration)

# v0.18.4 — Scopes: Project/Agent Areas (unreleased feature, first implementation)

**Scopes answer "which project does this belong to?"** — access levels already answer "who may see this?". Every memory can now carry a scope label so multiple agents sharing one memory store get clean, focused auto-recall instead of cross-project noise.

## New
- **Scope labels** (`scope`): optional area label on every memory (`nexus_remember(..., scope="voice")`). Valid: `[a-z0-9-]`, max 40 chars, normalized lowercase. Anything invalid degrades to `default` (fail-open) — behaves exactly like pre-scope memories.
- **Core principle — scopes steer automatic prefetch, never explicit search**: auto-prefetch (Hermes plugin `NEXUS_SCOPE` env / OpenClaw plugin `scope` config) surfaces only `default` memories plus the agent's own scope. Explicit `recall()` / `nexus_search` is NEVER scope-filtered — a scoped memory is never hidden from a direct question.
- **OpenClaw plugin parity**: `scope` config key + `NEXUS_SCOPE` env fallback in `lib/config.ts`, gating in the auto-recall hook, scope inheritance in capture + `nexus_store` tool (optional `scope` parameter with same normalization).
- **Claude Code plugin parity**: auto-recall hook gates on `NEXUS_SCOPE` (same client-side filter contract), auto-capture stores memories with inherited `_normalize_scope(NEXUS_SCOPE)`; graph-boost neighbors intentionally unfiltered (explicit relations). MCP-based Claude Code setups inherit v0.18.4 automatically.
- **Backward compatible**: no `NEXUS_SCOPE` set → the agent sees everything (old behavior); memories without a scope field behave as `default`.

## Tests
- 1048 passed on the production suite (1034 previous + 14 new scope tests; 21 additional scope/rewrite tests live on the development testbed workspace)

# v0.18.0 — Consolidation Daemon + Multi-Station Fuel Chain

**Ingestion-time consolidation is live.** Raw session dumps are distilled into atomic, self-contained facts (pronouns resolved, relative dates anchored) and contradictions are superseded at write time — the retrieval hebel from the LongMemEval findings, now in production path.

## New
- **Consolidation daemon** (`consolidation.py`): in-process background thread in the MCP server (no cron, harness-independent). Distills un-consolidated `session` points into `fact` points, resolves conflicts at write time via embed-similarity (≥0.75) + LLM classify (duplicate/supersede/unrelated). Never deletes — superseded facts keep lifecycle status. Kill-switch `NEXUS_CONSOLIDATION=0`. Interval `NEXUS_CONSOLIDATION_INTERVAL` (default 3600s).
- **Fuel chain** (`fuel_chain.py`): the daemon is a hitchhiker on the user's existing LLM config — no setup, no new account. Station order (cheapest first): local Ollama → OpenRouter → OpenAI-compatible keys (OPENAI_API_KEY / NOUS_API_KEY / explicit NEXUS_FUEL_BASE+NEXUS_FUEL_KEY). Cheapest tier model per station, never the user's flagship. All stations closed → daemon sleeps and retries next tick (fail-safe, never crashes, never blocks).
- **Monthly budget cap**: `NEXUS_FUEL_BUDGET_USD` (default 1.00). Paid stations pause when the cap is hit; free local Ollama keeps working. Spend tracker in `~/.nexus-memory/fuel_spend.json` (auto-reset each month).

## Tests
- 799 passed (8 new fuel-chain tests + 19 consolidation tests from the 05.09 GO)

# Changelog

All notable changes to **Nexus Memory** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v0.17.0] - 2026-09-04

### Changed

- **qwen3-embedding is now the preferred local embedding provider** (Ollama).
  Benchmark (LongMemEval-S, 100 identical questions, identical hybrid RRF
  protocol): qwen3-embedding:0.6b 66/72/75% (R@5/10/20) vs bge-m3 62/71/73%.
  Priority order is now: qwen3-embedding → bge-m3 → any 'embed' model.
  Existing users keep their current model automatically (collection drift
  guard reads `embedding_model` from config) — no silent mixed-model
  collections. bge-m3 remains fully supported as the second choice.
- **Instruct prefix for qwen3 queries**: qwen3-embedding is instruction-aware,
  so queries are embedded with the official Instruct prefix while documents
  stay plain — matches the model card guidance and the benchmark protocol.
- **Wizard**: recommends `ollama pull qwen3-embedding:0.6b` (639 MB, 1024d,
  MRL, instruction-aware, 32k context) instead of bge-m3 (1.2 GB); bge-m3
  stays available as the second option. Config now records the concrete
  local model (`embedding_model`).

## [v0.16.0] - 2026-09-03

### Added

- **Temporal Fact Validity**: every memory now carries `valid_from` (defaults to
  `created_at`, overridable via the new `effective_from` parameter for
  retro-dated imports such as mail) and `valid_to` (`None` = still valid).
  Auto-supersession stamps `valid_to` on the superseded fact at supersession
  time — the full validity history is retained instead of being overwritten.
- **Point-in-time recall**: new optional `as_of` parameter on `recall`.
  With `as_of`, deprecated facts are returned when they were valid at that
  date, and the `valid_until` TTL is compared against the cutoff instead of
  "now". Omitting `as_of` keeps the previous behavior exactly unchanged.
- **`fact_history` MCP tool**: traces the supersession chain of a memory in
  both directions (successors and predecessors), ordered by `valid_from`,
  showing how a fact evolved over time.

## [v0.15.0] - 2026-09-03

### Added

- **Memory Dynamics**: Decay (5%/Monat linear, Floor 0.3), Salience-Boost (>= 0.8 immun), ln-Versetärkung via use/access-Counts — Scores bereinigt sich jetzt mit tatsächlicher Nutzung.
- `effective_score` / `normalize_salience` / `access_update_payload` in neuem Modul `src/nexus_memory/memory_dynamics.py`.
- Recall-Flugbahn: Top-Treffer verstärken sich (use_count), ungenutzte gedächtnisse verblassen langsam.

### Fixed

- Review Runde 1+2 (Verifier GREEN): B2 (source_url-Passthrough), M2 (Dynamik-Fenster auf Basis-Score statt effektivem Score — sonst sortiert Dynamik die semantische Rerank-Ordnung um), use/access_count getrennt incrementiert mit retrieve-before-write (Reset-Bug + Lost-Update gefixt).
- ln-Cap-Docstring Klarstellung (Verifier-Kosmetik).
- README: Memory-Dynamics-Sektion.

## [v0.13.2] - 2026-08-30

### Fixed

- **Prefetch slot-replacement race** (hermes-agent `memory_manager.py`) - when a
  turn began while the previous prefetch thread was still in flight, the old
  code replaced the `_external_prefetch_threads` mapping regardless, silently
  dropping that turn's memory recall. Fixed to only skip when the previous
  prefetch is genuinely still running. 71/71 memory-provider tests green.

### Changed

- **Prefetch capacity doubled** - Auto-Prefetch now fetches 10 (was 5) vector
  hits with a 2400-char (was 1200) budget. At 14k+ stored memories the old
  limits surfaced only 2-3 hits and let session-noise dominate context.
  Both still overridable via `NEXUS_PREFETCH_CHARS`.

### Added

- **Hardware auto-entity detection** - `sync_turn` now triggers entity extraction
  immediately when a user message contains declarative hardware statements
  ("Ich habe X", "Ich nutze Y") together with known hardware keywords
  (Bose, Razer, Mikrofon, USB, Bluetooth, ...). Stores with confidence 0.9
  instead of letting the fact sink as a confidence-0.5 session log entry.
  Question sentences are excluded. Failure is non-blocking.

## [v0.13.1] - 2026-08-30

### Added

- **OpenClaw update-check** - the OpenClaw plugin now checks
  `releases/latest` on GitHub at startup (24h cache, semver compare via
  `packaging.version`) and injects a once-per-lifetime nudge into the
  system prompt when a newer release exists - update-notification parity
  across all 3 install paths (Hermes plugin, MCP server, OpenClaw plugin).

## [v0.13.0] - 2026-08-31

### Added

- **Point-in-Time Queries** (roadmap 4.5) - `nexus_recall` accepts
  `as_of: "YYYY-MM-DD"`: only memories created on/before that date are
  returned ("what did we know on date X?"). Empty string (default) keeps
  all memories visible. Lexicographic date compare, timezone-agnostic.
- **Supersede reason** (roadmap 4.7) - auto-supersession now writes
  `supersede_reason` ("replaced by fact X (similarity 0.93 >= 0.90)")
  into the deprecated payload, making silent overwrites auditable.
- **Skill health monitor** (roadmap 4.10) - SICA flags category=skill
  memories unused for 180 days as review suggestions (never auto-delete;
  skills decay by lack of use, not by error).

### Not built (documented decision)

- **Tiered Loading L2** (roadmap 3.1 remainder) - intentionally not
  built: L0 (embed cache) + L1 (prefetch budget) already cover the real
  need; a summary layer has no consumer at the current memory size.
  Building it now would be overengineering. Revisit when the store
  grows past ~50k points.

## [v0.12.0] - 2026-08-30

### Added

- **Latency benchmark** (roadmap 3.3) - `scripts/bench_latency.py`:
  p50/p95/p99 over 30 real queries (embed + qdrant + filter + rerank +
  graph). Baseline: p50=485ms, p95=610ms. Breakdown: Voyage embed ~256ms
  (53%), Qdrant ~9ms (2%), rerank+graph ~220ms (45%). The <100ms target
  needs the cloud-roundtrip removal track, not query tuning. Honest
  report, not aspirational numbers.
- **EmbedCache L0** (roadmap 3.1) - new `nexus_memory/embed_cache.py`:
  thread-safe LRU (256) for query vectors. `_recall` and `_do_prefetch`
  reuse repeated queries - second hit skips the ~256ms Voyage roundtrip
  and its API cost. Semantic no-op. 5 tests.
- **Prefetch token budget** (roadmap 3.1b) - auto-injected memory context
  was up to ~3.5k chars; now capped by `NEXUS_PREFETCH_CHARS` (default
  1200) with item-boundary-aware trimming. ~65% context tokens saved per
  message. 1 test.
- **Data flywheel** (roadmap 4.9) - recall bumps `access_count` + sets
  `last_accessed` on the top-3 hits in a fire-and-forget daemon thread;
  results order never touched. SICA can use access_count as trust signal.
  1 e2e test.
- **Autonomous purge** (roadmap 4.8) - SICA `_detect_low_confidence` now
  auto-deletes memories that are conf<0.2 **and** never accessed **and**
  older than 30 days (3-of-3 rule; everything else stays review-only).
  `_apply_auto_patch` accepts the new delete type. Unit + e2e tests.



### Added

- **Superseded-by recall skip** (roadmap 4.6) - deprecated and
  rolled_back facts stay in Qdrant for audit but never surface in the
  Hermes plugin recall or auto-prefetch (mirrors the MCP server filter).
  Legacy points without lifecycle_status stay visible. Plugin upserts
  now tag new memories lifecycle_status: canonical. 3 new tests.
- **Auto entity enrichment on nexus_remember** (roadmaps 1.1/4.1) -
  every nexus_remember with 80+ chars queues a fail-open entity
  extraction pass in a daemon thread (single-flight): entities stored as
  Qdrant points, relationships as graph edges. No tool-call latency.
  Hash-dedup skips repeated texts, single-flight lock, NEXUS_AUTO_ENRICH
  opt-out, access_level propagation (private stays private). 6 new tests.
  Session-end extraction now shares the same code path (rel_map removed).

### Changed

- **Recall pipeline order (review)** - lifecycle filter now runs BEFORE
  reranking so deprecated points don't burn rerank-pool slots; graph-boost
  neighbors also skip deprecated facts.
- **Entity edge store reuse (review)** - one EdgeStore instance per
  extraction run instead of one per relationship; identity rel_map
  removed.

## [v0.10.0] - 2026-08-30

### Added

- **Cross-Encoder Reranking** (roadmap 1.2) - new `nexus_memory/reranker.py`;
  optional rerank step in the Hermes plugin `_recall` pipeline. Config-driven
  via `nexus-memory.rerank` in `~/.hermes/config.yaml` (reranker: `auto`
  default - adapts per user: Voyage API when a key is present, free local
  CrossEncoder otherwise - or explicit `voyage` / `cross-encoder`;
  `rerank_pool` pool size; env overrides NEXUS_RERANK / NEXUS_RERANKER).
  Disabled by default; fail-open (rerank errors return the original
  vector order). 15 new tests.
- **Reflect insights** (roadmap 2.1) - SICA synthesizes one
  deterministic insight per contradiction group (confidence-based winner
  + concrete resolution suggestion) instead of review-only suggestions,
  stored in `SICAResult.reflect_insights`. LLM-free.
- **Entity dedup detection** (roadmap 4.2) - SICA groups
  category=entity memories by (entity_type, casefold-normalized name);
  duplicates surface as `merge_review` suggestions (never auto-delete;
  keeper = oldest point) and generate a keeper-focused insight.
- **Per-category retention policies** (roadmap 2.2) - SICA `_detect_retention`
  replaces the temp-only scan: temp=1 day, session=7 days by default,
  override via SICA_RETENTION_<CATEGORY> env vars; unlisted categories and
  no SICA_DEFAULT_RETENTION_DAYS keep memories forever. Memory with
  missing/unparseable timestamps is never deleted. Legacy
  SICA_STALE_TEMP_DAYS keeps working (feeds the temp policy).
  `_apply_auto_patch` now deletes `retention_expired` issues. 16 new tests.

### Changed

- SICA issue type for expired memories is now `retention_expired` (was
  `stale_temp`); `_detect_stale_temp` stays as a backwards-compatible bridge.

## [v0.9.1] — 2026-07-27

### Fixed

- **Discovery content-dict handling** — `nexus/discovery/__init__.py` now handles `content` field stored as dict (not string) in Qdrant payloads, fixing the Auto-Discovery crash that affected 23% of points
- **SICA session storage dimension mismatch** — `_store_sica_session` now loads `.env` before creating EmbeddingProvider (was picking Ollama 768d instead of Voyage 1024d), accepts caller-provided embedder, and checks collection dimension before upsert
- **Hermes plugin passes embedder to SICA** — `_sica_run()` now passes `self._embedder` to `run_sica()` to guarantee dimension match

## [v0.9.0] — 2026-07-27

### Added

- **Graph-Boosted Auto-Recall** — all 3 plugins (Hermes, OpenClaw, Claude Code) now fetch 1-hop graph neighbors from the top 3 vector search results via `GraphTraversal.get_related()`. Graph-boosted entries are tagged `[graph:<relation>]` in context output.
- **SICA Self-Improvement Cycle** — `nexus/sica/` module implementing Detect → Reflect → Act → Learn loop
  - Detect: stale temp memories (configurable age threshold), low-confidence memories, contradictions via graph edges
  - Act: auto-patches non-destructive issues (stale temp deletion), all other issues become suggestions
  - Learn: stores SICA session as memory for future iterations
  - `nexus_sica_run` tool in Hermes plugin (12 tools total)
  - Harness-independent: any plugin can call `run_sica()` directly
- **Access-level filtering in graph-boost** — OpenClaw and Claude Code plugins check target point access_level before including graph neighbors
- **SkillGraph caching** — Hermes plugin caches SkillGraph instance across calls instead of per-call init+close
- **`SkillGraph.get_point()`** — public API replacing private `_scroll_point` access
- **`_load_env()`** in SICA — loads `.env` files before creating EmbeddingProvider to ensure correct provider detection
- 20 new tests (578 total)

### Fixed

- 64 code review issues across 7 review rounds (bugs, edge cases, security, resource leaks, type safety)
- Graph-boost `break` vs `continue` — suggestion cap no longer skips auto-fixable items
- `or 0.5` truthiness — confidence=0.0 no longer silently becomes 0.5
- `_scroll_all` offset check — `if offset is not None` instead of `if offset` (offset=0 is valid)
- Shutdown race condition — `_skill_graph_lock` acquired before closing SkillGraph
- ThreadPoolExecutor leak — futures cancelled on timeout
- SICA session storage dimension mismatch — embedder from caller prevents 768d vs 1024d error
- Naive datetime handling — timezone-naive timestamps treated as UTC
- Confidence type coercion — `float()` guard for string confidence values in Qdrant payloads
- Edges type guard — `isinstance(edges, list)` prevents TypeError on malformed payloads

## [v0.8.0] — 2026-07-25

### Added

- **Cost-Aware Routing** — tier-based embedding provider selection based on memory category
- **cost_router.py** — `CostAwareRouter` class with tier mapping, provider detection, routing decisions
- **Tier system**: premium (Voyage/OpenAI for facts, rules, entities), standard (Google/Jina for preferences, beliefs, procedures), economy (Ollama/sentence-transformers for sessions, temp)
- **Cost estimation**: per-provider cost per 1M tokens, `estimate_cost()` method
- **Routing stats + explain**: MCP tools `cost_routing_stats` and `cost_routing_explain`, Hermes plugin tools `nexus_cost_routing_stats` and `nexus_cost_routing_explain`
- **Auto-enable**: routing activates when 2+ providers are available, disabled with single provider
- **Graceful fallback**: if recommended tier has no provider, falls up (economy→standard→premium)
- **Config support**: `cost_aware_routing` flag in config.json, `NEXUS_EMBEDDING_PROVIDER` env var
- 34 new tests (558 total)

## [v0.7.0] — 2026-07-25

### Added

- **Knowledge Graph Layer** — entity extraction and typed relationships alongside Qdrant vectors
- **entity_extractor.py** — two-tier entity extraction (LLM preferred, heuristic pattern-based fallback)
- **Entity types**: device, service, person, location, organization, concept, software, protocol
- **11 new EdgeRelation types** for typed entity relationships: installed_at, connected_to, manages, runs_on, part_of, owns, located_at, depends_on_service, uses, provides, controls
- **graph/traversal.py** — multi-hop BFS traversal via NetworkX (deque-based, O(1) per pop), configurable depth, relation filter, entity_type filter
- **Graph queries**: traverse(), find_entities(), get_subgraph(), get_related(), stats()
- **KG tools for all 4 plugins**: MCP Server (4 tools), Hermes Plugin (4 tools), OpenClaw (TypeScript), Claude Code (CLI script)
- **Plugin integration**: on_session_end now extracts entities alongside facts, deterministic uuid5 entity IDs, relationships stored as graph edges via EdgeStore
- **Quick health check**: 1s TCP probe before LLM call, 10s API timeout
- 48 new tests (entity_extractor: 35, traversal: 13), 524 total

### Fixed

- None.strip() crash fix for LLM null values: `(e.get('name') or '').strip()`
- Deterministic entity IDs: uuid5 instead of uuid4 (no duplicates across sessions)
- Relationship storage: extracted relationships now stored as graph edges (was silently dropped)
- BFS performance: deque.popleft() instead of list.pop(0) (O(n) → O(1))
- OpenClaw KG tools adapted to SDK API by Miosha (TypeBox schemas, registerTool pattern)

## [v0.6.0] — 2026-07-25

### Added

- **Session→Memory Pipeline** — native fact extraction in on_session_end (no more raw text dumps)
- **extractor.py** — two-tier fact extraction: LLM (preferred, uses configured model) with heuristic pattern-based fallback (always works, no external dependencies)
- **Categorization**: fact, rule, preference, belief with confidence scores (0.0-1.0)
- **Inline execution**: runs in MemoryManager's background executor (no race condition with shutdown)
- **Race condition guard**: checks _write_stop before each Qdrant write
- **Stateless recovery**: each on_session_end probes LLM fresh, auto-recovers when endpoint comes back
- 24 new tests, 476 total

### Fixed

- JSON code block parsing: case-insensitive `json|JSON` tag matching
- Empty response.choices guard: no IndexError on empty API responses
- Correction patterns: fix `ne\d+` typo → `nee?` (German colloquial "nee")
- Fact patterns: remove overly broad `ist|is`, add word boundaries
- Remove password/token pattern (security: never extract secrets)

## [v0.5.1] — 2026-07-19

### Added

- **Auto-Supersession** — automatic deprecation of similar facts at similarity > 0.90
- `superseded_by` + `supersedes` tracking in payload
- Non-blocking: if check fails, fact is still stored
- Only for fact/rule/preference/procedure (not session/belief/temp)
- 452 tests

## [v0.5.0] — 2026-07-19

### Added

- **Active Guardrails** — memory-driven prevention of destructive actions
- guardrail_check + guardrail_override MCP tools
- Pattern matching for rm/drop/kill/recreate/find-delete/git-clean/dd
- Override with audit trail (min 10 chars reasoning, stored as private session memory)
- Fail-open: Qdrant outage degrades to ALLOW (never blocks agent work by accident)
- All 4 integration paths (MCP Server, Hermes Plugin, OpenClaw Plugin, Claude Code Plugin)
- 445 tests

## [v0.4.0] — 2026-06-19

### Added

- **OpenClaw native plugin** — Auto-Recall (memories injected before every turn) and Auto-Capture (facts extracted after every turn) powered by local Qdrant
- **`install_openclaw_plugin.sh`** — one-command install script that detects OpenClaw, configures `plugins.load.paths`, sets `plugins.slots.memory`, auto-detects embedding provider, and restarts the gateway
- **3-way architecture** — Hermes Plugin · OpenClaw Plugin · MCP Server, all sharing the same Qdrant collection
- **"Which path should I use?" table** in AGENTS.md and README.md
- **MCP Server → Core Engine integration** — SkillGraph initialization, Auto-Discovery after remember(), lifecycle status filtering in recall(), supersession in update(), Events on remember/update/forget
- **Time Decay in retrieval** — Gauss-shaped score decay (offset=30d, scale=365d) so recent memories rank higher, old ones fade gracefully
- **PROCEDURE memory category** — new `MemoryCategory.PROCEDURE` for workflow/procedural memory with step ordering
- **Staging with real embeddings** — replaced placeholder zero vectors with actual embedding provider calls (auto-detect Voyage → OpenAI → Google → Jina → Ollama → sentence-transformers)

### Changed

- README.md completely rewritten — 3-way architecture diagram, all 3 install paths, release history table, GitHub Sponsors badge
- Version badge updated to v0.4.0
- All version numbers synchronized (pyproject.toml, nexus.__version__, plugin.yaml, mcp_server.py)
- Staging `ensure_collections()` auto-detects vector dimension from embedding provider (was hardcoded 512d)
- AGENTS.md categories updated to include `procedure`

### Fixed

- **Staging placeholder vectors** — `[0.0] * 512` replaced with real embeddings via `_detect_vector_size()` and `_embed_content()`
- **Personal data removed** from public files (internal IPs, names, addresses)
- **Test suite** — updated for new category count and dimension detection

### Notes

- No breaking changes — same Qdrant collection, same API, same tools
- OpenClaw plugin uses Qdrant REST via `fetch()` (no Python dependency on the OpenClaw side)
- Lifecycle filtering is backwards compatible — entries without `lifecycle_status` field pass through
- Time decay only applies when timestamps are present — entries without timestamps are not penalized

---

## [v0.3.0] — 2026-06-18

### Added

- **Hermes native MemoryProvider plugin** — direct Qdrant access with zero MCP overhead
  - Auto-prefetch: relevant memories injected into context before every turn
  - Auto-sync: user + assistant turns saved as memories automatically
  - 3 manual tools: `nexus_recall`, `nexus_remember`, `nexus_forget`
  - Dimension-mismatch protection warns if embedding provider changed
- **`install_hermes_plugin.sh`** — one-command install: symlinks plugin, sets `memory.provider`, verifies
- **Embedding Provider Selection wizard** — `nexus-memory-init` interactive CLI
  - Scans system for all 6 providers
  - Shows quality ranking (excellent / good / basic)
  - Auto-selects best available as default
  - API key URL hints for cloud providers
- **Separate landing page server** + marketing assets (poster, references)

### Changed

- `pyproject.toml` version bumped to 0.3.0
- AGENTS.md restructured with Hermes Plugin and OpenClaw Plugin sections

### Notes

- Hermes plugin shares the same Qdrant collection with the MCP server
- No breaking changes to the MCP server API

---

## [v0.2.5] — 2026-06-13

### Fixed

- **`is_success()` helper** replaces raw `status_code == 200` across 29 sites in 10 files — Qdrant 201/204 responses no longer falsely treated as errors
  - `apply.py` (9 sites), `events.py` (7 sites), `staging.py` (3 sites), `nexus/__init__.py` (2 sites), `provenance/__init__.py` (3 sites), `cli.py` (1 site), `mcp_server.py` (1 site), `retrieval/__init__.py` (1 site), examples (2 sites)
- **5 bugs from Verifier audit** — Google async, try/except handlers, missing if-condition, deps, version drift

### Changed

- **TRUST_EPSILON consolidated** — same value (0.01) in `recompute_trust` + `recompute_all` (previously 0.01 vs 1e-9)
- **EVENT_TYPES derived from Enum** — single source of truth instead of duplicate
- **Deprecated `asyncio.get_event_loop()`** replaced with `get_running_loop()`
- **Re-embedding on hybrid fallback eliminated** — one API call instead of two
- **Unused imports removed** (json, Any, datetime, timezone, sys locales)
- **Unused constants removed** (STATUS_CONTESTED, RETRACTED, HISTORICAL, VALID_STATUSES)
- **`config.py` docstring corrected** — says "nexus" instead of "hermes-memory"

### Added

- **Audit GitHub Action** — automatic check on every push:
  - Collection-name check (finds `openclaw-memory`, `hermes-memory-1024d` etc.)
  - Status-code check (finds raw `== 200`)
  - Python compile check
  - pytest
- **SECURITY.md** — contact, supported versions, reporting process
- **Webhook subscriptions** — 3 new tools: `subscribe`, `unsubscribe`, `list_subscriptions`
  - Fire-and-forget HTTP POST to registered URLs on memory events
  - Persisted in `~/.nexus-webhooks.json` (no Qdrant, no SQLite, no new dependency)
  - Event types: `memory.remember`, `memory.update`, `memory.forget`
  - 27 new tests (379 total, all passing)

### Notes

- No breaking changes — same Qdrant collection, same API

---

## [v0.2.4] — 2026-06-12

### Added

- **Web UI** with live D3.js v7 force-directed graph
  - Interactive node graph of all memories
  - Clustering and category-mapping
  - Detail view on node click
  - Drift ampel (traffic light) for belief health
  - Stats cards with tooltips
  - Filter by category, full-text search
- **`nexus-memory webui` CLI command** — launches dashboard at `http://127.0.0.1:9121`
- **Ko-fi integration** in Web UI header and footer

### Fixed

- Graph.js crash on `d.full` → `fullText` property
- Safari reader mode prevention, marked as web app
- Cache-bust all assets (`?v=20260612`)
- Graph edges visibility (4px / 75% opacity, hover 5px / 100%)
- Node sizing (7 + 15×confidence), thicker edges, larger labels

### Changed

- WebUI refactored to graph-only landing page, removed marketing clutter
- `cli()` cleaned up after patch damage, proper argparse restored

---

## [v0.2.3] — 2026-06-08

### Added

- **`check_update` tool** — checks if a newer version is available on GitHub. Returns local vs latest version, release URL, and whether an update is available
- **`do_update` tool** — pulls latest version from GitHub, reinstalls via pip, and restarts the server. Requires `confirm: true` as safety guard
- **Self-restart** — after successful `do_update`, the server exits cleanly; the MCP client automatically reconnects with the new version

### Fixed

- **macOS setup.sh** — `grep -oP` → `-oE` compatibility fix
- **uv --system** flag added for venv creation

### Notes

- Agent workflow: `check_update` → ask user → `do_update(confirm: true)` → automatic reconnect
- Language-neutral — agent communicates in whatever language the user speaks

---

## [v0.2.2] — 2026-06-08

### Added

- **Justification Check (Rung 2)** — source URL verification on recall
  - `verification` field in recall results: `verified`, `unreachable`, or `unchecked`
  - `_check_sources()` async method — parallel HTTP HEAD checks on all source URLs
  - Payload enrichment — hybrid search results now include `source_url`, `access_level`, `category`, `source`, `created_at`, `provenance`

### Fixed

- **Score key** — `rrf_score` instead of `score` in HybridRetriever
- **Score normalization** — relative instead of fixed `/10`
- **Hybrid search embedding pass-through** + shim correction
- **HybridRetriever.search() shim** — resolves recall crash (`AttributeError`)
- **Default collection** → `nexus` (was: `hermes-memory`)
- **Voyage API key detection** — support both `pa-` and `vo-` prefix
- **Health check** — `model_name` property added to EmbeddingProvider
- **pyproject.toml** — `where=['src', '.']` finds both `nexus/` (root) and `nexus_memory/` (src/)
- **CLI sync** — `cli()` wrapper for async `main()` (entrypoint bug)

### Changed

- **Privacy** — author name `Nebojsa Kacavenda` → `Nebo` in all public files
- **Headline** — "One brain for all your agents" (pain-first positioning)

### Removed

- **Hardcoded `~/.hermes/.env` path** — replaced with generic MCP `env:` block, `NEXUS_ENV_FILE`, or `cwd/.env` fallback

---

## [v0.2.0] — 2026-06-07

### Added

- **MemoryCategory Enum** — 6 scopes: `fact`, `belief`, `session`, `rule`, `preference`, `temp`
- **Provenance tracking** — `source_url`, `confidence`, `attach_source()`
- **Guardrails** — content-length warnings (>5,000 chars), PII detection hints
- **Access Control** — `public` / `trusted` / `private` levels
- **Hybrid Search** — BM25 + Vector + Reciprocal Rank Fusion
- **Health monitoring** — Qdrant + embedding provider health checks
- **Drift detection** — scored 0–10 with healthy/attention/action thresholds
- **Auto-Discovery** — zero-token relation discovery between canonical facts
- **Graph Analytics** — hub scores, isolation scores, knowledge gaps, connected components
- **Skill Export** — `export_skill()` generates `SKILL.md` from canonical facts
- **`update` tool** — in-place metadata-preserving memory updates
- **5 MCP tools** — `remember`, `recall`, `forget`, `update`, `health`

### Changed

- Full v2.8.0 feature parity ported from `hermes-nexus-memory`
- 224 tests passing
- Single collection for all agents (no per-agent silos)

### Notes

- Backward-compatible with `hermes-nexus-memory` data
- All existing memories preserved in Qdrant

---

## [v0.1.0] — 2026-06-07

### Added

- **Initial release** — Universal Memory Layer for AI Agents
- **MCP Server** with 4 tools: `remember`, `recall`, `forget`, `health`
- **Access control** — `public` / `trusted` / `private` levels
- **Qdrant-backed vector storage** (1024d, voyage-3-large)
- **Automatic `.env` loading** — `~/.hermes/.env` [deprecated since v0.2.1] and `./.env`
- **Security** — local-only server, no cloud dependencies
- **Single collection** for all agents (no per-agent silos)

### Known Limitations

- No hybrid search yet (BM25 planned)
- No encryption at rest
- No Web UI
- Qdrant must be running separately

---

[v0.4.0]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.4.0
[v0.3.0]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.3.0
[v0.2.5]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.2.5
[v0.2.4]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.2.4
[v0.2.3]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.2.3
[v0.2.2]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.2.2
[v0.2.0]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.2.0
[v0.1.0]: https://github.com/Neboy72/nexus-memory/releases/tag/v0.1.0