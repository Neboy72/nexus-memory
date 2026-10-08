"""Nexus Memory — Collection binding (Regel B).

The collection model is stored IN Qdrant as a named vector. This module is
the single place that decides how a collection is created or bound to an
embedding provider. It NEVER writes user config / JSON files.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from nexus_memory.embeddings import (
    CollectionModelUnavailable,
    EmbeddingProvider,
    vector_fingerprint,
)

logger = logging.getLogger(__name__)


class CollectionBinding:
    """Bind (or create) a Qdrant collection to the fingerprint of ``provider``.

    Implements Regel B:
      1. Read the collection.
      2. Does not exist -> create it with the named vector fingerprint.
      3. Read again, obtain the active vector name ``F``.
      4. Cases:
         * ``F == fingerprint`` -> use it.
         * ``F`` named, ``!= fingerprint``, collection empty -> recreate it
           loss-free with the new fingerprint.
         * ``F`` named, ``!= fingerprint``, collection not empty -> if the
           caller originally preferred ``auto`` and the locally matching provider
           for ``F`` is available, switch to that provider; otherwise hard
           error with instructions.
         * ``F`` unnamed (legacy) and not empty -> look up ``legacy_collections``
           config mapping ``{collection: fingerprint}``. Missing -> hard error.
           Present -> treat like named, but never pass ``using`` (legacy
           single-vector collection).
         * Qdrant unreachable -> hard error.

    The binding object is intentionally stateful only around the resolved vector
    name. All Qdrant writes happen inside ``ensure()``.
    """

    def __init__(
        self,
        client: QdrantClient,
        collection_name: str,
        provider: EmbeddingProvider,
        *,
        config_legacy_collections: Optional[dict[str, str]] = None,
        provider_factory: Optional[Any] = None,
    ) -> None:
        self.client = client
        self.collection_name = collection_name
        self.provider = provider
        self.config_legacy_collections = config_legacy_collections or {}
        # Injectable for tests: builds a provider for a given backend id.
        # Defaults to the real EmbeddingProvider. Lets the auto-switch path be
        # exercised without downloading a real model (it was untested before).
        self._provider_factory = provider_factory or (lambda backend: EmbeddingProvider(preferred=backend))
        self._vector_name: Optional[str] = None
        self._is_legacy: bool = False
        self._ensured: bool = False

    def _fingerprint(self) -> str:
        return vector_fingerprint(
            self.provider.backend, self.provider.model_name, self.provider.dim
        )

    def _require_provider(self) -> None:
        """Fail closed when no embedding provider is available.

        Creating a collection with the placeholder fingerprint ``none__none__384``
        would bind a bogus vector space; better to refuse and say why.
        """
        if getattr(self.provider, "available", True) is False:
            raise CollectionModelUnavailable(
                "No embedding provider is available, so the collection cannot be "
                "created or bound. Install a local backend "
                "(pip install sentence-transformers), start Ollama, or set the "
                "provider explicitly (NEXUS_EMBEDDING_PROVIDER + its key)."
            )

    def ensure(self) -> "CollectionBinding":
        """Bind or create the collection. Idempotent."""
        if self._ensured:
            return self

        self._require_provider()
        try:
            collections = [c.name for c in self.client.get_collections().collections]
        except Exception as exc:
            raise RuntimeError(
                f"Qdrant unreachable at binding time: {exc}. "
                "Check NEXUS_QDRANT_HOST/NEXUS_QDRANT_PORT."
            ) from exc

        exists = self.collection_name in collections
        if not exists:
            self._create_with_fingerprint()
            self._ensured = True
            return self

        info = self._get_vector_info()
        fp = self._fingerprint()

        if info.named is None:
            # Legacy unnamed collection
            if info.point_count == 0:
                # Empty legacy collection can be safely recreated as named.
                self._recreate_with_fingerprint()
                self._ensured = True
                return self
            self._bind_legacy(info)
            self._ensured = True
            return self

        if info.named == fp:
            self._vector_name = fp
            self._is_legacy = False
            logger.info(
                "Collection '%s' bound to vector '%s'", self.collection_name, fp
            )
            self._ensured = True
            return self

        # Named, but a different fingerprint.
        if info.point_count == 0:
            self._recreate_with_fingerprint()
            self._ensured = True
            return self

        # Non-empty, mismatched named collection.
        self._handle_mismatch(info.named, fp)
        self._ensured = True
        return self

    def vector_name(self) -> Optional[str]:
        """Active vector name, or ``None`` for legacy unnamed collections."""
        return self._vector_name

    def is_named(self) -> bool:
        return self._vector_name is not None and not self._is_legacy

    def is_legacy(self) -> bool:
        return self._is_legacy

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    class _VectorInfo:
        def __init__(
            self,
            named: Optional[str],
            point_count: int,
            size: Optional[int] = None,
        ) -> None:
            self.named = named
            self.point_count = point_count
            # Actual vector size of the collection (for the legacy cross-check).
            self.size = size

    def _size_of(self, vectors: Any) -> Optional[int]:
        """Actual vector size, whether the collection is named or unnamed."""
        if isinstance(vectors, dict):
            if "size" in vectors and "distance" in vectors:
                # Plain VectorParams serialised as a dict: unnamed collection.
                return int(vectors.get("size") or 0) or None
            first = next(iter(vectors.values()), None)
            return getattr(first, "size", None)
        return getattr(vectors, "size", None)

    def _get_vector_info(self) -> _VectorInfo:
        cfg = self.client.get_collection(self.collection_name).config.params
        vectors = cfg.vectors
        count = self.client.count(self.collection_name).count
        size = self._size_of(vectors)

        if isinstance(vectors, dict):
            names = [n for n in vectors.keys() if n not in ("size", "distance")]
            if len(names) == 1:
                return self._VectorInfo(named=names[0], point_count=count, size=size)
            if len(names) > 1:
                # Mixed vector spaces are not supported by the binding contract.
                # Pick the first and let the fingerprint check below fail or match.
                return self._VectorInfo(named=names[0], point_count=count, size=size)
            # Empty dict / plain VectorParams dict is degenerate -> legacy.
            return self._VectorInfo(named=None, point_count=count, size=size)

        # Unnamed / VectorParams
        return self._VectorInfo(named=None, point_count=count, size=size)

    def _create_with_fingerprint(self) -> None:
        fp = self._fingerprint()
        try:
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config={
                    fp: qmodels.VectorParams(
                        size=self.provider.dim,
                        distance=qmodels.Distance.COSINE,
                    )
                },
            )
            self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name="access_level",
                field_type=qmodels.PayloadSchemaType.KEYWORD,
            )
        except Exception as exc:
            # Race: another process may have created the collection in the
            # meantime. Do NOT return with an unbound name — that would let the
            # caller write a raw vector into a named collection (Qdrant rejects
            # it, and if the error is swallowed the write is silently lost).
            # Instead re-read the collection and run the normal binding path.
            try:
                if self.collection_name in [
                    c.name for c in self.client.get_collections().collections
                ]:
                    info = self._get_vector_info()
                    fp_actual = self._fingerprint()
                    if info.named is None:
                        if info.point_count == 0:
                            self._recreate_with_fingerprint()
                        else:
                            self._bind_legacy(info)
                    elif info.named == fp_actual:
                        self._vector_name = fp_actual
                        self._is_legacy = False
                    elif info.point_count == 0:
                        self._recreate_with_fingerprint()
                    else:
                        self._handle_mismatch(info.named, fp_actual)
                    self._is_legacy = self._vector_name is None
                    return
            except Exception:
                pass
            raise RuntimeError(
                f"Failed to create collection '{self.collection_name}': {exc}"
            ) from exc

        self._vector_name = fp
        self._is_legacy = False
        logger.info(
            "Created collection '%s' with vector '%s' (%dd)",
            self.collection_name, fp, self.provider.dim,
        )

    def _recreate_with_fingerprint(self) -> None:
        """Delete an empty collection and recreate it with the new fingerprint."""
        fp = self._fingerprint()
        try:
            self.client.delete_collection(self.collection_name)
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config={
                    fp: qmodels.VectorParams(
                        size=self.provider.dim,
                        distance=qmodels.Distance.COSINE,
                    )
                },
            )
            self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name="access_level",
                field_type=qmodels.PayloadSchemaType.KEYWORD,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to recreate collection '{self.collection_name}' as '{fp}': {exc}"
            ) from exc

        self._vector_name = fp
        self._is_legacy = False
        logger.info(
            "Recreated empty collection '%s' with vector '%s' (%dd)",
            self.collection_name, fp, self.provider.dim,
        )

    def _bind_legacy(self, info: _VectorInfo) -> None:
        mapped_fp = self.config_legacy_collections.get(self.collection_name)
        if not mapped_fp:
            # NEVER suggest the ACTIVE provider's fingerprint here: copying that
            # snippet is exactly how a user would mix two vector spaces in an
            # unnamed collection (same dimension, different model — Qdrant
            # cannot catch it). Ask for the BUILDER, not the current wish.
            raise CollectionModelUnavailable(
                f"Collection '{self.collection_name}' has no named vector and is "
                "not empty, so its vector space is unknown. Tell the library who "
                "BUILT it (not who should use it now) via a config entry "
                f"'legacy_collections': {{\"{self.collection_name}\": "
                "\"<backend>__<model>__<dim>\"}}, or start a new collection and "
                "re-embed. Do not guess: a wrong entry silently mixes vector "
                "spaces whenever the dimension happens to match."
            )

        # Cross-check the mapping against the ACTUAL vector size of the
        # collection. A dimension mismatch means the mapping is wrong (the
        # suffix encodes the size), so fail before any read/write happens.
        if info.size is not None:
            suffix = mapped_fp.rsplit("__", 1)[-1]
            if suffix.isdigit() and int(suffix) != int(info.size):
                raise CollectionModelUnavailable(
                    f"Collection '{self.collection_name}' holds {info.size}-dimensional "
                    f"vectors, but the legacy mapping '{mapped_fp}' declares "
                    f"{suffix} dimensions. Fix legacy_collections before using "
                    "this collection."
                )

        # The user told us which fingerprint the legacy collection belongs to.
        # The current provider must match, otherwise we cannot write/search
        # vectors into this single-vector collection.
        if mapped_fp != self._fingerprint():
            raise CollectionModelUnavailable(
                f"Collection '{self.collection_name}' is legacy and mapped to "
                f"fingerprint '{mapped_fp}', but the active provider is "
                f"'{self._fingerprint()}'. Switch providers or update "
                "legacy_collections."
            )

        self._vector_name = None
        self._is_legacy = True
        logger.info(
            "Bound legacy unnamed collection '%s' (fingerprint %s)",
            self.collection_name, mapped_fp,
        )

    def _handle_mismatch(self, stored_name: str, fp: str) -> None:
        # If the caller explicitly preferred a provider, fail hard.
        preferred = getattr(self.provider, "_preferred", "")
        if preferred and preferred.lower() != "auto":
            raise CollectionModelUnavailable(
                f"Collection '{self.collection_name}' already uses vector "
                f"'{stored_name}', but the active provider wants '{fp}'. "
                "Set the preference back to the provider that built the "
                f"collection, or use a new NEXUS_COLLECTION."
            )

        # Auto mode: try to switch to a provider that matches ``stored_name``.
        stored_backend = _backend_from_fingerprint(stored_name)
        if stored_backend:
            try:
                new_provider = self._provider_factory(stored_backend)
            except Exception as exc:
                logger.warning("Auto-switch provider build failed: %s", exc)
                new_provider = None
            if new_provider is not None and new_provider.available:
                expected_fp = vector_fingerprint(
                    new_provider.backend, new_provider.model_name, new_provider.dim
                )
                if expected_fp == stored_name:
                    logger.info(
                        "Auto-switching provider to '%s' to match collection '%s' vector '%s'",
                        stored_backend, self.collection_name, stored_name,
                    )
                    self.provider = new_provider
                    self._vector_name = stored_name
                    self._is_legacy = False
                    return

        raise CollectionModelUnavailable(
            f"Collection '{self.collection_name}' already uses vector "
            f"'{stored_name}', but the active provider wants '{fp}' and no "
            f"local provider for '{stored_name}' is available. Either set the "
            "preference back to the provider that BUILT the collection, or "
            "start a NEW collection (NEXUS_COLLECTION) and re-embed. Do NOT "
            "'fix' this by pointing a mapping at the active provider when the "
            "dimensions match but the model differs — that silently mixes two "
            "vector spaces."
        )


def _backend_from_fingerprint(name: str) -> Optional[str]:
    """Infer a provider backend from the fingerprint prefix, if possible."""
    if not name:
        return None
    prefix = name.split("__", 1)[0]
    local_backends = {
        "sentence-transformers": "local",
        "huggingface": "local",
        "ollama": "ollama",
    }
    return local_backends.get(prefix)


def using_name(binding_or_name: Any) -> dict[str, Any]:
    """Return the keyword args to pass to a SEARCH/QUERY operation.

    ``using`` is a read-side argument only (``query_points``/``search``). For
    WRITES it does not exist — Qdrant expects the vector itself to carry the
    name, i.e. ``PointStruct(vector={name: [...]})``. Passing ``using`` to
    ``upsert`` raises ``Unknown arguments: ['using']`` (silently swallowed by
    some callers, which is how a write turns into a no-op). Use
    :func:`upsert_named` for writes.

    Accepts a ``CollectionBinding`` or a raw vector name (str/None).
    """
    if isinstance(binding_or_name, CollectionBinding):
        name = binding_or_name.vector_name()
    else:
        name = binding_or_name
    return {"using": name} if name else {}


def named_point_vector(vector_name: Optional[str], vec: Any) -> Any:
    """Wrap a raw vector as the named-vector dict Qdrant needs on write."""
    return {vector_name: vec} if vector_name else vec


def upsert_named(
    client: Any,
    collection_name: str,
    points: list,
    vector_name: Optional[str],
) -> Any:
    """Write ``points`` into a (possibly named-vector) collection.

    Single place that knows how to write into a named-vector collection:
    each ``PointStruct.vector`` becomes ``{vector_name: vec}``. Without this,
    a raw list is rejected by Qdrant ("Unnamed vectors are not allowed...")
    and a stray ``using=`` kwarg is rejected too — both silently turn the
    write into a no-op with a warning. Legacy collections (vector_name None)
    keep plain vectors.
    """
    if vector_name:
        for p in points:
            vec = getattr(p, "vector", None)
            if vec is not None and not isinstance(vec, dict):
                p.vector = {vector_name: vec}
    return client.upsert(collection_name=collection_name, points=points)
