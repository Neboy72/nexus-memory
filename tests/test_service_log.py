"""Tests fuer das Bewegungsprotokoll (``src/nexus_memory/service_log.py``).

Warum es diesen Test gibt: das Protokoll soll in Sekunden zeigen, wer wann was
getan hat — und dabei selbst kein zweites, ungeschuetztes Gedaechtnis werden.
Deshalb prueft dieser Test nicht nur, dass eine Zeile entsteht, sondern auch
was NICHT in ihr steht: bei ``private`` kein Thema, keine Zahlenwerte aus dem
Inhalt, nie der volle Text, keine Zeile ueber 400 Zeichen.

Dazu die Einbindung an den vorhandenen Wegen: Bibliothek, ``POST /write`` und
die MCP-Werkzeuge schreiben je genau eine Zeile — eine Abweisung des
Schreibtors eingeschlossen. Ein Protokollfehler darf den Vorgang nie
verhindern.

Bewusst am echten ``remember``: ein Fake, der selbst protokolliert, wuerde nur
den Fake testen.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from nexus_memory import service_log

# Rollen (15.11.2026): ``restore``/``guardrail_override`` sind Verwaltung und
# damit ``admin``. Diese Datei prueft das Protokoll, nicht die Berechtigung —
# sie laeuft deshalb als bekannter Admin-Agent (Rollen: test_roles.py).
pytestmark = pytest.mark.usefixtures("admin_caller")

FAKT = "Der Verteiler liegt im Flur neben der Kellertuer"  # 47 Zeichen
ZIEL = "3f8f5c2e-6a1b-4c7d-9e2f-1a2b3c4d5e6f"


# ── Hilfen ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _rotation_frisch(monkeypatch):
    """Rotation je Test einmal laufen lassen.

    ``_rotated_on`` merkt sich den Tag pro Prozess. Ohne das Zuruecksetzen
    wuerde nur der erste Test einer Sitzung rotieren — und der Rotationstest
    waere von der Reihenfolge der Tests abhaengig.
    """
    monkeypatch.setattr(service_log, "_rotated_on", None)


def _lines() -> list[str]:
    """Alle Zeilen aller Tagesdateien im aktuellen Protokollordner."""
    lines: list[str] = []
    for path in sorted(service_log.log_dir().glob("service-*.jsonl")):
        lines.extend(path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line.strip()]


def _entries() -> list[dict]:
    return [json.loads(line) for line in _lines()]


def _today_file() -> Path:
    """Die Tagesdatei, in die ``record`` gerade schreibt."""
    day = datetime.now(timezone.utc).date().isoformat()
    return service_log.log_dir() / f"service-{day}.jsonl"


class _FakeQdrant:
    """Nur die Attribute, die die Schreibwege anfassen.

    ``query_points`` wirft: ein abgewiesener Eintrag darf keine
    Aehnlichkeitssuche starten.
    """

    def __init__(self) -> None:
        self.payload: dict = {}
        self.deleted: list = []
        self.set_payloads: list = []

    def query_points(self, **_kwargs):
        raise AssertionError(
            "ein abgewiesener Eintrag darf keine Aehnlichkeitssuche starten")

    def delete(self, **_kwargs):
        selector = _kwargs.get("points_selector")
        self.deleted.extend(getattr(selector, "points", []) or [])
        return SimpleNamespace(status="completed")

    def retrieve(self, **_kwargs):
        return [SimpleNamespace(payload=dict(self.payload))] if self.payload else []

    def set_payload(self, **kwargs):
        self.set_payloads.append(kwargs)
        return SimpleNamespace(status="completed")


@pytest.fixture
def store(monkeypatch):
    """Ein echter ``MemoryStore`` mit Attrappen fuer Qdrant und Embedder."""
    import nexus_memory.collection_vectors as collection_vectors
    import nexus_memory.mcp_server as mcp

    points: list = []
    monkeypatch.setattr(
        collection_vectors,
        "upsert_named",
        lambda _client, _collection, pts, _vector_name: points.extend(pts),
    )
    monkeypatch.setattr(mcp, "_index_written_point", lambda _payload: None)

    memory_store = object.__new__(mcp.MemoryStore)
    memory_store.vector_name = None
    memory_store._hybrid_retriever = None
    memory_store._skill_graph = None
    memory_store._scope_centroids = None
    memory_store.client = _FakeQdrant()

    async def _embed(_text, is_query=True):
        return [0.1] * 4

    memory_store._embed = _embed
    memory_store.written = points
    monkeypatch.setattr(mcp, "get_store", lambda: memory_store)
    return memory_store


def _http_client(monkeypatch):
    from starlette.testclient import TestClient

    import nexus_memory.mcp_server as mcp

    monkeypatch.setenv("NEXUS_SERVICE_AUTH_KEY", "test-service-key")
    return TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122")


def _http_headers():
    # 15.11.2026: der Installationsschluessel schreibt keine Daten mehr — der
    # HTTP-Weg authentifiziert sich wie ein Plugin mit eigenem Schluessel.
    from nexus_memory import service_auth

    return {service_auth.AUTH_HEADER: service_auth.issue_key("probe-agent", "Probe")}


# ── 1. Eine Zeile je Vorgang, Felder vollstaendig ────────────────────


async def test_ein_vorgang_schreibt_genau_eine_zeile_mit_allen_feldern(store):
    result = await store.remember(
        FAKT, access_level="trusted", category="fact", source="openclaw",
        scope="work", actor="ayosha",
    )
    assert result["status"] == "ok"

    lines = _lines()
    assert len(lines) == 1, "ein Vorgang ist genau eine Zeile"

    entry = json.loads(lines[0])
    assert entry["actor"] == "ayosha"
    assert entry["action"] == "write"
    assert entry["result"] == "ok"
    assert entry["target_id"] == result["id"]
    assert entry["category"] == "fact"
    assert entry["scope"] == "work"
    assert entry["via"] == "library"
    assert entry["topic"].startswith("Der Verteiler")
    # ``write`` traegt nur die Pruefsumme des geschriebenen Stands.
    assert entry["after_hash"] == service_log.content_hash(FAKT)
    assert "before_hash" not in entry
    # Erste Zeile der Datei: die Kette beginnt bei "genesis".
    assert entry["prev"] == "genesis"
    # Zeitstempel ist ISO-8601 in UTC.
    assert entry["ts"].endswith("+00:00")
    datetime.fromisoformat(entry["ts"])


# ── 2. Datenschutz: kein Inhalt, kein Thema bei private ──────────────


async def test_privater_eintrag_hat_kein_thema(store):
    await store.remember(
        FAKT, access_level="private", category="fact", source="openclaw")

    entry = _entries()[0]
    assert entry["action"] == "write"
    assert "topic" not in entry, "bei private bleibt das Thema leer"
    assert FAKT not in "\n".join(_lines()), "der volle Text gehoert nie ins Protokoll"


async def test_zahlenwerte_aus_dem_inhalt_stehen_nicht_im_protokoll(store):
    """Eine Zahl im Text ist oft die Angabe selbst — Tuer-Code, Betrag, Stand."""
    text = "Der Tuer-Code der Garage lautet 4711 und die Fassung ist 0.23.1"
    await store.remember(
        text, access_level="public", category="fact", source="openclaw")

    line = _lines()[0]
    entry = json.loads(line)
    assert "4711" not in line
    assert "0.23.1" not in line
    assert "…" in entry["topic"], "die Zahl wird ersetzt, nicht abgeschrieben"


# ── 3. Laengen: Thema 40, Zeile 400 ──────────────────────────────────


async def test_langes_thema_wird_gekuerzt_und_die_zeile_bleibt_kurz(store):
    text = (
        "Der Verteiler im Flur wurde am 15.11.2026 von einem Techniker "
        "ausgetauscht und dabei neu beschriftet. "
    ) * 4
    await store.remember(
        text, access_level="public", category="fact", source="openclaw")

    line = _lines()[0]
    entry = json.loads(line)
    assert len(entry["topic"]) <= service_log.TOPIC_MAX_CHARS
    assert len(line) <= service_log.MAX_LINE_CHARS


def test_alle_felder_auf_anschlag_bleiben_unter_400_zeichen():
    entry = service_log.record(
        "error", "failed", text="x" * 500, access_level="public",
        target_id="t" * 36, category="fact", scope="s" * 40,
        reason="r" * 200, detail="RuntimeError: " + "y" * 500,
        via="cli", actor="a" * 64,
    )

    assert entry is not None
    line = _lines()[0]
    assert len(line) <= service_log.MAX_LINE_CHARS
    json.loads(line)  # bleibt gueltiges JSON, nur gekuerzt


def test_ausnahme_steht_als_error_mit_kurzem_detail_im_protokoll():
    try:
        raise RuntimeError("erste Zeile\nzweite Zeile " + "z" * 500)
    except RuntimeError as exc:
        entry = service_log.record_error(
            "write", exc, access_level="private", via="mcp")

    assert entry is not None
    assert entry["action"] == "error", "eine Ausnahme ist action=error"
    assert entry["result"] == "failed"
    assert entry["reason"] == "write failed", \
        "der gescheiterte Weg bleibt benannt"
    assert entry["detail"].startswith("RuntimeError: erste Zeile")
    assert len(entry["detail"]) <= service_log.DETAIL_MAX_CHARS
    assert "zweite Zeile" not in entry["detail"]


# ── 4. Abgewiesene Schreibvorgaenge ──────────────────────────────────


async def test_abgewiesener_schreibvorgang_steht_als_denied_im_protokoll(store):
    result = await store.remember(
        "   ", category="fact", source="openclaw", via="http")

    assert result["status"] == "rejected"
    entries = _entries()
    assert len(entries) == 1, "auch eine Abweisung ist genau eine Zeile"
    entry = entries[0]
    assert entry["action"] == "denied"
    assert entry["result"] == "refused"
    assert entry["via"] == "http"
    assert "empty" in entry["reason"]
    # Eine Abweisung aendert nichts — kein Stand davor, keiner danach.
    assert "before_hash" not in entry
    assert "after_hash" not in entry


def test_http_weg_protokolliert_die_abweisung_genau_einmal(store, monkeypatch):
    client = _http_client(monkeypatch)
    response = client.post(
        "/write",
        json={"content": "   ", "category": "fact", "source": "openclaw_tool",
              "source_type": "chat"},
        headers=_http_headers(),
    )

    assert response.status_code == 422
    entries = _entries()
    assert len(entries) == 1, "die 422-Abweisung darf nicht doppelt protokolliert werden"
    assert entries[0]["action"] == "denied"
    assert entries[0]["via"] == "http"


# ── 5. Fail-open: ein Protokollfehler haelt nichts auf ───────────────


async def test_protokollfehler_haelt_den_vorgang_nicht_auf(
        store, tmp_path, monkeypatch, capsys):
    sperre = tmp_path / "kein-ordner"
    sperre.write_text("eine Datei, kein Ordner", encoding="utf-8")
    monkeypatch.setenv(service_log.LOG_DIR_ENV, str(sperre / "logs"))

    # Das Protokoll meldet den Fehler auf stderr und gibt None zurueck …
    assert service_log.record("write", "ok", text=FAKT) is None
    assert "service_log" in capsys.readouterr().err

    # … und der eigentliche Vorgang laeuft trotzdem durch.
    result = await store.remember(
        FAKT, access_level="private", category="fact", source="openclaw")
    assert result["status"] == "ok"
    assert len(store.written) == 1, "der Eintrag wurde geschrieben"


# ── 6. Rotation ──────────────────────────────────────────────────────


def test_rotation_entfernt_dateien_aelter_als_neunzig_tage():
    ordner = service_log.log_dir()
    ordner.mkdir(parents=True, exist_ok=True)
    heute = datetime.now(timezone.utc).date()
    alt = ordner / f"service-{(heute - timedelta(days=100)).isoformat()}.jsonl"
    frisch = ordner / f"service-{(heute - timedelta(days=30)).isoformat()}.jsonl"
    for path in (alt, frisch):
        path.write_text("{}\n", encoding="utf-8")

    service_log.record("write", "ok", text=FAKT, access_level="private")

    assert not alt.exists(), "Dateien aelter als 90 Tage werden entfernt"
    assert frisch.exists(), "Dateien von vor 30 Tagen bleiben liegen"


# ── 6b. Kette: Manipulation erkennbar ────────────────────────────────


def test_kette_ist_intakt_und_verify_meldet_ok():
    for index in range(3):
        service_log.record("write", "ok", actor="ayosha", target_id=f"t{index}")

    result = service_log.verify_chain(_today_file())

    assert result == {"ok": True, "lines": 3, "broken_at": None}
    entries = _entries()
    assert entries[0]["prev"] == "genesis"
    assert entries[1]["prev"] == service_log._line_hash(entries[0])
    assert entries[2]["prev"] == service_log._line_hash(entries[1])


def test_verify_erkennt_eine_manipulierte_zeile():
    for index in range(3):
        service_log.record("write", "ok", actor="ayosha", target_id=f"t{index}")
    path = _today_file()
    lines = path.read_text(encoding="utf-8").splitlines()

    # Die mittlere Zeile wird nachtraeglich geaendert. Ihre eigene Pruefsumme
    # passt noch zur Zeile davor — aber die NAECHSTE Zeile verweist auf den
    # alten Stand. Genau dort bricht die Kette.
    second = json.loads(lines[1])
    second["actor"] = "fremd"
    lines[1] = json.dumps(second, ensure_ascii=False, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = service_log.verify_chain(path)

    assert result["ok"] is False
    assert result["broken_at"] == 3
    assert result["lines"] == 3


def test_verify_erkennt_eine_geloeschte_zeile():
    for index in range(3):
        service_log.record("write", "ok", actor="ayosha", target_id=f"t{index}")
    path = _today_file()
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = service_log.verify_chain(path)

    assert result["ok"] is False
    assert result["broken_at"] == 2


def test_fehlendes_prev_mitten_in_der_kette_ist_ein_bruch():
    """Das Feld zu entfernen darf die Kette nicht umgehen."""
    for index in range(3):
        service_log.record("write", "ok", actor="ayosha", target_id=f"t{index}")
    path = _today_file()
    lines = path.read_text(encoding="utf-8").splitlines()
    second = json.loads(lines[1])
    second.pop("prev")
    lines[1] = json.dumps(second, ensure_ascii=False, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = service_log.verify_chain(path)

    assert result["ok"] is False
    assert result["broken_at"] == 2


def test_altbestand_ohne_prev_vor_der_kette_bricht_sie_nicht():
    """Zeilen von heute VOR der Kette haben kein ``prev``.

    Sie stehen als geschlossener Anfang vor der ersten verketteten Zeile; die
    Kette haengt an ihrer Pruefsumme. Ein Bruch entsteht dadurch nicht.
    """
    path = _today_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {"ts": "2026-11-15T08:00:00+00:00", "actor": "ayosha",
              "action": "write", "result": "ok", "target_id": "alt"}
    path.write_text(json.dumps(legacy, ensure_ascii=False) + "\n", encoding="utf-8")

    service_log.record("write", "ok", actor="ayosha", target_id="neu")

    assert service_log.verify_chain(path) == {"ok": True, "lines": 2,
                                              "broken_at": None}


def test_cli_verify_meldet_eine_kaputte_kette_mit_rueckgabewert(capsys):
    service_log.record("write", "ok", actor="ayosha", target_id="a")
    service_log.record("write", "ok", actor="ayosha", target_id="b")

    assert service_log.main(["--verify"]) == 0
    meldung = json.loads(capsys.readouterr().out)
    assert meldung == {"ok": True, "lines": 2, "broken_at": None}

    path = _today_file()
    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["actor"] = "fremd"
    lines[0] = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert service_log.main(["--verify"]) == 1
    assert json.loads(capsys.readouterr().out)["broken_at"] == 2


def test_cli_verify_ohne_datei_ist_ein_fehler(capsys):
    """Ein fehlendes Protokoll darf nicht wie ein intaktes aussehen."""
    assert service_log.main(["--verify", "--date", "2000-01-01"]) == 2
    assert "kein Protokoll" in capsys.readouterr().err


# ── 7. Lesen und Filtern ─────────────────────────────────────────────


def test_read_events_filtert_nach_aktion_und_ausloeser():
    service_log.record("write", "ok", actor="ayosha", target_id="a")
    service_log.record("forget", "ok", actor="ayosha", target_id="b",
                       reason="veraltet")
    service_log.record("write", "ok", actor="hermes", target_id="c")

    alle = service_log.read_events()
    assert [entry["target_id"] for entry in alle] == ["c", "b", "a"], \
        "neueste zuerst"

    nur_forget = service_log.read_events(action="forget")
    assert [entry["target_id"] for entry in nur_forget] == ["b"]

    nur_ayosha = service_log.read_events(actor="ayosha")
    assert {entry["target_id"] for entry in nur_ayosha} == {"a", "b"}

    assert [entry["target_id"] for entry in service_log.read_events(limit=1)] == ["c"]


def test_read_events_schneidet_mit_since_ab():
    service_log.record("write", "ok", actor="ayosha", target_id="a")

    assert service_log.read_events(since="2000-01-01")
    assert service_log.read_events(since="2999-01-01") == []


def test_read_events_liest_genau_einen_tag(monkeypatch):
    """``day`` liest nur diese Tagesdatei — das Dashboard fragt einen Tag ab.

    Ohne den Filter muesste es alle neueren Tage lesen und koennte bei einem
    alten Tag hinter lauter juengeren Zeilen nichts finden.
    """
    heute = datetime.now(timezone.utc).date()
    gestern = heute - timedelta(days=1)

    monkeypatch.setattr(service_log, "_today", lambda: heute)
    service_log.record("write", "ok", actor="ayosha", target_id="heute")
    monkeypatch.setattr(service_log, "_today", lambda: gestern)
    service_log.record("write", "ok", actor="ayosha", target_id="gestern")

    assert [e["target_id"] for e in
            service_log.read_events(day=heute.isoformat())] == ["heute"]
    assert [e["target_id"] for e in
            service_log.read_events(day=gestern.isoformat())] == ["gestern"]
    assert service_log.read_events(day="2000-01-01") == []

    with pytest.raises(ValueError):
        # Ein stillschweigend ignorierter Tagesfilter zeigt mehr als gewollt.
        service_log.read_events(day="15.11.2026")


# ── 8. CLI ───────────────────────────────────────────────────────────


def test_cli_zeigt_die_juengsten_vorgaenge(capsys):
    service_log.record("write", "ok", actor="ayosha", target_id="a")
    service_log.record("forget", "ok", actor="ayosha", target_id="b",
                       reason="veraltet")

    assert service_log.main(["--tail", "1", "--action", "forget"]) == 0

    zeilen = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(zeilen) == 1
    assert json.loads(zeilen[0])["target_id"] == "b"


def test_cli_weist_einen_kaputten_since_filter_ab():
    """Ein stillschweigend ignorierter Filter wuerde mehr zeigen als gewollt."""
    with pytest.raises(SystemExit):
        service_log.main(["--since", "irgendwann"])


# ── 9. Einbindung: jeder Weg nennt sich selbst ───────────────────────


async def test_mcp_werkzeug_remember_schreibt_eine_zeile_mit_via_mcp(store):
    import nexus_memory.mcp_server as mcp

    await mcp.handle_call_tool("remember", {"text": FAKT, "category": "fact"})

    entries = _entries()
    assert len(entries) == 1
    assert entries[0]["action"] == "write"
    assert entries[0]["via"] == "mcp"


def test_http_weg_remember_schreibt_eine_zeile_mit_via_http(store, monkeypatch):
    client = _http_client(monkeypatch)
    response = client.post(
        "/write",
        json={"content": FAKT, "category": "fact", "access_level": "trusted",
              "source": "openclaw_tool", "source_type": "chat"},
        headers=_http_headers(),
    )

    assert response.status_code == 200
    entries = _entries()
    assert len(entries) == 1, "der Dienstweg protokolliert genau einmal"
    assert entries[0]["action"] == "write"
    assert entries[0]["via"] == "http"


async def test_mcp_forget_protokolliert_ziel_und_grund(store):
    import nexus_memory.mcp_server as mcp

    await mcp.handle_call_tool(
        "forget", {"memory_id": ZIEL, "reason": "veraltet"})

    entry = _entries()[0]
    assert entry["action"] == "forget"
    assert entry["result"] == "ok"
    assert entry["target_id"] == ZIEL
    assert entry["reason"] == "veraltet"
    assert entry["via"] == "mcp"
    assert "topic" not in entry, "ein Loeschvorgang hat keinen Inhalt"


async def test_mcp_update_protokolliert_kategorie_bereich_und_stufe(
        store, monkeypatch):
    import nexus_memory.mcp_server as mcp

    store.client.payload = {
        "category": "fact", "scope": "work", "access_level": "trusted",
    }
    monkeypatch.setattr("nexus.nexus_update", lambda **_kwargs: {"status": "ok"})

    await mcp.handle_call_tool("update", {"memory_id": ZIEL, "text": FAKT})

    entry = _entries()[0]
    assert entry["action"] == "update"
    assert entry["result"] == "ok"
    assert entry["target_id"] == ZIEL
    assert entry["category"] == "fact"
    assert entry["scope"] == "work"
    assert entry["topic"].startswith("Der Verteiler")
    assert entry["via"] == "mcp"


# ── 10. Vorher-nachher als Pruefsumme ────────────────────────────────


async def test_update_traegt_pruefsumme_vorher_und_nachher(store, monkeypatch):
    import nexus_memory.mcp_server as mcp

    ALT = "Der Verteiler steht im Flur"
    NEU = "Der Verteiler steht im Keller"
    store.client.payload = {
        "content": ALT, "category": "fact", "scope": "work",
        "access_level": "trusted",
    }
    monkeypatch.setattr("nexus.nexus_update", lambda **_kwargs: {"status": "ok"})

    await mcp.handle_call_tool("update", {"memory_id": ZIEL, "text": NEU})

    entry = _entries()[0]
    assert entry["before_hash"] == service_log.content_hash(ALT)
    assert entry["after_hash"] == service_log.content_hash(NEU)
    assert entry["before_hash"] != entry["after_hash"]
    # Kein Inhalt, nur Pruefsummen.
    assert ALT not in "\n".join(_lines())


async def test_forget_traegt_nur_die_pruefsumme_davor(store):
    """Nach dem Loeschen gibt es keinen Stand mehr — also nur ``before_hash``."""
    import nexus_memory.mcp_server as mcp

    ALT = "Der Verteiler steht im Flur"
    store.client.payload = {
        "content": ALT, "category": "fact", "access_level": "private",
    }

    await mcp.handle_call_tool("forget", {"memory_id": ZIEL})

    entry = _entries()[0]
    assert entry["action"] == "forget"
    assert entry["result"] == "ok"
    assert entry["before_hash"] == service_log.content_hash(ALT)
    assert "after_hash" not in entry
    assert ALT not in "\n".join(_lines())


# ── 11. Die Luecke: Plugin und Hook protokollieren selbst ────────────


def test_hermes_plugin_pfad_erzeugt_genau_eine_zeile(monkeypatch):
    """``_upsert`` ist der Hauptschreiber (3.455 Sitzungen) und schrieb
    bisher direkt nach Qdrant — ohne Protokollzeile."""
    import nexus_memory.collection_vectors as collection_vectors
    import plugins.memory.nexus as plugin

    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)

    provider = object.__new__(plugin.NexusMemoryProvider)
    provider._qdrant = object()
    provider._collection = "nexus"
    provider.vector_name = None
    provider._embedder = SimpleNamespace(
        embed=lambda text, is_query=False: [0.1] * 4)

    monkeypatch.setattr(plugin, "_index_written_point", lambda *_a, **_k: None)
    monkeypatch.setattr(plugin.NexusMemoryProvider, "_bump_agent_stats",
                        lambda *_a, **_k: None)
    written: list = []
    monkeypatch.setattr(
        collection_vectors, "upsert_named",
        lambda _client, _collection, pts, _vector_name: written.extend(pts),
    )

    result = provider._upsert(
        text=FAKT, category="fact", access_level="trusted",
        source="hermes-plugin",
    )

    assert result["status"] == "ok"
    assert len(written) == 1, "der Punkt wurde geschrieben"
    entries = _entries()
    assert len(entries) == 1, "Plugin-Weg: genau eine Zeile, keine Doppelzaehlung"
    assert entries[0]["action"] == "write"
    assert entries[0]["via"] == "plugin"
    assert entries[0]["target_id"] == written[0].payload["id"]
    assert entries[0]["category"] == "fact"
    assert entries[0]["after_hash"] == service_log.content_hash(FAKT)
    # Der Pfad schreibt direkt nach Qdrant — er muss sich selbst benennen.
    assert entries[0]["actor"] == "hermes"


def test_hermes_plugin_entity_pfad_erzeugt_genau_eine_zeile(monkeypatch):
    import nexus_memory.collection_vectors as collection_vectors
    import plugins.memory.nexus as plugin

    provider = object.__new__(plugin.NexusMemoryProvider)
    provider._qdrant = object()
    provider._collection = "nexus"
    provider.vector_name = None
    provider._embedder = SimpleNamespace(
        embed=lambda text, is_query=False: [0.1] * 4)

    class _Cache:
        def get(self, _text, _is_query=True):
            return None

        def put(self, _text, _vector, _is_query=True):
            return None

    provider._embed_cache = _Cache()
    monkeypatch.setattr(plugin, "_index_written_point", lambda *_a, **_k: None)
    written: list = []
    monkeypatch.setattr(
        collection_vectors, "upsert_named",
        lambda _client, _collection, pts, _vector_name: written.extend(pts),
    )

    entity = SimpleNamespace(
        entity_type="software", name="Nexus", attributes={},
        evidence="laeuft auf dem Server", confidence=0.9,
    )
    result = provider._upsert_entity(entity)

    assert result["status"] == "ok"
    entries = _entries()
    assert len(entries) == 1
    assert entries[0]["action"] == "write"
    assert entries[0]["via"] == "plugin"
    assert entries[0]["category"] == "entity"
    assert entries[0]["target_id"] == written[0].payload["id"]


def _load_plugin_module(relpath: str, name: str):
    """Ein Hook-Skript als Modul laden (es liegt ausserhalb des Pakets)."""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / relpath
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Der Hook stellt sein eigenes Verzeichnis an den Anfang von ``sys.path``.
    # Fuer den Test wird das zurueckgenommen: sonst koennte ein Modul gleichen
    # Namens aus dem Hook-Verzeichnis spaetere Tests verdecken.
    before = list(sys.path)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = before
    return module


def _load_auto_capture():
    """Den Claude-Code-Hook als Modul laden (er liegt ausserhalb des Pakets)."""
    return _load_plugin_module(
        "plugins/claude-code/scripts/auto_capture.py",
        "nexus_auto_capture_under_test")


def test_claude_code_hook_pfad_erzeugt_genau_eine_zeile(tmp_path, monkeypatch):
    module = _load_auto_capture()
    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)
    monkeypatch.setattr(module, "AGENTS_FILE", tmp_path / "agents.json")
    monkeypatch.setattr(module, "get_embedding", lambda _text: [0.1] * 4)
    monkeypatch.setattr(module, "_point_vector", lambda embedding: embedding)

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(module.urllib.request, "urlopen",
                        lambda *_a, **_k: _Response())

    assert module.store_memory(FAKT, "session") is True

    entries = _entries()
    assert len(entries) == 1, "Hook-Weg: genau eine Zeile"
    assert entries[0]["action"] == "write"
    assert entries[0]["via"] == "hook"
    assert entries[0]["after_hash"] == service_log.content_hash(FAKT)
    # Der Hook schreibt direkt nach Qdrant — er muss sich selbst benennen.
    assert entries[0]["actor"] == "claude-code"


# ── 12. Absender: der Schluessel nennt den Client ────────────────────
#
# 15.11.2026: der Installationsschluessel liest keine Daten mehr. Jeder Client
# meldet sich einmal ueber ``POST /enroll`` an und schickt danach NUR seinen
# eigenen Schluessel. Der Kopf ``x-nexus-agent-id`` wird nicht mehr gesendet —
# der Schluessel bestimmt die Identitaet, nicht eine Behauptung.


def _capture_urlopen(monkeypatch) -> dict:
    """Alle ``urllib``-Anfragen abfangen: Ziel, Kopfzeilen, Koerper.

    ``/enroll`` antwortet wie der echte Dienst mit einem frischen Schluessel,
    alles andere mit einem Vektor — so laeuft der ganze Weg (anmelden, dann
    einbetten) durch denselben Test.
    """
    import urllib.request as urlreq

    calls: list = []

    class _Antwort:
        status = 200

        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return self._body

    def _urlopen(req, timeout=None):
        call = {
            "url": req.full_url,
            "headers": {key.lower(): value for key, value in req.header_items()},
            "body": (req.data or b"").decode("utf-8", "replace"),
        }
        calls.append(call)
        if call["url"].endswith("/enroll"):
            return _Antwort(json.dumps({
                "agent_id": "probe123", "key": "agent-key-123",
                "is_owner": False, "created": True,
            }).encode())
        return _Antwort(b'{"embedding":[0.1,0.2],"model":"test","dim":2}')

    monkeypatch.setattr(urlreq, "urlopen", _urlopen)
    return {"calls": calls}


def test_hermes_plugin_enrolls_and_then_sends_only_its_own_key(monkeypatch):
    """Der Hermes-Schreiber meldet sich selbst an und schickt danach seinen Schluessel."""
    import plugins.memory.nexus as plugin

    monkeypatch.setenv("NEXUS_SERVICE_AUTH_KEY", "test-key")
    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)
    captured = _capture_urlopen(monkeypatch)

    assert plugin._embed_via_service(FAKT) is not None

    enroll, embed = captured["calls"]
    assert enroll["url"].endswith("/enroll")
    assert enroll["headers"]["x-nexus-service-key"] == "test-key"
    assert json.loads(enroll["body"])["harness"] == "hermes"

    assert embed["url"].endswith("/embed")
    assert embed["headers"]["x-nexus-service-key"] == "agent-key-123"
    assert "x-nexus-agent-id" not in embed["headers"], \
        "der Kopf bestimmt keine Identitaet mehr"

    key_file = Path(os.environ["NEXUS_DATA_DIR"]) / "agent_keys" / "hermes.key"
    assert key_file.read_text(encoding="utf-8") == "agent-key-123"
    assert (key_file.stat().st_mode & 0o777) == 0o600


def test_hermes_plugin_reuses_the_stored_key(monkeypatch):
    """Liegt der eigene Schluessel schon da, wird NICHT erneut angemeldet."""
    import plugins.memory.nexus as plugin

    monkeypatch.setenv("NEXUS_SERVICE_AUTH_KEY", "test-key")
    key_file = Path(os.environ["NEXUS_DATA_DIR"]) / "agent_keys" / "hermes.key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text("stored-key", encoding="utf-8")
    captured = _capture_urlopen(monkeypatch)

    assert plugin._embed_via_service(FAKT) is not None

    assert len(captured["calls"]) == 1, "kein zweites /enroll"
    assert captured["calls"][0]["url"].endswith("/embed")
    assert captured["calls"][0]["headers"]["x-nexus-service-key"] == "stored-key"


def test_claude_code_hook_enrolls_and_sends_only_its_own_key(monkeypatch):
    """Der Claude-Code-Hook geht denselben Weg."""
    monkeypatch.setenv("NEXUS_SERVICE_AUTH_KEY", "test-key")
    monkeypatch.delenv("NEXUS_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)
    module = _load_plugin_module(
        "plugins/claude-code/scripts/auto_capture.py",
        "nexus_auto_capture_header")
    captured = _capture_urlopen(monkeypatch)

    assert module.get_embedding(FAKT)

    enroll, embed = captured["calls"]
    assert enroll["url"].endswith("/enroll")
    assert json.loads(enroll["body"])["harness"] == "claude-code"
    assert embed["url"].endswith("/embed")
    assert embed["headers"]["x-nexus-service-key"] == "agent-key-123"
    assert "x-nexus-agent-id" not in embed["headers"]


_OPENCLAW_HEADER_SCRIPT = """
import { WriteGateClient } from "./plugins/openclaw/lib/write-gate.ts";
const calls = [];
globalThis.fetch = async (url, init) => {
  calls.push({ url: String(url), headers: init.headers });
  if (String(url).endsWith("/enroll")) {
    return new Response(
      JSON.stringify({ agent_id: "probe123", key: "agent-key-node", is_owner: false, created: true }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  }
  return new Response(JSON.stringify({ reason: "probe" }), {
    status: 500,
    headers: { "content-type": "application/json" },
  });
};
const cfg = {
  writeGate: { url: "http://127.0.0.1:9122", key: "test-key" },
  scope: "openclaw-voice",
};
const client = new WriteGateClient(cfg, "chat");
try {
  await client.upsert("id-1", [], { content: "Der Verteiler steht im Flur" });
} catch (_err) {
  // Ein 500er ist Absicht: er bricht VOR der Erfolgszaehlung ab.
}
console.log(JSON.stringify(calls));
"""


def _node_supports_ts_strip() -> bool:
    """Die TS-Datei wird direkt importiert — das braucht node >= 22.6."""
    import subprocess

    try:
        done = subprocess.run(["node", "--version"], capture_output=True,
                              text=True, timeout=15)
        parts = done.stdout.strip().lstrip("v").split(".")
        return (int(parts[0]), int(parts[1])) >= (22, 6)
    except Exception:
        return False


def test_openclaw_write_gate_enrolls_and_sends_only_its_own_key():
    """Das OpenClaw-Schreibtor meldet sich an und schickt danach seinen Schluessel."""
    import subprocess

    if not _node_supports_ts_strip():
        pytest.skip("requires node >= 22.6 with --experimental-strip-types")

    done = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module",
         "-e", _OPENCLAW_HEADER_SCRIPT],
        capture_output=True, text=True,
        cwd=Path(__file__).resolve().parents[1], timeout=120,
    )
    assert done.returncode == 0, f"node failed: {done.stderr[-800:]}"

    calls = json.loads(done.stdout.strip().splitlines()[-1])
    enroll, write = calls[0], calls[1]
    assert enroll["url"].endswith("/enroll")
    assert enroll["headers"]["x-nexus-service-key"] == "test-key"
    assert write["url"].endswith("/write")
    assert write["headers"]["x-nexus-service-key"] == "agent-key-node"
    assert "x-nexus-agent-id" not in write["headers"]


def test_dienst_weg_nennt_den_schluessel_inhaber(store, monkeypatch):
    """Die Zeile des Dienstwegs traegt die Kennung aus dem Schluessel."""
    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)
    client = _http_client(monkeypatch)
    from nexus_memory import service_auth

    response = client.post(
        "/write",
        json={"content": FAKT, "category": "fact", "access_level": "trusted",
              "source": "openclaw_tool", "source_type": "chat"},
        headers={service_auth.AUTH_HEADER:
                 service_auth.issue_key("openclaw", "OpenClaw")},
    )

    assert response.status_code == 200
    entries = _entries()
    assert len(entries) == 1
    assert entries[0]["actor"] == "openclaw", \
        "die Zeile nennt den Aufrufer, nicht 'unidentified'"


def test_ohne_schluessel_wird_gar_nichts_protokolliert(store, monkeypatch):
    """Ohne Schluessel gibt es keinen Absender — und keine Zeile.

    15.11.2026: der Kopf ``x-nexus-agent-id`` erfindet keine Identitaet mehr;
    ein Aufruf ohne gueltigen Schluessel endet an der Middleware mit 401.
    """
    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)
    client = _http_client(monkeypatch)

    response = client.post(
        "/write",
        json={"content": FAKT, "category": "fact", "access_level": "trusted",
              "source": "openclaw_tool", "source_type": "chat"},
        headers={"x-nexus-agent-id": "openclaw"},
    )

    assert response.status_code == 401
    assert _entries() == []


# ── 13. Die letzten Wege: je Vorgang genau eine Zeile ────────────────
#
# Diese fuenf Wege legten Punkte an oder ueberschrieben sie, ohne dass eine
# Zeile sie nannte. Geprueft wird je Weg die ZAHL der Zeilen — eine zu viel
# waere Doppelzaehlung, eine zu wenig die alte Luecke.


async def test_konsolidierung_schreibt_genau_eine_zeile(store):
    from nexus_memory.consolidation import Consolidator

    konsolidierer = Consolidator(store, "nexus", embed_fn=lambda _text: [0.1] * 4)

    new_id, scope = konsolidierer._store_fact(
        FAKT, "quell-punkt", {"access_level": "trusted"})

    assert len(store.written) == 1, "der verdichtete Fakt wurde geschrieben"
    entries = _entries()
    assert len(entries) == 1, "Konsolidierung: genau eine Zeile je Fakt"
    entry = entries[0]
    assert entry["action"] == "write"
    assert entry["result"] == "ok"
    assert entry["via"] == "consolidation"
    assert entry["target_id"] == new_id
    assert entry["scope"] == scope
    assert entry["category"] == "fact"
    assert entry["actor"] == "system:consolidation", \
        "ein Systemvorgang traegt seinen Bereich, nicht 'unidentified'"
    assert entry["after_hash"] == service_log.content_hash(FAKT)


def test_systemvorgaenge_tragen_ihren_bereich(monkeypatch):
    """Ein Systemvorgang hat keinen Agenten, aber einen Urheber.

    Aufraeumlauf, SICA und Staging schrieben ``unidentified`` — die falsche
    Aussage: der Urheber war bekannt, er war nur kein Agent. ``unidentified``
    bleibt fuer echte Fremdzugriffe ohne Absender.
    """
    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)
    for via, bereich in (
        ("consolidation", "system:consolidation"),
        ("sica", "system:sica"),
        ("staging", "system:staging"),
        ("guard", "system:guard"),
    ):
        entry = service_log.record("write", "ok", via=via)
        assert entry["actor"] == bereich, f"via={via}"

    restore = service_log.record("restore", "ok", via="mcp")
    assert restore["actor"] == "system:restore", \
        "ohne Aufrufer ist ein Restore ein Vorgang des Dienstes"

    fremd = service_log.record("write", "ok", via="http")
    assert fremd["actor"] == "unidentified", \
        "ein Fremdzugriff ohne Absender bleibt unidentified"


def test_ein_bekannter_aufrufer_schlaegt_den_bereich(monkeypatch):
    """Nennt der Aufrufer sich, steht seine Kennung in der Zeile."""
    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)
    assert service_log.record(
        "restore", "ok", via="mcp", actor="ayosha")["actor"] == "ayosha"
    assert service_log.record(
        "write", "ok", via="sica", actor="hermes")["actor"] == "hermes"


async def test_restore_schreibt_genau_eine_zeile(store, tmp_path):
    """Ein Restore ist EINE Zeile — nicht eine je zurueckgeschriebenem Punkt."""
    import nexus_memory.mcp_server as mcp

    backup = tmp_path / "sicherung-2026-11-15.json"
    backup.write_text(json.dumps({"points": [
        {"id": ZIEL,
         "payload": {"content": FAKT, "category": "fact",
                     "access_level": "trusted"},
         "vector": [0.1, 0.2, 0.3, 0.4]},
        {"id": "8e2cb680-1111-4c7d-9e2f-1a2b3c4d5e6f",
         "payload": {"content": "Der Verteiler steht im Keller",
                     "category": "fact", "access_level": "trusted"},
         "vector": [0.4, 0.3, 0.2, 0.1]},
    ]}), encoding="utf-8")

    await mcp.handle_call_tool("restore", {"backup_path": str(backup)})

    entries = _entries()
    assert len(entries) == 1, "zwei Punkte, eine Zeile — kein Doppelzaehlen"
    entry = entries[0]
    assert entry["action"] == "restore"
    assert entry["result"] == "ok"
    assert entry["via"] == "mcp"
    assert entry["actor"] == "system:restore", \
        "ohne Aufrufer ist ein Restore ein Vorgang des Dienstes"
    assert entry["reason"] == "backup sicherung-2026-11-15.json"
    assert entry["detail"] == "2 points"
    assert "after_hash" not in entry, "ein Restore traegt keinen neuen Stand"
    assert FAKT not in "\n".join(_lines())


def test_guardrail_override_schreibt_genau_eine_zeile():
    from unittest.mock import MagicMock

    from nexus_memory.guardrails import GuardrailEngine

    engine = GuardrailEngine(MagicMock(), vector_dim=4)
    override_id = engine.record_override(
        command="rm -rf ~/nexus-memory-test/",
        matched_rules=[{
            "protected_path": "~/nexus-memory-test/",
            "rule_text": "NIEMALS loeschen",
            "source_memory_id": "rule1",
        }],
        reasoning="Aufraeumen nach Sicherung ausdruecklich freigegeben",
        agent_id="ayosha",
    )

    entries = _entries()
    assert len(entries) == 1, "Guardrail-Override: genau eine Zeile"
    entry = entries[0]
    assert entry["action"] == "guardrail_override"
    assert entry["result"] == "ok"
    assert entry["via"] == "guard"
    assert entry["actor"] == "ayosha"
    assert entry["target_id"] == override_id
    assert entry["detail"] == "1 rules"
    # Kein Inhalt: Befehl und Begruendung stehen im Audit-Punkt, nicht hier.
    assert "rm -rf" not in "\n".join(_lines())


def test_guardrail_override_ohne_aufrufer_ist_ein_systemvorgang(monkeypatch):
    """Der Vorgabewert ``"unknown"`` heisst: kein Aufrufer bekannt.

    Dann ist der Override ein Vorgang des Waechters (``system:guard``) — nicht
    ein Agent namens "unknown" und nicht ``unidentified``.
    """
    from unittest.mock import MagicMock

    from nexus_memory.guardrails import GuardrailEngine

    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)
    engine = GuardrailEngine(MagicMock(), vector_dim=4)
    engine.record_override(
        command="rm -rf ~/nexus-memory-test/",
        matched_rules=[{"protected_path": "~/nexus-memory-test/",
                        "rule_text": "NIEMALS loeschen",
                        "source_memory_id": "rule1"}],
        reasoning="Aufraeumen nach Sicherung ausdruecklich freigegeben",
    )

    assert _entries()[0]["actor"] == "system:guard"


def test_sica_schreibt_genau_eine_zeile(monkeypatch):
    import nexus_memory.collection_vectors as collection_vectors
    from nexus.sica import SICAResult, _store_sica_session

    written: list = []
    monkeypatch.setattr(
        collection_vectors, "upsert_named",
        lambda _client, _collection, pts, _vector_name: written.extend(pts),
    )

    class _Client:
        def get_collection(self, **_kwargs):
            raise RuntimeError("Dimensionspruefung nicht moeglich — fail-open")

    result = SICAResult()
    result.total_scanned = 12
    embedder = SimpleNamespace(embed=lambda _text: [0.1, 0.2], dim=2)

    _store_sica_session(_Client(), "nexus", result, embedder=embedder)

    assert len(written) == 1, "der Sitzungspunkt wurde geschrieben"
    entries = _entries()
    assert len(entries) == 1, "SICA: genau eine Zeile je Sitzung"
    entry = entries[0]
    assert entry["action"] == "write"
    assert entry["via"] == "sica"
    assert entry["actor"] == "system:sica", \
        "ein Systemvorgang traegt seinen Bereich, nicht 'unidentified'"
    assert entry["category"] == "sica_session"
    assert entry["target_id"] == written[0].payload["id"]
    assert "topic" not in entry, "die SICA-Sitzung ist privat"


def test_staging_schreibt_je_punkt_genau_eine_zeile(monkeypatch):
    """Staging schreibt einen Punkt je Status — und genau eine Zeile dazu.

    ``create_pending`` legt einen Verlaufspunkt an; ``promote`` legt den
    kanonischen Verlaufspunkt an UND ueberschreibt den Kanonik-Punkt. Drei
    Punkte, drei Zeilen — keiner davon doppelt.
    """
    import nexus.staging as staging

    monkeypatch.setattr(staging, "_auto_ensure_collections",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staging, "_embed_content", lambda _text: [0.1, 0.2])

    class _Antwort:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(staging.requests, "put",
                        lambda *_args, **_kwargs: _Antwort())
    monkeypatch.setattr(staging, "_get_current_canonical",
                        lambda *_args, **_kwargs: None)

    pending = staging.create_pending({"content": FAKT, "category": "fact"},
                                     fact_id="fakt-1")
    assert len(_lines()) == 1, "create_pending: ein Punkt, eine Zeile"

    staging.promote(pending)

    entries = _entries()
    assert len(entries) == 3, "drei geschriebene Punkte, drei Zeilen"
    assert all(entry["action"] == "write" for entry in entries)
    assert all(entry["via"] == "staging" for entry in entries)
    assert all(entry["actor"].startswith("system:") for entry in entries), \
        "Staging ist ein Systemvorgang, kein fremder Absender"
    assert [entry["reason"] for entry in entries] == [
        "pending", "canonical", "canonical"]
    assert FAKT not in "\n".join(_lines())
