#!/usr/bin/env python3
"""Re-embed all Qdrant points from voyage-3-large to voyage-4.

Reads all points from each collection, re-embeds the text content
with voyage-4, and upserts the new vectors back.

Usage:
    python3 reembed_voyage4.py [--dry-run] [--collection nexus]
"""

import argparse
import logging
import os
import sys
import time
from typing import List, Dict, Any

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reembed")

QDRANT_URL = "http://localhost:6333"
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")
VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
NEW_MODEL = "voyage-4"
BATCH_SIZE = 50  # Voyage API supports up to 128, use 50 for safety
SCROLL_SIZE = 100  # Qdrant scroll batch size

if not VOYAGE_API_KEY:
    logger.error("VOYAGE_API_KEY not set in environment")
    sys.exit(1)

# Also load from .env if not in env
if not VOYAGE_API_KEY:
    env_file = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                if line.startswith("VOYAGE_API_KEY="):
                    VOYAGE_API_KEY = line.split("=", 1)[1].strip()
                    break

if not VOYAGE_API_KEY:
    logger.error("Could not find VOYAGE_API_KEY")
    sys.exit(1)


def voyage_embed_batch(texts: List[str]) -> List[List[float]]:
    """Embed a batch of texts with voyage-4."""
    resp = requests.post(
        VOYAGE_URL,
        headers={
            "Authorization": f"Bearer {VOYAGE_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "input": texts,
            "model": NEW_MODEL,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return [item["embedding"] for item in data["data"]]


def scroll_points(collection: str, offset=None) -> Dict[str, Any]:
    """Scroll through all points in a collection.
    
    Qdrant scroll API returns next_offset=None even when more points exist.
    We manually paginate by using the last point ID as the next offset.
    """
    payload = {
        "limit": SCROLL_SIZE,
        "with_payload": True,
        "with_vector": False,
    }
    if offset is not None:
        payload["offset"] = offset

    resp = requests.post(
        f"{QDRANT_URL}/collections/{collection}/points/scroll",
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["result"]


def upsert_points(collection: str, points: List[Dict[str, Any]]):
    """Upsert points with new vectors."""
    resp = requests.put(
        f"{QDRANT_URL}/collections/{collection}/points",
        json={"points": points},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def extract_text(payload: Dict[str, Any]) -> str:
    """Extract text content from a point payload."""
    if not payload:
        return ""
    # Nexus Memory stores text in 'text' or 'content' field
    text = payload.get("text") or payload.get("content") or ""
    if not text:
        # Try concatenating other fields as fallback
        parts = []
        for key in ["text", "content", "query", "answer", "title"]:
            val = payload.get(key)
            if val and isinstance(val, str):
                parts.append(val)
        text = " ".join(parts)
    return text


def reembed_collection(collection: str, dry_run: bool = False):
    """Re-embed all points in a collection."""
    logger.info(f"=== Collection: {collection} ===")

    # Get collection info
    resp = requests.get(f"{QDRANT_URL}/collections/{collection}")
    resp.raise_for_status()
    info = resp.json()["result"]
    total = info["points_count"]
    dim = info["config"]["params"]["vectors"]["size"]
    logger.info(f"  Points: {total}, Dimensions: {dim}d")

    if dry_run:
        logger.info("  DRY RUN - not re-embedding")
        return

    offset = None
    processed = 0
    reembedded = 0
    skipped = 0
    errors = 0
    total_tokens = 0

    while True:
        result = scroll_points(collection, offset)
        points = result.get("points", [])
        next_offset = result.get("next_offset")

        if not points:
            break

        # Extract texts
        texts = []
        valid_points = []
        for pt in points:
            text = extract_text(pt.get("payload", {}))
            if text and len(text.strip()) > 0:
                texts.append(text)
                valid_points.append(pt)
            else:
                skipped += 1
                logger.warning(f"  Skipping point {pt.get('id')} - no text content")

        if texts:
            # Embed in batches
            for i in range(0, len(texts), BATCH_SIZE):
                batch_texts = texts[i:i + BATCH_SIZE]
                batch_points = valid_points[i:i + BATCH_SIZE]

                # Retry with backoff
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        vectors = voyage_embed_batch(batch_texts)
                        break
                    except Exception as e:
                        if attempt < max_retries - 1:
                            wait = 2 ** (attempt + 1)
                            logger.warning(f"  Embedding failed (attempt {attempt+1}): {e}. Retrying in {wait}s...")
                            time.sleep(wait)
                        else:
                            logger.error(f"  Embedding failed for batch: {e}")
                            vectors = None
                            errors += len(batch_texts)

                if vectors:
                    # Build upsert points
                    upsert_data = []
                    for pt, vec in zip(batch_points, vectors):
                        upsert_data.append({
                            "id": pt["id"],
                            "vector": vec,
                            "payload": pt.get("payload", {})
                        })

                    upsert_points(collection, upsert_data)
                    reembedded += len(upsert_data)

                    # Estimate tokens (rough: 1 token ~ 4 chars)
                    batch_tokens = sum(len(t) // 4 for t in batch_texts)
                    total_tokens += batch_tokens

                    if reembedded % 500 < BATCH_SIZE:
                        logger.info(f"  Progress: {reembedded}/{total} re-embedded ({skipped} skipped, {errors} errors)")

        processed += len(points)
        # Qdrant scroll returns next_offset=None even when more points exist.
        # Use the last point ID as the next offset for manual pagination.
        if len(points) < SCROLL_SIZE:
            # Fewer points than requested = we're at the end
            break
        offset = points[-1]["id"]

    logger.info(f"  DONE: {collection}")
    logger.info(f"  Re-embedded: {reembedded}, Skipped: {skipped}, Errors: {errors}")
    logger.info(f"  Estimated tokens used: ~{total_tokens:,}")

    return {"reembedded": reembedded, "skipped": skipped, "errors": errors, "tokens": total_tokens}


def main():
    parser = argparse.ArgumentParser(description="Re-embed Qdrant points to voyage-4")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually re-embed")
    parser.add_argument("--collection", type=str, default=None, help="Specific collection (default: all)")
    args = parser.parse_args()

    collections = ["nexus", "nexus-test", "test-collection"]
    if args.collection:
        collections = [args.collection]

    logger.info(f"Re-embedding with model: {NEW_MODEL}")
    logger.info(f"Collections: {collections}")
    logger.info(f"Dry run: {args.dry_run}")
    logger.info("")

    # First, verify voyage-4 works
    try:
        test_vec = voyage_embed_batch(["test"])
        logger.info(f"Voyage-4 test: OK ({len(test_vec[0])}d)")
    except Exception as e:
        logger.error(f"Voyage-4 test failed: {e}")
        sys.exit(1)

    logger.info("")

    results = {}
    for col in collections:
        result = reembed_collection(col, dry_run=args.dry_run)
        if result:
            results[col] = result
        logger.info("")

    # Summary
    logger.info("=== SUMMARY ===")
    for col, r in results.items():
        logger.info(f"  {col}: {r['reembedded']} re-embedded, {r['skipped']} skipped, {r['errors']} errors, ~{r['tokens']:,} tokens")


if __name__ == "__main__":
    main()