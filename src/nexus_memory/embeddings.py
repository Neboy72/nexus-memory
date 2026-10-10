"""Nexus Memory — Embedding Provider (shared between MCP server and Hermes plugin).

Rule A (resolve intent; pure function, reads only config/ENV):
  1. Preference P = ENV ``NEXUS_EMBEDDING_PROVIDER`` > config
     ``embedding_provider`` > ``"auto"``.
  2. Cloud is allowed EXACTLY when P is a cloud provider AND its key is present.
     No fallback to a different cloud provider. No other consent source.
  3. ``auto`` = local, and there is exactly ONE automatic path: HuggingFace
     (sentence-transformers ships with the package, so a machine with nothing
     installed still gets a local memory). Ollama is never chosen automatically
     — only when named explicitly. NEVER cloud.
  4. P explicit but unavailable -> hard error, no detection behind it.

The collection model is stored IN Qdrant as a named vector. The binding logic
lives in ``nexus_memory.collection_vectors`` (Rule B); this module stays free of
Qdrant writes and of any config/JSON writes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")

# Provider "get key" URLs
VOYAGE_KEY_URL = "https://dash.voyageai.com/api-keys"
OPENAI_KEY_URL = "https://platform.openai.com/api-keys"
GOOGLE_KEY_URL = "https://aistudio.google.com/apikey"
JINA_KEY_URL = "https://jina.ai/platform/embeddings"

# Quality rankings
QUALITY_EXCELLENT = "excellent"
QUALITY_GOOD = "good"
QUALITY_BASIC = "basic"

# Developer default for the local HuggingFace route: the model a fresh install
# uses when there is no Ollama and no cloud key. Qwen3-Embedding-0.6B is
# fetched by sentence-transformers itself (~600 MB, cached after first use).
LOCAL_HF_DEFAULT = "Qwen/Qwen3-Embedding-0.6B"

# Unified boolean-env vocabulary (review fix MEDIUM :305): one consistent
# parser instead of per-variable ad-hoc checks. Case-insensitive; unknown
# values fall back to the caller's default.
_ENV_FALSY = frozenset({"0", "false", "no", "off", "n", ""})
_ENV_TRUTHY = frozenset({"1", "true", "yes", "y", "on"})


def _env_bool(value: str) -> Optional[bool]:
    """Map an env value through the shared boolean vocabulary.

    Returns True for "1"/"true"/"yes"/"y"/"on", False for
    "0"/"false"/"no"/"off"/"n"/"" (case-insensitive) and None for anything
    else (e.g. a model name).
    """
    token = (value or "").strip().lower()
    if token in _ENV_TRUTHY:
        return True
    if token in _ENV_FALSY:
        return False
    return None


# Preferred provider config sources
# Regel A.1: ENV > Config embedding_provider > "auto".
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

    return "auto"


class CollectionModelUnavailable(RuntimeError):
    """The model recorded for the existing collection is not available.

    Raised instead of silently selecting a different model/backend, which would
    mix incompatible vector spaces.
    """


_NO_PROVIDER_ERROR = (
    "No embedding provider available.\n"
    "Install a local backend: pip install sentence-transformers\n"
    "Or start Ollama: ollama serve\n"
    "Or choose a cloud provider explicitly: NEXUS_EMBEDDING_PROVIDER=voyage "
    "(and set VOYAGE_API_KEY)."
)


# Cloud provider ids. Regel A.2: cloud allowed ONLY when P is one of these
# AND the provider-specific key is present.
CLOUD_PROVIDER_IDS = frozenset({"voyage", "openai", "google", "jina"})


def _cloud_key_present(provider_id: str) -> bool:
    """Return True when the named cloud provider has a key in the environment."""
    if provider_id == "voyage":
        key = os.environ.get("VOYAGE_API_KEY") or VOYAGE_API_KEY
        return bool(key and (key.startswith("vo-") or key.startswith("pa-")))
    if provider_id == "openai":
        key = os.environ.get("OPENAI_API_KEY") or OPENAI_API_KEY
        return bool(key and key.startswith("sk-"))
    if provider_id == "google":
        key = os.environ.get("GOOGLE_API_KEY") or GOOGLE_API_KEY
        return bool(key and key.startswith("AIza"))
    if provider_id == "jina":
        return bool(os.environ.get("JINA_API_KEY", ""))
    return False


_INVALID_FP_CHARS = re.compile(r"[^a-z0-9_-]+")


def _sanitize_fp_token(token: str) -> str:
    if not isinstance(token, str):
        token = str(token) if token is not None else "unknown"
    token = token.lower()
    token = re.sub(r"[\s/:.,;+\\|<>!?@#$%^&*()[\]{}]+", "_", token)
    token = _INVALID_FP_CHARS.sub("", token)
    token = re.sub(r"_+", "_", token)
    return token.strip("_")


def vector_fingerprint(backend: str, model_name: str, dim: int) -> str:
    """Deterministic vector name: ``<backend>__<model>__<dim>``.

    Normalisation (model part only):
      - lowercase
      - replace ``/``, ``:``, ``.``, whitespace and any other non-allowed
        characters with ``_``
      - collapse multiple ``_``
      - strip leading/trailing ``_``
    Allowed final characters: ``[a-z0-9_-]``.
    """
    backend_norm = _sanitize_fp_token(backend)
    model_norm = _sanitize_fp_token(model_name)
    # NOTE: the parts are already sanitised (single underscores, no double
    # ones at the edges), so we must NOT collapse underscores across the
    # final string — that would eat the ``__`` separators and silently
    # produce names like ``voyage_voyage-4_1024``.
    return f"{backend_norm}__{model_norm}__{dim}"


class EmbeddingProvider:
    """Auto-detect best embedding provider according to Regel A.

    Priority: explicit preference > auto, where auto is HuggingFace — the one
    automatic path. Ollama is used only when named explicitly. Cloud is used
    only when the user explicitly preferred a cloud provider and its key is
    present. No silent fallback to another provider.
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
        """Backend type of the selected provider (dispatch key for embed())."""
        return self._backend

    @property
    def name(self) -> str:
        return self._name

    @property
    def dim(self) -> int:
        return self._dim

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

    def _detect(self):
        """Detect provider according to Regel A."""
        preferred = self._preferred

        if preferred and preferred != "auto":
            logger.info("Embedding: preferred provider '%s'", preferred)
            if preferred in CLOUD_PROVIDER_IDS:
                if not _cloud_key_present(preferred):
                    raise RuntimeError(
                        f"Preferred cloud provider '{preferred}' is not available: "
                        f"no valid API key. Set the provider's key environment variable."
                    )
                if self._try_provider(preferred):
                    return
                raise RuntimeError(
                    f"Preferred cloud provider '{preferred}' is not available. "
                    f"Refusing to fall back to another provider (fail-closed)."
                )
            # Explicit local provider
            if self._try_provider(preferred):
                return
            raise RuntimeError(
                f"Preferred embedding provider '{preferred}' is not available. "
                f"Refusing to fall back to another provider (fail-closed). "
                f"Make the chosen backend available again, or switch deliberately: "
                f"NEXUS_EMBEDDING_PROVIDER=auto for local-first auto-detection."
            )

        # auto / no preference: local-first only (Regel A.3).
        self._detect_auto()
        if not self.available:
            logger.error(_NO_PROVIDER_ERROR)

    def _reset_provider_state(self) -> None:
        """Clear all provider state after a failed init attempt."""
        self._name = "none"
        self._dim = 384
        self._backend = "none"
        self._client = None
        self._model = None

    def _try_provider(self, provider_id: str) -> bool:
        """Try to initialize a specific provider by id. Returns True on success.

        Regel A.4 fail-closed: an explicitly unavailable provider is a HARD
        error. ``CollectionModelUnavailable`` therefore propagates instead of
        being swallowed into a silent fallback (a swallowed error is exactly
        how a wrong vector space used to go unnoticed).
        """
        try:
            ok = self._try_provider_inner(provider_id)
        except CollectionModelUnavailable:
            self._reset_provider_state()
            raise
        except Exception:
            ok = False
        if not ok:
            self._reset_provider_state()
        return ok

    def _try_provider_inner(self, provider_id: str) -> bool:
        if provider_id == "voyage":
            return self._try_voyage()
        if provider_id == "openai":
            return self._try_openai()
        if provider_id == "google":
            return self._try_google()
        if provider_id == "jina":
            return self._try_jina()
        if provider_id == "ollama":
            return self._try_ollama()
        if provider_id in ("local", "sentence-transformers", "huggingface"):
            return self._try_sentence_transformers()
        return False

    def _detect_auto(self):
        """Auto-detect providers — LOCAL FIRST (Regel A.3).

        The ONE automatic path is HuggingFace (sentence-transformers): it ships
        with the package, so a machine with nothing installed still gets a
        local memory. Ollama is deliberately NOT part of the automatic path —
        it is extra software that not every user has, and if it ever dropped
        the model, an install that had landed on it would silently move to a
        different provider. Choosing Ollama stays possible, but only
        explicitly: ``NEXUS_EMBEDDING_PROVIDER=ollama``.
        Cloud is NEVER used when the preference is "auto".
        """
        # The single local-first default: HuggingFace.
        if self._try_sentence_transformers():
            return
        self._reset_provider_state()

    def _try_voyage(self) -> bool:
        """Try Voyage AI. Returns True on success."""
        key = os.environ.get("VOYAGE_API_KEY") or VOYAGE_API_KEY
        if not key or not (key.startswith("vo-") or key.startswith("pa-")):
            return False
        try:
            import voyageai
            self._client = voyageai.Client(api_key=key)
            self._name = "voyage-4"
            self._dim = 1024
            self._backend = "voyage"
            logger.info("Embedding: %s (1024d, cloud)", self._name)
            return True
        except Exception:
            return False

    def _try_openai(self) -> bool:
        """Try OpenAI. Returns True on success."""
        key = os.environ.get("OPENAI_API_KEY") or OPENAI_API_KEY
        if not key or not key.startswith("sk-"):
            return False
        try:
            from openai import OpenAI
            self._client = OpenAI(api_key=key)
            self._name = "text-embedding-3-small"
            self._dim = 1536
            self._backend = "openai"
            logger.info("Embedding: %s (1536d, cloud)", self._name)
            return True
        except Exception:
            return False

    def _try_google(self) -> bool:
        """Try Google / Vertex AI. Returns True on success."""
        key = os.environ.get("GOOGLE_API_KEY") or GOOGLE_API_KEY
        if not key or not key.startswith("AIza"):
            return False
        try:
            import google.generativeai as genai
            genai.configure(api_key=key)
            self._client = genai
            self._name = "text-embedding-004"
            self._dim = 768
            self._backend = "google"
            logger.info("Embedding: Google/%s (768d, cloud)", self._name)
            return True
        except Exception:
            return False

    def _try_jina(self) -> bool:
        """Try Jina. Returns True on success."""
        jina_key = os.environ.get("JINA_API_KEY", "")
        if not jina_key:
            return False
        self._client = {"api_key": jina_key, "base_url": "https://api.jina.ai/v1"}
        self._name = "jina-embeddings-v3"
        self._dim = 1024
        self._backend = "jina"
        logger.info("Embedding: Jina/%s (1024d, cloud)", self._name)
        return True

    def _try_ollama(self) -> bool:
        """Try Ollama. Returns True on success.

        Only ONE model (Design-Entscheidung 08.10.2026): qwen3-embedding (1024d).
        The dimension is measured with a probe embedding instead of being
        hardcoded, so a model with unexpected dimensions is detected rather
        than assumed.
        """
        try:
            import requests
            r = requests.get("http://localhost:11434/api/tags", timeout=2)
            if r.status_code < 400:
                models = [m["name"] for m in r.json().get("models", [])]
                emb_model = next(
                    (m for m in models if m.lower().startswith("qwen3-embedding")),
                    None,
                )
                if emb_model:
                    self._client = {"base_url": "http://localhost:11434"}
                    self._name = emb_model
                    self._backend = "ollama"
                    dim = self._probe_ollama_dim()
                    if not dim:
                        return False
                    self._dim = dim
                    logger.info("Embedding: Ollama/%s (%dd, local)", emb_model, dim)
                    return True
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
        """Try local HuggingFace embeddings. Returns True on success.

        DEVELOPER DEFAULT: Qwen3-Embedding-0.6B (1024d).
        The only automatic candidate; NEXUS_HF_MODEL is honoured as an explicit
        override. With the collection model now stored in Qdrant, this route no
        longer reads or writes any config file.
        """
        explicit = (os.environ.get("NEXUS_HF_MODEL") or "").strip()
        legacy_raw = (os.environ.get("NEXUS_HF_BGE3") or "").strip()
        wanted = explicit
        if legacy_raw and not explicit:
            legacy_flag = _env_bool(legacy_raw)
            if legacy_flag is False:
                wanted = ""
            elif legacy_flag is True:
                wanted = "BAAI/bge-m3"
            elif legacy_flag is None:
                wanted = legacy_raw
        if not wanted:
            wanted = LOCAL_HF_DEFAULT

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            logger.warning(
                "No embedding provider found.\n"
                "Install: pip install sentence-transformers  (local, free)\n"
                "Or start Ollama locally."
            )
            return False

        last_error: Exception | None = None
        for model_name in (wanted,):
            try:
                self._model = SentenceTransformer(model_name)
                probe = self._model.encode("nexus dimension probe")
                self._name = model_name
                self._dim = int(len(probe))
                self._backend = "sentence-transformers"
                logger.info("Embedding: %s (%dd, local HF)", self._name, self._dim)
                return True
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "HF model %s unavailable (%s); no local fallback available.",
                    model_name, exc,
                )
        logger.warning(
            "sentence-transformers init failed (%s); no local fallback available.",
            last_error,
        )
        return False

    async def embed(self, text: str, is_query: bool = True) -> list[float]:
        """Embed one text. ``is_query`` selects the query/document mode."""
        backend = getattr(self, "_backend", None)
        if backend == "voyage":
            result = await asyncio.to_thread(self._client.embed, [text], model=self._name)
            return result.embeddings[0]
        if backend == "openai":
            result = await asyncio.to_thread(
                self._client.embeddings.create,
                model=self._name, input=[text]
            )
            return result.data[0].embedding
        if backend == "google":
            result = await asyncio.to_thread(
                self._client.embed_content, model=self._name, content=text
            )
            return result["embedding"]
        if backend == "jina":
            import requests as _req

            def _jina_post() -> list:
                r = _req.post(
                    f"{self._client['base_url']}/embeddings",
                    json={"model": self._name, "input": [text]},
                    headers={"Authorization": f"Bearer {self._client['api_key']}"},
                    timeout=30,
                )
                return r.json()["data"][0]["embedding"]

            return await asyncio.to_thread(_jina_post)
        if backend == "ollama" or (
                backend is None and "localhost:11434" in str((getattr(self, "_client", None) or {}).get("base_url", ""))
        ):
            payload_text = text
            if is_query and "qwen3-embedding" in (self._name or "").lower():
                payload_text = f"Instruct: retrieve the relevant memory for the user query. Query: {text}"

            def _ollama_post() -> list:
                import requests as _req
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

            return await asyncio.to_thread(_ollama_post)
        if self._model:  # sentence-transformers
            vector = await asyncio.to_thread(self._model.encode, text)
            return vector.tolist()
        raise RuntimeError(
            f"No embedding provider available ({self._name}).\n"
            "Install: pip install sentence-transformers\n"
            "Or set VOYAGE_API_KEY or OPENAI_API_KEY"
        )


def detect_available() -> list[dict]:
    """Return ALL available embedding providers with their status."""
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
        jina_available = True
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
    ollama_dims = 0
    try:
        import requests
        r = requests.get("http://localhost:11434/api/tags", timeout=2)
        if r.status_code < 400:
            models = [m["name"] for m in r.json().get("models", [])]
            emb_model = next((m for m in models if m.lower().startswith("qwen3-embedding")), None)
            if emb_model:
                ollama_available = True
                ollama_model = emb_model
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

    # Local HuggingFace default
    local_available = False
    local_model = LOCAL_HF_DEFAULT
    try:
        import importlib.util
        local_available = importlib.util.find_spec("sentence_transformers") is not None
    except (ImportError, ValueError):
        local_available = False
    results.append({
        "id": "local",
        "name": "HuggingFace (Qwen3, lokal)",
        "dims": 1024,
        "quality": QUALITY_GOOD,
        "type": "local",
        "available": local_available,
        "model": local_model,
        "url": "",
    })

    return results
