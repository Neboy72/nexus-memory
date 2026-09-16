/**
 * Numeric input hardening (H123, Welle 13).
 *
 * Tool parameters arrive from a host/model and are declared as numbers in the
 * Typebox schema — but a host that bypasses the schema (or a model emitting
 * `1e999` / `-5` / `"12"`) can hand us anything. Every limit/depth parameter
 * that feeds a Qdrant request or a BFS loop must therefore be clamped before
 * use, otherwise one call can fan out unboundedly (limit=1e9) or make the
 * loop semantics nonsensical (negative/NaN depth).
 */

/**
 * Coerce an untrusted value to a bounded integer.
 *
 * Contract:
 *  - Finite numbers pass through; numeric strings ("12") are accepted too.
 *  - Everything else (undefined, null, NaN, ±Infinity, "abc", objects) → dflt.
 *  - Fractions are truncated toward zero (Math.trunc).
 *  - The result is clamped into [min, max]. `dflt` is clamped the same way, so
 *    a misconfigured default can never escape the declared bounds.
 *  - Negative inputs are NOT treated as "missing": they clamp up to `min`.
 *  - String parsing is `Number()` semantics: "0x1f" (31), "1e5" and
 *    surrounding whitespace are accepted on purpose. Note that "1e999"
 *    therefore parses to Infinity and falls back to `dflt`.
 */
export function clampInt(
  v: unknown,
  dflt: number,
  min: number,
  max: number,
): number {
  // W40-11: bounds come from the caller, not from the schema. A non-finite
  // bound or an inverted interval (min > max) made the clamp math return NaN
  // for *every* input — including the fallback (Math.trunc(NaN) → NaN) —
  // instead of a bounded integer. Normalize to a finite, ordered interval.
  const hi = Number.isFinite(max) ? max : Number.isFinite(min) ? min : 0
  const lo = Number.isFinite(min) ? min : hi
  const lower = Math.min(lo, hi)
  const upper = Math.max(lo, hi)

  const base = Number.isFinite(dflt) ? dflt : lower
  const fallback = Math.min(upper, Math.max(lower, Math.trunc(base)))

  let n: number
  if (typeof v === "number") {
    n = v
  } else if (typeof v === "string" && v.trim() !== "") {
    n = Number(v)
  } else {
    return fallback
  }

  if (!Number.isFinite(n)) return fallback
  return Math.min(upper, Math.max(lower, Math.trunc(n)))
}
