import { log } from "../logger.ts"

const INTERACTIVE_TRIGGERS = new Set(["user", "manual"])

export function isInteractiveTrigger(trigger: string | undefined): boolean {
  // Keep this allowlist in sync with OpenClaw's agent trigger values. Unknown
  // non-empty values are logged at debug level (once per call), so allowlist
  // drift is detectable — the fail-closed decision itself is unchanged.
  // "user" and "manual" are interactive; automated triggers such as
  // "heartbeat", "cron", "memory", and "overflow" should skip memory hooks.
  // Undefined preserves legacy behavior for OpenClaw versions that do not
  // provide ctx.trigger yet. "" is NOT a valid trigger and must not be treated
  // as interactive (previously `!trigger` conflated undefined with "").
  if (
    trigger !== undefined &&
    trigger !== "" &&
    !INTERACTIVE_TRIGGERS.has(trigger)
  ) {
    log.debug(
      `trigger: unknown trigger value '${trigger}' treated as non-interactive (allowlist drift?)`,
    )
  }
  return trigger === undefined || INTERACTIVE_TRIGGERS.has(trigger)
}
