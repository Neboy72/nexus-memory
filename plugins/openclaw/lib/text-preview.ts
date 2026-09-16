/**
 * Shared text-preview helper (W25 Nr 493).
 *
 * forget.ts and store.ts each had an inline preview with DIFFERENT
 * thresholds (100 vs 80). Extracted so the mechanics cannot drift; the
 * threshold stays a parameter because the two tools intentionally keep
 * their own limits.
 */

/**
 * Default preview length. Matches the literal forget.ts passes (100); both
 * tools intentionally keep their own limit (store.ts passes 80).
 */
export const PREVIEW_MAX = 100
/** Suffix appended when the text was truncated. */
export const ELLIPSIS = "…"

/**
 * Truncate text to `max` code points with an ellipsis when longer.
 *
 * W40-8: slicing by UTF-16 code units could split a surrogate pair
 * (e.g. an emoji) and return a replacement char in the preview, so the limit
 * counts code points via Array.from(). `max` is normalized to a non-negative
 * integer first — a negative/fractional/NaN budget would otherwise slice from
 * the end or produce a partial budget.
 */
export function limitText(text: string, max: number = PREVIEW_MAX): string {
  const limit = Number.isFinite(max) ? Math.max(0, Math.trunc(max)) : PREVIEW_MAX
  // W40-scan: a short-enough string can never be truncated — skip the
  // code-point materialization entirely (UTF-16 length <= code-point count,
  // so this fast path is always safe).
  if (text.length <= limit) return text
  const chars = Array.from(text)
  return chars.length > limit ? `${chars.slice(0, limit).join("")}${ELLIPSIS}` : text
}
