/**
 * Knowledge-graph edge normalization (H125, Welle 13).
 *
 * Edges live as plain objects inside an entity point's `edges` payload array
 * (payload is untyped JSON written by whichever writer). A malformed edge —
 * missing/blank `target_fact_id` or `relation` — used to be cast with `as
 * string`, so `undefined` flowed straight into `visited`, the BFS queue and
 * the tool results, corrupting the traversal (a single `undefined` target
 * collapses every later path) and emitting `fact_id: undefined` to callers.
 *
 * `normalizeEdge` is the single gate both graph tools run edges through.
 */

export type NormalizedEdge = {
  /** Non-empty target fact id — the only value that may enter traversal state. */
  targetId: string
  /** Non-empty relation label (e.g. "manages", "runs_on"). */
  relation: string
  /** Optional edge id; absent → "" (never `undefined`). */
  edgeId: string
}

/**
 * Validate one raw edge. Returns a normalized, fully-string edge or `null`
 * when the edge is unusable and must be skipped.
 *
 * Rules:
 *  - Not an object → null.
 *  - `target_fact_id` must be a non-empty string (an id is required to
 *    traverse; a blank id would poison `visited`).
 *  - `relation` must be a non-empty string (it is part of the public
 *    { fact_id, relation, edge_id } contract).
 *  - `edge_id` is optional: a missing or non-string value becomes "", which
 *    preserves the previous "edge_id may be empty" behavior without ever
 *    leaking `undefined`.
 */
export function normalizeEdge(edge: unknown): NormalizedEdge | null {
  const e = asEdgeRecord(edge)
  if (!e) return null

  const targetId = e.target_fact_id
  // W40-9: a whitespace-only id is as blank as "" and would poison `visited`
  // just the same, so the emptiness test trims.
  if (typeof targetId !== "string" || targetId.trim() === "") return null

  const relation = e.relation
  if (typeof relation !== "string" || relation.trim() === "") return null

  const edgeId =
    typeof e.edge_id === "string" && e.edge_id !== "" ? e.edge_id : ""

  return { targetId, relation, edgeId }
}

/**
 * Narrow an untrusted value to a plain object record, or null. Shared by both
 * helpers so a future tightening (e.g. rejecting arrays, which currently pass
 * `typeof edge === "object"`) cannot drift between them.
 */
function asEdgeRecord(edge: unknown): Record<string, unknown> | null {
  return edge && typeof edge === "object" ? (edge as Record<string, unknown>) : null
}

/**
 * True when an edge is still active.
 *
 * Intent (W40-9), in order:
 *  - a non-object is not an edge → false;
 *  - an ABSENT status counts as active (legacy edges predate the field);
 *  - an empty-string status also counts as active (same legacy treatment);
 *  - only the exact string "active" is active. Every other string — a
 *    differently-cased "Active", "deleted", "  " — is inactive;
 *  - a non-string status (null, 0, true) is inactive: a malformed value must
 *    not silently resurrect an edge that was meant to be removed.
 */
export function isActiveEdge(edge: unknown): boolean {
  const e = asEdgeRecord(edge)
  if (!e) return false
  const status = e.status
  return status === undefined || status === "" || status === "active"
}
