"""Das Lese-Protokoll (20.11.2026): eine Zeile je Lesevorgang.

Bis hierhin protokollierte ``service_log`` nur Aenderungen; Lesen war ein
blosser Zaehler. Damit liess sich nicht nachsehen, WELCHE Eintraege ein Agent
bekommen hat — also auch nicht, warum er Unsinn erinnert hat.

Was hier festgehalten wird:

1. Die Zeile enthaelt genau, was sie enthalten soll: ``ts``, ``actor``,
   ``actor_label``, ``action: "read"``, ``hits``, ``ids``, ``via``, ``key``.
2. Sie enthaelt NICHT: Inhalt, Klartext der Anfrage. Von der Anfrage bleibt
   nur eine Pruefsumme.
3. Eigenes Tagesfile ``read-YYYY-MM-DD.jsonl`` mit eigener Kette, Rotation
   ueber dieselbe ``RETENTION_DAYS`` wie das Aenderungsprotokoll.
4. Fail-open: ein Protokollfehler scheitert nicht am Aufruf.
5. Leerer Abruf schreibt keine Zeile.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from nexus_memory import agent_detect, service_log  # noqa: E402


@pytest.fixture(autouse=True)
def _rotation_frisch(monkeypatch):
    monkeypatch.setattr(service_log, "_rotated_on", None)


def _read_file() -> Path:
    tag = datetime.now(timezone.utc).date().isoformat()
    return service_log.log_dir() / f"read-{tag}.jsonl"


def _zeilen() -> list[dict]:
    path = _read_file()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


KENNUNGEN = [
    "3f8f5c2e-6a1b-4c7d-9e2f-1a2b3c4d5e6f",
    "9a1c2b3d-4e5f-6071-8293-a4b5c6d7e8f9",
]


# ── 1. Die Zeile und ihre Felder ─────────────────────────────────────


def test_ein_lesevorgang_schreibt_genau_eine_zeile():
    entry = service_log.record_read(
        query="Wo liegt der Verteiler?", hits=2, ids=KENNUNGEN,
        via="mcp", actor="ayosha",
    )
    assert entry is not None
    zeilen = _zeilen()
    assert len(zeilen) == 1
    zeile = zeilen[0]
    assert zeile["action"] == "read"
    assert zeile["actor"] == "ayosha"
    assert zeile["hits"] == 2
    assert zeile["ids"] == KENNUNGEN
    assert zeile["via"] == "mcp"
    assert zeile["ts"].startswith(datetime.now(timezone.utc).date().isoformat())


def test_actor_label_steht_neben_der_kennung():
    """Das Label zum Zeitpunkt der Handlung, nicht der aktuelle Name."""
    agent_detect.register_agent(
        agent_id="leser", name="Leser-Bot", icon="", trust_level="trusted",
        install_type="plugin",
    )
    service_log.record_read(query="frage", hits=1, ids=[KENNUNGEN[0]],
                            via="mcp", actor="leser")
    zeile = _zeilen()[0]
    assert zeile["actor"] == "leser"
    assert zeile["actor_label"] == "Leser-Bot"


def test_ohne_abruf_keine_zeile():
    """Ein leerer Abruf ist kein Vorgang — und kein Rauschen im Protokoll."""
    assert service_log.record_read(query="frage", hits=0, ids=[]) is None
    assert service_log.record_read(query="frage", hits=0, ids=None) is None
    assert _zeilen() == []
    assert not _read_file().exists()


# ── 2. Was NICHT drinsteht ───────────────────────────────────────────


def test_kein_inhalt_und_kein_klartext_der_anfrage():
    anfrage = "Rechnung von Dr. Kavacenda ueber 4711 Euro"
    service_log.record_read(query=anfrage, hits=1, ids=[KENNUNGEN[0]],
                            via="mcp", actor="ayosha")
    roh = _read_file().read_text(encoding="utf-8")
    assert "Kavacenda" not in roh
    assert "4711" not in roh
    assert "Rechnung" not in roh
    # Von der Anfrage bleibt die Pruefsumme — gleicher Text, gleiche Summe.
    zeile = _zeilen()[0]
    assert zeile["query_hash"] == service_log.content_hash(
        " ".join(anfrage.split()).lower())
    assert len(zeile["query_hash"]) == service_log.HASH_CHARS


def test_gleiche_anfrage_gleiche_summe_andere_anfrage_andere_summe():
    service_log.record_read(query="Wo liegt der Verteiler?", hits=1,
                            ids=[KENNUNGEN[0]], actor="a")
    service_log.record_read(query="  wo   liegt der verteiler? ", hits=1,
                            ids=[KENNUNGEN[1]], actor="a")
    service_log.record_read(query="Wer hat den Schluessel?", hits=1,
                            ids=[KENNUNGEN[0]], actor="a")
    summen = [z["query_hash"] for z in _zeilen()]
    # Normalisiert (Leerraum, Gross/Klein) ist dieselbe Anfrage dieselbe Summe.
    assert summen[0] == summen[1]
    assert summen[0] != summen[2]


def test_eintraege_nur_als_kennung_kein_text():
    agent_detect.register_agent(
        agent_id="ayosha", name="Ayosha", icon="", trust_level="trusted",
        install_type="plugin",
    )
    service_log.record_read(query="frage", hits=1, ids=[KENNUNGEN[0]],
                            via="mcp", actor="ayosha")
    zeile = _zeilen()[0]
    assert set(zeile) == {"ts", "actor", "actor_label", "action", "hits",
                          "ids", "query_hash", "via", "prev"}
    assert zeile["ids"] == [KENNUNGEN[0]]


# ── 3. Eigenes File, eigene Kette, Rotation ──────────────────────────


def test_eigenes_tagesfile_und_eigene_kette():
    service_log.record_read(query="eins", hits=1, ids=[KENNUNGEN[0]], actor="a")
    service_log.record_read(query="zwei", hits=1, ids=[KENNUNGEN[1]], actor="a")
    # Das Aenderungsprotokoll bleibt unberuehrt.
    assert list(service_log.log_dir().glob("service-*.jsonl")) == []
    zeilen = _zeilen()
    assert len(zeilen) == 2
    assert zeilen[0]["prev"] == service_log.GENESIS
    # Die Kette laeuft ueber die Zeile selbst (ohne ``prev``).
    ohne_prev = {k: v for k, v in zeilen[0].items() if k != "prev"}
    assert zeilen[1]["prev"] == service_log._hash16(
        json.dumps(ohne_prev, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")))


def test_rotation_nimmt_die_lesedateien_mit_derselben_frist():
    """Kein eigener Aufbewahrungswert: dieselbe ``RETENTION_DAYS``."""
    heute = datetime.now(timezone.utc).date()
    alt_tag = (heute - timedelta(days=service_log.RETENTION_DAYS + 1)).isoformat()
    frisch_tag = (heute - timedelta(days=service_log.RETENTION_DAYS - 1)).isoformat()
    alt = service_log.log_dir() / f"read-{alt_tag}.jsonl"
    frisch = service_log.log_dir() / f"read-{frisch_tag}.jsonl"
    service_log.log_dir().mkdir(parents=True, exist_ok=True)
    alt.write_text('{"ts":"x"}\n', encoding="utf-8")
    frisch.write_text('{"ts":"x"}\n', encoding="utf-8")

    service_log.record_read(query="frage", hits=1, ids=[KENNUNGEN[0]], actor="a")

    assert not alt.exists(), "aeltere Lesedatei als RETENTION_DAYS wird entfernt"
    assert frisch.exists(), "juengere bleibt liegen"


# ── 4. Fail-open ─────────────────────────────────────────────────────


def test_protokollfehler_verhindert_den_abruf_nicht(monkeypatch, tmp_path):
    """Ein Schreibfehler wird gemeldet, aber nie geworfen."""
    kaputt = tmp_path / "kein-ordner"
    kaputt.write_text("", encoding="utf-8")  # eine DATEI, kein Verzeichnis
    monkeypatch.setenv("NEXUS_LOG_DIR", str(kaputt / "unterordner"))
    assert service_log.record_read(query="frage", hits=1, ids=[KENNUNGEN[0]],
                                   actor="a") is None


def test_lange_liste_wird_gekuerzt_nicht_abgeschnitten():
    """Eine halbe Kennung waere keine Kennung: die Liste wird gekuerzt."""
    viele = [f"{i:08d}-1111-2222-3333-444455556666" for i in range(40)]
    service_log.record_read(query="frage", hits=len(viele), ids=viele, actor="a")
    zeile = _zeilen()[0]
    assert zeile["hits"] == 40
    assert len(zeile["ids"]) <= service_log.READ_MAX_IDS
    for kennung in zeile["ids"]:
        assert kennung in viele
    roh = _read_file().read_text(encoding="utf-8").splitlines()[0]
    assert len(roh) <= service_log.READ_MAX_LINE_CHARS
