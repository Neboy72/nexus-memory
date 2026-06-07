"""Belief Drift Detection — Automated memory health monitoring.

Detects stale, contradictory, and orphaned memory entries before they
corrupt your agent's decision-making. Scores overall memory health 0-10.

Also provides:
- **Semantic Contradiction Detection** — finds pairs of memories with
  opposing semantics via embedding similarity.
- **Usage Tracking** — logs last-accessed timestamps and prunes unused
  memories.

Based on: "Why AI Agents Drift: Belief State Is the Real Bottleneck"

Usage:
    from nexus.health import DriftDetector

    detector = DriftDetector()
    report = detector.run()
    print(report.summary)       # "🟢 Score: 0.4/10"
    print(report.stale)         # list of stale entries
    print(report.json())        # full structured output
"""

from __future__ import annotations
import json, re, os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from nexus.config import get_collection

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ── Optional embedding dependencies ─────────────────────────────────────────

HAS_VOYAGE = False
try:
    import voyageai
    HAS_VOYAGE = True
except ImportError:
    pass

HAS_SENTENCE_TRANSFORMERS = False
try:
    from sentence_transformers import SentenceTransformer
    HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    pass

HAS_SKLEARN = False
try:
    from sklearn.metrics.pairwise import cosine_similarity
    import numpy as np
    HAS_SKLEARN = True
except ImportError:
    pass


# ── Usage tracking constants ────────────────────────────────────────────────

USAGE_FILE = Path.home() / ".hermes" / "nexus_usage.json"


# ── Stale Patterns ──────────────────────────────────────────────────────────
# These patterns flag entries that describe things as "active" or "running"
# when they should be "disabled" or "deprecated".

DEFAULT_STALE_PATTERNS = [
    (r"\bdeepseek\s+(v[0-9])?\s*(pro|flash)\s+(?:as|running|active|fallback)",
     "DeepSeek is listed as active, but may be disabled"),
    (r"\bnomic.embed\b.*\b(nomic|384)\b",
     "Embedding provider was Nomic — may have switched"),
    (r"\bollama\b(?!.*cloud).*(?:local|localhost|port\s+1)",
     "Ollama listed as local — may have moved to cloud"),
]

# ── Historical Exclusion ────────────────────────────────────────────────────
# Entries with these statuses are excluded from drift detection.

HISTORICAL_MARKER_STATUSES = ["HISTORICAL", "RESOLVED", "ARCHIVED", "FIXED"]


# ── Memory Expiry ───────────────────────────────────────────────────────────


class ExpiryPolicy(str, Enum):
    """How long a memory entry remains valid.

    The policy is stored in the Qdrant payload as ``expiry_policy``.
    If the field is missing, ``NORMAL`` is assumed.
    """
    STATIC = "static"      # Never expires — configs, paths, policies
    NORMAL = "normal"      # Standard expiry — 90 days
    VOLATILE = "volatile"  # Short-lived — 7 days


# Default shelf life (in days) per expiry policy
DEFAULT_EXPIRY_DAYS: dict[ExpiryPolicy, int | None] = {
    ExpiryPolicy.STATIC: None,     # None = never expires
    ExpiryPolicy.NORMAL: 90,
    ExpiryPolicy.VOLATILE: 7,
}


def compute_expires_at(
    created_at: datetime | None,
    last_confirmed_at: datetime | None,
    policy: ExpiryPolicy | str | None,
) -> tuple[datetime | None, bool]:
    """Compute when a memory expires and whether it's already expired.

    Args:
        created_at: When the memory was created (from ``timestamp`` payload field).
        last_confirmed_at: When the memory was last confirmed as still valid.
        policy: The expiry policy (``static``, ``normal``, or ``volatile``).
            If ``None`` or unrecognised, defaults to ``NORMAL``.

    Returns:
        Tuple of ``(expires_at, is_expired)``:
        - ``expires_at``: The calculated expiry datetime (``None`` for never-expiring).
        - ``is_expired``: ``True`` if the memory is already past its expiry date.
    """
    # Resolve policy
    if isinstance(policy, str):
        try:
            policy = ExpiryPolicy(policy)
        except ValueError:
            policy = ExpiryPolicy.NORMAL
    if policy is None:
        policy = ExpiryPolicy.NORMAL

    # STATIC never expires
    if policy == ExpiryPolicy.STATIC:
        return None, False

    # Determine the anchor date: last_confirmed_at if available, else created_at
    anchor = last_confirmed_at or created_at
    if anchor is None:
        # No anchor → treat as already expired (unknown age is unsafe)
        return datetime.now(timezone.utc), True

    # Calculate expiry
    days = DEFAULT_EXPIRY_DAYS.get(policy)
    if days is None:
        return None, False  # shouldn't happen, but be safe

    expires_at = anchor + timedelta(days=days)
    is_expired = datetime.now(timezone.utc) > expires_at
    return expires_at, is_expired


@dataclass
class DriftReport:
    """Structured drift detection report.

    v1.8.0+: DriftDetector is ADVISORY only — it never modifies data.
    ``promote_suggestions``, ``deprecate_suggestions``, and
    ``rollback_suggestions`` contain recommended actions for manual review.
    """
    total_entries: int = 0
    stale: list[dict] = field(default_factory=list)
    old: list[dict] = field(default_factory=list)
    expired: list[dict] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    score: float = 0.0
    contradictions: list[dict] = field(default_factory=list)
    excluded_count: int = 0

    # -- Advisory suggestions (v1.8.0+) --
    promote_suggestions: list[dict] = field(default_factory=list)
    deprecate_suggestions: list[dict] = field(default_factory=list)
    rollback_suggestions: list[dict] = field(default_factory=list)

    @property
    def summary(self) -> str:
        s = self.score
        emoji = "🟢" if s < 1 else "🟡" if s < 3 else "🔴"
        parts = [f"{emoji} Score: {s:.1f}/10"]
        if self.promote_suggestions:
            parts.append(f"  [{len(self.promote_suggestions)} promote suggestion(s)]")
        if self.deprecate_suggestions:
            parts.append(f"  [{len(self.deprecate_suggestions)} deprecate suggestion(s)]")
        if self.rollback_suggestions:
            parts.append(f"  [{len(self.rollback_suggestions)} rollback suggestion(s)]")
        return "".join(parts)

    def json(self) -> str:
        return json.dumps({
            "total_entries": self.total_entries,
            "stale_count": len(self.stale),
            "old_count": len(self.old),
            "expired_count": len(self.expired),
            "mismatches": self.mismatches,
            "contradictions": len(self.contradictions),
            "excluded_count": self.excluded_count,
            "score": self.score,
            "promote_suggestions": len(self.promote_suggestions),
            "deprecate_suggestions": len(self.deprecate_suggestions),
            "rollback_suggestions": len(self.rollback_suggestions),
        }, indent=2)


class DriftDetector:
    """Detects belief drift in Nexus memory entries.

    Also provides semantic contradiction detection and usage tracking
    for pruning unused memories.
    """

    def __init__(
        self,
        qdrant_host: str = "localhost",
        qdrant_port: int = 6333,
        collection_name: Optional[str] = None,
        stale_patterns: list[tuple[str, str]] | None = None,
        old_threshold_days: int = 90,
    ):
        collection_name = get_collection(collection_name)
        if not HAS_REQUESTS:
            raise ImportError("requests is required: pip install requests")

        self.qdrant_url = f"http://{qdrant_host}:{qdrant_port}"
        self.collection = collection_name
        self.stale_patterns = stale_patterns or DEFAULT_STALE_PATTERNS
        self.old_threshold = timedelta(days=old_threshold_days)
        # Expiry is stateless — uses compute_expires_at() directly

    # ── Private helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _is_excluded(payload: dict) -> bool:
        """Check if an entry should be excluded from drift detection.

        Entries whose ``status`` metadata field matches one of the
        :const:`HISTORICAL_MARKER_STATUSES` are excluded.

        Args:
            payload: The point's payload dict.

        Returns:
            ``True`` if the entry should be skipped.
        """
        status = payload.get("status", "")
        return status in HISTORICAL_MARKER_STATUSES

    def _scroll_all(self) -> list[dict]:
        """Pull all points from Qdrant."""
        points = []
        offset = None
        while True:
            body = {"limit": 100, "with_payload": True}
            if offset:
                body["offset"] = offset
            r = requests.post(
                f"{self.qdrant_url}/collections/{self.collection}/points/scroll",
                json=body, timeout=10,
            )
            data = r.json().get("result", {})
            batch = data.get("points", [])
            if not batch:
                break
            points.extend(batch)
            offset = data.get("next_page_offset")
            if not offset:
                break
        return points

    def _check_stale(self, content_raw: str | dict) -> list[str]:
        """Check content against stale patterns.

        Handles both:
        - legacy: ``content`` is a string directly
        - v1.8.0: ``content`` is a dict with ``content`` key inside
        """
        if isinstance(content_raw, dict):
            content = content_raw.get("content", json.dumps(content_raw))
        else:
            content = content_raw
        if not isinstance(content, str):
            content = str(content)
        findings = []
        for pattern, note in self.stale_patterns:
            if re.search(pattern, content, re.IGNORECASE):
                findings.append(note)
        return findings

    @staticmethod
    def _check_expiry(payload: dict) -> tuple[datetime | None, bool]:
        """Check if a memory payload has expired.

        Reads ``valid_until``, ``expiry_policy``, ``last_confirmed_at``,
        and ``timestamp`` from the payload.

        ``valid_until`` takes precedence: if set, it overrides the
        expiry-policy based check (a memory with ``valid_until`` far in
        the future won't be flagged as expired even if its anchor is old,
        and one with ``valid_until`` in the past is expired regardless
        of policy).

        Returns:
            Tuple of ``(expires_at, is_expired)``.
        """
        # Check valid_until override first
        valid_until_str = payload.get("valid_until")
        if valid_until_str:
            try:
                valid_until = datetime.fromisoformat(
                    valid_until_str.replace("Z", "+00:00")
                )
                now = datetime.now(timezone.utc)
                if now > valid_until:
                    # Past valid_until → expired regardless of policy
                    return valid_until, True
                # Future valid_until → not expired, skip policy check
                return valid_until, False
            except (ValueError, TypeError):
                pass  # fall through to policy-based check

        # Policy-based expiry
        created = payload.get("timestamp")
        created_at = None
        if created:
            try:
                created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        last_confirmed = payload.get("last_confirmed_at")
        last_confirmed_at = None
        if last_confirmed:
            try:
                last_confirmed_at = datetime.fromisoformat(last_confirmed.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        policy = payload.get("expiry_policy")
        return compute_expires_at(created_at, last_confirmed_at, policy)

    # ── Embedding helpers ───────────────────────────────────────────────────

    def _get_embedder(self):
        """Return an embedding function or ``None`` if no embedder is available.

        Priority: Voyage (best quality) → sentence-transformers (local).
        """
        if HAS_VOYAGE:
            client = voyageai.Client()
            return lambda texts: client.embed(
                texts, model="voyage-3", input_type="document"
            ).embeddings
        if HAS_SENTENCE_TRANSFORMERS:
            model = SentenceTransformer("all-MiniLM-L6-v2")
            return lambda texts: model.encode(texts).tolist()
        return None

    def _compute_similarity(self, emb_a: list[float], emb_b: list[float]) -> float:
        """Compute cosine similarity between two embedding vectors."""
        if HAS_SKLEARN:
            return float(cosine_similarity([emb_a], [emb_b])[0][0])
        # Manual fallback
        import math
        dot = sum(a * b for a, b in zip(emb_a, emb_b))
        norm_a = math.sqrt(sum(a * a for a in emb_a))
        norm_b = math.sqrt(sum(b * b for b in emb_b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _parse_sentiment(text: str) -> float:
        """Heuristic sentiment score from -1 (negative) to +1 (positive).

        Simple word-match approach — counts positive vs negative words.
        """
        positive_words = {
            "active", "running", "working", "enabled", "success", "good",
            "fast", "high", "best", "great", "stable", "live", "online",
            "supported", "recommended", "improved", "fixed", "upgraded",
        }
        negative_words = {
            "disabled", "deprecated", "broken", "failed", "bad", "slow",
            "low", "worst", "unstable", "offline", "dead", "removed",
            "unsupported", "not_recommended", "issue", "bug", "error",
            "inactive", "stopped", "discontinued",
        }
        words = set(text.lower().split())
        pos = len(words & positive_words)
        neg = len(words & negative_words)
        total = pos + neg
        if total == 0:
            return 0.0
        return (pos - neg) / total

    # ── Main drift detection ───────────────────────────────────────────────

    def run(self) -> DriftReport:
        """Run full drift detection over all memories.

        Returns:
            DriftReport with score, stale entries, old entries, mismatches,
            contradictions, and excluded_count.
        """
        points = self._scroll_all()
        report = DriftReport(total_entries=len(points))

        now = datetime.now(timezone.utc)

        for p in points:
            payload = p.get("payload", {})

            # Skip historical / resolved / archived entries
            if self._is_excluded(payload):
                report.excluded_count += 1
                continue

            content = payload.get("content", "")
            # v1.8.0+: content may be a dict {content: "text", ...}
            if isinstance(content, dict):
                text_content = content.get("content", "")
            else:
                text_content = content
            if not text_content:
                text_content = f"{payload.get('user_content', '')} -> {payload.get('assistant_content', '')}"

            # Stale pattern check
            stale = self._check_stale(text_content)
            if stale:
                report.stale.append({
                    "id": str(p.get("id", "")),
                    "issues": stale,
                    "category": payload.get("category", "unknown"),
                })

            # Old content detection
            ts = payload.get("timestamp")
            if ts:
                try:
                    created = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    age = now - created
                    if age > self.old_threshold:
                        report.old.append({
                            "id": str(p.get("id", "")),
                            "age_days": age.days,
                            "category": payload.get("category", "unknown"),
                        })
                except (ValueError, TypeError):
                    pass

            # Expiry check
            expires_at, is_expired = self._check_expiry(payload)
            if is_expired:
                policy = payload.get("expiry_policy", "normal")
                # Determine what triggered expiry
                has_valid_until = bool(payload.get("valid_until"))
                report.expired.append({
                    "id": str(p.get("id", "")),
                    "expires_at": expires_at.isoformat() if expires_at else None,
                    "policy": policy,
                    "expiry_reason": "valid_until" if has_valid_until else "policy",
                    "category": payload.get("category", "unknown"),
                    "content_preview": (text_content or "")[:150],
                })

        # Drift score: weighted combination
        report.score = min(
            len(report.stale) * 0.4 +
            len(report.old) * 0.1 +
            len(report.expired) * 0.5 +
            len(report.mismatches) * 0.3,
            10.0,
        )

        # ── Contradiction detection MUST run BEFORE suggestions ────────────
        # rollback_suggestions depends on report.contradictions being populated
        if points:
            try:
                # Filter out excluded entries before contradiction detection
                active_points = [
                    p for p in points
                    if not self._is_excluded(p.get("payload", {}))
                ]
                if active_points:
                    report.contradictions = self.detect_contradictions(active_points)
            except Exception:
                pass

        # ── Advisory suggestions (v1.8.0+ — NEVER auto-apply) ─────────────
        # Expired entries -> suggest deprecation
        for exp in report.expired:
            report.deprecate_suggestions.append({
                "fact_id": exp.get("id", ""),
                "reason": f"Entry expired ({exp.get('policy', 'unknown')} policy, "
                          f"reason: {exp.get('expiry_reason', 'unknown')})",
                "content_preview": exp.get("content_preview", "")[:100],
            })

        # Stale pattern matches -> suggest deprecation
        for st in report.stale:
            report.deprecate_suggestions.append({
                "fact_id": st.get("id", ""),
                "reason": f"Stale pattern match: {'; '.join(st.get('issues', []))}",
                "category": st.get("category", "unknown"),
            })

        # Contradictions -> suggest review (NOW has data!)
        for c in report.contradictions:
            report.rollback_suggestions.append({
                "id_a": c.get("id_a", ""),
                "id_b": c.get("id_b", ""),
                "reason": f"Contradiction detected: {c.get('type', 'semantic')} "
                          f"(similarity: {c.get('similarity', 0):.2f})",
            })

        return report

    def run_from_texts(self, entries: list[dict]) -> DriftReport:
        """Run drift detection on a list of dicts (for testing / offline use).

        Each entry: {"id": str, "content": str, "timestamp": str|None,
                     "payload": dict|None}
        """
        report = DriftReport(total_entries=len(entries))
        now = datetime.now(timezone.utc)

        for entry in entries:
            payload = entry.get("payload", {})
            # Ensure timestamp is available in payload for expiry check
            if payload.get("timestamp") is None:
                payload = {**payload, "timestamp": entry.get("timestamp")}

            # Skip historical / resolved / archived entries
            if self._is_excluded(payload):
                report.excluded_count += 1
                continue

            content = entry.get("content", "")
            # v1.8.0+: content may be a dict {content: "text", ...}
            if isinstance(content, dict):
                text_content = content.get("content", "")
            else:
                text_content = content
            stale = self._check_stale(text_content)
            if stale:
                report.stale.append({
                    "id": entry.get("id", ""),
                    "issues": stale,
                })

            ts = entry.get("timestamp")
            if ts:
                try:
                    created = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    age = now - created
                    if age > self.old_threshold:
                        report.old.append({
                            "id": entry.get("id", ""),
                            "age_days": age.days,
                        })
                except (ValueError, TypeError):
                    pass

            # Expiry check
            expires_at, is_expired = self._check_expiry(payload)
            if is_expired:
                policy = payload.get("expiry_policy", "normal")
                report.expired.append({
                    "id": entry.get("id", ""),
                    "expires_at": expires_at.isoformat() if expires_at else None,
                    "policy": policy,
                    "content_preview": (text_content or "")[:150],
                })

        report.score = min(
            len(report.stale) * 0.4 + len(report.old) * 0.1 +
            len(report.expired) * 0.5 + len(report.mismatches) * 0.3,
            10.0,
        )

        # Run contradiction detection on active entries only
        active_entries = [
            e for e in entries
            if not self._is_excluded(e.get("payload", {}))
        ]
        if active_entries:
            try:
                report.contradictions = self.detect_contradictions(active_entries)
            except Exception:
                pass

        return report

    # ── Feature 2: Semantic Contradiction Detection ─────────────────────────

    def detect_contradictions(
        self,
        memories: list[dict] | None = None,
        near_dup_threshold: float = 0.85,
        contradiction_threshold: float = 0.35,
        sentiment_weight: float = 2.0,
    ) -> list[dict]:
        """Find pairs of memories that semantically contradict each other.

        Uses embedding similarity to detect:
        - **Near-duplicates** (cosine similarity >= `near_dup_threshold`):
          Nearly identical memories that should probably be merged.
        - **Semantic contradictions** (cosine similarity >=
          `contradiction_threshold` with opposing sentiment): Memories
          that talk about the same concept but disagree — e.g. "X is
          enabled" vs "X is disabled".

        Works with Voyage embeddings, sentence-transformers, or
        gracefully returns empty results if neither is available.

        Args:
            memories: List of dicts with ``"id"`` and ``"content"`` keys.
                If ``None``, pulls all memories from Qdrant.
            near_dup_threshold: Cosine similarity threshold for flagging
                near-duplicate pairs (default 0.85).
            contradiction_threshold: Minimum cosine similarity for
                considering a pair a potential contradiction (default 0.35).
            sentiment_weight: How much the sentiment polarity difference
                is amplified in the contradiction score (default 2.0).

        Returns:
            List of dicts, each with:
                - ``type``: ``"near_duplicate"`` or ``"contradiction"``
                - ``id_a``, ``id_b``: The two memory IDs
                - ``content_a``, ``content_b``: Truncated content (first 200 chars)
                - ``similarity``: Cosine similarity score
                - ``sentiment_diff``: Absolute sentiment polarity difference
                - ``score``: Overall contradiction confidence

        Raises:
            RuntimeError: If no embedding provider is available.
        """
        embed_fn = self._get_embedder()
        if embed_fn is None:
            # Graceful degradation: return empty list
            return []

        if memories is None:
            if not HAS_REQUESTS:
                return []
            try:
                memories = self._scroll_all()
                # Filter out excluded entries (historical / resolved / archived)
                memories = [
                    m for m in memories
                    if not self._is_excluded(m.get("payload", {}))
                ]
            except Exception:
                return []

        if len(memories) < 2:
            return []

        # Extract texts and IDs
        texts = []
        ids = []
        for m in memories:
            if isinstance(m, str):
                texts.append(m)
                ids.append(str(len(ids)))
                continue
            payload = m.get("payload", m) if isinstance(m, dict) else {"content": str(m)}
            content = payload.get("content", "")
            if not content:
                content = f"{payload.get('user_content', '')} → {payload.get('assistant_content', '')}"
            if content:
                texts.append(content)
                if isinstance(m, dict) and "id" in m:
                    ids.append(str(m["id"]))
                elif isinstance(payload, dict) and "id" in payload:
                    ids.append(str(payload["id"]))
                else:
                    ids.append(str(len(ids)))

        if len(texts) < 2:
            return []

        # Compute embeddings
        try:
            embeddings = embed_fn(texts)
        except Exception:
            return []

        if len(embeddings) != len(texts):
            return []

        contradictions = []

        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                sim = self._compute_similarity(embeddings[i], embeddings[j])

                if sim >= near_dup_threshold:
                    contradictions.append({
                        "type": "near_duplicate",
                        "id_a": ids[i],
                        "id_b": ids[j],
                        "content_a": texts[i][:200],
                        "content_b": texts[j][:200],
                        "similarity": round(sim, 4),
                        "sentiment_diff": 0.0,
                        "score": round(sim, 4),
                    })
                elif sim >= contradiction_threshold:
                    # Check for opposing sentiment
                    sent_a = self._parse_sentiment(texts[i])
                    sent_b = self._parse_sentiment(texts[j])
                    sent_diff = abs(sent_a - sent_b)

                    # A real contradiction requires meaningful sentiment polarity
                    if sent_diff > 0.3:
                        contradictions.append({
                            "type": "contradiction",
                            "id_a": ids[i],
                            "id_b": ids[j],
                            "content_a": texts[i][:200],
                            "content_b": texts[j][:200],
                            "similarity": round(sim, 4),
                            "sentiment_diff": round(sent_diff, 4),
                            "score": round(sim * sentiment_weight * sent_diff, 4),
                        })

        # Sort by score descending
        contradictions.sort(key=lambda x: x["score"], reverse=True)
        return contradictions

    # ── Feature 3: Usage Tracking ───────────────────────────────────────────

    @staticmethod
    def _load_usage() -> dict[str, str]:
        """Load usage tracking data from disk.

        Returns:
            Dict mapping ``memory_id`` → ISO-8601 timestamp string.
        """
        if USAGE_FILE.exists():
            try:
                return json.loads(USAGE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    @staticmethod
    def _save_usage(usage: dict[str, str]) -> None:
        """Save usage tracking data to disk."""
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps(usage, indent=2))

    def track_usage(self, memory_id: str) -> dict:
        """Record that a memory was accessed at the current time.

        Args:
            memory_id: The ID of the memory being accessed.

        Returns:
            dict with ``memory_id`` and ``last_accessed`` timestamp.

        Example:
            >>> detector.track_usage("abc-123")
            {"memory_id": "abc-123", "last_accessed": "2026-05-18T16:00:00"}
        """
        usage = self._load_usage()
        now = datetime.now(timezone.utc).isoformat()
        usage[memory_id] = now
        self._save_usage(usage)
        return {"memory_id": memory_id, "last_accessed": now}

    def prune_unused(self, days: int = 90) -> list[str]:
        """Find memories not accessed in the specified number of days.

        Does NOT delete the memories from Qdrant — it only returns the
        list of memory IDs that are candidates for pruning.

        Args:
            days: Number of days of inactivity after which a memory is
                considered unused (default 90).

        Returns:
            List of memory IDs that have not been accessed in ``days`` days.

        Example:
            >>> unused = detector.prune_unused(days=90)
            >>> len(unused)
            12
        """
        usage = self._load_usage()
        if not usage:
            return []

        cutoff = datetime.now() - timedelta(days=days)
        unused = []

        for memory_id, ts_str in usage.items():
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts < cutoff:
                    unused.append(memory_id)
            except (ValueError, TypeError):
                # Cant parse timestamp — treat as unused
                unused.append(memory_id)

        return unused

    def get_usage_stats(self) -> dict:
        """Get aggregate usage tracking statistics.

        Returns:
            dict with keys:
                - ``total_tracked``: Number of memories with usage data
                - ``last_accessed``: ISO timestamp of most recent access
                - ``oldest_accessed``: ISO timestamp of oldest tracked access
        """
        usage = self._load_usage()
        if not usage:
            return {"total_tracked": 0}

        timestamps = list(usage.values())
        return {
            "total_tracked": len(usage),
            "last_accessed": max(timestamps) if timestamps else None,
            "oldest_accessed": min(timestamps) if timestamps else None,
        }


# ── Wikilink Orphan Detection ─────────────────────────────────────────────


def find_wikilink_orphans(workspace: str | None = None) -> list[dict]:
    """Find [[wikilinks]] in memory files that don't resolve to any file or heading.

    Backtick-aware: skips wikilinks inside inline code spans (no false
    positives from code examples).

    Checks these locations:
    - ``workspace/wiki/*.md`` — wiki entity files
    - ``workspace/MEMORY.md`` — headings in the main memory file
    - ``workspace/memory/202*.md`` — date-named memory files
    - ``~/ObsidianVault/Miosha/Wiki/entities/*.md`` — shared Obsidian wiki

    Args:
        workspace: Path to workspace directory. Defaults to
            ``~/.hermes/nexus-workspace``.

    Returns:
        List of dicts, each with ``target``, ``file``, ``line_num``, ``line``.
    """
    if workspace is None:
        workspace = os.path.expanduser("~/.hermes/nexus-workspace")

    wikilink_re = re.compile(r"\[\[([^|#\]]+)(?:[|#][^\]]*)?\]\]")
    orphans: list[dict] = []
    reported_targets: set[str] = set()

    # Collect all resolvable targets
    wiki_dir = os.path.join(workspace, "wiki")
    wiki_files: set[str] = set()
    if os.path.isdir(wiki_dir):
        wiki_files = {
            fn[:-3].lower()
            for fn in os.listdir(wiki_dir)
            if fn.endswith(".md")
        }

    memory_headings: set[str] = set()
    memory_file = os.path.join(workspace, "MEMORY.md")
    if os.path.exists(memory_file):
        try:
            with open(memory_file) as f:
                for line in f:
                    m = re.match(r"^(#{1,6})\s+(.+)", line)
                    if m:
                        memory_headings.add(m.group(2).strip().lower())
        except Exception:
            pass

    memory_dates: set[str] = set()
    memory_dir = os.path.join(workspace, "memory")
    if os.path.isdir(memory_dir):
        memory_dates = {
            fn[:-3].lower()
            for fn in os.listdir(memory_dir)
            if fn.endswith(".md")
        }

    # Also check shared Obsidian Wiki
    obsidian_wiki = os.path.expanduser("~/ObsidianVault/Miosha/Wiki/entities")
    if os.path.isdir(obsidian_wiki):
        wiki_files.update({
            fn[:-3].lower()
            for fn in os.listdir(obsidian_wiki)
            if fn.endswith(".md")
        })

    # Scan all memory files for wikilinks
    texts: dict[str, str] = {}
    if os.path.exists(memory_file):
        try:
            with open(memory_file) as f:
                texts["MEMORY.md"] = f.read()
        except Exception:
            pass
    if os.path.isdir(memory_dir):
        for fn in sorted(os.listdir(memory_dir)):
            if fn.endswith(".md"):
                try:
                    with open(os.path.join(memory_dir, fn)) as f:
                        texts[fn] = f.read()
                except Exception:
                    pass

    for fname, content in texts.items():
        for line_num, line in enumerate(content.splitlines(), 1):
            # Strip inline code first (backtick-aware)
            clean_line = re.sub(r"`[^`]+`", "", line)
            for match in wikilink_re.finditer(clean_line):
                target = match.group(1).strip()
                target_key = target.lower()
                if not target or target_key in reported_targets:
                    continue
                reported_targets.add(target_key)

                target_path = os.path.join(workspace, target)
                target_wiki_path = os.path.join(wiki_dir, f"{target}.md")
                found = (
                    target_key in wiki_files
                    or target_key in memory_headings
                    or target_key in memory_dates
                    or os.path.isfile(target_path)
                    or os.path.isfile(f"{target_path}.md")
                    or os.path.isfile(target_wiki_path)
                )
                if not found:
                    orphans.append({
                        "target": target,
                        "file": fname,
                        "line_num": line_num,
                        "line": line.strip()[:200],
                    })

    return orphans


def format_orphan_report(orphans: list[dict]) -> str:
    """Format orphan wikilink findings as a human-readable report.

    Args:
        orphans: List of orphans from :func:`find_wikilink_orphans`.

    Returns:
        Markdown-formatted report string.
    """
    lines = ["🔗 **Wikilink Orphan Check**", ""]
    if not orphans:
        lines.append("✅ All wikilinks resolve — no orphans found")
    else:
        lines.append(f"⚠️ Found {len(orphans)} orphan link(s):")
        for item in orphans:
            lines.append(f"  ⚠️ Orphan: [[{item['target']}]] — target not found")
            lines.append(f"    Referenced in **{item['file']}**:{item['line_num']}")
    lines.append("")
    return "\n".join(lines)
