"""Regressionstest: Das Claude-Code-Plugin muss ins Gedaechtnis SCHREIBEN koennen.

Fund 09.10.2026: ``auto_capture.store_memory`` baute einen ``urllib.request.Request``
ohne ``method=``. urllib nimmt dann POST, Qdrant verlangt fuer das Anlegen von Punkten
aber PUT. Jeder Schreibversuch endete mit HTTP 400 "missing field `ids`", der
umgebende ``except`` schluckte den Fehler, und der Hook meldete nur nach stderr.
Folge: Claude Code las Erinnerungen, schrieb aber nie welche.

Dieser Test faehrt den echten Weg: schreiben, mit dem offiziellen Client zuruecklesen,
aufraeumen. Er prueft ausdruecklich die HTTP-Methode, damit derselbe Fehler nicht
unbemerkt zurueckkommt.
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.request
import uuid

import pytest

WURZEL = pathlib.Path(__file__).resolve().parents[1]
SKRIPTE = WURZEL / "plugins" / "claude-code" / "scripts"

QDRANT = "http://127.0.0.1:6333"
SAMMLUNG = "nexus_qwen"


def _qdrant_erreichbar() -> bool:
    try:
        with urllib.request.urlopen(f"{QDRANT}/collections", timeout=4):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _qdrant_erreichbar(), reason="Qdrant nicht erreichbar"
)


@pytest.fixture(scope="module")
def capture_modul():
    if not (SKRIPTE / "auto_capture.py").exists():
        pytest.skip("Claude-Code-Plugin nicht vorhanden")
    sys.path.insert(0, str(SKRIPTE))
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_cc_auto_capture_under_test", SKRIPTE / "auto_capture.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _quelle():
    """Der Quelltext der Schreibfunktion — fuer die Methodenpruefung."""
    return (SKRIPTE / "auto_capture.py").read_text()


def test_schreibweg_nutzt_PUT():
    """Qdrant lehnt POST auf diesem Endpunkt mit 400 ab. Ohne PUT schreibt
    das Plugin nichts — und sagt es nicht."""
    src = _quelle()
    # Der Schreibaufruf auf /points (ohne Unterpfad) muss method="PUT" tragen.
    stelle = src.find('collections/{COLLECTION}/points"')
    assert stelle > 0, "Schreibaufruf nicht gefunden"
    umfeld = src[stelle : stelle + 300]
    assert 'method="PUT"' in umfeld or "method='PUT'" in umfeld, (
        "Der Schreibaufruf auf /points braucht method=\"PUT\" — "
        "ohne das antwortet Qdrant mit 400 und der Schreibvorgang verschwindet still."
    )


def test_lesewege_bleiben_POST():
    """Gegenprobe: /search und /scroll erwarten POST. Wer dort PUT setzt,
    macht das Lesen kaputt."""
    src = _quelle()
    for pfad in ("points/search", "points/scroll"):
        stelle = src.find(pfad)
        if stelle < 0:
            continue
        umfeld = src[max(0, stelle - 300) : stelle]
        assert 'method="PUT"' not in umfeld, f"{pfad} muss POST bleiben"


def test_schreiben_und_zuruecklesen(capture_modul):
    """Der scharfe Beweis: ein Punkt faellt an, ist auffindbar und laesst
    sich wieder entfernen."""
    from qdrant_client import QdrantClient
    from qdrant_client.http import models as qm

    marke = f"regression-{uuid.uuid4().hex[:10]}"
    text = f"Regressionstest {marke}: der Schreibweg des Claude-Plugins funktioniert."

    gespeichert = capture_modul.store_memory(text, category="temp")
    assert gespeichert is True, (
        "store_memory() meldete einen Fehler — der Schreibweg ist kaputt "
        "(haeufigste Ursache: fehlendes method=\"PUT\")"
    )

    C = QdrantClient(url=QDRANT)
    treffer, _ = C.scroll(
        SAMMLUNG,
        scroll_filter=qm.Filter(
            must=[qm.FieldCondition(key="text", match=qm.MatchText(text=marke))]
        ),
        limit=5,
        with_payload=True,
    )
    try:
        assert treffer, f"Geschriebener Punkt {marke} ist nicht auffindbar"
    finally:
        if treffer:
            C.delete(
                SAMMLUNG,
                points_selector=qm.PointIdsList(points=[p.id for p in treffer]),
            )


def test_schreibt_trusted_nicht_private(capture_modul):
    """Claude Code ist Stufe 'trusted' und darf NICHTS als 'private' ablegen
    (Nebo-Entscheid 02.09.2026)."""
    import json as _json
    import urllib.request as _url

    marke = f"trustcheck-{uuid.uuid4().hex[:10]}"
    capture_modul.store_memory(f"Trustpruefung {marke}", category="temp")

    C_url = f"{QDRANT}/collections/{SAMMLUNG}/points/scroll"
    body = _json.dumps(
        {"limit": 3, "with_payload": True,
         "filter": {"must": [{"key": "text", "match": {"text": marke}}]}}
    ).encode()
    req = _url.Request(C_url, data=body, headers={"Content-Type": "application/json"})
    with _url.urlopen(req, timeout=8) as r:
        punkte = _json.load(r).get("result", {}).get("points", [])

    try:
        assert punkte, "Trustpruefung konnte nicht zurueckgelesen werden"
        stufe = punkte[0].get("payload", {}).get("access_level")
        assert stufe == "trusted", (
            f"Claude Code muss 'trusted' schreiben, schrieb aber {stufe!r}"
        )
    finally:
        if punkte:
            ids = [p["id"] for p in punkte]
            d = _json.dumps({"points": ids}).encode()
            dreq = _url.Request(
                f"{QDRANT}/collections/{SAMMLUNG}/points/delete?wait=true",
                data=d, headers={"Content-Type": "application/json"},
            )
            _url.urlopen(dreq, timeout=8)
