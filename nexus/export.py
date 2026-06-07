"""
Nexus Memory Skill Export — Turn canonical facts into ready-to-use SKILL.md files.

v1.9.0: Search Nexus Memory → cluster related facts → generate Hermes-compatible
SKILL.md with frontmatter, steps, pitfalls, prerequisites, and verification.

Usage:
    # CLI
    nexus-export --skill "code-review" --deploy
    nexus-export --list

    # Python
    from nexus.export import export_skill, search_knowledge, list_topics
    result = export_skill("code-review", topic="code review patterns")
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from nexus.config import get_collection


# ── Search ──────────────────────────────────────────────────────────────────


def search_knowledge(
    topic: str,
    limit: int = 20,
    qdrant_host: str = "localhost",
    qdrant_port: int = 6333,
    collection_name: Optional[str] = None,
) -> list[dict]:
    """Search Nexus Memory for canonical facts related to *topic*.

    Uses hybrid search (BM25 + Vector + RRF) for relevance, then filters
    to canonical-only facts via CanonicalView (v1.8.0 lifecycle).

    Returns results ordered by RRF score, each with:
        id, rrf_score, content, category, tier, fact_id, version_id
    """
    collection_name = get_collection(collection_name)
    from nexus import nexus_search_hybrid

    raw = nexus_search_hybrid(
        query=topic,
        top_k=limit,
        qdrant_host=qdrant_host,
        qdrant_port=qdrant_port,
        collection_name=collection_name,
    )

    if not raw:
        return []

    results = []
    for r in raw:
        # Extract content — handle nested payload
        payload = r.get("payload", r)
        fact_id = payload.get("fact_id") or r.get("id", "")
        content = payload.get("content") or payload.get("text") or r.get("text", "")

        # Filter canonical-only (v1.8.0 lifecycle)
        status = payload.get("status", "canonical")  # pre-v1.8.0 = canonical
        if status not in ("canonical", "static"):
            continue

        # Filter out empty/noise
        if not content or len(content.strip()) < 10:
            continue

        results.append({
            "id": r.get("id", ""),
            "rrf_score": r.get("rrf_score", 0.0),
            "content": content.strip(),
            "category": (payload.get("category") or "fact").lower(),
            "tier": payload.get("tier", 1),
            "fact_id": fact_id,
            "version_id": payload.get("version_id", ""),
        })

    return results


# ── Clustering ──────────────────────────────────────────────────────────────


def cluster_facts(facts: list[dict]) -> dict[str, list[str]]:
    """Group facts into SKILL.md sections based on category and content signals.

    Returns dict with keys: steps, pitfalls, prerequisites, verification.
    """
    clusters: dict[str, list[str]] = {
        "steps": [],
        "pitfalls": [],
        "prerequisites": [],
        "verification": [],
    }

    for f in facts:
        content = f.get("content", "").strip()
        if not content:
            continue
        cat = (f.get("category") or "fact").lower()

        # Categorize by category field
        if cat in ("pattern", "procedure", "workflow", "step"):
            clusters["steps"].append(content)
        elif cat in ("lesson", "pitfall", "warning", "gotcha"):
            clusters["pitfalls"].append(content)
        elif cat in ("requirement", "prerequisite", "config", "setup", "install"):
            clusters["prerequisites"].append(content)
        elif cat in ("check", "verification", "test", "assertion", "validate"):
            clusters["verification"].append(content)

        # If category is generic "fact" or "decision", detect from content
        elif cat in ("fact", "decision", "pattern"):
            # Content-based heuristic
            # Pitfalls FIRST — negation & warning keywords override verification
            # (a fact saying "Don't use X — always check Y" is a pitfall, not verification)
            low = content.lower()
            if any(kw in low for kw in ("never", "don't", "avoid", "watch out", "⚠", "caution", "pitfall")):
                clusters["pitfalls"].append(content)
            elif any(kw in low for kw in ("verify", "check", "ensure", "validate", "test that", "assert")):
                clusters["verification"].append(content)
            elif any(kw in low for kw in ("need", "require", "prerequisite", "must have", "install", "setup")):
                clusters["prerequisites"].append(content)
            else:
                clusters["steps"].append(content)

    # Deduplicate
    for key in clusters:
        seen: set[str] = set()
        deduped: list[str] = []
        for item in clusters[key]:
            # Dedupe on normalized first 80 chars
            norm = item[:80].strip().lower()
            if norm not in seen:
                seen.add(norm)
                deduped.append(item)
        clusters[key] = deduped

    return clusters


# ── SKILL.md Generator ─────────────────────────────────────────────────────


SKILL_TEMPLATE = """---
name: {name}
description: >-
  {description}
version: 1.0.0
author: auto-exported
license: MIT
platforms: [agent]
tags: [{tags_str}]
metadata:
  hermes:
    tags: [{tags_str}]
    source: nexus-memory-export
    exported: {date}
    facts: [{fact_ids_str}]
---

# {title}

{overview}

## Prerequisites

{prerequisites}

## Steps

{steps}

## Pitfalls

{pitfalls}

## Verification

{verification}

---

*Auto-generated by Nexus Memory v1.9.0 Skill Export on {date}*
"""


def _format_section(items: list[str], default: str = "None yet documented.") -> str:
    """Format a section as bullet list or default text."""
    if not items:
        return default
    return "\n".join(f"- {item.strip()}" for item in items)


def _auto_describe(clusters: dict[str, list[str]]) -> str:
    """Generate a description from clustered content."""
    steps_count = len(clusters["steps"])
    pitfalls_count = len(clusters["pitfalls"])
    prereqs_count = len(clusters["prerequisites"])

    parts = []
    if steps_count:
        parts.append(f"{steps_count} documented steps")
    if pitfalls_count:
        parts.append(f"{pitfalls_count} known pitfalls")
    if prereqs_count:
        parts.append(f"{prereqs_count} prerequisites")

    if parts:
        return f"Skill with {', '.join(parts)}, auto-exported from Nexus Memory."
    return "Auto-exported skill from Nexus Memory canonical facts."


def _auto_tags(clusters: dict[str, list[str]], facts: list[dict]) -> list[str]:
    """Extract relevant tags from fact categories and content."""
    tags: set[str] = set()

    # Categories become tags
    for f in facts[:10]:
        cat = (f.get("category") or "").lower()
        if cat and cat != "fact":
            tags.add(cat)

    # Content-based tags
    for item in clusters.get("pitfalls", [])[:5]:
        low = item.lower()
        if "deprecated" in low or "outdated" in low:
            tags.add("deprecation")
        if "config" in low:
            tags.add("configuration")
        if "api" in low:
            tags.add("api")
        if "test" in low:
            tags.add("testing")
        if "security" in low or "auth" in low:
            tags.add("security")

    if not tags:
        tags.add("auto-exported")

    return sorted(tags)


def build_skill_md(
    name: str,
    clusters: dict[str, list[str]],
    facts: list[dict],
    description: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Build a complete SKILL.md string from clustered facts.

    Args:
        name: Skill name (lowercase, hyphens)
        clusters: Dict with steps, pitfalls, prerequisites, verification lists
        facts: Original fact list (for tag extraction)
        description: Optional override. Auto-generated if None.
        tags: Optional tag list. Auto-extracted if None.

    Returns:
        Complete SKILL.md content (frontmatter + body)
    """
    if description is None:
        description = _auto_describe(clusters)
    if tags is None:
        tags = _auto_tags(clusters, facts)

    title = name.replace("-", " ").replace("_", " ").title()
    tags_str = ", ".join(tags)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Fact IDs for traceability in frontmatter
    fact_ids = [f.get("fact_id") or f.get("id", "") for f in facts if f.get("fact_id") or f.get("id")]
    fact_ids_str = ", ".join(fact_ids[:20])

    # Overview — top 3 facts by RRF score as summary
    top_facts = [f.get("content", "") for f in facts[:3] if f.get("content")]
    overview_parts = top_facts[:2]  # keep it short, max 2
    overview = "\n".join(f"- {p.strip()[:200]}" for p in overview_parts) if overview_parts else "Auto-exported from Nexus Memory."

    return SKILL_TEMPLATE.format(
        name=name,
        description=description,
        tags_str=tags_str,
        title=title,
        overview=overview,
        prerequisites=_format_section(clusters["prerequisites"]),
        steps=_format_section(clusters["steps"]),
        pitfalls=_format_section(clusters["pitfalls"]),
        verification=_format_section(clusters["verification"]),
        date=date_str,
        fact_ids_str=fact_ids_str,
    )


# ── Main Export ──────────────────────────────────────────────────────────────


HERMES_SKILLS_DIR = os.path.expanduser("~/.hermes/skills")


def export_skill(
    name: str,
    topic: str | None = None,
    output_dir: str | None = None,
    deploy: bool = False,
    description: str | None = None,
    tags: list[str] | None = None,
    limit: int = 20,
    **search_kw: Any,
) -> dict:
    """Search, cluster, and export a skill from Nexus Memory.

    Args:
        name: Skill name (used for filename and frontmatter)
        topic: Search query. If None, uses *name* as topic.
        output_dir: Output directory. Ignored if *deploy* is True.
        deploy: If True, write directly to ``~/.hermes/skills/<name>/SKILL.md``.
        description: Optional description override.
        tags: Optional tag list override.
        limit: Max facts to fetch.
        **search_kw: Passed to ``search_knowledge()``.

    Returns:
        Dict with keys: name, topic, facts_found, steps, pitfalls, prerequisites,
        verification, output_path, skill_md
    """
    if topic is None:
        topic = name

    # 1. Search
    facts = search_knowledge(topic, limit=limit, **search_kw)

    # 2. Cluster
    clusters = cluster_facts(facts)

    # 3. Build SKILL.md
    skill_md = build_skill_md(name, clusters, facts, description=description, tags=tags)

    # 4. Determine output path
    if deploy:
        skill_dir = os.path.join(HERMES_SKILLS_DIR, name)
        os.makedirs(skill_dir, exist_ok=True)
        output_path = os.path.join(skill_dir, "SKILL.md")
    elif output_dir:
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"{name}.md")
    else:
        output_path = os.path.join(os.getcwd(), f"{name}.md")

    # 5. Write
    with open(output_path, "w") as f:
        f.write(skill_md)

    return {
        "name": name,
        "topic": topic,
        "facts_found": len(facts),
        "clusters": {k: len(v) for k, v in clusters.items()},
        "output_path": output_path,
        "deployed": deploy,
    }


def list_topics(
    qdrant_host: str = "localhost",
    qdrant_port: int = 6333,
    collection_name: Optional[str] = None,
    min_facts: int = 3,
    max_scan: int = 2000,
) -> list[dict]:
    """Discover categories with enough canonical facts for skill export.

    Groups canonical + legacy (pre-v1.8.0, no status) facts by category
    via paginated Qdrant scroll, returns categories with at least ``min_facts``
    entries.

    Uses the same inline canonical/legacy filtering as ``search_knowledge()``
    for consistent lifecycle handling across both search and discovery.

    Returns list of dicts: category, fact_count, sample_content.
    """
    collection_name = get_collection(collection_name)
    import requests

    # Paginated scroll — filter in Python for consistent legacy handling
    scroll_url = f"http://{qdrant_host}:{qdrant_port}/collections/{collection_name}/points/scroll"
    all_points: list[dict] = []
    scroll_offset: str | None = None
    page_size = 200

    while len(all_points) < max_scan:
        body: dict[str, Any] = {
            "limit": page_size,
            "with_payload": True,
        }
        if scroll_offset:
            body["offset"] = scroll_offset
        r = requests.post(scroll_url, json=body, timeout=30)
        r.raise_for_status()
        data = r.json().get("result", {})
        batch = data.get("points", [])
        if not batch:
            break
        all_points.extend(batch)
        scroll_offset = data.get("next_page_offset")
        if not scroll_offset:
            break

    # Same inline filter as search_knowledge(): missing status = canonical
    groups: dict[str, list[str]] = {}
    for point in all_points:
        pl = point.get("payload", {})
        status = pl.get("status", "canonical")
        if status not in ("canonical", "static"):
            continue
        cat = (pl.get("category") or "uncategorized").lower()
        raw_content = pl.get("content") or pl.get("text", "")
        # Handle v1.8.0 content format (dict with nested "content" key)
        if isinstance(raw_content, dict):
            content = raw_content.get("content", str(raw_content))
        else:
            content = str(raw_content) if raw_content else ""
        if not content or len(content.strip()) < 10:
            continue
        if cat not in groups:
            groups[cat] = []
        groups[cat].append(content.strip())

    # Build result list
    result = []
    for category, contents in sorted(groups.items()):
        if len(contents) >= min_facts:
            sample = contents[0][:120] if contents else ""
            result.append({
                "category": category,
                "fact_count": len(contents),
                "sample": sample,
            })

    return result


# ── CLI ─────────────────────────────────────────────────────────────────────


def cli_main() -> None:
    parser = argparse.ArgumentParser(
        description="Nexus Memory Skill Export — turn canonical facts into SKILL.md",
    )

    # Mutually exclusive modes
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="List exportable topics")
    mode.add_argument("--skill", type=str, help="Skill name to export")

    # Export options
    parser.add_argument("--topic", type=str, help="Search query (defaults to skill name)")
    parser.add_argument("--deploy", action="store_true", help=f"Write to {HERMES_SKILLS_DIR}/<name>/SKILL.md")
    parser.add_argument("--output", type=str, help="Output directory (ignored with --deploy)")
    parser.add_argument("--description", type=str, help="Override auto-generated description")
    parser.add_argument("--tags", type=str, help="Comma-separated tags (overrides auto-detection)")
    parser.add_argument("--limit", type=int, default=20, help="Max facts to fetch (default: 20)")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")

    args = parser.parse_args()

    if args.list:
        topics = list_topics(min_facts=3)
        if args.json:
            print(json.dumps(topics, indent=2))
        else:
            print(f"\n🧠 Exportable Topics ({len(topics)} found):\n")
            for t in topics:
                cat = t.get("category", "?")
                count = t.get("fact_count", 0)
                sample = t.get("sample", "")
                print(f"  • {cat:30s} ({count} facts)")
                if sample:
                    print(f"    ↳ {sample}")
                print()
        return

    # Export mode
    tags = [t.strip() for t in args.tags.split(",")] if args.tags else None

    result = export_skill(
        name=args.skill,
        topic=args.topic,
        output_dir=args.output,
        deploy=args.deploy,
        description=args.description,
        tags=tags,
        limit=args.limit,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        status_icon = "🗂️" if result.get("deployed") else "📄"
        print(f"\n{status_icon} Skill exported: {result.get('name', '?')}\n")
        print(f"  Topic:       {result.get('topic', '?')}")
        print(f"  Facts found: {result.get('facts_found', 0)}")
        clusters = result.get("clusters", {})
        print(f"  Clusters:    {clusters}")
        print(f"  Output:      {result.get('output_path', '?')}")


if __name__ == "__main__":
    cli_main()
