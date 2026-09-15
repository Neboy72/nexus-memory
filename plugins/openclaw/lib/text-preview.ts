/**
 * Shared text-preview helper (W25 Nr 493).
 *
 * forget.ts and store.ts each had an inline preview with DIFFERENT
 * thresholds (100 vs 80). Extracted so the mechanics cannot drift; the
 * threshold stays a parameter because the two tools intentionally keep
 * their own limits.
 */

/** Default preview length (forget.ts); store.ts passes its own (80). */
export const PREVIEW_MAX = 100
/** Suffix appended when the text was truncated. */
export const ELLIPSIS = "…"

/** Truncate text to `max` chars with an ellipsis when longer. */
export function limitText(text: string, max: number = PREVIEW_MAX): string {
  return text.length > max ? `${text.slice(0, max)}${ELLIPSIS}` : text
}
