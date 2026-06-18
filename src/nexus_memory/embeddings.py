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


class EmbeddingProvider:
    """Auto-detect best embedding provider.

    Priority: Voyage (cloud, 1024d) → OpenAI (cloud, 1536d) →
    Google (cloud, 768d) → Jina (cloud, 1024d) →
    Ollama (local, 768d) → sentence-transformers (local, 384d).
    """

    def __init__(self, preferred: str = ""):
        self._name = "none"
        self._dim = 384
        self._client: Any = None
        self._model: Any = None
        self._preferred = preferred or _read_preferred_provider()
        self._detect()

    def _detect(self):
        """Detect best available embedding backend.

        If a preferred provider is set, try that first.
        Falls back to auto-detect if the preferred provider is unavailable.
        """
        preferred = self._preferred

        if preferred:
            logging.info(f"Embedding: trying preferred provider '{preferred}'")
            if self._try_provider(preferred):
                return
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
            self._name = "voyage-3-large"
            self._dim = 1024
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
            logging.info(f"Embedding: Jina/{self._name} (1024d, cloud)")
            return True
        except Exception:
            return False

    def _try_ollama(self) -> bool:
        """Try Ollama. Returns True on success."""
        try:
            import requests
            r = requests.get("http://localhost:11434/api/tags", timeout=2)
            if r.status_code < 400:
                models = [m["name"] for m in r.json().get("models", [])]
                emb_model = next((m for m in models if "embed" in m.lower()), None)
                if emb_model:
                    self._client = {"base_url": "http://localhost:11434"}
                    self._name = emb_model
                    self._dim = 768
                    logging.info(f"Embedding: Ollama/{emb_model} (768d, local)")
                    return True
        except Exception:
            pass
        return False

    def _try_sentence_transformers(self) -> bool:
        """Try sentence-transformers. Returns True on success."""
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
        if "voyage" in (self._name or ""):
            result = await asyncio.to_thread(self._client.embed, [text], model=self._name)
            return result.embeddings[0]
        elif "text-embedding" in (self._name or ""):
            result = await asyncio.to_thread(
                self._client.embeddings.create,
                model=self._name, input=[text]
            )
            return result.data[0].embedding
        elif self._model:
            vector = await asyncio.to_thread(self._model.encode, text)
            return vector.tolist()
        elif "jina" in (self._name or ""):
            import requests as _req
            r = _req.post(
                f"{self._client['base_url']}/embeddings",
                json={"model": self._name, "input": [text]},
                headers={"Authorization": f"Bearer {self._client['api_key']}"},
                timeout=30,
            )
            return r.json()["data"][0]["embedding"]
        elif isinstance(self._client, dict):  # Ollama
            import requests as _req
            r = _req.post(
                f"{self._client['base_url']}/api/embeddings",
                json={"model": self._name, "prompt": text},
                timeout=30,
            )
            return r.json()["embedding"]
        elif "google" in str(type(self._client)).lower() or "generativeai" in str(type(self._client)).lower():
            result = await asyncio.to_thread(self._client.embed_content, model=self._name, content=text)
            return result["embedding"]
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
    try:
        import requests
        r = requests.get("http://localhost:11434/api/tags", timeout=2)
        if r.status_code < 400:
            models = [m["name"] for m in r.json().get("models", [])]
            emb_model = next((m for m in models if "embed" in m.lower()), None)
            if emb_model:
                ollama_available = True
                ollama_model = emb_model
    except Exception:
        pass
    results.append({
        "id": "ollama",
        "name": "Ollama",
        "dims": 768,
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