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
from typing import Any, Optional

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

# Developer default for the local HuggingFace route: the model a fresh install
# uses when there is no Ollama, no cloud key and no recorded collection model.
# Qwen3-Embedding-0.6B is fetched by sentence-transformers itself (~600 MB,
# cached after first use) and was measured on par with a comparable cloud
# embedding model (R@5 66 % vs 67 %).
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


def _model_in_inventory(name: str, models: list[str]) -> bool:
    """True when *name* is present in an Ollama inventory.

    Uses the same tag normalization as _same_local_model so an untagged
    stored name ('bge-m3') matches the tagged inventory entry
    ('bge-m3:latest') — a plain ``name in models`` check missed that and
    wrongly reported a still-installed model as unavailable.
    """
    return any(_same_local_model(name, m) for m in models)


# Cloud fallback is opt-in: switching to a cloud provider happens only when
# the user explicitly asked for it via the preferred-provider setting.
# Security review fix: a failed *explicitly chosen local* provider used to
# trigger cloud-first auto-detection, silently shipping private text to a
# cloud API. Now an explicit choice fails closed with a clear error unless
# the user configured "auto" or listed allowed cloud fallbacks.
CLOUD_PROVIDER_IDS = frozenset({"voyage", "openai", "google", "jina"})


def _allowed_cloud_fallback(preferred: str = "") -> bool:
    """True when the user explicitly allowed cloud fallback providers.

    Allowed configurations (checked for the *explicitly preferred* provider
    only, never during pure auto-detection):
    - preferred provider is "auto" (cloud-first auto-detect by design)
    - NEXUS_ALLOWED_CLOUD_FALLBACK is "1"/"true"/"yes" (any cloud provider)
    - NEXUS_ALLOWED_CLOUD_FALLBACK is a comma-separated list of provider ids
      and *preferred* is one of them. A non-empty value that does NOT list
      the requested provider does not allow the fallback (the docstring used
      to promise a whitelist while the code accepted any non-empty string).
    """
    env = os.environ.get("NEXUS_ALLOWED_CLOUD_FALLBACK", "").strip().lower()
    if not env:
        return False
    if env in ("1", "true", "yes"):
        return True
    allowed = {p.strip() for p in env.split(",") if p.strip()}
    return bool(preferred) and preferred.strip().lower() in allowed


_NO_PROVIDER_ERROR = (
    "No embedding provider available.\n"
    "Install a local backend: pip install sentence-transformers\n"
    "Or start Ollama: ollama serve\n"
    "Or choose a cloud provider explicitly: NEXUS_EMBEDDING_PROVIDER=voyage "
    "(plus NEXUS_ALLOWED_CLOUD_FALLBACK=1 for an allowed fallback)."
)


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
        Auto-detection is local-first; the cloud path is only entered when
        the user explicitly allowed it.

        Without any backend the provider stays unavailable and the reason is
        logged at ERROR (``_NO_PROVIDER_ERROR``); it is deliberately not
        raised, so an agent without an embedding backend keeps running
        instead of dying. An explicit *preference* that cannot be served
        still raises (fail-closed). ``_try_*`` methods that find the
        collection's recorded model gone raise ``CollectionModelUnavailable``
        — that must reach the caller untouched.
        """
        preferred = self._preferred

        if preferred:
            explicit_cloud = preferred in CLOUD_PROVIDER_IDS
            explicit_auto = preferred == "auto"

            logger.info("Embedding: trying preferred provider '%s'", preferred)
            if self._try_provider(preferred):
                return

            if explicit_auto:
                # "auto" is local-first. The cloud level is only reachable
                # with the explicit NEXUS_ALLOWED_CLOUD_FALLBACK opt-in.
                if _allowed_cloud_fallback("auto"):
                    logger.warning(
                        "'auto' fell back to detection; "
                        "NEXUS_ALLOWED_CLOUD_FALLBACK permits the cloud level."
                    )
                self._detect_auto(allow_cloud=_allowed_cloud_fallback("auto"))
            elif explicit_cloud:
                if not _allowed_cloud_fallback(preferred):
                    raise RuntimeError(
                        f"Preferred embedding provider '{preferred}' is not "
                        f"available. Refusing to fall back to another provider "
                        f"(fail-closed: texts must not be sent to a different "
                        f"backend than the explicitly configured one). Set "
                        f"NEXUS_ALLOWED_CLOUD_FALLBACK=1 or change "
                        f"NEXUS_EMBEDDING_PROVIDER to 'auto' to allow fallbacks."
                    )
                logger.warning(
                    "Preferred embedding provider '%s' is not available; "
                    "NEXUS_ALLOWED_CLOUD_FALLBACK allows the fallback — the "
                    "selected backend may differ from the configured one.",
                    preferred,
                )
                self._detect_auto(allow_cloud=True)
            else:
                # Explicit local provider (ollama / sentence-transformers /
                # huggingface) fails closed — no silent backend switch.
                raise RuntimeError(
                    f"Preferred embedding provider '{preferred}' is not "
                    f"available. Refusing to fall back to another provider "
                    f"(fail-closed: texts must not be sent to a different "
                    f"backend than the explicitly configured one). Make the "
                    f"chosen backend available again, or switch deliberately: "
                    f"NEXUS_EMBEDDING_PROVIDER=auto for local-first "
                    f"auto-detection."
                )

            if not self.available:
                # Same contract as the default path below: report, do not
                # kill. "auto" is auto-detection, and a missing backend must
                # not take down an agent that merely lacks embeddings.
                logger.error(_NO_PROVIDER_ERROR)
            return

        # Default (no explicit preference): local-first auto-detection.
        # A fresh install never leaves the machine unless the user opts in.
        #
        # No backend at all is REPORTED, not raised: the plugin/MCP contract is
        # "never let a missing dependency kill the module — record the failure
        # and report it instead" (a raised error here would take down an agent
        # that merely lacks an embedding backend). An explicit *preference*
        # keeps its fail-closed RuntimeError above.
        self._detect_auto(allow_cloud=False)
        if not self.available:
            logger.error(_NO_PROVIDER_ERROR)

    def _reset_provider_state(self) -> None:
        """Clear all provider state after a failed init attempt.

        Review fix (MEDIUM :278): a _try_* method that crashed AFTER setting
        e.g. self._name/_backend but BEFORE completing its probe used to
        leave a 'phantom available' provider behind — available reported
        True while every embed() call would fail. Resetting centrally after
        every failed attempt guarantees consistent state.
        """
        self._name = "none"
        self._dim = 384
        self._backend = "none"
        self._client = None
        self._model = None

    def _try_provider(self, provider_id: str) -> bool:
        """Try to initialize a specific provider by id. Returns True on success.

        CollectionModelUnavailable propagates (fail-closed: the collection's
        stored model is gone — silently falling through would mix vector
        spaces). Any other failure resets the phantom-provider state.
        """
        try:
            ok = self._try_provider_inner(provider_id)
        except CollectionModelUnavailable:
            raise
        except Exception:
            ok = False
        if not ok:
            self._reset_provider_state()
        return ok

    def _try_provider_inner(self, provider_id: str) -> bool:
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
        elif provider_id in ("local", "sentence-transformers", "huggingface"):
            # W33-9: the wizard records "huggingface" when its bge-m3 HF
            # fallback (NEXUS_HF_BGE3=1) replaced Ollama. Without this id the
            # explicit preference would hit the fail-closed branch below and
            # refuse to initialise any embedding backend at all.
            return self._try_sentence_transformers()
        return False

    def _detect_auto(self, allow_cloud: bool = False):
        """Auto-detect providers — LOCAL FIRST (developer default).

        Order (developer default, 2026-10-07):
          1. Ollama (local service) — keeps existing local installs (including
             their recorded collection model) working exactly as before.
          2. HuggingFace local — Qwen3-Embedding by default; needs no Ollama
             install and no cloud key, so a fresh machine just works.
          3. Cloud providers — LAST resort, and only when *allow_cloud* is
             True (i.e. the user explicitly opted in via a cloud provider
             preference plus NEXUS_ALLOWED_CLOUD_FALLBACK, or via "auto"
             with the same opt-in). A user with no cloud opt-in never leaves
             the machine.

        Collection-drift guard: an existing collection keeps its recorded
        model. CollectionModelUnavailable must propagate (fail-closed, never
        silently switch models/backends behind the user's back).
        """
        # 1. Ollama (local service) — may raise CollectionModelUnavailable
        try:
            if self._try_ollama():
                return
        except CollectionModelUnavailable:
            raise
        except Exception:
            pass
        self._reset_provider_state()

        # 2. HuggingFace local (Qwen3 -> bge-m3 -> MiniLM)
        try:
            if self._try_sentence_transformers():
                return
        except CollectionModelUnavailable:
            raise
        except Exception:
            pass
        self._reset_provider_state()

        # 3. Cloud — only when explicitly allowed.
        if not allow_cloud:
            return
        for attempt in (self._try_voyage, self._try_openai,
                        self._try_google, self._try_jina):
            try:
                if attempt():
                    return
            except CollectionModelUnavailable:
                raise
            except Exception:
                pass
            self._reset_provider_state()


    def _try_voyage(self) -> bool:
        """Try Voyage AI. Returns True on success.

        The key is read live from the environment (a key exported after this
        module was imported must still work); the module constant is only a
        fallback for importers that set it directly.
        """
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
        """Try OpenAI. Returns True on success.

        Key read live from the environment; the module constant is a fallback.
        """
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
        """Try Google / Vertex AI. Returns True on success.

        Key read live from the environment; the module constant is a fallback.
        """
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
        """Try Jina. Returns True on success.

        Nr 457: unlike voyage/openai/google, Jina has no SDK import to probe —
        availability is intentionally key-presence only (``jina_valid``); the
        first real embed validates the key. The previous try/except wrapped a
        pure assignment and could never fail, so it was removed.
        """
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
                        if _model_in_inventory(existing_model, models):
                            logger.info(
                                "Embedding: keeping existing local model '%s' "
                                "(collection already uses it; '%s' also available)",
                                existing_model, emb_model,
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
                    logger.info("Embedding: Ollama/%s (%dd, local)", emb_model, dim)
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
        """Try local HuggingFace embeddings. Returns True on success.

        DEVELOPER DEFAULT: Qwen3-Embedding-0.6B (1024d).
        Chosen because it needs no Ollama install — the sentence-transformers
        library fetches the weights itself (~600 MB on first use, cached
        afterwards) — and no cloud key. Tested quality is on par with a
        comparable cloud embedding model (R@5 66 % vs 67 %).

        Order: a model already recorded for this collection (drift guard) →
        an explicit NEXUS_HF_MODEL → Qwen3 → bge-m3 → all-MiniLM-L6-v2.
        NEXUS_HF_BGE3=0 only removes bge-m3 from the candidate list; the local
        route continues with the remaining models.

        Network note (air-gapped installs): the first use downloads the model
        weights from huggingface.co. No user text leaves the machine, but a
        machine without internet needs the weights pre-cached; with
        ``HF_HUB_OFFLINE=1`` the download is skipped and this route fails
        cleanly (warning + False / CollectionModelUnavailable for a recorded
        model).
        """
        explicit = (os.environ.get("NEXUS_HF_MODEL") or "").strip()
        legacy_raw = (os.environ.get("NEXUS_HF_BGE3") or "").strip()
        skip_bge3 = False
        # Only an actually-present value is a switch. An UNSET variable must not
        # disable the local route: _env_bool("") reports False, which would have
        # silently turned the whole HF path off on every fresh install.
        if legacy_raw:
            legacy_flag = _env_bool(legacy_raw)
            if legacy_flag is False:
                skip_bge3 = True
            elif legacy_flag is True and not explicit:
                explicit = "BAAI/bge-m3"
            elif legacy_flag is None and not explicit:
                explicit = legacy_raw

        recorded = _read_existing_collection_model()

        # Drift guard: if the collection already stores vectors from a local
        # model, the embedding library must be available. A silent failure here
        # would switch to a different model/backend and mix vector spaces.
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            if recorded:
                raise CollectionModelUnavailable(
                    f"Collection uses local model '{recorded}', but no local "
                    "embedding backend is available. Embedding into this "
                    "collection with a different model would mix incompatible "
                    "vector spaces. Restore the backend that serves it — "
                    "`pip install sentence-transformers` for HuggingFace "
                    "models, a running Ollama for Ollama models — or start a "
                    "new collection."
                )
            logger.warning(
                "No embedding provider found.\n"
                "Install: pip install sentence-transformers  (local, free)\n"
                "Or start Ollama locally. Cloud embeddings require an explicit "
                "choice: NEXUS_EMBEDDING_PROVIDER=voyage (plus "
                "NEXUS_ALLOWED_CLOUD_FALLBACK=1 for a permitted fallback)."
            )
            return False

        wanted: list[str] = []
        if recorded:
            wanted.append(recorded)
            if explicit and explicit != recorded:
                # Silent override would be a drift hazard in disguise: say it.
                logger.warning(
                    "NEXUS_HF_MODEL=%s ignored: the collection already stores "
                    "vectors from '%s' and keeps its model (drift guard).",
                    explicit, recorded,
                )
        if explicit and explicit not in wanted:
            wanted.append(explicit)
        for name in (LOCAL_HF_DEFAULT, "BAAI/bge-m3", "all-MiniLM-L6-v2"):
            if skip_bge3 and name == "BAAI/bge-m3":
                continue
            if name not in wanted:
                wanted.append(name)

        last_error: Exception | None = None
        for model_name in wanted:
            try:
                self._model = SentenceTransformer(model_name)
                probe = self._model.encode("nexus dimension probe")
                self._name = model_name
                self._dim = int(len(probe))
                self._backend = "sentence-transformers"
                logger.info("Embedding: %s (%dd, local HF)", self._name, self._dim)
                return True
            except CollectionModelUnavailable:
                raise
            except Exception as exc:
                # A recorded model that cannot be loaded must NOT be replaced
                # silently — that would mix incompatible vector spaces in a
                # collection that already holds vectors (drift guard).
                last_error = exc
                if model_name == recorded:
                    raise CollectionModelUnavailable(
                        f"Collection uses local model '{recorded}', but it could "
                        f"not be loaded ({exc}). Embedding into this collection "
                        f"with a different model would mix incompatible vector "
                        f"spaces. Make it available again (e.g. delete the "
                        f"cached weights so they re-download) or start a new "
                        f"collection."
                    ) from exc
                logger.warning(
                    "HF model %s unavailable (%s); trying next local option.",
                    model_name, exc,
                )
        logger.warning(
            "sentence-transformers init failed (%s); no local fallback available.",
            last_error,
        )
        return False


    async def embed(self, text: str, is_query: bool = True) -> list[float]:
        """Embed one text. ``is_query`` selects the query/document mode.

        Instruction-aware models (qwen3-embedding) prefix only QUERY text —
        document embeddings must be produced plain, so the document call
        sites pass ``is_query=False``. For every other backend the flag is
        accepted and ignored (behavior unchanged)."""
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
            # Review fix (MEDIUM :369): blocking HTTP must not run on the
            # event loop thread — moved to a worker thread via to_thread.
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
        elif backend == "ollama" or (
                backend is None and "localhost:11434" in str((getattr(self, "_client", None) or {}).get("base_url", ""))
        ):
            # Review fix (MEDIUM :369): blocking HTTP must not run on the
            # event loop thread — the whole ollama request chain (modern
            # endpoint + legacy fallback) moved to a worker thread.
            # qwen3-embedding is instruction-aware: queries get the Instruct
            # prefix, documents are embedded plain (official usage guidance,
            # benchmark protocol that measured +4 R@5 vs bge-m3). The prefix
            # is applied for QUERY embeddings only (is_query=True).
            payload_text = text
            if is_query and "qwen3-embedding" in (self._name or "").lower():
                payload_text = f"Instruct: retrieve the relevant memory for the user query. Query: {text}"

            def _ollama_post() -> list:
                import requests as _req
                # Modern endpoint first (/api/embed, batched input),
                # legacy /api/embeddings as fallback
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