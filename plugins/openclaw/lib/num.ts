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
 */
export function clampInt(
  v: unknown,
  dflt: number,
  min: number,
  max: number,
): number {
  const fallback = Math.min(max, Math.max(min, Math.trunc(dflt)))

  let n: number
  if (typeof v === "number") {
    n = v
  } else if (typeof v === "string" && v.trim() !== "") {
    n = Number(v)
  } else {
    return fallback
  }

  if (!Number.isFinite(n)) return fallback
  return Math.min(max, Math.max(min, Math.trunc(n)))
}
