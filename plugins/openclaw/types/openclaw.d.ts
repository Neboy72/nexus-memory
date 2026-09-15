declare module "openclaw/plugin-sdk" {
  /**
   * Plugin hook event name. The `(string & {})` member is the documented
   * escape hatch: it keeps autocomplete for the known names while still
   * accepting forward-compatible event strings the SDK may add later.
   */
  export type PluginHookEvent =
    | "before_prompt_build"
    | "before_tool_call"
    | "message_sending"
    | "agent_end"
    | (string & {})

  /** Tool definition with in-object execute() (openclaw >= 2026.5). */
  export interface ToolDefinition {
    name: string
    label?: string
    description?: string
    parameters: object
    // Method syntax (not a function-type property): parameters are then
    // checked bivariantly, so a tool that declares its own typed `params`
    // object stays assignable without falling back to `any`.
    execute(id: string, params: unknown): unknown
  }

  /**
   * Hook handler with `unknown` args.
   *
   * The `bivarianceHack` indexing is deliberate: a plain
   * `(...args: unknown[]) => void` is checked contravariantly under
   * `strictFunctionTypes`, so the existing handlers in index.ts (which take
   * typed event objects) would stop being assignable — exactly the situation
   * `any` used to paper over. Indexing a method type keeps the parameters
   * bivariant, so `unknown` replaces `any` without breaking callers.
   */
  export type PluginHookHandler = {
    bivarianceHack(...args: unknown[]): unknown
  }["bivarianceHack"]

  export interface OpenClawPluginApi {
    pluginConfig: unknown
    logger: {
      info: (msg: string, ...args: unknown[]) => void
      warn: (msg: string, ...args: unknown[]) => void
      error: (msg: string, ...args: unknown[]) => void
      debug?: (msg: string) => void
    }
    registerTool(tool: ToolDefinition, options?: unknown): void
    registerCommand(command: unknown): void
    registerCli(handler: unknown, options?: unknown): void
    registerService(service: unknown): void
    on(event: PluginHookEvent, handler: PluginHookHandler): void
    /** Unified memory capability registration (preferred since openclaw 2026.4.7). */
    registerMemoryCapability?(capability: unknown): void
    /** @deprecated Use registerMemoryCapability({ runtime }) instead. */
    registerMemoryRuntime?(runtime: unknown): void
    /** @deprecated Use registerMemoryCapability({ promptBuilder }) instead. */
    registerMemoryPromptSection?(builder: unknown): void
    /** @deprecated Use registerMemoryCapability({ flushPlanResolver }) instead. */
    registerMemoryFlushPlan?(resolver: unknown): void
  }
}
