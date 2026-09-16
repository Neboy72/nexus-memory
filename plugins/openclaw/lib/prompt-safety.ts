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
 * Remove every "<nexus-context …>" and "</nexus-context>" (case-insensitive,
 * self-closing and whitespace variants included) from stored text.
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
  do {
    prev = out
    out = out.replace(/<\/nexus-context\s*\/?\s*>/gi, "")
  } while (out !== prev && out.length < prev.length)
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
  // `\s*` before `>`: without it a whitespace variant (`</nexus-context >`)
  // never completed a block here, so the text instead fell through to the
  // unterminated-tag rule below and the whole prompt was truncated.
  const withoutComplete = text.replace(
    /<nexus-context[^>]*>[\s\S]*?<\/nexus-context\s*>\s*/gi,
    "",
  )
  const withoutTags = withoutComplete.replace(/<\/?nexus-context[^>]*>/gi, "")
  return withoutTags.trim()
}
