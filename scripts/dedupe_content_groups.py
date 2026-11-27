#!/usr/bin/env python3
"""Remove byte-identical copies of the same memory — without losing their edges.

Why this tool exists: the health report found 59 groups of copies (139 points,
80 surplus) in one collection, all from the ``paperless`` importer: the same
page stored again under another Paperless id. Inside every group ``content`` is
byte-identical; ``doc_id``, ``title`` and ``tags`` differ. Just deleting the
surplus copies would damage the graph, because points can be REFERENCED by
edges stored on other points:

* ``edges[].metadata.source_memory_id`` — the memory an edge belongs to. On
  27.11.2026 exactly this field was the only reference to the 139 ids in the
  whole collection (844 occurrences, 172 carrying points, 74 of the 139 ids
  referenced). Deleting such a point leaves those edges pointing at nothing.
* ``edges[].target_fact_id`` — the graph target. In this collection it only
  ever holds UUIDs of entity points, never a numeric memory id (measured over
  all 117,474 edges), but the tool handles it anyway: after the next write it
  may be different, and a rerouting tool that only knows one shape is a trap.

What it does, per group:
  1. Re-check the report against the live data: every member must really carry
     the same ``content`` (SHA-1). A group that does not match is skipped and
     reported — never guessed at.
  2. Pick a keeper: the member with the most incoming references, then the
     oldest ``created``, then the lowest id (deterministic in every case).
  3. Reroute every edge that references a removed member to the keeper —
     on foreign points AND on the keeper itself — before anything is deleted.
  4. Carry the removed members' OWN edges onto the keeper (deduplicated by
     ``edge_id``, then by relation+target) so no edge disappears with a point.
  5. Snapshot first (name, size, checksum required). No snapshot, no write.
  6. After the writes, scan the WHOLE collection again: as long as one edge
     still references a removed id, stop before deleting and say so.

Usage:
  python3 scripts/dedupe_content_groups.py                  # dry run (default)
  python3 scripts/dedupe_content_groups.py --limit 3        # first 3 groups
  python3 scripts/dedupe_content_groups.py --apply          # snapshot + write

The dry run is read-only and prints the exact plan: keeper, rerouted edges,
merged edges, points that would be deleted.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333").rstrip("/")
DEFAULT_COLLECTION = os.environ.get("NEXUS_COLLECTION", "nexus_hf")
DEFAULT_REPORT_DIR = os.path.expanduser("~/.nexus-memory/reports")

EDGES_KEY = "edges"
SOURCE_MEMORY_KEY = "source_memory_id"
TARGET_KEY = "target_fact_id"


# ── HTTP ───────────────────────────────────────────────────────────────────

def _request(method: str, url: str, body: dict | None = None,
             timeout: float = 600.0) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def as_point_id(raw) -> "int | str":
    """Qdrant ids are unsigned integers or UUIDs; the report keeps strings."""
    text = str(raw)
    return int(text) if text.isdigit() else text


def fetch_edges(base: str, collection: str) -> "dict[str, list[dict]]":
    """Every point's ``edges`` array, keyed by point id. One pass, edges only."""
    out: dict[str, list[dict]] = {}
    offset = None
    pages = 0
    while True:
        body: dict = {"limit": 1000, "with_payload": [EDGES_KEY],
                      "with_vector": False}
        if offset:
            body["offset"] = offset
        result = _request("POST", base + "/collections/%s/points/scroll" % collection,
                          body)["result"]
        for point in result.get("points") or []:
            out[str(point["id"])] = edges_of((point.get("payload") or {}))
        offset = result.get("next_page_offset")
        pages += 1
        if not offset or pages > 2000:
            break
    return out


def fetch_payloads(base: str, collection: str,
                   ids: "list[str]") -> "dict[str, dict]":
    """Full payloads for the given ids, keyed by id string."""
    out: dict[str, dict] = {}
    wanted = [as_point_id(i) for i in ids]
    for start in range(0, len(wanted), 200):
        chunk = wanted[start:start + 200]
        result = _request("POST", base + "/collections/%s/points" % collection,
                          {"ids": chunk, "with_payload": True,
                           "with_vector": False})["result"]
        for point in result or []:
            out[str(point["id"])] = dict(point.get("payload") or {})
    return out


def count_points(base: str, collection: str) -> int:
    out = _request("POST", base + "/collections/%s/points/count" % collection,
                   {"exact": True})
    return int(out["result"]["count"])


def set_edges(base: str, collection: str, point_id: str,
              edges: "list[dict]") -> None:
    """Replace only the ``edges`` field of one point; leave the rest alone."""
    _request("POST", base + "/collections/%s/points/payload?wait=true" % collection,
             {"payload": {EDGES_KEY: edges}, "points": [as_point_id(point_id)]})


def delete_points(base: str, collection: str, ids: "list[str]") -> None:
    _request("POST", base + "/collections/%s/points/delete?wait=true" % collection,
             {"points": [as_point_id(i) for i in ids]})


def create_snapshot(base: str, collection: str) -> dict:
    return _request("POST", base + "/collections/%s/snapshots" % collection,
                    {}, timeout=1800)["result"]


# ── Edges and references (pure) ────────────────────────────────────────────

def edges_of(payload: dict) -> "list[dict]":
    edges = (payload or {}).get(EDGES_KEY)
    if not isinstance(edges, list):
        return []
    return [dict(e) for e in edges if isinstance(e, dict)]


def edge_source_memory(edge: dict) -> str:
    meta = edge.get("metadata")
    if isinstance(meta, dict):
        return str(meta.get(SOURCE_MEMORY_KEY) or "")
    return ""


def referenced_ids(edge: dict, idset: "set[str]") -> "set[str]":
    """Which ids from ``idset`` this edge references, in either shape."""
    hits = set()
    source = edge_source_memory(edge)
    if source in idset:
        hits.add(source)
    target = str(edge.get(TARGET_KEY) or "")
    if target in idset:
        hits.add(target)
    return hits


def scan_references(edges_by_point: "dict[str, list[dict]]",
                    idset: "set[str]") -> "dict[str, int]":
    """How often each id in ``idset`` is referenced anywhere in the collection."""
    counts = {i: 0 for i in idset}
    for edges in edges_by_point.values():
        for edge in edges:
            for hit in referenced_ids(edge, idset):
                counts[hit] += 1
    return counts


def retarget(edge: dict, mapping: "dict[str, str]") -> dict:
    """Copy of ``edge`` with every reference into ``mapping`` moved to its keeper."""
    new = dict(edge)
    target = str(new.get(TARGET_KEY) or "")
    if target in mapping:
        new[TARGET_KEY] = mapping[target]
    meta = new.get("metadata")
    if isinstance(meta, dict):
        source = str(meta.get(SOURCE_MEMORY_KEY) or "")
        if source in mapping:
            meta = dict(meta)
            meta[SOURCE_MEMORY_KEY] = mapping[source]
            new["metadata"] = meta
    return new


def _is_self_edge(edge: dict, point_id: str) -> bool:
    return (str(edge.get(TARGET_KEY) or "") == point_id
            and edge_source_memory(edge) == point_id)


def merge_edges(keeper_edges: "list[dict]",
                carried: "list[dict]") -> "tuple[list[dict], int, int]":
    """Keeper edges plus carried ones, without duplicates.

    ``edge_id`` is the identifier (same edge moved, keep one). Beyond that an
    edge with the same relation and target is the same statement made twice by
    two copies of the document — one of them is enough.
    """
    out = [dict(e) for e in keeper_edges]
    seen_ids = {str(e.get("edge_id")) for e in out if e.get("edge_id")}
    seen_pairs = {(str(e.get("relation")), str(e.get(TARGET_KEY))) for e in out}
    added = skipped = 0
    for edge in carried:
        edge_id = str(edge.get("edge_id") or "")
        pair = (str(edge.get("relation")), str(edge.get(TARGET_KEY)))
        if (edge_id and edge_id in seen_ids) or pair in seen_pairs:
            skipped += 1
            continue
        if edge_id:
            seen_ids.add(edge_id)
        seen_pairs.add(pair)
        out.append(dict(edge))
        added += 1
    return out, added, skipped


def _id_sort_key(point_id: str):
    return (0, int(point_id)) if str(point_id).isdigit() else (1, str(point_id))


def keeper_sort_key(point_id: str, incoming: "dict[str, int]",
                    payload: dict):
    """Most referenced first, then oldest ``created``, then lowest id."""
    created = payload.get("created") or payload.get("created_at") or "~"
    return (-int(incoming.get(point_id, 0)), str(created), _id_sort_key(point_id))


def choose_keeper(members: "list[str]", incoming: "dict[str, int]",
                  payloads: "dict[str, dict]") -> str:
    return sorted(members,
                  key=lambda pid: keeper_sort_key(pid, incoming,
                                                  payloads.get(pid) or {}))[0]


def content_sha1(payload: dict) -> str:
    content = (payload or {}).get("content")
    if not isinstance(content, str):
        return ""
    return hashlib.sha1(content.encode("utf-8")).hexdigest()


# ── Planning ───────────────────────────────────────────────────────────────

class GroupPlan:
    """What would happen to one group."""

    def __init__(self, members: "list[str]"):
        self.members = list(members)
        self.keeper = ""
        self.removed: "list[str]" = []
        self.skipped = ""
        self.rewrites: "dict[str, list[dict]]" = {}
        self.rewritten_edges = 0
        self.keeper_edges: "list[dict]" = []
        self.merged_edges = 0
        self.dropped_edges = 0
        self.dropped_self_edges = 0

    @property
    def ok(self) -> bool:
        return not self.skipped


def plan_group(members: "list[str]", payloads: "dict[str, dict]",
               edges_by_point: "dict[str, list[dict]]") -> GroupPlan:
    """Build the plan for one group from live payloads and all edges."""
    plan = GroupPlan(members)
    missing = [m for m in members if m not in payloads]
    if missing:
        plan.skipped = "missing in the collection: %s" % ", ".join(missing[:5])
        return plan

    hashes = {m: content_sha1(payloads[m]) for m in members}
    if len(set(hashes.values())) != 1 or not next(iter(hashes.values())):
        plan.skipped = ("content is not byte-identical across the group "
                        "(sha1: %s)" % json.dumps(hashes, sort_keys=True))
        return plan

    member_set = set(members)
    incoming = scan_references(edges_by_point, member_set)
    keeper = choose_keeper(members, incoming, payloads)
    plan.keeper = keeper
    plan.removed = [m for m in members if m != keeper]
    mapping = {m: keeper for m in plan.removed}
    removed_set = set(plan.removed)

    # 1) edges on FOREIGN points that reference a removed member
    for point_id, edges in edges_by_point.items():
        if point_id in member_set:
            continue
        if not any(referenced_ids(e, removed_set) for e in edges):
            continue
        new_edges = []
        for edge in edges:
            if referenced_ids(edge, removed_set):
                edge = retarget(edge, mapping)
                plan.rewritten_edges += 1
            new_edges.append(edge)
        plan.rewrites[point_id] = new_edges

    # 2) the keeper's own edges, retargeted the same way
    keeper_edges = []
    for edge in edges_of(payloads[keeper]):
        if referenced_ids(edge, removed_set):
            edge = retarget(edge, mapping)
            plan.rewritten_edges += 1
        if _is_self_edge(edge, keeper):
            plan.dropped_self_edges += 1
            continue
        keeper_edges.append(edge)

    # 3) the removed copies' own edges move onto the keeper
    carried = []
    for member in plan.removed:
        for edge in edges_of(payloads[member]):
            edge = retarget(edge, mapping)
            if _is_self_edge(edge, keeper):
                plan.dropped_self_edges += 1
                continue
            carried.append(edge)
    plan.keeper_edges, plan.merged_edges, plan.dropped_edges = merge_edges(
        keeper_edges, carried)
    return plan


# ── Apply ──────────────────────────────────────────────────────────────────

def _verify_no_dangling(base: str, collection: str,
                        removed: "set[str]") -> "dict[str, int]":
    """Re-scan the collection: does any SURVIVING point still reference a removed id?

    References inside the removed points' own payloads do not count — those
    points are deleted next and their edges go with them. Everything else must
    be clean, otherwise the deletion would leave a dangling edge.
    """
    surviving = {pid: edges
                 for pid, edges in fetch_edges(base, collection).items()
                 if pid not in removed}
    return {k: v for k, v in
            scan_references(surviving, removed).items() if v}


def combined_rewrites(plans: "list[GroupPlan]",
                      edges_by_point: "dict[str, list[dict]]"
                      ) -> "dict[str, list[dict]]":
    """One new edge list per affected point, all groups in ONE mapping.

    Ten points in this collection reference copies from two different groups.
    Writing each group on its own would make the second write undo the first:
    both are computed from the same snapshot, so the second one still carries
    the first group's old reference. One write per point with every group's
    mapping at once is the only version that cannot lose a rewrite.
    """
    combined = {removed: plan.keeper
                for plan in plans for removed in plan.removed}
    out: dict[str, list[dict]] = {}
    for plan in plans:
        for point_id in plan.rewrites:
            if point_id in out:
                continue
            out[point_id] = [retarget(edge, combined)
                             for edge in edges_by_point.get(point_id) or []]
    return out


def apply_all(base: str, collection: str, plans: "list[GroupPlan]",
              edges_by_point: "dict[str, list[dict]]") -> "tuple[bool, str]":
    """Write every group's plan. Returns ``(ok, message)``.

    Order matters: every reference is moved BEFORE a point is deleted, and the
    deletion only happens after a full re-scan finds no reference left.
    """
    for point_id, edges in sorted(combined_rewrites(plans, edges_by_point).items()):
        set_edges(base, collection, point_id, edges)
    for plan in plans:
        set_edges(base, collection, plan.keeper, plan.keeper_edges)

    removed = {rid for plan in plans for rid in plan.removed}
    still = _verify_no_dangling(base, collection, removed)
    if still:
        return False, ("after rerouting, %d reference(s) still point at removed "
                       "points (%s) — nothing deleted; the snapshot restores "
                       "the graph if anything looks wrong"
                       % (sum(still.values()),
                          json.dumps(dict(sorted(still.items())), sort_keys=True)))
    for plan in plans:
        delete_points(base, collection, plan.removed)
    return True, "ok"


# ── Report ─────────────────────────────────────────────────────────────────

def latest_report(directory: str = DEFAULT_REPORT_DIR) -> "str | None":
    files = sorted(glob.glob(os.path.join(directory, "health-*.json")))
    return files[-1] if files else None


def load_groups(path: str) -> "list[list[str]]":
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    groups = []
    for group in data.get("duplicate_groups") or []:
        ids = [str(i) for i in (group.get("ids") or [])]
        if len(ids) > 1:
            groups.append(ids)
    return groups


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", default=None,
                    help="health report JSON (default: newest in %s)"
                         % DEFAULT_REPORT_DIR)
    ap.add_argument("--url", default=DEFAULT_URL, help="Qdrant base URL (QDRANT_URL)")
    ap.add_argument("--collection", default=DEFAULT_COLLECTION,
                    help="collection name (NEXUS_COLLECTION)")
    ap.add_argument("--limit", type=int, default=0,
                    help="only the first N groups (0 = all)")
    ap.add_argument("--apply", action="store_true",
                    help="snapshot, then actually write (default: dry run)")
    args = ap.parse_args(argv)

    base = args.url.rstrip("/")
    col = args.collection

    report = args.report or latest_report()
    if not report or not os.path.exists(report):
        print("ERROR: no health report found (looked at %s); pass --report"
              % DEFAULT_REPORT_DIR)
        return 1
    groups = load_groups(report)
    if args.limit and args.limit > 0:
        groups = groups[:args.limit]
    seen = {}
    for group in groups:
        for point_id in group:
            seen[point_id] = seen.get(point_id, 0) + 1
    twice = sorted(p for p, n in seen.items() if n > 1)
    if twice:
        print("ERROR: the report puts a point in more than one group (%s); a "
              "plan built on that would both keep and delete it — refusing."
              % ", ".join(twice[:5]))
        return 1
    print("report             : %s" % report)
    print("groups in report   : %d" % len(groups))

    try:
        before_count = count_points(base, col)
    except Exception as exc:
        print("ERROR: collection %r not readable at %s: %s" % (col, base, exc))
        return 1
    print("points before      : %d" % before_count)

    try:
        edges_by_point = fetch_edges(base, col)
    except Exception as exc:
        print("ERROR: could not scan the edges: %s" % exc)
        return 1
    all_members = [m for g in groups for m in g]
    try:
        payloads = fetch_payloads(base, col, all_members)
    except Exception as exc:
        print("ERROR: could not read the group payloads: %s" % exc)
        return 1

    plans = [plan_group(g, payloads, edges_by_point) for g in groups]

    skipped = [p for p in plans if not p.ok]
    usable = [p for p in plans if p.ok]
    print("")
    for plan in plans:
        if not plan.ok:
            print("SKIP group %s: %s" % (", ".join(plan.members[:3]), plan.skipped))
            continue
        print("group keeper=%s removes=%d rerouted_edges=%d merged_edges=%d "
              "duplicate_edges_skipped=%d self_edges_dropped=%d"
              % (plan.keeper, len(plan.removed), plan.rewritten_edges,
                 plan.merged_edges, plan.dropped_edges,
                 plan.dropped_self_edges))
    rewritten = sum(p.rewritten_edges for p in usable)
    merged = sum(p.merged_edges for p in usable)
    dropped = sum(p.dropped_edges for p in usable)
    self_dropped = sum(p.dropped_self_edges for p in usable)
    to_delete = sum(len(p.removed) for p in usable)
    touched = {pid for p in usable for pid in p.rewrites}
    write_entries = sum(len(p.rewrites) for p in usable)

    print("")
    print("usable groups      : %d (skipped: %d)" % (len(usable), len(skipped)))
    print("edges rerouted     : %d" % rewritten)
    print("edges carried over : %d (duplicates skipped: %d)"
          % (merged, dropped))
    print("self edges dropped : %d" % self_dropped)
    print("points to rewrite  : %d (group/point entries: %d, one write each)"
          % (len(touched), write_entries))
    print("points to delete   : %d (from %d)" % (to_delete, before_count))

    if not args.apply:
        print("")
        print("dry run: nothing written. Add --apply to snapshot and write.")
        return 0

    if not usable:
        print("nothing to do.")
        return 0

    try:
        snap = create_snapshot(base, col)
    except Exception as exc:
        print("ERROR: snapshot failed, nothing is written: %s" % exc)
        return 1
    name = snap.get("name") or ""
    if not name or not snap.get("size") or not snap.get("checksum"):
        print("ERROR: snapshot answer without name/size/checksum, nothing is "
              "written: %s" % json.dumps(snap, sort_keys=True))
        return 1
    print("")
    print("snapshot           : %s (%s bytes, %s)"
          % (name, snap.get("size"), snap.get("checksum")))
    print("restore with       : POST /collections/%s/snapshots/recover with %r"
          % (col, name))

    ok, message = apply_all(base, col, usable, edges_by_point)
    if not ok:
        print("ERROR: %s" % message)
        return 1
    for plan in usable:
        print("applied group keeper=%s removed=%d"
              % (plan.keeper, len(plan.removed)))

    after_count = count_points(base, col)
    print("")
    print("points after       : %d (expected %d)"
          % (after_count, before_count - to_delete))
    if after_count != before_count - to_delete:
        print("ERROR: the point count does not match the plan (%d deleted, "
              "%d -> %d). Check the collection; the snapshot above restores it."
              % (to_delete, before_count, after_count))
        return 1
    print("done: %d copies removed, %d edges rerouted, %d carried over."
          % (to_delete, rewritten, merged))
    return 0


if __name__ == "__main__":
    sys.exit(main())
