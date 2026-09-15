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

/** Remove every "</nexus-context>" (case-insensitive) from stored text. */
export function neutralizeContextClose(text: string): string {
  if (!text) return text
  return text.replace(/<\/nexus-context>/gi, "")
}

/**
 * Strip injected nexus-context blocks from inbound text.
 *
 * - Complete `<nexus-context …>…</nexus-context>` blocks (incl. multiline and
 *   attribute variants) are removed.
 * - An UNTERMINATED `<nexus-context …>` (no closing tag) removes everything
 *   from the tag onward — a stored memory must not be able to open a wrapper
 *   that swallows the rest of the prompt.
 */
export function stripNexusContextBlock(text: string): string {
  if (!text) return text
  const withoutComplete = text.replace(
    /<nexus-context[^>]*>[\s\S]*?<\/nexus-context>\s*/gi,
    "",
  )
  const withoutTail = withoutComplete.replace(/<nexus-context[^>]*>[\s\S]*$/i, "")
  return withoutTail.trim()
}
