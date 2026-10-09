#!/usr/bin/env python3
"""Detect a legacy (unnamed) collection and print the exact config entry needed.

Background
----------
Since the collection model moved into Qdrant as a NAMED vector, an existing
installation whose collection was created by an older version has an UNNAMED
vector space. Without a mapping the engine refuses to guess (that is
deliberate: same dimension, different model cannot be detected by Qdrant, so a
wrong guess silently mixes vector spaces). It stops with a clear error asking
for the BUILDER's fingerprint.

This tool answers that question for the user: it reads the embedding config
that was in use, derives the fingerprint, cross-checks it against the actual
vector size, and prints the exact `legacy_collections` line to paste.

It never writes to the collection and never changes any config.

Usage:
    python3 scripts/detect-legacy-collection.py                 # auto (config)
    python3 scripts/detect-legacy-collection.py --collection X
    python3 scripts/detect-legacy-collection.py --backend ollama \
        --model qwen3-embedding:0.6b --collection nexus
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

QDRANT_URL = os.environ.get("NEXUS_QDRANT_URL", "http://localhost:6333")


def _load_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def config_candidates() -> list[Path]:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return [home / "nexus" / "config.json", home / "config.json",
            Path.home() / ".nexus-memory" / "config.json"]


def read_configured_values() -> dict:
    """Collect what the installs currently declare (first hit wins)."""
    out: dict[str, str] = {}
    for path in config_candidates():
        cfg = _load_json(path)
        if not cfg:
            continue
        out.setdefault("collection", cfg.get("collection_name", ""))
        out.setdefault("provider", cfg.get("embedding_provider",
                                           cfg.get("provider", "")))
        out.setdefault("model", cfg.get("embedding_model", cfg.get("model", "")))
    out.setdefault("provider", os.environ.get("NEXUS_EMBEDDING_PROVIDER", ""))
    out.setdefault("model", os.environ.get("NEXUS_EMBEDDING_MODEL", ""))
    out.setdefault("collection", os.environ.get("NEXUS_COLLECTION", "nexus"))
    return out


def fingerprint(backend: str, model: str, dim: int) -> str:
    """The library's own fingerprint function — never a re-implementation.

    A hand-written copy here produced ``qwen3_embedding_0_6b`` where the library
    builds ``qwen3-embedding_0_6b`` (hyphens are preserved, dots become
    underscores). That mismatch is exactly the silent vector-space mix the
    mapping exists to prevent, so the real function is imported instead.
    """
    try:
        sys.path.insert(0, str(_repo_root()))
        from nexus_memory.embeddings import vector_fingerprint
        return vector_fingerprint(backend, model, dim)
    except Exception:
        # Last resort only (library not importable): mirror the documented rule.
        def _t(tok: str) -> str:
            tok = tok.lower()
            tok = "".join(c if (c.isalnum() or c in "-_") else "_" for c in tok)
            while "__" in tok:
                tok = tok.replace("__", "_")
            return tok.strip("_")
        return f"{_t(backend)}__{_t(model)}__{dim}"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def qdrant_get(path: str) -> dict:
    import urllib.request
    req = urllib.request.Request(f"{QDRANT_URL}{path}")
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.load(resp)


def inspect(collection: str) -> dict:
    data = qdrant_get(f"/collections/{collection}")
    result = data.get("result", {})
    vectors = (result.get("config", {}).get("params", {}) or {}).get("vectors")
    points = result.get("points_count", 0)
    if not isinstance(vectors, dict):
        return {"exists": True, "named": None, "size": None, "points": points}
    if "size" in vectors:
        return {"exists": True, "named": None, "size": vectors.get("size"),
                "points": points}
    names = list(vectors.keys())
    return {"exists": True, "named": names, "size": None,
            "sizes": {n: vectors[n].get("size") for n in names},
            "points": points}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--collection", help="collection to inspect")
    ap.add_argument("--backend", help="embedding backend that BUILT it (voyage, ollama, ...)")
    ap.add_argument("--model", help="model that BUILT it (e.g. voyage-4, qwen3-embedding:0.6b)")
    ap.add_argument("--dim", type=int, help="vector size (read from the collection if omitted)")
    args = ap.parse_args()

    cfg = read_configured_values()
    collection = args.collection or cfg["collection"] or "nexus"

    try:
        info = inspect(collection)
    except Exception as exc:
        print(f"Kann '{collection}' nicht lesen ({exc}).\n"
              f"Laeuft Qdrant unter {QDRANT_URL}?")
        return 1

    print(f"Sammlung      : {collection}")
    print(f"Punkte        : {info['points']}")
    if info.get("named"):
        print(f"Vektorraum    : BENANNT {info['named']}")
        print("\nNichts zu tun — diese Sammlung traegt ihren Einbetter bereits "
              "im Namen. Kein legacy_collections-Eintrag noetig.")
        return 0
    if info.get("size") is None:
        print("Vektorraum    : (unbekannt)")
        return 1

    dim = info["size"]
    print(f"Vektorraum    : anonym, {dim} Dimensionen")
    if info["points"] == 0:
        print("\nDie Sammlung ist LEER — sie wird beim naechsten Start automatisch "
              "mit dem benannten Vektorraum neu angelegt. Nichts zu tun.")
        return 0

    backend = args.backend or cfg.get("provider") or ""
    model = args.model or cfg.get("model") or ""
    if not backend or not model:
        print("\nBackend/Modell unbekannt. Bitte angeben, WER die Sammlung GEBAUT hat "
              "(nicht wer sie jetzt nutzen soll):\n"
              "  python3 scripts/detect-legacy-collection.py --backend voyage --model voyage-4")
        return 1

    fp = fingerprint(backend, model, args.dim or dim)
    print(f"Erwartet      : {backend} / {model}")
    if args.dim and args.dim != dim:
        print(f"WARNUNG       : du hast {args.dim} Dimensionen angegeben, die Sammlung "
              f"haelt aber {dim}. Das Mapping waere falsch.")

    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    print("\nEintragen in " + str(home / "nexus" / "config.json") + ":\n")
    print(json.dumps({"legacy_collections": {collection: fp}}, indent=2,
                     ensure_ascii=False))
    print("\nDanach den Dienst neu starten. Der Wert beschreibt den ERZEUGER der "
          "Sammlung,\nnicht den Wunsch fuer die Zukunft — eine falsche Angabe mischt "
          "Vektorraeume still,\nsobald die Dimension zufaellig passt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
