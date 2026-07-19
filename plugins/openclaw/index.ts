import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import { Embedder } from "./lib/embedder.ts"
import { QdrantClient } from "./lib/qdrant-client.ts"
import { nexusConfigSchema, parseConfig } from "./lib/config.ts"
import { buildCaptureHandler } from "./hooks/capture.ts"
import { buildRecallHandler } from "./hooks/recall.ts"
import { initLogger, log } from "./logger.ts"
import { buildMemoryRuntime, buildPromptSection } from "./runtime.ts"
import { registerForgetTool } from "./tools/forget.ts"
import { registerSearchTool } from "./tools/search.ts"
import { registerStoreTool } from "./tools/store.ts"
import { registerGuardrailCheckTool, registerGuardrailOverrideTool } from "./tools/guardrail_check.ts"

const PLUGIN_VERSION = "0.5.0"

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
      return
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

    // Register memory capability
    const memoryRuntime = buildMemoryRuntime(qdrantClient)
    const noopFlushPlan = () => null

    if (typeof api.registerMemoryCapability === "function") {
      api.registerMemoryCapability({
        runtime: memoryRuntime,
        promptBuilder: buildPromptSection,
        flushPlanResolver: noopFlushPlan,
      })
    } else {
      api.registerMemoryRuntime?.(memoryRuntime)
      api.registerMemoryPromptSection?.(buildPromptSection)
      api.registerMemoryFlushPlan?.(noopFlushPlan)
    }

    // Register tools
    registerSearchTool(api, embedder, qdrantClient, cfg)
    registerStoreTool(api, embedder, qdrantClient, cfg)
    registerForgetTool(api, embedder, qdrantClient, cfg)
    registerGuardrailCheckTool(api, qdrantClient, cfg)
    registerGuardrailOverrideTool(api, qdrantClient, cfg, embedder)

    // Register hooks
    if (cfg.autoRecall) {
      api.on("before_prompt_build", buildRecallHandler(embedder, qdrantClient, cfg))
    }

    if (cfg.autoCapture) {
      api.on("agent_end", buildCaptureHandler(embedder, qdrantClient, cfg))
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