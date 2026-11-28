#!/usr/bin/env python3
"""Idempotent payload index on ``scope`` for a Qdrant collection.

Why: the prefetch visibility rule filters every read down to
``scope in ("default", <own bundle>) or scope is empty`` (see ``_scope_should``
in ``plugins/memory/nexus/__init__.py``). Without a payload index Qdrant
evaluates that condition against the whole collection; with
``on_disk_payload=true`` that means reading payloads from disk before a single
candidate is left over. Measured 27.11.2026 on this collection: 365-416 ms
filtered against 3-7 ms unfiltered, 68,602 points, ``payload_schema`` empty.

This is NOT a fix for the coldstart deadline — it saves about 0.4 s on the
filtered count, not seconds. What it does is keep that read cheap and survive
a reindex: after a collection is recreated the index is gone unless a
migration step puts it back. That is what this script is for.

Every step aborts with exit 1 instead of guessing:
  1. Read the collection: point count, payload_schema, on_disk_payload.
  2. Is ``scope`` already indexed as keyword? Then this is a no-op — unless
     ``--recreate`` was asked for. An existing index with any OTHER type fails
     closed (exit != 0) and names ``--recreate`` instead of warning.
  3. Snapshot the collection and require name, size and checksum. No
     successful snapshot, no build.
  4. Create the index: ``field_name=scope``, ``field_schema=keyword``,
     ``wait=true``.
  5. Verify: ``payload_schema`` names ``scope`` as keyword AND the filtered
     count (the same three visibility conditions) equals the count taken
     before the build.

Usage:
  python3 scripts/scope_payload_index.py                     # read, report, stop
  python3 scripts/scope_payload_index.py --apply             # snapshot + index
  python3 scripts/scope_payload_index.py --apply --recreate  # rebuild the index

Standard library only, so it runs wherever Python 3 is installed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

SHARED_SCOPE = "default"
DEFAULT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333").rstrip("/")
DEFAULT_COLLECTION = os.environ.get("NEXUS_COLLECTION", "nexus_hf")
DEFAULT_BUNDLE = os.environ.get("NEXUS_AGENT_ID", "hermes")


def _request(method: str, url: str, body: dict | None = None,
             timeout: float = 600.0) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def scope_filter(bundle: str) -> dict:
    """The same three conditions as ``_scope_should`` in the plugin."""
    return {"should": [
        {"key": "scope", "match": {"value": SHARED_SCOPE}},
        {"key": "scope", "match": {"value": bundle}},
        {"is_empty": {"key": "scope"}},
    ]}


def filtered_count(base: str, collection: str, bundle: str) -> int:
    out = _request("POST", base + "/collections/%s/points/count" % collection,
                   {"filter": scope_filter(bundle), "exact": True})
    return int(out["result"]["count"])


def scope_index_state(base: str, collection: str) -> dict | None:
    info = _request("GET", base + "/collections/%s" % collection)["result"]
    params = (info.get("config") or {}).get("params") or {}
    schema = params.get("payload_schema") or info.get("payload_schema") or {}
    entry = schema.get("scope")
    return entry if isinstance(entry, dict) else None


def _index_is_keyword(state: dict) -> bool:
    kind = str(state.get("data_type") or state.get("type") or "").lower()
    return "keyword" in kind


def _index_type(state: dict) -> str:
    """The type Qdrant reports for an index, or ``"unknown"``."""
    return str(state.get("data_type") or state.get("type") or "unknown")


# ``wait=true`` on the index call makes Qdrant build the index before it
# answers, but ``payload_schema`` in the collection info is filled from the
# optimiser side and can lag by a moment. On 27.11.2026 that turned a finished
# build (index in place, filtered search down to 2-3 ms) into a red alarm:
# "payload_schema does not name 'scope' as keyword after the build: null".
# Poll for the finished state instead of reading it once.
_INDEX_WAIT_SEC = 30.0
_INDEX_POLL_SEC = 0.5


def _now() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def wait_for_scope_index(base: str, collection: str,
                         timeout: float | None = None,
                         poll: float | None = None) -> "tuple[dict | None, float]":
    """Poll until the collection reports a keyword index on ``scope``.

    Returns ``(state, waited_seconds)``: the last state seen and how long the
    check took. ``state`` is ``None`` when the schema never named ``scope``.
    Bounded on purpose — a schema that never shows up is a real error, just
    not the one a single read reports.
    """
    limit = _INDEX_WAIT_SEC if timeout is None else timeout
    step = _INDEX_POLL_SEC if poll is None else poll
    started = _now()
    state = None
    while True:
        state = scope_index_state(base, collection)
        if state is not None and _index_is_keyword(state):
            return state, _now() - started
        waited = _now() - started
        if waited >= limit:
            return state, waited
        _sleep(step)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL, help="Qdrant base URL (QDRANT_URL)")
    ap.add_argument("--collection", default=DEFAULT_COLLECTION,
                    help="collection name (NEXUS_COLLECTION)")
    ap.add_argument("--bundle", default=DEFAULT_BUNDLE,
                    help="own bundle, i.e. the second visibility value "
                         "(NEXUS_AGENT_ID)")
    ap.add_argument("--apply", action="store_true",
                    help="actually build (without it: read and report only)")
    ap.add_argument("--recreate", action="store_true",
                    help="drop an existing index on scope and build it again")
    args = ap.parse_args(argv)

    base = args.url.rstrip("/")
    col = args.collection

    try:
        info = _request("GET", base + "/collections/%s" % col)["result"]
    except urllib.error.HTTPError as exc:
        print("ERROR: collection %r not readable: HTTP %s" % (col, exc.code))
        return 1
    except Exception as exc:
        print("ERROR: Qdrant at %s not reachable: %s" % (base, exc))
        return 1

    params = info.get("config", {}).get("params", {})
    points = info.get("points_count")
    print("collection         : %s" % col)
    print("points             : %s" % points)
    print("on_disk_payload    : %s" % params.get("on_disk_payload"))
    print("full_scan_threshold: %s" % info.get("config", {}).get(
        "hnsw_config", {}).get("full_scan_threshold"))

    before = filtered_count(base, col, args.bundle)
    print("filtered count before (%s): %d" % (args.bundle, before))

    existing = scope_index_state(base, col)
    if existing is not None and not args.recreate:
        print("index on 'scope' exists: %s" % json.dumps(existing, sort_keys=True))
        if not _index_is_keyword(existing):
            # Fail closed (28.11.2026): a WARNING plus exit 0 reads as "all
            # good" to a human, a cron job and CI alike, while every filtered
            # read keeps paying for the wrong index type.
            print("ERROR: the index on 'scope' has data_type %r, but this "
                  "script needs 'keyword'." % _index_type(existing))
            print("       Re-run with --recreate to drop it and build the "
                  "keyword index.")
            return 1
        print("nothing to do (idempotent). Use --recreate to rebuild.")
        return 0

    if not args.apply:
        print("dry run: would %s — after a snapshot."
              % ("drop and rebuild the index" if existing else "create the index"))
        return 0

    # A build without a restore point is not a migration step. Require the
    # snapshot Qdrant itself reports, with name, size and checksum.
    print("creating snapshot (may take a while for %s points) ..." % points)
    try:
        snap = _request("POST", base + "/collections/%s/snapshots" % col,
                        {}, timeout=1800)["result"]
    except Exception as exc:
        print("ERROR: snapshot failed, nothing is built: %s" % exc)
        return 1
    name = snap.get("name") or ""
    size = snap.get("size")
    checksum = snap.get("checksum") or ""
    if not name or not size or not checksum:
        print("ERROR: snapshot answer without name/size/checksum, nothing is "
              "built: %s" % json.dumps(snap, sort_keys=True))
        return 1
    print("snapshot           : %s" % name)
    print("size               : %s bytes" % size)
    print("checksum           : %s" % checksum)
    print("restore with       : POST /collections/%s/snapshots/recover"
          " with %r if anything below goes wrong" % (col, name))

    if existing is not None:
        print("dropping the existing index on 'scope' ...")
        _request("DELETE", base + "/collections/%s/index/scope?wait=true" % col)

    print("creating index: scope / keyword (wait=true) ...")
    try:
        _request("PUT", base + "/collections/%s/index?wait=true" % col,
                 {"field_name": "scope", "field_schema": "keyword"}, timeout=900)
    except Exception as exc:
        print("ERROR: index could not be created: %s" % exc)
        print("       the index on 'scope' is now MISSING; restore from the "
              "snapshot above: %s" % name)
        return 1

    after_state, waited = wait_for_scope_index(base, col)
    if after_state is None or not _index_is_keyword(after_state):
        print("ERROR: after waiting %.1f s the collection still does not report "
              "a keyword index on 'scope': %s"
              % (waited, json.dumps(after_state, sort_keys=True)))
        print("       The index may still be building. Check by hand with "
              "GET /collections/%s (config.params.payload_schema) and re-run "
              "this script — it is idempotent and will report the finished "
              "state instead of building again." % col)
        return 1
    if waited >= _INDEX_POLL_SEC:
        print("payload_schema     : appeared after %.1f s" % waited)
    print("payload_schema     : %s" % json.dumps(after_state, sort_keys=True))

    try:
        after = filtered_count(base, col, args.bundle)
    except Exception as exc:
        print("ERROR: verification count failed: %s" % exc)
        return 1
    print("filtered count after : %d" % after)
    if after != before:
        print("ERROR: the filtered set changed (%d -> %d). Drop the index again "
              "and find the cause before relying on it." % (before, after))
        return 1

    print("done: index on 'scope' is in place, visibility unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
