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

// OCR-5 (bug low + perf medium): the Segmenter was constructed on every
// limitText call AND used unguarded — a runtime without Intl.Segmenter
// threw TypeError instead of degrading to code-point splitting. Memoize
// the instance; return null when unsupported (limitText falls back).
let segmenterInstance: Intl.Segmenter | null | undefined
function ensureSegmenter(): Intl.Segmenter | null {
  if (segmenterInstance !== undefined) return segmenterInstance
  try {
    segmenterInstance = new Intl.Segmenter("und", { granularity: "grapheme" })
  } catch {
    segmenterInstance = null
  }
  return segmenterInstance
}

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
  // OCR-5 (bug low): positive Infinity meant "no limit" for callers but
  // silently fell back to PREVIEW_MAX (100), truncating text the caller
  // expected to keep in full. Infinity now means "no truncation" (NaN still
  // documented → falls back to PREVIEW_MAX).
  if (max === Infinity) return text
  const limit = Number.isFinite(max) ? Math.max(0, Math.trunc(max)) : PREVIEW_MAX
  // W40-scan: a short-enough string can never be truncated — skip the
  // grapheme materialization entirely (UTF-16 length <= cluster count,
  // so this fast path is always safe).
  if (text.length <= limit) return text
  // OCR-5 (bug low + performance medium): Intl.Segmenter was constructed on
  // EVERY call (perf) and used UNGUARDED (a runtime without it would throw
  // TypeError instead of degrading). Module-level memoized instance plus a
  // code-point fallback: a preview helper must never hard-fail recall.
  const segmenter = ensureSegmenter()
  const chars = segmenter
    ? [...segmenter.segment(text)].map((s) => s.segment)
    : [...text]
  return chars.length > limit ? `${chars.slice(0, limit).join("")}${ELLIPSIS}` : text
}
