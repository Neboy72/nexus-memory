"""Nexus Memory — central configuration.

Collects constants that would otherwise be scattered as magic strings
across multiple modules.
"""

import os
from typing import Optional

DEFAULT_COLLECTION: Optional[str] = "nexus"
"""Fallback collection name when no explicit value is passed.
Used in production when neither parameter nor $NEXUS_COLLECTION is set."""


def is_success(status_code: int) -> bool:
    """Return True for any 2xx HTTP status code.

    Centralises the "did the request succeed?" check so callers don't
    have to remember that Qdrant can legitimately return 200, 201 (Created)
    or 204 (No Content) — treating only ``== 200`` as success causes
    spurious error logs and aborted flows.
    """
    return 200 <= status_code < 300


def get_collection(override: Optional[str] = None) -> str:
    """Resolve the effective collection name.

    Priority:
    1. override parameter (explicit caller value)
    2. $NEXUS_COLLECTION environment variable
    3. DEFAULT_COLLECTION (config value, currently "nexus")
    4. -> ValueError
    """
    if override:
        return override

    env_collection = os.environ.get("NEXUS_COLLECTION")
    if env_collection:
        return env_collection

    if DEFAULT_COLLECTION is not None:
        return DEFAULT_COLLECTION

    raise ValueError(
        "No collection name specified. "
        "Pass collection_name=<name> or set $NEXUS_COLLECTION."
    )
