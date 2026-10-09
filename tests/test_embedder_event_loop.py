"""Regressionstest: Der Einbetter muss AUCH aus einem laufenden Event-Loop funktionieren.

Fund 10.10.2026: ``_Embedder.embed`` rief ``asyncio.run`` auf. Das bricht sofort mit
RuntimeError ab, wenn bereits ein Loop laeuft — und genau so ruft Hermes einen
Memory-Provider auf. Folge: nexus_remember UND nexus_recall schlugen fehl.

Der Fehler war unsichtbar, weil Tests ueblicherweise ohne laufenden Loop ausgefuehrt
werden. Dieser Test deckt beide Aufrufkontexte ab.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys

import pytest

WURZEL = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = WURZEL / "plugins" / "memory" / "nexus" / "__init__.py"


@pytest.fixture(scope="module")
def embedder():
    if not PLUGIN.exists():
        pytest.skip("Nexus-Plugin nicht vorhanden")
    spec = importlib.util.spec_from_file_location("_nexus_plugin_under_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod._Embedder()


def test_embed_ohne_laufenden_loop(embedder):
    """Der einfache Fall: kein Loop aktiv (Skripte, Cron, Tests)."""
    vektor = embedder.embed("Testtext ohne laufenden Event-Loop")
    assert isinstance(vektor, list)
    assert len(vektor) == embedder.dim


def test_embed_MIT_laufendem_loop(embedder):
    """Der Fehlerfall: Hermes ruft den Provider aus seinem eigenen Loop auf.

    Ohne den Fix wirft dieser Aufruf
    ``RuntimeError: asyncio.run() cannot be called from a running event loop``.
    """
    ergebnis: dict = {}

    async def aufruf():
        ergebnis["vektor"] = embedder.embed("Testtext aus laufendem Event-Loop")

    asyncio.run(aufruf())

    assert "vektor" in ergebnis, "embed() hat aus dem laufenden Loop nicht geantwortet"
    assert isinstance(ergebnis["vektor"], list)
    assert len(ergebnis["vektor"]) == embedder.dim


def test_beide_wege_liefern_gleiche_laenge(embedder):
    """Beide Aufrufkontexte muessen denselben Vektorraum liefern, sonst
    schreibt der eine Pfad Vektoren, die der andere nicht findet."""
    a = embedder.embed("gleicher Text", is_query=True)
    b: dict = {}

    async def aufruf():
        b["v"] = embedder.embed("gleicher Text", is_query=True)

    asyncio.run(aufruf())
    assert len(a) == len(b["v"]) == embedder.dim


def test_embed_aus_worker_thread_mit_eigenem_loop(embedder):
    """Symmetrie-Fall (Review-Fund): der Aufruf kommt aus einem Worker-Thread,
    der SELBST einen Loop fuehrt. ``get_running_loop()`` findet Loops nur im
    AKTUELLEN Thread — hier muss also ebenfalls der Thread-Pfad greifen."""
    import concurrent.futures

    def im_worker():
        async def innen():
            return embedder.embed("Text aus Worker-Thread mit eigenem Loop")
        return asyncio.run(innen())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        vektor = pool.submit(im_worker).result()

    assert isinstance(vektor, list)
    assert len(vektor) == embedder.dim
