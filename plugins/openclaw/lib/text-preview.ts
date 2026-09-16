/**
 * Shared text-preview helper (W25 Nr 493).
 *
 * forget.ts and store.ts each had an inline preview with DIFFERENT
 * thresholds (100 vs 80). Extracted so the mechanics cannot drift; the
 * threshold stays a parameter because the two tools intentionally keep
 * their own limits.
 */

/**
 * Default preview length, wired into forget.ts (store.ts intentionally
 * passes 80). OCR-4: previously this constant had no caller, which made the
 * "cannot drift" claim of this module false — forget.ts passed a bare 100
 * literal. forget.ts now imports this constant.
 */
export const PREVIEW_MAX = 100
/** Suffix appended when the text was truncated. */
export const ELLIPSIS = "…"

/**
 * Truncate text to `max` grapheme clusters with an ellipsis when longer.
 *
 * W40-8: slicing by UTF-16 code units could split a surrogate pair
 * (e.g. an emoji) and return a replacement char in the preview, so the limit
 * counts via Array.from() (code points). OCR-4: code points are still too
 * fine — a ZWJ family (👨‍👩‍👧‍👦 is 7 code points) or a flag (2) got cut
 * mid-glyph. The limit now counts GRAPHEME CLUSTERS via Intl.Segmenter
 * (Node 16+, zero deps), so composed emoji stay intact.
 * `max` is normalized to a non-negative integer first — a negative/
 * fractional/NaN budget would otherwise slice from the end or produce a
 * partial budget.
 */
export function limitText(text: string, max: number = PREVIEW_MAX): string {
  const limit = Number.isFinite(max) ? Math.max(0, Math.trunc(max)) : PREVIEW_MAX
  // W40-scan: a short-enough string can never be truncated — skip the
  // grapheme materialization entirely (UTF-16 length <= cluster count,
  // so this fast path is always safe).
  if (text.length <= limit) return text
  const segmenter = new Intl.Segmenter("und", { granularity: "grapheme" })
  const chars = [...segmenter.segment(text)].map((s) => s.segment)
  return chars.length > limit ? `${chars.slice(0, limit).join("")}${ELLIPSIS}` : text
}
