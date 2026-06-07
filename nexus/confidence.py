"""Grounding Scoring for RAG — 4-Signal Method.

Evaluates how trustworthy a generated answer is based on the retrieved
chunks. Does not modify the existing pipeline.

**Why Grounding?**
Stanford CS229 (Yann Dubois) shows: SFT (Supervised Fine-Tuning) trains
models to give plausible-sounding answers even if they never learned the
facts during pre-training. The result: hallucination.
Grounding is the countermeasure — it checks whether the answer is actually
supported by the retrieved facts, not just whether it sounds good.

Signals:
1. **similarity** — Query-Embedding ↔ Chunk-Embeddings (max cosine)
2. **dominance**  — How much does everything rely on a single top chunk?
3. **grounding**  — Answer-Embedding ↔ Chunk-Embeddings (max cosine)
4. **coverage**   — Chunk diversity / query breadth covered?

Usage:
    from nexus.confidence import ConfidenceScorer
    scorer = ConfidenceScorer()
    report = scorer.evaluate(query="What is Nexus?", answer="Nexus is...")
    print(report.json())
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

from nexus.config import get_collection

_logger = logging.getLogger(__name__)

# ── Optional dependencies ──────────────────────────────────────────

HAS_REQUESTS = False
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    pass

HAS_VOYAGE = False
try:
    import voyageai
    HAS_VOYAGE = True
except ImportError:
    pass

HAS_SKLEARN = False
try:
    import numpy as np
    from sklearn.metrics.pairwise import cosine_similarity
    HAS_SKLEARN = True
except ImportError:
    pass


# ── Data classes ───────────────────────────────────────────────────


@dataclass
class SignalScores:
    """The five individual signals, each 0.0 – 1.0."""
    similarity: float = 0.0
    dominance: float = 0.0
    grounding: float = 0.0
    coverage: float = 0.0
    factual: float = 0.0


@dataclass
class GroundingReport:
    """Complete Grounding Report for one evaluate() run."""
    query: str = ""
    answer: str = ""
    signals: SignalScores = field(default_factory=SignalScores)
    grounding: float = 0.0
    label: str = ""
    num_chunks: int = 0
    top_chunk_score: float = 0.0
    chunk_count: int = 0
    error: Optional[str] = None

    def json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


# ── Helper: Cosine Similarity ──────────────────────────────────────


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    if HAS_SKLEARN:
        return float(cosine_similarity([a], [b])[0][0])
    # NumPy-free fallback
    dot = sum(ai * bi for ai, bi in zip(a, b))
    norm_a = math.sqrt(sum(ai * ai for ai in a))
    norm_b = math.sqrt(sum(bi * bi for bi in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ── Embedding ──────────────────────────────────────────────────────


def _embed(texts: list[str], provider: str = "voyage") -> Optional[list[list[float]]]:
    """Embed a list of texts via Voyage, sentence-transformers, or Ollama.

    Args:
        texts: List of texts to embed.
        provider: "voyage" (512d), "sentence-transformers" (384d) or "ollama" (768d).

    Returns:
        List of embedding vectors or None on error.
    """
    if provider == "voyage":
        if not HAS_VOYAGE:
            _logger.warning("voyageai not installed")
            return None
        try:
            client = voyageai.Client()
            result = client.embed(texts, model="voyage-3-lite", input_type="document")
            return result.embeddings
        except Exception as e:
            _logger.warning(f"Voyage embedding failed: {e}")
            return None

    elif provider == "sentence-transformers":
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer("all-MiniLM-L6-v2")
            return model.encode(texts).tolist()
        except Exception as e:
            _logger.warning(f"sentence-transformers embedding failed: {e}")
            return None

    elif provider == "ollama":
        if not HAS_REQUESTS:
            return None
        try:
            r = requests.post(
                "http://localhost:11434/api/embed",
                json={"model": "nomic-embed-text", "input": texts},
                timeout=30,
            )
            data = r.json()
            return data.get("embeddings", None)
        except Exception as e:
            _logger.warning(f"Ollama embedding failed: {e}")
            return None

    else:
        _logger.warning(f"Unknown embedding provider: {provider}")
        return None


# ── Qdrant-Abfrage ─────────────────────────────────────────────────


def _fetch_chunks(
    query_embedding: list[float],
    qdrant_host: str = "localhost",
    qdrant_port: int = 6333,
    collection: Optional[str] = None,
    top_k: int = 5,
) -> list[dict]:
    """Fetch top-K chunks from Qdrant (with full payload)."""
    collection = get_collection(collection)
    if not HAS_REQUESTS:
        return []
    try:
        url = f"http://{qdrant_host}:{qdrant_port}/collections/{collection}/points/search"
        r = requests.post(
            url,
            json={
                "vector": query_embedding,
                "limit": top_k,
                "with_payload": True,
                "filter": {
                    "must": [{"key": "type", "match": {"value": "memory"}}]
                },
            },
            timeout=10,
        )
        results = []
        for point in r.json().get("result", []):
            payload = point.get("payload", {})
            results.append({
                "id": str(point.get("id", "")),
                "score": point.get("score", 0.0),
                "text": payload.get("content", ""),
            })
        return results
    except Exception as e:
        _logger.warning(f"Qdrant search failed: {e}")
        return []


# ── Grounding Scorer ──────────────────────────────────────────────


class GroundingScorer:
    """Evaluates the trustworthiness of a RAG answer.

    Uses four signals:
    1. similarity  — Query↔Chunk: Does the best chunk match the question?
    2. dominance   — Chunk distribution: One dominant chunk or many?
    3. grounding   — Answer↔Chunk: Does the answer actually use the chunks?
    4. coverage    — Chunk↔Query: Do the chunks cover the question breadth?
    """

    def __init__(
        self,
        embed_provider: str = "voyage",
        qdrant_host: str = "localhost",
        qdrant_port: int = 6333,
        collection: Optional[str] = None,
        top_k: int = 5,
    ):
        collection = get_collection(collection)
        self.embed_provider = embed_provider
        self.qdrant_host = qdrant_host
        self.qdrant_port = qdrant_port
        self.collection = collection
        self.top_k = top_k

    # ─ Public API ──────────────────────────────────────────────────

    def evaluate(
        self,
        query: str,
        answer: str,
        chunks: Optional[list[dict]] = None,
    ) -> ConfidenceReport:
        """Full grounding evaluation.

        Args:
            query: The original user question.
            answer: The generated answer.
            chunks: Optional — pre-retrieved chunks.
                    If None, fetched from Qdrant.

        Returns:
            GroundingReport with all signals.
        """
        report = GroundingReport(query=query, answer=answer)

        # Step 1: Embed query
        q_emb = _embed([query], provider=self.embed_provider)
        if q_emb is None:
            report.error = "Embedding failed"
            return report
        q_emb = q_emb[0]

        # Step 2: Fetch chunks (if not provided)
        if chunks is None:
            chunks = _fetch_chunks(
                q_emb,
                qdrant_host=self.qdrant_host,
                qdrant_port=self.qdrant_port,
                collection=self.collection,
                top_k=self.top_k,
            )

        if not chunks:
            report.error = "No chunks found"
            return report

        report.num_chunks = len(chunks)
        report.top_chunk_score = chunks[0].get("score", 0.0) if chunks else 0.0

        # Step 3: Embed chunk texts
        chunk_texts = [c.get("text", "") for c in chunks if c.get("text")]
        if not chunk_texts:
            report.error = "Chunks have no text"
            return report

        chunk_embs = _embed(chunk_texts, provider=self.embed_provider)
        if chunk_embs is None:
            report.error = "Chunk embedding failed"
            return report

        chunk_scores = [c.get("score", 0.0) for c in chunks]

        # Step 4: Compute five signals
        signals = SignalScores()
        signals.similarity = self._signal_similarity(q_emb, chunk_embs, chunk_scores)
        signals.dominance = self._signal_dominance(chunk_scores)
        signals.coverage = self._signal_coverage(q_emb, chunk_embs)

        # Grounding braucht Antwort-Embedding
        a_emb = _embed([answer], provider=self.embed_provider)
        if a_emb:
            signals.grounding = self._signal_grounding(a_emb[0], chunk_embs)
            # Factual check: lexical overlap (protects against hallucinations)
            signals.factual = self._signal_factual(answer, chunk_texts)

        report.signals = signals

        # Step 5: Aggregate grounding + label
        report.grounding = self._aggregate(signals)
        report.chunk_count = len(chunks)
        report.label = self._label(report.grounding)

        return report

    @staticmethod
    def _label(grounding: float) -> str:
        """Human-readable label for the grounding score."""
        if grounding >= 0.8:
            return "🟢 Very high"
        elif grounding >= 0.6:
            return "🟡 High"
        elif grounding >= 0.4:
            return "🟠 Medium"
        elif grounding >= 0.2:
            return "🔴 Low"
        else:
            return "⛔ Very low"

    # ─ Individual signals ──────────────────────────────────────────

    @staticmethod
    def _signal_similarity(
        query_emb: list[float],
        chunk_embs: list[list[float]],
        chunk_scores: list[float],
    ) -> float:
        """Signal 1: Similarity between query and chunks.

        Takes the highest cosine score between query and chunks,
        weighted by score dominance. If the top chunk has a
        high cosine value → high similarity.
        """
        if not chunk_embs:
            return 0.0
        similarities = [_cosine_sim(query_emb, ce) for ce in chunk_embs]
        # Maximum + leichter Boost durch Qdrant-Score
        max_sim = max(similarities) if similarities else 0.0
        qdrant_factor = min(chunk_scores[0] / 0.8, 1.0) if chunk_scores else 0.0
        return min((max_sim * 0.7 + qdrant_factor * 0.3), 1.0)

    @staticmethod
    def _signal_dominance(chunk_scores: list[float]) -> float:
        """Signal 2: Chunk dominance.

        Measures how much the semantic mass concentrates on the
        top chunk. Formula:
            dominance = (top_score / sum(all_scores)) ^ 0.5

        High dominance (0.7–1.0) = answer relies heavily on
        ONE chunk → good for factual questions.
        Low dominance (0.0–0.4) = even distribution
        → good for synthesizing answers.
        """
        if not chunk_scores or sum(chunk_scores) == 0:
            return 0.0
        ratio = chunk_scores[0] / sum(chunk_scores)
        # Square root — prevents moderate dominance from being penalised too harshly
        return math.sqrt(ratio)

    # Technical named entities for the factual signal
    _TECH_ENTITIES = {
        # Produkte & Frameworks
        "nexus", "qdrant", "voyage", "ollama", "bm25", "gpt", "claude",
        "gemini", "rag", "hermes", "openclaw", "whisper", "yt-dlp", "twikit",
        "github", "discord", "telegram", "docker", "python",
        # Konzepte
        "embedding", "token", "transformer", "attention", "finetune",
        "pretrain", "rlhf", "sft", "dpo", "ppo", "lora", "quantization",
        "quantization", "vector", "cosine", "similarity",
        # Fachbegriffe
        "grounding", "provenance", "hallucination", "chunk", "retrieval",
        "pipeline", "latency", "throughput", "inference",
        # Spezifisch
        "karpathy", "stanford", "cs229", "scaling", "chinchilla",
    }

    @staticmethod
    def _signal_factual(
        answer: str,
        chunk_texts: list[str],
    ) -> float:
        """Signal 5: Named Entity Matching — protects against hallucinations.

        Extracts technical named entities from the answer and checks
        whether they appear in the chunk texts. Recognizes terms like
        Voyage, Qdrant, BM25, GPT, RAG — not just simple words.

        Low value = answer uses technical terms not present in any
        chunk source text.
        """
        if not chunk_texts or not answer:
            return 0.0

        ans_lower = answer.lower()
        chunk_all_lower = " ".join(ct.lower() for ct in chunk_texts)

        # Entities in der Antwort finden
        ans_entities = set()
        for entity in GroundingScorer._TECH_ENTITIES:
            if entity in ans_lower:
                ans_entities.add(entity)

        if not ans_entities:
            return 1.0  # Keine technischen Begriffe → neutral

        # Check which entities also appear in chunks
        matched = sum(1 for e in ans_entities if e in chunk_all_lower)
        score = matched / len(ans_entities)

        # Bonus: Wenn alle Entities matched → 1.0
        # If none → 0.0, otherwise linear interpolation
        return round(min(score, 1.0), 4)

    @staticmethod
    def _signal_grounding(
        answer_emb: list[float],
        chunk_embs: list[list[float]],
    ) -> float:
        """Signal 3: Grounding — How much does the answer rely on chunks?

        Embeds the generated answer and compares it with the
        chunk embeddings. The maximum cosine score shows: the answer
        semantically overlaps with at least one chunk.

        Low grounding = answer mostly relies on LLM parametric knowledge.
        """
        if not chunk_embs:
            return 0.0
        similarities = [_cosine_sim(answer_emb, ce) for ce in chunk_embs]
        return max(similarities) if similarities else 0.0

    @staticmethod
    def _signal_coverage(
        query_emb: list[float],
        chunk_embs: list[list[float]],
    ) -> float:
        """Signal 4: Coverage — How well do the chunks cover the question?

        Measures the semantic distance between query and ALL chunks.
        Low std_dev + high mean_similarity = chunks cover
        the query breadth well.

        Idea: The more chunks have high similarity to the query,
        the more aspects of the question are covered.
        """
        if not chunk_embs or len(chunk_embs) < 1:
            return 0.0
        similarities = [_cosine_sim(query_emb, ce) for ce in chunk_embs]
        mean_sim = sum(similarities) / len(similarities)
        # Bonus: more chunks = more coverage (logarithmic, avoids overweighting)
        count_bonus = min(math.log2(len(chunk_embs) + 1) / 3.0, 1.0)
        return min((mean_sim * 0.7 + count_bonus * 0.3), 1.0)

    # ─ Aggregation ──────────────────────────────────────────────────

    @staticmethod
    def _aggregate(signals: SignalScores) -> float:
        """Compute overall grounding from the 5 individual signals.

        Weights:
        - similarity:  25% (Query-Chunk Fit)
        - dominance:   15% (Stability of chunk basis)
        - grounding:   25% (Answer-chunk semantic match)
        - factual:     20% (Word overlap, protects against hallucinations)
        - coverage:    15% (Breadth of coverage)
        """
        weights = {
            "similarity": 0.25,
            "dominance": 0.15,
            "grounding": 0.25,
            "factual":   0.20,
            "coverage":  0.15,
        }
        score = (
            signals.similarity * weights["similarity"]
            + signals.dominance * weights["dominance"]
            + signals.grounding * weights["grounding"]
            + signals.factual * weights["factual"]
            + signals.coverage * weights["coverage"]
        )
        return round(min(score, 1.0), 4)
