#!/usr/bin/env python3
"""Re-embed a Nexus collection with the local HuggingFace model.

Why: a collection's vectors come from one model and one model only. Switching
the embedding backend (here: Ollama -> HuggingFace) therefore means re-embedding
the whole store — mixing two vector spaces silently is exactly the failure this
project exists to prevent.

What it does:
  1. creates the TARGET collection with the HuggingFace fingerprint as its
     named vector (idempotent — an existing target is reused),
  2. scrolls the SOURCE collection in blocks,
  3. skips ids that are already present in the TARGET (restartable),
  4. sorts each block by text length and embeds in length-homogeneous groups
     (short texts batch 32, long texts batch 8) — padding is what makes a batch
     cost the longest text in it,
  5. upserts id + payload + new vector into the target (`wait=True`),
  6. prints progress with rates every 256 points and a phase-time summary.

Measured on the Mini (M4, 16 GB): 64 x 200 chars -> 32 texts/s, 8 x 4000 chars
-> 1.5 texts/s, peak MPS driver memory 7.7 GB. Long single texts are cheap; the
earlier 34 points/min came from unfavourable batching, not from the model.

The SOURCE collection is never written to. It stays as the rollback path until
someone deletes it on purpose.

Usage:
    python3 reembed_hf.py --source nexus_qwen --target nexus_hf [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
import time

from qdrant_client import QdrantClient
from qdrant_client import models as qm

sys.path.insert(0, "src")
from nexus_memory.embeddings import vector_fingerprint  # noqa: E402

QDRANT_URL = "http://localhost:6333"
HF_BACKEND = "sentence-transformers"
HF_MODEL = "Qwen/Qwen3-Embedding-0.6B"
DIM = 1024
SCROLL_BATCH = 256
BUFFER_TARGET = 2048
SHORT_LIMIT = 500        # characters: below this the text counts as "short"
BATCH_SHORT = 32
BATCH_LONG = 8
PROGRESS_EVERY = 256
# Text limit: median 156 characters, p99 7016. Only 94 points (0.23 %) are longer,
# some documents up to 140 000 characters. For similarity search the beginning
# carries the content; the limit is stated in the report.
TEXT_LIMIT = 4000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="nexus_qwen")
    ap.add_argument("--target", default="nexus_hf")
    ap.add_argument("--limit", type=int, default=0, help="0 = all points")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--buffer", type=int, default=BUFFER_TARGET)
    args = ap.parse_args()

    fp = vector_fingerprint(HF_BACKEND, HF_MODEL, DIM)
    print(f"Target fingerprint: {fp}", flush=True)

    client = QdrantClient(url=QDRANT_URL)
    existing = [c.name for c in client.get_collections().collections]
    if args.target not in existing:
        print(f"Creating '{args.target}' (named vector, {DIM}d, cosine) ...", flush=True)
        if not args.dry_run:
            client.create_collection(
                collection_name=args.target,
                vectors_config={fp: qm.VectorParams(size=DIM, distance=qm.Distance.COSINE)},
            )
    else:
        print(f"'{args.target}' already exists, reusing it.", flush=True)
    print(f"Source: {args.source} ({client.get_collection(args.source).points_count} points)",
          flush=True)

    model = None
    if not args.dry_run:
        from sentence_transformers import SentenceTransformer

        print(f"Loading {HF_MODEL} ...", flush=True)
        t0 = time.time()
        model = SentenceTransformer(HF_MODEL)
        print(f"  loaded in {time.time() - t0:.1f}s", flush=True)

    stats = {"scroll": 0.0, "skip": 0.0, "embed": 0.0, "upsert": 0.0}
    seen = written = skipped = already = 0
    started = time.time()
    next_report = PROGRESS_EVERY
    total_hint = int(args.limit or (client.get_collection(args.source).points_count or 0))
    buffer: list = []

    def embed_and_store(items: list) -> None:
        """Vektoren für eine längengleiche Gruppe bilden und schreiben."""
        nonlocal written, next_report
        texts = [it[2] for it in items]
        batch_size = BATCH_SHORT if len(items[0][2]) <= SHORT_LIMIT else BATCH_LONG
        t0 = time.time()
        vectors = model.encode(
            texts, batch_size=min(batch_size, len(items)), show_progress_bar=False,
            normalize_embeddings=False,
        )
        stats["embed"] += time.time() - t0
        points = [
            qm.PointStruct(id=it[0], vector={fp: vec.tolist()}, payload=it[1])
            for it, vec in zip(items, vectors)
        ]
        t0 = time.time()
        # wait=True is mandatory: without it Qdrant answers "acknowledged" and
        # the points are not there on the next read (a silent failure).
        client.upsert(collection_name=args.target, points=points, wait=True)
        stats["upsert"] += time.time() - t0
        written += len(points)
        if written >= next_report:
            rate = written / max(time.time() - started, 1)
            left = max(total_hint - written, 0) / max(rate, 0.01) / 60
            print(f"  {written} geschrieben ({rate:.1f}/s, noch ~{left:.1f} min) …", flush=True)
            while written >= next_report:
                next_report += PROGRESS_EVERY

    def process_buffer() -> None:
        """Puffer: vorhandene überspringen, nach Länge sortieren, in Gruppen schreiben."""
        nonlocal skipped, already, written
        if not buffer:
            return
        t_block = time.time()
        n_block = len(buffer)
        t0 = time.time()
        ids = [it[0] for it in buffer]
        have = set()
        if not args.dry_run:
            have = {p.id for p in client.retrieve(
                collection_name=args.target, ids=ids, with_vectors=False)}
        stats["skip"] += time.time() - t0
        if have:
            already += len(have)
        todo = [it for it in buffer if it[0] not in have]
        buffer.clear()
        if not todo:
            return
        todo.sort(key=lambda it: len(it[2]))
        short = [it for it in todo if len(it[2]) <= SHORT_LIMIT]
        long = [it for it in todo if len(it[2]) > SHORT_LIMIT]
        if args.dry_run:
            written_dry = len(short) + len(long)
            print(f"  [dry-run] würde {len(short)} kurze + {len(long)} lange Texte schreiben",
                  flush=True)
            written += written_dry
            return
        for group, bs in ((short, BATCH_SHORT), (long, BATCH_LONG)):
            for k in range(0, len(group), bs):
                embed_and_store(group[k:k + bs])
        print(f"  [block] {n_block} gelesen, {len(todo)} neu ({len(short)} kurz / "
              f"{len(long)} lang), {len(have)} vorhanden | Blockdauer "
              f"{time.time() - t_block:.1f}s | kumulativ: "
              + ", ".join(f"{k} {v:.1f}s" for k, v in stats.items()), flush=True)

    offset = None
    while True:
        t0 = time.time()
        pts, offset = client.scroll(
            collection_name=args.source, limit=SCROLL_BATCH, offset=offset,
            with_payload=True, with_vectors=False,
        )
        stats["scroll"] += time.time() - t0
        for p in pts:
            seen += 1
            pl = p.payload or {}
            text = (pl.get("text") or pl.get("content") or "").strip()
            if not text:
                skipped += 1
                continue
            buffer.append((p.id, pl, text[:TEXT_LIMIT]))
            if len(buffer) >= args.buffer:
                process_buffer()
        if offset is None or (args.limit and seen >= args.limit):
            break
    process_buffer()

    dt = time.time() - started
    print(f"\nDone: {seen} read, {written} written, {skipped} without text, "
          f"{already} already present, {dt/60:.1f} min", flush=True)
    print("Phase times: " + ", ".join(f"{k} {v:.1f}s" for k, v in stats.items()), flush=True)
    if not args.dry_run:
        print("Target points per Qdrant:", client.get_collection(args.target).points_count,
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
