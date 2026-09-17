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
 *    OCR-6 (documentation medium, L795): DEGENERATE bounds are the exception —
 *    if the interval contains no integer (min=0.5, max=0.9) or is inverted
 *    (min=5, max=3.5), the collapse (Math.floor of the lower bound) can land
 *    BELOW `min` (0 or 3). The comment inside documents why the clamp
 *    direction is fail-closed; callers must not assume result >= min holds
 *    for such bounds.
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
  // W40-scan: fractional bounds are truncated like values, so the clamp can
  // never widen a declared integer interval ("max: 4.5" behaves as 4).
  // OCR-4: trunc on the LOWER bound WIDENED the declared interval
  // (clampInt(-1, 1, 0.5, 4) returned 0, below the declared min). Bounds are
  // now rounded INWARD: lower = ceil(min), upper = floor(max) — an integer
  // inside [ceil(min), floor(max)] is always inside [min, max]. Degenerate
  // bounds still fail closed exactly as before.
  // DEGENERATE BOUNDS (non-finite or inverted) FAIL CLOSED BY DESIGN: a host
  // that bypasses the schema is misconfigured, and a silently unbounded
  // clamp ("max: Infinity" → every input passes) is the unsafe direction for
  // a loop/limit parameter. Collapsing to the finite bound (or 0 when both
  // are non-finite) is documented here on purpose — production callsites all
  // pass finite constants (1..5 / 1..500 / 1..50), so this path only fires
  // for schema-bypassing hosts, and fail-closed (tiny) beats fail-open
  // (unbounded).
  const hiRaw = Number.isFinite(max) ? max : Number.isFinite(min) ? min : 0
  // OCR-6 (bug high): the old `loRaw = finite min ? min : hiRaw` collapsed a
  // non-finite MIN onto the finite MAX — clampInt(x, 1, NaN, 1000) returned
  // 1000 for every input: the fail-OPEN direction, contradicting the
  // fail-closed contract above. A non-finite min declares no lower bound, so
  // the safe collapse is the SMALLEST value: 0 when it fits under the upper
  // bound, else the (negative) upper bound itself.
  const loRaw = Number.isFinite(min) ? min : Math.min(0, hiRaw)
  // OCR-5 (bug medium): the old Math.min/Math.max swap silently REORDERED an
  // inverted interval (min=5, max=3 → [3,5]) and clampInt(4,…) returned 4 —
  // above the declared max AND below the declared min, i.e. fail-open,
  // contradicting the fail-closed contract below. An interval containing no
  // integer (0.5..0.9) likewise collapsed to `upper` (0) for every input —
  // below the declared min. Both degenerate shapes now collapse to ONE
  // value: floor(smaller bound) — tiny, finite, documented. A
  // schema-bypassing host gets the safe direction: as small as the smaller
  // declared bound allows.
  const inverted = min > max
  const loBound = Math.min(loRaw, hiRaw)
  const hiBound = Math.max(loRaw, hiRaw)
  let lower = Math.ceil(loBound)
  let upper = Math.floor(hiBound)
  if (inverted || lower > upper) {
    const collapsed = Math.floor(loBound)
    lower = collapsed
    upper = collapsed
  }
  const clampInto = (x: number): number => Math.min(upper, Math.max(lower, x))

  const base = Number.isFinite(dflt) ? dflt : lower
  const fallback = clampInto(Math.trunc(base))

  let n: number
  if (typeof v === "number") {
    n = v
  } else if (typeof v === "string" && v.trim() !== "") {
    n = Number(v)
  } else {
    return fallback
  }

  if (!Number.isFinite(n)) return fallback
  return clampInto(Math.trunc(n))
}
