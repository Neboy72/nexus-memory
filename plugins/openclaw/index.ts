import type { OpenClawPluginApi } from "openclaw/plugin-sdk"
import { Embedder } from "./lib/embedder.ts"
import { QdrantClient } from "./lib/qdrant-client.ts"
import { nexusConfigSchema, parseConfig } from "./lib/config.ts"
import { ScopeCentroidCache } from "./lib/scope-auto.ts"
import { buildCaptureHandler } from "./hooks/capture.ts"
import { buildRecallHandler } from "./hooks/recall.ts"
import { buildPreToolGateHandler, resolvePlanLockPath } from "./hooks/pre-tool-gate.ts"
import { buildThoughtFilterHandler } from "./hooks/thought-filter.ts"
import { buildCronFormGateHandler } from "./hooks/cron-form-gate.ts"
import { buildSelfCheckWarningHandler } from "./hooks/self-check-warning.ts"
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
import { writeSelfCheck } from "./lib/self-check.ts"
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

/**
 * One-line repair hint shown to the agent when the Qdrant backend is
 * unreachable. Deliberately a hint and not a command: the plugin ships as a
 * source path, and there is no published npm package to reinstall from.
 */
const SELF_CHECK_FIX =
  "start Qdrant or point qdrantUrl at your instance in the OpenClaw plugin config"
/** Same, when the cause is embedding configuration rather than a server outage. */
const SELF_CHECK_FIX_CONFIG =
  "check embedding.provider / embedding.apiKey in the OpenClaw plugin config and set the matching API key env var"

/**
 * Self-report probe budget (30.09.2026): a single hard 5s attempt used to turn
 * a momentary hiccup at gateway start (build/load spike, cold IPv4/IPv6
 * resolution) into a false "NOT WORKING" alert. One attempt plus exactly one
 * retry over a 15s per-attempt deadline rides out such a hiccup while a real
 * outage still fails both attempts and warns.
 */
export const SELF_CHECK_PROBE_TIMEOUT_MS = 15000
/** Attempts per probe: the initial attempt plus exactly one retry. */
export const SELF_CHECK_PROBE_ATTEMPTS = 2
/** Pause before the retry (ms) so both attempts do not hit the same load spike. */
export const SELF_CHECK_PROBE_PAUSE_MS = 750

/** Outcome of a single probe fetch; `timeout` marks a deadline/abort failure. */
type ProbeAttempt =
  | { ok: true }
  | { ok: false; kind: "http"; detail: string }
  | { ok: false; kind: "unreachable"; msg: string; timeout: boolean }

/** One probe attempt. Never throws — every fetch failure becomes a result. */
async function probeQdrantAttempt(url: string, fetchFn: typeof fetch): Promise<ProbeAttempt> {
  try {
    const res = await fetchFn(url, { signal: AbortSignal.timeout(SELF_CHECK_PROBE_TIMEOUT_MS) })
    if (!res.ok) {
      // The server ANSWERED with a failing status — that is an error, not a
      // network/deadline problem, so it is reported as such (after the retry).
      return { ok: false, kind: "http", detail: `Qdrant at ${url} responded HTTP ${res.status}` }
    }
    return { ok: true }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    const timeout = err instanceof Error && (err.name === "TimeoutError" || err.name === "AbortError")
    return { ok: false, kind: "unreachable", msg, timeout }
  }
}

/**
 * Reachability probe for the self-report: `ok:false` only when ALL attempts
 * fail, so a single deadline overrun no longer raises a false alarm. `reason`
 * always names the URL and the error; a final deadline failure also names the
 * attempt count. `fetchFn` is injectable for tests and defaults to global
 * fetch, so production behaviour is unchanged. Never throws.
 */
export async function probeQdrant(
  qdrantUrl: string,
  fetchFn: typeof fetch = fetch,
): Promise<{ ok: boolean; reason: string }> {
  const url = `${qdrantUrl}/collections`
  let last: ProbeAttempt = { ok: true }
  for (let attempt = 1; attempt <= SELF_CHECK_PROBE_ATTEMPTS; attempt++) {
    last = await probeQdrantAttempt(url, fetchFn)
    if (last.ok) return { ok: true, reason: "" }
    if (attempt < SELF_CHECK_PROBE_ATTEMPTS) {
      await new Promise((resolve) => setTimeout(resolve, SELF_CHECK_PROBE_PAUSE_MS))
    }
  }
  if (last.ok) return { ok: true, reason: "" }
  if (last.kind === "http") return { ok: false, reason: last.detail }
  const suffix = last.timeout ? `; ${SELF_CHECK_PROBE_ATTEMPTS} attempts` : ""
  return { ok: false, reason: `Qdrant at ${url} is unreachable (${last.msg}${suffix})` }
}

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
      const embedderError = err instanceof Error ? err.message : String(err)
      api.logger.error(`nexus: embedding init failed — ${embedderError}`)
      // Self-report: a broken embedder is exactly the silent death this
      // feature exists for — publish it (which also feeds the in-prompt
      // warning) BEFORE the registration fails, so the watchdog + agent know.
      writeSelfCheck(false, `embedding init failed: ${embedderError}`, SELF_CHECK_FIX_CONFIG)
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

    // Self-report (parity with the Hermes plugin + server watchdog): publish
    // provider health so a broken memory is alertable AND surfaces in the
    // prompt. Fire-and-forget (the per-attempt deadline + retry bound it);
    // writeSelfCheck is fail-open and never throws.
    probeQdrant(cfg.qdrantUrl)
      .then(({ ok, reason }) => writeSelfCheck(ok, ok ? "" : reason, SELF_CHECK_FIX))
      .catch((err) => {
        writeSelfCheck(false, err instanceof Error ? err.message : String(err), SELF_CHECK_FIX)
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

    // Pre-Tool Gate: Ebene 1 (Guardrails) ist UNBEDINGT — deshalb bleibt diese
    // Registrierung unbedingt. Ebene 3 (Plan-Zwang) ist über planGate.enabled
    // schaltbar (Default aus) und wird im Handler geprüft.
    api.on("before_tool_call", buildPreToolGateHandler(embedder, qdrantClient, cfg))
    log.info(
      `pre-tool-gate: before_tool_call hook aktiv (guardrails immer; plan-gate ${
        cfg.planGate.enabled ? `enabled, lock=${resolvePlanLockPath(cfg.planGate)}, ${cfg.planGate.maxAgeSeconds}s` : "aus"
      })`,
    )

    // Thought-Filter (29.08.2026): GLM-5.x emittiert CoT als plain text
    // (GitHub #42062) — filtert Reasoning-Blöcke vor dem Senden.
    // Standard: an. Ausschaltbar via thoughtFilter: false in Plugin-Config.
    // KONTRAKT-FIX 30.09.: der Filter lieferte {message: …} — der Host liest
    // aber AUSSCHLIESSLICH `content` (Doku docs/plugins/hooks/messages.md,
    // Typ PluginHookMessageSendingResult, Empirie gegen das Host-Bundle) und
    // ignorierte damit sowohl die Bereinigung als auch den Drop. Jetzt:
    // {content: bereinigt} bzw. {cancel: true} bei reinem Reasoning.
    // Reihenfolge bleibt Filter VOR Gate — jeder Handler sieht das ORIGINAL
    // und der letzte definierte content gewinnt; das Gate liefert nur cancel
    // und überschreibt deshalb nie einen bereinigten Text.
    if (cfg.thoughtFilter !== false) {
      api.on("message_sending", buildThoughtFilterHandler())
      log.info("thought-filter: message_sending hook aktiv")
    }

    // Cron-Form-Gate (Astra-R6 P0, 08.09.2026): unbeaufsichtigte Sends
    // nur als festes Formular, fail-closed.
    // H136: eigener, unbedingter Block — hing früher am thoughtFilter-if und
    // war damit über `thoughtFilter: false` abschaltbar. Ein Fail-closed-Gate
    // für unbeaufsichtigte Sends darf nicht an einem Reasoning-Filter hängen.
    // 27.09.2026 (universal): Titel/Limits/Schalter kommen aus cfg.cronFormGate
    // (Default AUS; enabled + leere Titel-Liste = AUS + Warn-Log).
    api.on("message_sending", buildCronFormGateHandler(cfg.cronFormGate))
    log.info(
      `cron-form-gate: message_sending hook aktiv (${
        cfg.cronFormGate.enabled && cfg.cronFormGate.titles.length > 0
          ? `enabled, ${cfg.cronFormGate.titles.length} Titel, max ${cfg.cronFormGate.maxLines} Zeilen/${cfg.cronFormGate.maxChars} Zeichen`
          : "aus (nicht konfiguriert)"
      })`,
    )

    // Self-Check-Chat-Warnung (30.09.2026): hängt bei kaputtem Memory EINMAL
    // pro Session einen kurzen, user-visible Warnblock an Outgoing-Nachrichten
    // an — der Prompt-Warntext allein erreicht den Operator nicht sicher.
    // Registrierung ALS LETZTER message_sending-Handler (bewusste Entscheidung,
    // Full-Proof im Dateikopf von hooks/self-check-warning.ts): die Merge-
    // Semantik ist lastDefined auf content + cancel-blockt alle weiteren
    // Handler. Nur wenn dieser Hook ZULETZT läuft, kann sein Anhang nicht
    // (a) ein fremdes bereinigtes content überschreiben und nicht (b) eine
    // Suppression "wiederbeleben" — ein später registrierter Content-Lieferant
    // wäre umgekehrt der Herr über den Text (Doku: last returned content wins).
    api.on("message_sending", buildSelfCheckWarningHandler())
    log.info("self-check-warning: message_sending hook aktiv (max 1 Warnung pro Session)")

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