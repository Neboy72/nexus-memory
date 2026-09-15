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
  if (!edge || typeof edge !== "object") return null
  const e = edge as Record<string, unknown>

  const targetId = e.target_fact_id
  if (typeof targetId !== "string" || targetId === "") return null

  const relation = e.relation
  if (typeof relation !== "string" || relation === "") return null

  const edgeId =
    typeof e.edge_id === "string" && e.edge_id !== "" ? e.edge_id : ""

  return { targetId, relation, edgeId }
}

/**
 * True when an edge is still active. An absent `status` counts as active
 * (legacy edges predate the field).
 */
export function isActiveEdge(edge: unknown): boolean {
  if (!edge || typeof edge !== "object") return false
  const status = (edge as Record<string, unknown>).status
  return typeof status !== "string" || status === "" || status === "active"
}
