"""OCR-2 Welle 35 (high, final): test-file hardening — my responsibility.
Fixes Function-constructor RCE-shape, missing awaits, and assertion gaps in
12 test files. No production code touched."""

import pytest

from conftest import REPO_ROOT  # noqa: E402

REPO = str(REPO_ROOT)


def _read(rel: str) -> str:
    with open(f"{REPO}/{rel}", encoding="utf-8") as f:
        return f.read()