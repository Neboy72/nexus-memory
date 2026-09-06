"""Nexus Memory — Embedding Provider (shared between MCP server and Hermes plugin).

Auto-detects the best available embedding backend:
1. Voyage AI (cloud, 1024d)
2. OpenAI (cloud, 1536d)
3. Google/Vertex AI (cloud, 768d)
4. Jina (cloud, 1024d)
5. Ollama (local, 768d)
6. sentence-transformers (local, 384d, zero-setup fallback)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")

# Provider "get key" URLs
VOYAGE_KEY_URL = "https://dash.voyageai.com/api-keys"
OPENAI_KEY_URL = "https://platform.openai.com/api-keys"
GOOGLE_KEY_URL = "https://aistudio.google.com/apikey"
JINA_KEY_URL = "https://jina.ai/platform/embeddings"

# Quality rankings
QUALITY_EXCELLENT = "excellent"
QUALITY_GOOD = "good"
QUALITY_BASIC = "basic"

# Preferred provider config sources
def _read_preferred_provider() -> str:
    """Read preferred embedding provider from env var or config files."""
    # 1. Environment variable
    provider = os.environ.get("NEXUS_EMBEDDING_PROVIDER", "")
    if provider:
        return provider.strip().lower()

    # 2. $HERMES_HOME/nexus/config.json
    hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    config_path = os.path.join(hermes_home, "nexus", "config.json")
    try:
        if os.path.exists(config_path):
            import json
            with open(config_path) as f:
                cfg = json.load(f)
            provider = cfg.get("embedding_provider", "")
            if provider:
                return provider.strip().lower()
    except Exception:
        pass

    # 3. ~/.nexus-memory/config.json
    nexus_config = os.path.expanduser("~/.nexus-memory/config.json")
    try:
        if os.path.exists(nexus_config):
            import json
            with open(nexus_config) as f:
                cfg = json.load(f)
            provider = cfg.get("embedding_provider", "")
            if provider:
                return provider.strip().lower()
    except Exception:
        pass

    return ""


def _read_existing_collection_model() -> str:
    """Read which local embedding model the existing collection uses.

    Reads $HERMES_HOME/nexus/config.json or ~/.nexus-memory/config.json
    (whichever the wizard writes). Returns '' when nothing recorded.
    """
    import json as _json
    candidates = []
    hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    candidates.append(os.path.join(hermes_home, "nexus", "config.json"))
    candidates.append(os.path.expanduser("~/.nexus-memory/config.json"))
    for path in candidates:
        try:
            if os.path.exists(path):
                with open(path) as f:
                    cfg = _json.load(f)
                model = cfg.get("embedding_model", "")
                if model:
                    return str(model).strip().lower()
        except Exception:
            continue
    return ""


class CollectionModelUnavailable(RuntimeError):
    """The model recorded for the existing collection is not available.

    Security review fix companion: raised instead of silently selecting a
    different local model (which would mix incompatible vector spaces) or
    falling through to another provider.
    """


def _same_local_model(a: str, b: str) -> bool:
    """True when two local model names are the exact same model.

    Security review fix: this used to strip the tag and additionally accept
    substring matches, so e.g. qwen3-embedding:0.6b and qwen3-embedding:8b
    were treated as interchangeable although they can produce different
    embedding dimensions. Model identity must include the tag.

    Ollama semantics kept intact: a name WITHOUT a tag resolves to the
    implicit default tag ':latest', so 'bge-m3' and 'bge-m3:latest' are the
    same model. Any EXPLICIT tag (':0.6b', ':8b', ':latest-x') is a distinct,
    well-defined model name and never equal to another tag.
    """
    if not a or not b:
        return False
    na = a.strip().lower()
    nb = b.strip().lower()
    if na and ":" not in na:
        na = f"{na}:latest"
    if nb and ":" not in nb:
        nb = f"{nb}:latest"
    return na == nb


# Cloud fallback is opt-in: switching to a cloud provider happens only when
# the user explicitly asked for it via the preferred-provider setting.
# Security review fix: a failed *explicitly chosen local* provider used to
# trigger cloud-first auto-detection, silently shipping private text to a
# cloud API. Now an explicit choice fails closed with a clear error unless
# the user configured "auto" or listed allowed cloud fallbacks.
CLOUD_PROVIDER_IDS = frozenset({"voyage", "openai", "google", "jina"})


def _allowed_cloud_fallback() -> bool:
    """True when the user explicitly allowed cloud fallback providers.

    Allowed configurations (checked for the *explicitly preferred* provider
    only, never during pure auto-detection):
    - preferred provider is "auto" (cloud-first auto-detect by design)
    - NEXUS_ALLOWED_CLOUD_FALLBACK contains one or more provider ids
      (comma-separated; "1"/"true"/"yes" allows any cloud provider)
    """
    env = os.environ.get("NEXUS_ALLOWED_CLOUD_FALLBACK", "").strip().lower()
    if env in ("1", "true", "yes"):
        return True
    return bool(env)


class EmbeddingProvider:
    """Auto-detect best embedding provider.

    Priority: Voyage (cloud, 1024d) → OpenAI (cloud, 1536d) →
    Google (cloud, 768d) → Jina (cloud, 1024d) →
    Ollama (local, 768d) → sentence-transformers (local, 384d).
    """

    def __init__(self, preferred: str = ""):
        self._name = "none"
        self._dim = 384
        self._backend: str = "none"
        self._client: Any = None
        self._model: Any = None
        self._preferred = preferred or _read_preferred_provider()
        self._detect()

    @property
    def backend(self) -> str:
        """Backend type of the selected provider (dispatch key for embed()).

        One of: voyage, openai, google, jina, ollama, sentence-transformers,
        none. Kept separate from the model name so name-based heuristics can
        never dispatch to the wrong API (Google's model name used to match
        the OpenAI branch).
        """
        return self._backend

    def _detect(self):
        """Detect best available embedding backend.

        If a preferred provider is set, try that first.
        Falls back to auto-detect if the preferred provider is unavailable.
        """
        preferred = self._preferred

        if preferred:
            explicit_cloud = preferred in CLOUD_PROVIDER_IDS
            # Security review fix (fail closed): a non-auto explicit choice
            # must never silently degrade to a different provider — in
            # particular not from a local backend to a cloud one. Only
            # "auto" (cloud-first by design) or an explicitly allowed cloud
            # fallback may continue into auto-detection.
            allowed_fallback = preferred == "auto" or (
                explicit_cloud and _allowed_cloud_fallback()
            )
            logging.info(f"Embedding: trying preferred provider '{preferred}'")
            if self._try_provider(preferred):
                return
            if not allowed_fallback:
                raise RuntimeError(
                    f"Preferred embedding provider '{preferred}' is not "
                    f"available. Refusing to fall back to another provider "
                    f"(fail-closed: texts must not be sent to a different "
                    f"backend than the explicitly configured one). Set "
                    f"NEXUS_ALLOWED_CLOUD_FALLBACK=1 or change "
                    f"NEXUS_EMBEDDING_PROVIDER to 'auto' to allow fallbacks."
                )
            logging.warning(
                f"Preferred embedding provider '{preferred}' is not available. "
                f"Falling back to auto-detect."
            )

        # Auto-detect: priority order
        self._detect_auto()

    def _try_provider(self, provider_id: str) -> bool:
        """Try to initialize a specific provider by id. Returns True on success."""
        if provider_id == "voyage":
            return self._try_voyage()
        elif provider_id == "openai":
            return self._try_openai()
        elif provider_id == "google":
            return self._try_google()
        elif provider_id == "jina":
            return self._try_jina()
        elif provider_id == "ollama":
            return self._try_ollama()
        elif provider_id == "local" or provider_id == "sentence-transformers":
            return self._try_sentence_transformers()
        return False

    def _detect_auto(self):
        """Auto-detect providers in priority order."""
        # 1. Voyage (cloud, best quality)
        if self._try_voyage():
            return
        # 2. OpenAI (cloud)
        if self._try_openai():
            return
        # 3. Google / Vertex AI (cloud)
        if self._try_google():
            return
        # 4. Jina (cloud, best value)
        if self._try_jina():
            return
        # 5. Ollama (local service)
        if self._try_ollama():
            return
        # 6. sentence-transformers (local, zero-setup fallback)
        self._try_sentence_transformers()

    def _try_voyage(self) -> bool:
        """Try Voyage AI. Returns True on success."""
        if not VOYAGE_API_KEY or not (VOYAGE_API_KEY.startswith("vo-") or VOYAGE_API_KEY.startswith("pa-")):
            return False
        try:
            import voyageai
            self._client = voyageai.Client(api_key=VOYAGE_API_KEY)
            self._name = "voyage-4"
            self._dim = 1024
            self._backend = "voyage"
            logging.info(f"Embedding: {self._name} (1024d, cloud)")
            return True
        except Exception:
            return False

    def _try_openai(self) -> bool:
        """Try OpenAI. Returns True on success."""
        if not OPENAI_API_KEY or not OPENAI_API_KEY.startswith("sk-"):
            return False
        try:
            from openai import OpenAI
            self._client = OpenAI(api_key=OPENAI_API_KEY)
            self._name = "text-embedding-3-small"
            self._dim = 1536
            self._backend = "openai"
            logging.info(f"Embedding: {self._name} (1536d, cloud)")
            return True
        except Exception:
            return False

    def _try_google(self) -> bool:
        """Try Google / Vertex AI. Returns True on success."""
        if not GOOGLE_API_KEY or not GOOGLE_API_KEY.startswith("AIza"):
            return False
        try:
            import google.generativeai as genai
            genai.configure(api_key=GOOGLE_API_KEY)
            self._client = genai
            self._name = "text-embedding-004"
            self._dim = 768
            self._backend = "google"
            logging.info(f"Embedding: Google/{self._name} (768d, cloud)")
            return True
        except Exception:
            return False

    def _try_jina(self) -> bool:
        """Try Jina. Returns True on success."""
        jina_key = os.environ.get("JINA_API_KEY", "")
        if not jina_key:
            return False
        try:
            self._client = {"api_key": jina_key, "base_url": "https://api.jina.ai/v1"}
            self._name = "jina-embeddings-v3"
            self._dim = 1024
            self._backend = "jina"
            logging.info(f"Embedding: Jina/{self._name} (1024d, cloud)")
            return True
        except Exception:
            return False

    def _try_ollama(self) -> bool:
        """Try Ollama. Returns True on success.

        Prefers qwen3-embedding (1024d, MRL, instruction-aware; benchmark
        04.09.: +4 R@5 vs bge-m3), then bge-m3 (1024d, multilingual), then
        any other model with 'embed' in its name. The dimension is measured
        with a probe embedding instead of being hardcoded, so new models with
        unexpected dimensions keep working.

        Guard: if the store already contains vectors from a different local
        model, keep the existing model to avoid mixed-model collections.
        """
        try:
            import requests
            r = requests.get("http://localhost:11434/api/tags", timeout=2)
            if r.status_code < 400:
                models = [m["name"] for m in r.json().get("models", [])]
                qwen = next((m for m in models if m.lower().startswith("qwen3-embedding")), None)
                bge = next((m for m in models if "bge-m3" in m.lower()), None)
                emb_model = qwen or bge or next((m for m in models if "embed" in m.lower()), None)
                if emb_model:
                    # Collection-drift guard: existing collections keep their
                    # model, but only if that model is actually installed.
                    # Security review fix: the old existence check compared a
                    # name with itself (always true), so a stored model that
                    # is no longer on this machine (or a bogus cloud name) was
                    # selected anyway; the first embed then failed or the
                    # guard silently switched models. Resolve against the
                    # real Ollama inventory and fail explicitly instead.
                    existing_model = _read_existing_collection_model()
                    if existing_model and not _same_local_model(existing_model, emb_model):
                        if existing_model in models:
                            logging.info(
                                f"Embedding: keeping existing local model '{existing_model}' "
                                f"(collection already uses it; '{emb_model}' also available)"
                            )
                            emb_model = existing_model
                        else:
                            raise CollectionModelUnavailable(
                                f"Collection uses local model '{existing_model}', "
                                f"but it is not available in Ollama. Embedding into "
                                f"this collection with a different model would mix "
                                f"incompatible vector spaces. Install it first "
                                f"(e.g. `ollama pull {existing_model}`) or start "
                                f"with an empty collection."
                            )
                    self._client = {"base_url": "http://localhost:11434"}
                    self._name = emb_model
                    self._backend = "ollama"
                    dim = self._probe_ollama_dim()
                    if not dim:
                        return False
                    self._dim = dim
                    logging.info(f"Embedding: Ollama/{emb_model} ({dim}d, local)")
                    return True
        except CollectionModelUnavailable:
            # Security review fix: the stored collection model is not usable —
            # this must surface as an explicit error, not as a silent fall
            # through to the next provider (which would switch models or
            # backends behind the user's back).
            raise
        except Exception:
            pass
        return False

    def _probe_ollama_dim(self) -> int | None:
        """Measure the embedding dimension of self._name with a tiny probe call."""
        try:
            import requests as _req
            import warnings as _warnings
            with _warnings.catch_warnings():
                _warnings.simplefilter("ignore")
                r = _req.post(
                    f"{self._client['base_url']}/api/embed",
                    json={"model": self._name, "input": "probe"},
                    timeout=30,
                )
                vec = r.json().get("embeddings", [[None]])[0]
                if vec and isinstance(vec[0], (int, float)):
                    return len(vec)
                # Legacy fallback: old /api/embeddings endpoint with prompt=
                r2 = _req.post(
                    f"{self._client['base_url']}/api/embeddings",
                    json={"model": self._name, "prompt": "probe"},
                    timeout=30,
                )
                vec2 = r2.json().get("embedding")
                return len(vec2) if vec2 else None
        except Exception:
            return None

    def _try_sentence_transformers(self) -> bool:
        """Try local HF embeddings. Returns True on success.

        Priority: bge-m3 (1024d, multilingual, via HuggingFace weights) when
        the model is locally available (NEXUS_HF_BGE3=1 forces the attempt),
        then all-MiniLM-L6-v2 (384d, smallest, always works with the package).

        bge-m3 via HF downloads ~2.3 GB on first use (cached afterwards) so it
        is opt-in per environment; the wizard offers it after Ollama fails."""
        hf_candidate = os.environ.get("NEXUS_HF_BGE3") or ""
        if hf_candidate:
            # explicit opt-in (or explicit 0/1 with default model name)
            model_name = hf_candidate if hf_candidate not in ("1", "true", "yes") else "BAAI/bge-m3"
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(model_name)
                probe = self._model.encode("nexus dimension probe")
                self._name = model_name
                self._dim = int(len(probe))
                logging.info(f"Embedding: {self._name} ({self._dim}d, local HF)")
                return True
            except Exception as exc:
                logging.warning(f"HF model {model_name} unavailable ({exc}); falling back to MiniLM.")
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer("all-MiniLM-L6-v2")
            self._name = "all-MiniLM-L6-v2"
            self._dim = 384
            logging.info(f"Embedding: {self._name} (384d, local)")
            return True
        except ImportError:
            logging.warning(
                "No embedding provider found.\n"
                "Install: pip install sentence-transformers  (local, free)\n"
                "Or set VOYAGE_API_KEY or OPENAI_API_KEY"
            )
            return False

    async def embed(self, text: str) -> list[float]:
        # Dispatch on the stored backend type, never on the model name.
        # Security review fix: the model-name heuristics sent Google's
        # 'text-embedding-004' into the OpenAI branch (the google.generativeai
        # module has no embeddings.create), so every Google call failed.
        # getattr fallback: legacy/constructed-by-hand provider instances
        # (tests, tools) may not carry _backend — they keep working.
        backend = getattr(self, "_backend", None)
        if backend == "voyage":
            result = await asyncio.to_thread(self._client.embed, [text], model=self._name)
            return result.embeddings[0]
        elif backend == "openai":
            result = await asyncio.to_thread(
                self._client.embeddings.create,
                model=self._name, input=[text]
            )
            return result.data[0].embedding
        elif backend == "google":
            result = await asyncio.to_thread(
                self._client.embed_content, model=self._name, content=text
            )
            return result["embedding"]
        elif backend == "jina":
            import requests as _req
            r = _req.post(
                f"{self._client['base_url']}/embeddings",
                json={"model": self._name, "input": [text]},
                headers={"Authorization": f"Bearer {self._client['api_key']}"},
                timeout=30,
            )
            return r.json()["data"][0]["embedding"]
        elif backend == "ollama" or (
                backend is None and "localhost:11434" in str((getattr(self, "_client", None) or {}).get("base_url", ""))
        ):
            import requests as _req
            # qwen3-embedding is instruction-aware: queries get the Instruct prefix,
            # documents are embedded plain (matches the official usage guidance and
            # the benchmark protocol that measured +4 R@5 vs bge-m3).
            payload_text = text
            if "qwen3-embedding" in (self._name or "").lower():
                payload_text = f"Instruct: retrieve the relevant memory for the user query. Query: {text}"
            # Modern endpoint first (/api/embed, batched input), legacy /api/embeddings as fallback
            try:
                r = _req.post(
                    f"{self._client['base_url']}/api/embed",
                    json={"model": self._name, "input": [payload_text]},
                    timeout=30,
                )
                vec = r.json().get("embeddings", [[None]])[0]
                if vec and isinstance(vec[0], (int, float)):
                    return vec
            except Exception:
                pass
            r = _req.post(
                f"{self._client['base_url']}/api/embeddings",
                json={"model": self._name, "prompt": payload_text},
                timeout=30,
            )
            return r.json()["embedding"]
        elif self._model:  # sentence-transformers
            vector = await asyncio.to_thread(self._model.encode, text)
            return vector.tolist()
        raise RuntimeError(
            f"No embedding provider available ({self._name}).\n"
            "Install: pip install sentence-transformers\n"
            "Or set VOYAGE_API_KEY or OPENAI_API_KEY"
        )

    @property
    def name(self) -> str: return self._name

    @property
    def dim(self) -> int: return self._dim

    @property
    def available(self) -> bool:
        return self._name != "none"

    @property
    def model_name(self) -> str:
        return self._name

    @property
    def provider_type(self) -> str:
        """Cloud or local for the selected backend ('none' when unavailable)."""
        return "cloud" if self._backend in CLOUD_PROVIDER_IDS else (
            "local" if self._backend != "none" else "none"
        )


def detect_available() -> list[dict]:
    """Return ALL available embedding providers with their status.

    Used by the wizard to show the user what's available.
    """
    results = []

    # Voyage AI
    voyage_key = os.environ.get("VOYAGE_API_KEY", "")
    voyage_valid = bool(voyage_key and (voyage_key.startswith("vo-") or voyage_key.startswith("pa-")))
    voyage_available = False
    if voyage_valid:
        try:
            import voyageai
            voyageai.Client(api_key=voyage_key)
            voyage_available = True
        except Exception:
            pass
    results.append({
        "id": "voyage",
        "name": "Voyage AI",
        "dims": 1024,
        "quality": QUALITY_EXCELLENT,
        "type": "cloud",
        "available": voyage_available,
        "key_detected": voyage_valid,
        "url": VOYAGE_KEY_URL,
    })

    # OpenAI
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    openai_valid = bool(openai_key and openai_key.startswith("sk-"))
    openai_available = False
    if openai_valid:
        try:
            from openai import OpenAI
            OpenAI(api_key=openai_key)
            openai_available = True
        except Exception:
            pass
    results.append({
        "id": "openai",
        "name": "OpenAI",
        "dims": 1536,
        "quality": QUALITY_EXCELLENT,
        "type": "cloud",
        "available": openai_available,
        "key_detected": openai_valid,
        "url": OPENAI_KEY_URL,
    })

    # Google
    google_key = os.environ.get("GOOGLE_API_KEY", "")
    google_valid = bool(google_key and google_key.startswith("AIza"))
    google_available = False
    if google_valid:
        try:
            import google.generativeai as genai
            genai.configure(api_key=google_key)
            google_available = True
        except Exception:
            pass
    results.append({
        "id": "google",
        "name": "Google / Vertex AI",
        "dims": 768,
        "quality": QUALITY_GOOD,
        "type": "cloud",
        "available": google_available,
        "key_detected": google_valid,
        "url": GOOGLE_KEY_URL,
    })

    # Jina
    jina_key = os.environ.get("JINA_API_KEY", "")
    jina_valid = bool(jina_key)
    jina_available = False
    if jina_valid:
        try:
            jina_available = True
        except Exception:
            pass
    results.append({
        "id": "jina",
        "name": "Jina",
        "dims": 1024,
        "quality": QUALITY_GOOD,
        "type": "cloud",
        "available": jina_available,
        "key_detected": jina_valid,
        "url": JINA_KEY_URL,
    })

    # Ollama
    ollama_available = False
    ollama_model = ""
    ollama_dims = 768
    try:
        import requests
        r = requests.get("http://localhost:11434/api/tags", timeout=2)
        if r.status_code < 400:
            models = [m["name"] for m in r.json().get("models", [])]
            # Same priority as _try_ollama: qwen3 → bge-m3 → any embed model
            emb_model = next((m for m in models if m.lower().startswith("qwen3-embedding")), None)
            if not emb_model:
                emb_model = next((m for m in models if "bge-m3" in m.lower()), None)
            if not emb_model:
                emb_model = next((m for m in models if "embed" in m.lower()), None)
            if emb_model:
                ollama_available = True
                ollama_model = emb_model
                if "qwen3-embedding" in emb_model.lower():
                    ollama_dims = 1024
                elif "bge-m3" in emb_model.lower():
                    ollama_dims = 1024
    except Exception:
        pass
    results.append({
        "id": "ollama",
        "name": "Ollama",
        "dims": ollama_dims,
        "quality": QUALITY_GOOD,
        "type": "local",
        "available": ollama_available,
        "model": ollama_model,
        "url": "https://ollama.com/download",
    })

    # sentence-transformers
    local_available = False
    try:
        from sentence_transformers import SentenceTransformer
        SentenceTransformer("all-MiniLM-L6-v2")
        local_available = True
    except Exception:
        pass
    results.append({
        "id": "local",
        "name": "sentence-transformers",
        "dims": 384,
        "quality": QUALITY_BASIC,
        "type": "local",
        "available": local_available,
        "url": "",
    })

    return results