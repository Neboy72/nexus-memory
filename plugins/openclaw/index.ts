import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import { Embedder } from "./lib/embedder.ts"
import { QdrantClient } from "./lib/qdrant-client.ts"
import { nexusConfigSchema, parseConfig } from "./lib/config.ts"
import { ScopeCentroidCache } from "./lib/scope-auto.ts"
import { buildCaptureHandler } from "./hooks/capture.ts"
import { buildRecallHandler } from "./hooks/recall.ts"
import { buildPreToolGateHandler } from "./hooks/pre-tool-gate.ts"
import { buildThoughtFilterHandler } from "./hooks/thought-filter.ts"
import { buildCronFormGateHandler } from "./hooks/cron-form-gate.ts"
import { initLogger, log } from "./logger.ts"
import {
  buildMemoryRuntime,
  buildPromptSection,
  consumeUpdateNudge,
  NEXUS_SEARCH_TOOL,
  NEXUS_STORE_TOOL,
  setUpdateCheckResult,
} from "./runtime.ts"
import { checkForUpdate } from "./lib/update-check.ts"
import { registerForgetTool } from "./tools/forget.ts"
import { registerSearchTool } from "./tools/search.ts"
import { registerStoreTool } from "./tools/store.ts"
import { registerGuardrailCheckTool, registerGuardrailOverrideTool } from "./tools/guardrail_check.ts"
import {
  registerGraphTraverseTool,
  registerFindEntitiesTool,
  registerGetSubgraphTool,
  registerGetRelatedTool,
} from "./tools/graph_traverse.ts"

export default {
  id: "nexus-memory",
  name: "Nexus Memory",
  description: "OpenClaw memory plugin powered by Qdrant — Auto-Recall + Auto-Capture",
  kind: "memory" as const,
  configSchema: nexusConfigSchema,

  register(api: OpenClawPluginApi) {
    const cfg = parseConfig(api.pluginConfig)

    initLogger(api.logger, cfg.debug)

    // Initialize embedder — throws if no provider is configured
    let embedder: Embedder
    try {
      embedder = new Embedder(
        cfg.embedding.provider,
        cfg.embedding.model,
        cfg.embedding.apiKey,
        cfg.embedding.baseUrl,
        cfg.embedding.dimensions,
      )
    } catch (err) {
      api.logger.error(
        `nexus: embedding init failed — ${err instanceof Error ? err.message : String(err)}`,
      )
      // Re-throw: without an embedder this plugin cannot function, so the
      // host must treat the registration as FAILED instead of loading a
      // half-dead memory capability.
      throw err
    }

    const dimensions = embedder.getDimensions()

    // Initialize Qdrant client
    const qdrantClient = new QdrantClient(
      cfg.qdrantUrl,
      cfg.collection,
      dimensions,
    )

    // Ensure collection exists (async, non-blocking — will retry on first search/upsert)
    qdrantClient.ensureCollection(dimensions).catch((err) => {
      log.error("failed to ensure Qdrant collection on startup", err)
      api.logger.warn(
        `nexus: Qdrant collection not ready — will be created on first write. Make sure Qdrant is running at ${cfg.qdrantUrl}`,
      )
    })

    // Roadmap v0.13.1: fire-and-forget update check (fail-open, 24h cache)
    // OCR-5 (maintainability low): the catch is contractually dead
    // (checkForUpdate must never throw — lib/update-check.ts), but if it
    // EVER fires the error was silently discarded. Log it so a real defect
    // in the fail-open contract becomes visible; shape matches fail-open.
    checkForUpdate()
      .then((result) => setUpdateCheckResult(result))
      .catch((err) => {
        log.error("checkForUpdate rejected (contract says never-throw — investigating)", err)
        setUpdateCheckResult({ available: false, latest: "", url: "" })
      })

    // Register memory capability
    // H122: the runtime probes delegate to the real embedder/client, so both
    // must be handed over (the probes used to be hardcoded stubs).
    const memoryRuntime = buildMemoryRuntime(qdrantClient, embedder)
    const noopFlushPlan = () => null

    // H138: buildPromptSection is pure. The caller owns the once-per-process
    // update nudge here so prompt building is deterministic.
    // OCR-4: consume the nudge ONLY when the prompt section will actually
    // carry tools — when neither nexus_search nor nexus_store is available,
    // buildPromptSection returns [] and the consumed nudge would be thrown
    // away forever (no reset), so no session would ever see the update hint.
    const promptBuilder = (params: { availableTools: Set<string> }) => {
      const hasSearch = params.availableTools.has(NEXUS_SEARCH_TOOL)
      const hasStore = params.availableTools.has(NEXUS_STORE_TOOL)
      // OCR-5 (bug low): the flag name hid the semantics — `nudged: text ===
      // null` was true EXACTLY when nothing fresh was consumed (no update OR
      // already shown). Explicit named flag + comment so a future change of
      // consumeUpdateNudge() semantics cannot silently invert this.
      const nudge = hasSearch || hasStore ? consumeUpdateNudge() : { text: null, lines: [] }
      const nudgeConsumed = nudge.text !== null // fresh nudge handed out THIS build
      // OCR-6 (L953): hand the SAME derivation to buildPromptSection instead
      // of letting it re-derive from updateInfo (single source of truth).
      return buildPromptSection({
        availableTools: params.availableTools,
        nudged: !nudgeConsumed,
        nudgeLines: nudge.lines,
      })
    }

    let memoryCapabilityRegistered = false
    if (typeof api.registerMemoryCapability === "function") {
      api.registerMemoryCapability({
        runtime: memoryRuntime,
        promptBuilder,
        flushPlanResolver: noopFlushPlan,
      })
      memoryCapabilityRegistered = true
    }
    if (!memoryCapabilityRegistered) {
      // Native capability API missing: fail
      // loudly instead of silently loading a memory-less plugin.
      api.logger.error("nexus: memory capability could not be registered")
      throw new Error("nexus: memory capability could not be registered")
    }

    // Self-organizing memory (Nebo law 07.09: full automation): one shared
    // centroid cache feeds auto-recall gating + auto-capture tagging.
    const centroidCache = new ScopeCentroidCache(cfg.qdrantUrl, cfg.collection)

    // Register tools
    registerSearchTool(api, embedder, qdrantClient, cfg)
    registerStoreTool(api, embedder, qdrantClient, cfg, NEXUS_STORE_TOOL, centroidCache)
    registerForgetTool(api, embedder, qdrantClient, cfg)
    registerGuardrailCheckTool(api, qdrantClient, cfg)
    registerGuardrailOverrideTool(api, qdrantClient, cfg, embedder)

    // Register Knowledge Graph tools (v0.7.0)
    registerGraphTraverseTool(api, qdrantClient, cfg)
    registerFindEntitiesTool(api, qdrantClient, cfg)
    registerGetSubgraphTool(api, qdrantClient, cfg)
    registerGetRelatedTool(api, qdrantClient, cfg)

    // Register hooks
    if (cfg.autoRecall) {
      api.on("before_prompt_build", buildRecallHandler(embedder, qdrantClient, cfg, centroidCache))
    }

    // Pre-Tool Gate: forces Nexus recall + plan before non-trivial actions
    api.on("before_tool_call", buildPreToolGateHandler(embedder, qdrantClient, cfg))

    // Thought-Filter (29.08.2026): GLM-5.x emittiert CoT als plain text
    // (GitHub #42062) — filtert Reasoning-Blöcke vor dem Senden.
    // Standard: an. Ausschaltbar via thoughtFilter: false in Plugin-Config.
    // OCR-4 (korrigiert): der Filter läuft VOR dem Gate. Begründung: die
    // message_sending-Merge-Semantik ist "last returned content wins" — ein
    // späterer Handler ERSETZT das bisherige Ergebnis. Läuft das Gate zuerst,
    // ersetzt die Filter-Rückgabe ({ message: undefined } beim Drop) das
    // Gate-Urteil { cancel: true } vollständig = fail-open. In dieser
    // Reihenfolge (Filter erst, Gate zuletzt) ist jeder Fall sicher:
    // Filter-Drop → Gate { cancel: false } ohne message-Key → Original-Text
    // geht raus (kein Verlust); Gate-Block → cancel:true bleibt letzte
    // Rückgabe. (Der ursprüngliche OCR-4-Tausch-Entwurf wurde durch die
    // Kette-Kette-Beweise /tmp/chain-probe2.mjs widerlegt und zurückgebaut.)
    if (cfg.thoughtFilter !== false) {
      api.on("message_sending", buildThoughtFilterHandler())
      log.info("thought-filter: message_sending hook aktiv")
    }

    // Cron-Form-Gate (Astra-R6 P0, 08.09.2026): unbeaufsichtigte Sends
    // nur als festes Formular, fail-closed.
    // H136: eigener, unbedingter Block — hing früher am thoughtFilter-if und
    // war damit über `thoughtFilter: false` abschaltbar. Ein Fail-closed-Gate
    // für unbeaufsichtigte Sends darf nicht an einem Reasoning-Filter hängen.
    api.on("message_sending", buildCronFormGateHandler())
    log.info("cron-form-gate: message_sending hook aktiv")

    if (cfg.autoCapture) {
      api.on("agent_end", buildCaptureHandler(embedder, qdrantClient, cfg, centroidCache))
    }

    // Register service
    api.registerService({
      id: "nexus-memory",
      start: () => {
        api.logger.info(
          `nexus: connected (provider=${embedder.getProvider()}, dims=${dimensions}, qdrant=${cfg.qdrantUrl}, collection=${cfg.collection})`,
        )
      },
      stop: () => {
        api.logger.info("nexus: stopped")
      },
    })
  },
}