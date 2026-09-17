/**
 * Prompt-safety helpers for the <nexus-context> wrapper.
 *
 * Auto-recall wraps injected memories in a <nexus-context> … </nexus-context>
 * block. Two hazards follow from that:
 *
 *  1. Inbound prompts may already carry a previously injected block (or a
 *     fragment of one) that must be stripped before re-use as a query.
 *  2. A captured/embedded memory whose TEXT contains a literal
 *     "</nexus-context>" could prematurely close the wrapper on the next
 *     recall and smuggle the rest of the block out as prompt text.
 *
 * Both are handled here so recall.ts and capture.ts share one implementation.
 */

/**
 * Remove every CLOSING "</nexus-context>" variant (case-insensitive,
 * self-closing, whitespace and attribute variants included) from stored text.
 * OPENING "<nexus-context …>" tags are deliberately NOT removed here — the
 * block stripper (stripNexusContextBlock) owns those; callers must run both
 * when both directions are needed.
 * OCR-6 (documentation medium, Z686): the old JSDoc claimed opening tags are
 * removed too, which the implementation does not do — readers could wrongly
 * assume capture/recall are already protected against opening tags and skip
 * the block stripper.
 *
 * `\\s*` and `/` before `>`: variants such as `</nexus-context >`,
 * `</nexus-context\\t>` or `</nexus-context/>` close the wrapper just as well
 * in HTML and must not survive neutralization. W40-scan proved the bypass:
 * `</nexus-context/>` fell through the old `\\\\s*>` regex, survived into the
 * recall wrapper and closed it early (everything after it became free prompt
 * text).
 *
 * Fixed-point loop (OCR-4 regression scan): a single pass is NOT idempotent.
 * Removing a fragment can splice the two halves of the surrounding text into
 * a NEW live tag — `</nexus-con</nexus-context>text>` becomes
 * `</nexus-context>` after one pass. The loop re-applies the replace until
 * the output stabilizes; the length strictly decreases on every non-empty
 * iteration, so it always terminates.
 */
export function neutralizeContextClose(text: string): string {
  if (!text) return text
  let out = text
  let prev: string
  // OCR-6 (performance medium, Z716): the until-stable loop is quadratic on
  // adversarial input (k nested splices need k full-rescan passes). Every
  // productive pass removes at least one closing tag, so the number of
  // REQUIRED passes is bounded by the number of closing-tag openings in the
  // input; cap the loop there (+1 for the final no-op pass that proves the
  // fixed point). Bounded passes make the loop provably terminating even
  // without the length-decrease condition.
  // Every productive pass removes ≥1 tag (length strictly decreases), and
  // the input length bounds the number of removable fragments — text.length
  // is therefore a safe upper bound on required passes (quadratic worst case
  // capped per-call, loop provably terminating). The length-decrease
  // condition below still stops the loop early in all non-adversarial cases.
  let pass = 0
  const maxPasses = text.length
  do {
    prev = out
    // OCR-6 (security high): the closing-tag shape must match the strip
    // helper exactly — HTML-aware consumers ignore attributes on end tags,
    // so `</nexus-context foo>` (e.g. from splice stabilization) must be
    // removed here too, not just the bare form. [^>]* keeps both security
    // paths in lockstep.
    // OCR-6 (bug medium, Z705): `[^>]*` prefix-matched any token that merely
    // STARTED with the tag name (`<nexus-contextual>`, `</nexus-context foo>`
    // was intended, but `<nexus-context-block>` too) and silently discarded
    // legitimate prompt text mentioning the name. Require a tag boundary
    // (whitespace, `/`, or `>`) right after the name.
    out = out.replace(/<\/nexus-context(?=[\s/>])[^>]*>/gi, "")
    pass++
    // OCR-6 (maintainability low, L694): the old length-decrease clause was
    // redundant — String.replace with "" only ever deletes characters, so
    // out !== prev already implies a strictly shorter string. The pass bound
    // above carries the termination guarantee.
  } while (out !== prev && pass < maxPasses)
  return out
}

/**
 * Strip injected nexus-context blocks from inbound text.
 *
 * - Complete `<nexus-context …>…</nexus-context>` blocks (incl. multiline and
 *   attribute variants) are removed.
 * - A tag that never gets its closing tag (an UNTERMINATED
 *   `<nexus-context …>`, or a stray `</nexus-context>`) is neutralized: the
 *   tag itself is removed and the surrounding text is kept. Removing the tag
 *   is what stops a wrapper from being opened — deleting everything after it
 *   (W40-12) protected nothing extra and silently discarded a prompt that
 *   merely mentioned the tag.
 */
export function stripNexusContextBlock(text: string): string {
  if (!text) return text
  // `\\s*` before `>`: without it a whitespace variant (`</nexus-context >`)
  // never completed a block here, so the text instead fell through to the
  // unterminated-tag rule below and the whole prompt was truncated.
  // OCR-5 (security high, /tmp/z750-proof.mjs): both replaces ran ONCE, so a
  // spliced fragment pair (`</nexus-con</nexus-context>text>`) removed the
  // inner tag and re-joined the halves into a NEW live wrapper tag — the
  // function was not a fixed point. recall.ts:190 uses this helper alone.
  // Re-apply until stable: each pass either strictly shortens the string or
  // removes nothing, so the loop terminates.
  let out = text
  for (let prev = ""; prev !== out; ) {
    prev = out
    // OCR-5 (bug medium): the closing tag accepted `\s*/?\s*` but the block
    // regex required exactly `\s*>` — `</nexus-context/>` never completed a
    // BLOCK and fell through to the stray-tag rule, leaving the block BODY
    // behind as free query text. Also: HTML tokenizers ignore attributes on
    // end tags, so `</nexus-context foo>` closes the wrapper for HTML-aware
    // consumers — widen to the same `[^>]*` shape used by the stray rule
    // (over-stripping is the safe direction for stored memory text).
    // Z705: same tag-boundary requirement as neutralizeContextClose — both
    // block and stray shapes only match real tags, not name-prefix tokens.
    // The closing side needs the boundary too: `</nexus-context foo>` is a
    // valid end tag for HTML-aware consumers, but `</nexus-contextual>` is
    // NOT ours and must not trigger block deletion of the span between two
    // mere mentions.
    out = out.replace(/<nexus-context(?=[\s/>])[^>]*>[\s\S]*?<\/nexus-context(?=[\s/>])[^>]*>\s*/gi, "")
    out = out.replace(/<\/?nexus-context(?=[\s/>])[^>]*>/gi, "")
  }
  return out.trim()
}
