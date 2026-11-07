"""Room status for the ACTIVE Hermes memory provider.

A chat type is no longer evidence of anything: a room counts as shared only
once a HUMAN OTHER THAN THE OWNER has actually written in it. The owner's own
Discord channel stays private until someone else speaks there, and a room that
was once shared stays shared for good (recorded in
``<hermes_home>/nexus/chat-sharing.json``).

The host reports this per turn — ``chat_id``, ``author_id``, ``author_is_bot``,
``chat_shared`` (see ``NexusMemoryProvider.observe_turn``). ``chat_shared``
wins over everything because the host may know for certain (member counts);
``private_chat_ids`` survives only as an emergency exit that forces a chat
private.

Everything else must stay exactly as it is: a room nobody foreign has written
in, the CLI/GUI, and an undeterminable room keep today's visibility.

The module under test is ``plugins/memory/nexus/__init__.py`` — the provider
Hermes actually loads. The older copy under ``integrations/`` is NOT the live
code.

No network, no Qdrant, no embedding model: the client is a stub, the embedder
and the graph walk are replaced.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import os
import sqlite3
import stat
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = str(_REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Load the ACTIVE provider point-blank (the repo also ships a top-level `nexus`
# package and the older integrations/hermes-plugin copy).
_PLUGIN_PATH = _REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py"
_spec = importlib.util.spec_from_file_location(
    "nexus_active_provider_group_privacy", str(_PLUGIN_PATH)
)
assert _spec is not None, f"Could not load {_PLUGIN_PATH}"
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

NexusMemoryProvider = plugin.NexusMemoryProvider

SESSION = "sess-room-status"

OWNER = "u-owner"
STRANGER = "u-stranger"
BOT = "u-bot"
# The same person, one id per platform — the live failure: with only the
# Telegram id configured, every Discord message of the owner looked foreign.
TELEGRAM = "5763330319"
DISCORD = "373816693522432001"
CHANNEL_A = "1497111222333"
CHANNEL_B = "1497444555666"

STATE_FILE = "chat-sharing.json"


# ── Helpers ────────────────────────────────────────────────────────────────


def _write_config(home: Path, **values) -> None:
    """Write ``<hermes_home>/nexus/config.json`` — where the provider looks."""
    directory = home / "nexus"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(json.dumps(values))


def _state_path(home: Path) -> Path:
    return home / "nexus" / STATE_FILE


def _state(home: Path) -> dict:
    return json.loads(_state_path(home).read_text())


def _write_state_db(home: Path, chat_type, session_id: str = SESSION) -> None:
    """Create the slice of Hermes' state.db the fallback path reads."""
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, chat_type TEXT)")
        conn.execute(
            "INSERT INTO sessions (id, chat_type) VALUES (?, ?)", (session_id, chat_type)
        )
        conn.commit()
    finally:
        conn.close()


def _bare() -> NexusMemoryProvider:
    """An instance without ``__init__``.

    The real constructor probes the local embedder service over HTTP and may
    spawn it — a unit test must not do either. ``__new__`` plus the attributes
    the read paths touch is exactly what the bench/test pattern in the module
    itself uses.
    """
    p = NexusMemoryProvider.__new__(NexusMemoryProvider)
    p._session_id = ""
    p._hermes_home = ""
    p._agent_context = "primary"
    p._turn_signals = {}
    p._chat_shared_cache = plugin._UNSET
    p._chat_sharing_state = None
    p._chat_sharing_load_warned = False
    p._chat_sharing_write_warned = False
    p._owner_ids_cache = plugin._UNSET
    p._owner_unknown_warned = False
    p._host_user_id = ""
    p._group_context_warned = False
    p._chat_id = ""
    p._private_chat_ids = frozenset()
    p._private_chat_exempt_logged = False
    return p


def _provider(home: Path, config: dict | None = None, **kwargs) -> NexusMemoryProvider:
    """Provider wired the way ``initialize()`` wires it — without Qdrant."""
    p = _bare()
    p._session_id = SESSION
    p._hermes_home = str(home)
    if config is not None:
        _write_config(home, **config)
    p._private_chat_ids = p._read_private_chat_ids()
    # initialize() runs the same normalizer over its kwargs.
    p._apply_turn_signals({k: v for k, v in kwargs.items() if v is not None})
    return p


def _owner_provider(home: Path, **kwargs) -> NexusMemoryProvider:
    """A provider whose owner is known from the config."""
    return _provider(home, config={"owner_user_id": OWNER}, **kwargs)


def _turn(provider: NexusMemoryProvider, chat_id=CHANNEL_A, author_id=OWNER,
          author_is_bot=None, chat_shared=None):
    """One human turn as the host reports it."""
    return provider.observe_turn(chat_id=chat_id, author_id=author_id,
                                 author_is_bot=author_is_bot,
                                 chat_shared=chat_shared)


class _Point:
    def __init__(self, pid: str, payload: dict, score: float = 0.9) -> None:
        self.id = pid
        self.payload = payload
        self.score = score


class _StubQdrant:
    """Stand-in for QdrantClient: returns fixed points, touches no network."""

    def __init__(self, points):
        self._points = points
        self.queries = []

    def query_points(self, **kwargs):
        self.queries.append(kwargs)
        return SimpleNamespace(points=list(self._points))

    def retrieve(self, *args, **kwargs):
        return []


_POINTS = [
    _Point("p-public", {"content": "public note", "access_level": "public"}),
    _Point("p-trusted", {"content": "trusted note", "access_level": "trusted"}),
    _Point("p-private", {"content": "private note", "access_level": "private"}),
    # Legacy point without a level: counts as private (fail closed).
    _Point("p-legacy", {"content": "legacy note"}),
]


def _read_provider(shared) -> NexusMemoryProvider:
    """Provider whose read paths run against stubs only."""
    p = _bare()
    p._chat_shared_cache = shared
    p._collection = "nexus"
    p.vector_name = None
    p._qdrant = _StubQdrant(_POINTS)
    p._embedder = SimpleNamespace(embed=lambda text, is_query=True: [0.0] * 4)
    p._embed_cached = lambda text, is_query=True: [0.0] * 4
    p._embedder_available = lambda: True
    p._graph_boost = lambda *a, **k: []
    p._rerank_cfg = {"enabled": False}
    p._flywheel_bump = lambda entries: None
    p._bump_agent_stats = lambda **kwargs: None
    # No scope inference (it would need a real collection); {} → "default".
    p._scope_centroids = SimpleNamespace(get=lambda: {})
    p._prefetch_lock = threading.Lock()
    p._prefetch_result = ""
    p._prefetch_gate = threading.Lock()
    return p


# ── The core observation ───────────────────────────────────────────────────


class TestRoomStatusFromAuthorship:
    def test_the_owner_alone_keeps_the_room_private(self, tmp_path):
        """A Discord channel where only the owner writes is NOT shared."""
        p = _owner_provider(tmp_path)

        assert _turn(p, author_id=OWNER) is False
        assert p._visible_levels() == {"public", "trusted", "private"}
        assert p._can_read({"access_level": "private"})
        assert not _state_path(tmp_path).exists()

    def test_a_foreign_human_makes_the_room_shared(self, tmp_path):
        p = _owner_provider(tmp_path)

        assert _turn(p, author_id=STRANGER) is True
        assert p._visible_levels() == {"public"}
        assert p._agent_level() == "public"
        assert not p._can_read({"access_level": "private"})
        assert p._can_read({"access_level": "public"})

    def test_a_shared_room_stays_shared_for_the_owner(self, tmp_path):
        """Once shared, the owner's own later turn is public too — permanent."""
        p = _owner_provider(tmp_path)
        _turn(p, author_id=STRANGER)

        assert _turn(p, author_id=OWNER) is True
        assert p._visible_levels() == {"public"}

    def test_a_shared_room_stays_shared_in_a_new_process(self, tmp_path):
        _turn(_owner_provider(tmp_path), author_id=STRANGER)

        # A fresh instance stands in for the next process: only the file knows.
        fresh = _owner_provider(tmp_path)

        assert _turn(fresh, author_id=OWNER) is True

    def test_a_foreign_bot_does_not_make_the_room_shared(self, tmp_path):
        p = _owner_provider(tmp_path)

        assert _turn(p, author_id=BOT, author_is_bot=True) is False
        assert p._visible_levels() == {"public", "trusted", "private"}
        assert not _state_path(tmp_path).exists()

    def test_author_is_bot_false_is_a_human(self, tmp_path):
        p = _owner_provider(tmp_path)

        assert _turn(p, author_id=STRANGER, author_is_bot=False) is True

    def test_no_author_id_changes_nothing(self, tmp_path):
        """Without an author the provider behaves exactly as before."""
        p = _owner_provider(tmp_path)

        status = _turn(p, author_id="")

        # No author, no chat_type in the session database → undeterminable.
        assert status is None
        assert p._readable_levels() is None
        assert p._visible_levels() == {"public", "trusted", "private"}
        assert not _state_path(tmp_path).exists()

    def test_two_channels_do_not_interfere(self, tmp_path):
        p = _owner_provider(tmp_path)

        assert _turn(p, chat_id=CHANNEL_A, author_id=STRANGER) is True
        assert _turn(p, chat_id=CHANNEL_B, author_id=OWNER) is False
        assert p._visible_levels() == {"public", "trusted", "private"}
        # …and A is still shared when the owner writes there again.
        assert _turn(p, chat_id=CHANNEL_A, author_id=OWNER) is True

    def test_only_the_shared_channel_is_recorded(self, tmp_path):
        p = _owner_provider(tmp_path)
        _turn(p, chat_id=CHANNEL_A, author_id=STRANGER)
        _turn(p, chat_id=CHANNEL_B, author_id=OWNER)

        assert list(_state(tmp_path)["chats"]) == [CHANNEL_A]


class TestHostStatementWins:
    """``chat_shared`` overrides every observation while it is set."""

    def test_chat_shared_false_beats_a_foreign_author(self, tmp_path):
        p = _owner_provider(tmp_path)

        assert _turn(p, author_id=STRANGER, chat_shared=False) is False
        assert p._visible_levels() == {"public", "trusted", "private"}
        assert not _state_path(tmp_path).exists()

    def test_chat_shared_true_beats_the_owner_as_author(self, tmp_path):
        p = _owner_provider(tmp_path)

        assert _turn(p, author_id=OWNER, chat_shared=True) is True
        assert p._visible_levels() == {"public"}

    @pytest.mark.parametrize("value", [False, "false", 0, "no", " off "])
    def test_false_shapes_are_understood(self, tmp_path, value):
        p = _owner_provider(tmp_path)

        assert _turn(p, author_id=STRANGER, chat_shared=value) is False

    @pytest.mark.parametrize("value", [True, "true", 1, "yes", " ON "])
    def test_true_shapes_are_understood(self, tmp_path, value):
        p = _owner_provider(tmp_path)

        assert _turn(p, author_id=OWNER, chat_shared=value) is True

    def test_an_unparsable_statement_falls_through_to_the_observation(self, tmp_path):
        """A broken flag is "not stated", never an accidental "private"."""
        p = _owner_provider(tmp_path)

        assert _turn(p, author_id=STRANGER, chat_shared="maybe") is True


# ── The state file ─────────────────────────────────────────────────────────


class TestStateFile:
    def test_schema(self, tmp_path):
        _turn(_owner_provider(tmp_path), author_id=STRANGER)

        data = _state(tmp_path)

        assert data["version"] == 1
        entry = data["chats"][CHANNEL_A]
        assert entry["shared"] is True
        assert entry["reason"] == "foreign-author"
        assert isinstance(entry["first_seen"], int)
        assert entry["first_seen"] > 0

    def test_authors_are_stored_only_as_hashes(self, tmp_path):
        """The file must never name a person — not the stranger, not the owner."""
        p = _owner_provider(tmp_path)
        _turn(p, author_id=STRANGER)

        raw = _state_path(tmp_path).read_text()

        assert STRANGER not in raw
        assert OWNER not in raw
        assert len(_state(tmp_path)["chats"][CHANNEL_A]["last_author"]) == 64

    def test_the_file_is_owner_only(self, tmp_path):
        _turn(_owner_provider(tmp_path), author_id=STRANGER)

        mode = stat.S_IMODE(_state_path(tmp_path).stat().st_mode)

        assert mode == 0o600

    def test_no_temp_file_is_left_behind(self, tmp_path):
        _turn(_owner_provider(tmp_path), author_id=STRANGER)

        leftovers = [p.name for p in (tmp_path / "nexus").iterdir() if ".tmp-" in p.name]

        assert leftovers == []

    def test_missing_file_is_not_an_error(self, tmp_path):
        p = _owner_provider(tmp_path)

        assert p._read_chat_sharing() == {}
        assert not _state_path(tmp_path).exists()

    def test_a_corrupt_file_does_not_crash(self, tmp_path, caplog):
        p = _owner_provider(tmp_path)
        _state_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        _state_path(tmp_path).write_bytes(b"{ this is not json")

        with caplog.at_level(logging.WARNING, logger=plugin.__name__):
            assert _turn(p, author_id=OWNER) is False
            p.observe_turn(chat_id=CHANNEL_A, author_id=OWNER)  # warns once only

        assert p._visible_levels() == {"public", "trusted", "private"}
        warnings = [r for r in caplog.records if "room state unreadable" in r.getMessage()]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]

    def test_a_file_of_the_wrong_shape_does_not_crash(self, tmp_path, caplog):
        p = _owner_provider(tmp_path)
        _state_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        _state_path(tmp_path).write_text("[1, 2, 3]")

        with caplog.at_level(logging.WARNING, logger=plugin.__name__):
            assert p._read_chat_sharing() == {}

        assert p._visible_levels() == {"public", "trusted", "private"}

    def test_an_unreadable_path_does_not_crash(self, tmp_path):
        """chat-sharing.json replaced by a directory: reading must not raise."""
        _state_path(tmp_path).mkdir(parents=True)
        p = _owner_provider(tmp_path)

        assert p._read_chat_sharing() == {}
        assert _turn(p, author_id=OWNER) is False

    def test_reading_never_creates_the_file(self, tmp_path):
        p = _owner_provider(tmp_path)

        p._read_chat_sharing()
        _turn(p, author_id=OWNER)

        assert not _state_path(tmp_path).exists()

    def test_another_process_change_lands_on_the_next_turn(self, tmp_path):
        """The cache lasts one turn — not the lifetime of the process."""
        p = _owner_provider(tmp_path)
        assert _turn(p, author_id=STRANGER) is True

        # Another process forgets the room.
        _state_path(tmp_path).write_text(json.dumps({"version": 1, "chats": {}}))

        assert _turn(p, author_id=OWNER) is False

    def test_a_write_failure_does_not_break_the_turn(self, tmp_path, caplog):
        """An unwritable directory: warn once, keep the room shared in memory."""
        p = _owner_provider(tmp_path)
        (tmp_path / "nexus").mkdir(parents=True, exist_ok=True)
        _state_path(tmp_path).mkdir()          # blocks the atomic replace

        with caplog.at_level(logging.WARNING, logger=plugin.__name__):
            assert _turn(p, author_id=STRANGER) is True
            p.observe_turn(chat_id=CHANNEL_A, author_id=STRANGER)

        assert p._visible_levels() == {"public"}
        warnings = [r for r in caplog.records if "could not record chat" in r.getMessage()]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]


# ── The owner ──────────────────────────────────────────────────────────────


class TestOwner:
    def test_the_owner_comes_from_the_config(self, tmp_path):
        p = _provider(tmp_path, config={"owner_user_id": OWNER})

        assert p._owner_user_id() == OWNER
        assert _turn(p, author_id=OWNER) is False
        assert _turn(p, author_id=STRANGER) is True

    def test_the_host_user_id_is_stored_in_the_config_once(self, tmp_path):
        p = _provider(tmp_path, user_id=OWNER)

        assert p._owner_user_id() == OWNER
        written = json.loads((tmp_path / "nexus" / "config.json").read_text())
        assert written["owner_user_id"] == OWNER

        # Every later process reads it from the file.
        assert _provider(tmp_path)._owner_user_id() == OWNER

    def test_the_stored_owner_keeps_the_rest_of_the_config(self, tmp_path):
        p = _provider(tmp_path, config={"collection_name": "custom"}, user_id=OWNER)
        p._owner_user_id()

        written = json.loads((tmp_path / "nexus" / "config.json").read_text())

        assert written == {"collection_name": "custom", "owner_user_id": OWNER}

    def test_the_config_owner_wins_over_the_host(self, tmp_path):
        p = _provider(tmp_path, config={"owner_user_id": OWNER}, user_id="someone-else")

        assert p._owner_user_id() == OWNER

    def test_no_owner_warns_once(self, tmp_path, caplog):
        p = _provider(tmp_path)

        with caplog.at_level(logging.WARNING, logger=plugin.__name__):
            p.observe_turn(chat_id=CHANNEL_A, author_id=STRANGER)
            p.observe_turn(chat_id=CHANNEL_A, author_id=STRANGER)

        warnings = [r for r in caplog.records if "no owner known" in r.getMessage()]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]

    def test_no_owner_means_no_restriction(self, tmp_path):
        """Without an owner the observation is impossible — nothing narrows."""
        p = _provider(tmp_path)

        assert p.observe_turn(chat_id=CHANNEL_A, author_id=STRANGER) is None
        assert p._visible_levels() == {"public", "trusted", "private"}
        assert not _state_path(tmp_path).exists()

    def test_an_owner_reported_late_still_counts(self, tmp_path):
        """initialize() without user_id, a later turn that names it."""
        p = _provider(tmp_path)
        assert p.observe_turn(chat_id=CHANNEL_A, author_id=STRANGER) is None

        assert p.observe_turn(chat_id=CHANNEL_A, author_id=STRANGER,
                              user_id=OWNER) is True

    def test_a_broken_config_is_no_exception(self, tmp_path, caplog):
        (tmp_path / "nexus").mkdir()
        (tmp_path / "nexus" / "config.json").write_bytes(b"{ not json")
        p = _provider(tmp_path)

        assert p._owner_user_id() == ""

    def test_a_config_that_is_a_directory_is_no_exception(self, tmp_path):
        (tmp_path / "nexus" / "config.json").mkdir(parents=True)
        p = _provider(tmp_path)

        assert p._owner_user_id() == ""


# ── Owner ids per platform ─────────────────────────────────────────────────


class TestOwnerIdsPerPlatform:
    """Ownership is a SET: one person, one id per platform.

    The live failure this covers: ``owner_user_id`` held only the Telegram id,
    so the owner's Discord id (373816693522432001) read as a stranger and both
    of his Discord channels were narrowed to "public" for good.
    """

    def test_a_list_names_every_owner(self, tmp_path):
        p = _provider(tmp_path, config={"owner_user_ids": [TELEGRAM, DISCORD]})

        assert p._owner_user_ids() == {TELEGRAM, DISCORD}
        assert _turn(p, chat_id=CHANNEL_A, author_id=TELEGRAM) is False
        assert _turn(p, chat_id=CHANNEL_B, author_id=DISCORD) is False
        assert _turn(p, chat_id=CHANNEL_A, author_id=STRANGER) is True

    def test_a_platform_mapping_uses_the_values(self, tmp_path):
        p = _provider(tmp_path, config={
            "owner_user_ids": {"telegram": TELEGRAM, "discord": DISCORD},
        })

        assert p._owner_user_ids() == {TELEGRAM, DISCORD}
        assert _turn(p, author_id=TELEGRAM) is False
        assert _turn(p, author_id=DISCORD) is False
        # The labels are labels — they are never compared against an author.
        assert _turn(p, chat_id=CHANNEL_B, author_id="telegram") is True

    def test_values_are_normalized_to_strings(self, tmp_path):
        p = _provider(tmp_path, config={
            "owner_user_ids": {"telegram": int(TELEGRAM), "discord": f" {DISCORD} "},
        })

        assert p._owner_user_ids() == {TELEGRAM, DISCORD}
        assert _turn(p, author_id=TELEGRAM) is False

    def test_a_legacy_single_value_still_counts(self, tmp_path):
        p = _provider(tmp_path, config={"owner_user_id": OWNER})

        assert p._owner_user_ids() == {OWNER}
        assert _turn(p, author_id=OWNER) is False
        assert _turn(p, chat_id=CHANNEL_B, author_id=STRANGER) is True

    def test_the_legacy_value_is_migrated_to_the_list_form(self, tmp_path):
        p = _provider(tmp_path, config={"owner_user_id": OWNER,
                                        "collection_name": "custom"})

        assert p._owner_user_ids() == {OWNER}
        written = json.loads((tmp_path / "nexus" / "config.json").read_text())
        assert written == {"owner_user_id": OWNER,
                           "collection_name": "custom",
                           "owner_user_ids": [OWNER]}

    def test_the_migration_happens_once(self, tmp_path, monkeypatch):
        _provider(tmp_path, config={"owner_user_id": OWNER})._owner_user_ids()
        assert "owner_user_ids" in json.loads(
            (tmp_path / "nexus" / "config.json").read_text())

        # A later process finds the new form and never writes again.
        calls = []
        monkeypatch.setattr(plugin, "_write_json_atomic",
                            lambda path, data: calls.append(data))
        fresh = _provider(tmp_path, config=None)
        fresh._owner_user_ids()
        assert calls == []

    def test_the_migrated_file_is_owner_only(self, tmp_path):
        _provider(tmp_path, config={"owner_user_id": OWNER})._owner_user_ids()

        mode = stat.S_IMODE((tmp_path / "nexus" / "config.json").stat().st_mode)
        assert mode == 0o600

    def test_both_keys_are_unioned(self, tmp_path):
        p = _provider(tmp_path, config={"owner_user_ids": [DISCORD],
                                        "owner_user_id": TELEGRAM})

        assert p._owner_user_ids() == {TELEGRAM, DISCORD}
        assert _turn(p, chat_id=CHANNEL_A, author_id=TELEGRAM) is False
        assert _turn(p, chat_id=CHANNEL_B, author_id=DISCORD) is False

    def test_an_existing_list_is_not_rewritten(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(plugin, "_write_json_atomic",
                            lambda path, data: calls.append(data))

        _provider(tmp_path, config={"owner_user_ids": [TELEGRAM, DISCORD],
                                    "owner_user_id": TELEGRAM})._owner_user_ids()

        assert calls == []

    def test_the_host_does_not_overwrite_configured_entries(self, tmp_path):
        p = _provider(tmp_path,
                      config={"owner_user_ids": [TELEGRAM, DISCORD]},
                      user_id=STRANGER)

        assert p._owner_user_ids() == {TELEGRAM, DISCORD}
        written = json.loads((tmp_path / "nexus" / "config.json").read_text())
        assert written == {"owner_user_ids": [TELEGRAM, DISCORD]}

    def test_the_host_is_taken_when_the_config_names_nobody(self, tmp_path):
        p = _provider(tmp_path, config={"owner_user_ids": []}, user_id=OWNER)

        assert p._owner_user_ids() == {OWNER}
        written = json.loads((tmp_path / "nexus" / "config.json").read_text())
        assert written["owner_user_id"] == OWNER

    @pytest.mark.parametrize("empty", [[], {}, "", None])
    def test_empty_values_are_no_exception(self, tmp_path, caplog, empty):
        p = _provider(tmp_path, config={"owner_user_ids": empty})

        with caplog.at_level(logging.WARNING, logger=plugin.__name__):
            assert p._owner_user_ids() == frozenset()
            p.observe_turn(chat_id=CHANNEL_A, author_id=STRANGER)

        assert p.observe_turn(chat_id=CHANNEL_A, author_id=STRANGER) is None
        warnings = [r for r in caplog.records if "no owner known" in r.getMessage()]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]

    def test_only_the_channel_with_the_foreign_author_is_shared(self, tmp_path):
        """Two Discord channels, owner writes in both, a stranger in one."""
        p = _provider(tmp_path, config={"owner_user_ids": [TELEGRAM, DISCORD]})

        assert _turn(p, chat_id=CHANNEL_A, author_id=DISCORD) is False
        assert _turn(p, chat_id=CHANNEL_B, author_id=TELEGRAM) is False
        assert _turn(p, chat_id=CHANNEL_B, author_id=STRANGER) is True
        assert _turn(p, chat_id=CHANNEL_A, author_id=DISCORD) is False

        recorded = _state(tmp_path)["chats"]
        assert set(recorded) == {CHANNEL_B}

    def test_the_single_value_helper_stays_stable(self, tmp_path):
        p = _provider(tmp_path, config={"owner_user_ids": [TELEGRAM, DISCORD]})

        assert p._owner_user_id() == min(TELEGRAM, DISCORD)


# ── Emergency exit: private_chat_ids ───────────────────────────────────────


class TestPrivateChatIdsEmergencyExit:
    def test_a_listed_chat_is_forced_private(self, tmp_path):
        p = _provider(tmp_path, config={"owner_user_id": OWNER,
                                        "private_chat_ids": [CHANNEL_A]})

        assert _turn(p, author_id=STRANGER) is False
        assert p._visible_levels() == {"public", "trusted", "private"}
        assert not _state_path(tmp_path).exists()

    def test_an_unlisted_chat_is_observed_normally(self, tmp_path):
        p = _provider(tmp_path, config={"owner_user_id": OWNER,
                                        "private_chat_ids": [CHANNEL_B]})

        assert _turn(p, chat_id=CHANNEL_A, author_id=STRANGER) is True

    def test_a_listed_chat_is_private_even_if_already_recorded(self, tmp_path):
        p = _owner_provider(tmp_path)
        _turn(p, author_id=STRANGER)

        p._private_chat_ids = frozenset({CHANNEL_A})

        assert _turn(p, author_id=OWNER) is False

    def test_the_override_is_logged_once(self, tmp_path, caplog):
        p = _provider(tmp_path, config={"owner_user_id": OWNER,
                                        "private_chat_ids": [CHANNEL_A]})

        with caplog.at_level(logging.INFO, logger=plugin.__name__):
            _turn(p)
            _turn(p)

        records = [r for r in caplog.records if "private_chat_ids" in r.getMessage()]
        assert len(records) == 1
        assert CHANNEL_A in records[0].getMessage()


# ── Fallback: nothing to observe ───────────────────────────────────────────


class TestChatTypeFallback:
    """Only reached when neither an author nor an owner can be observed."""

    def test_a_group_session_is_narrowed(self, tmp_path):
        """No authorship reported at all → the old chat-type check decides."""
        _write_state_db(tmp_path, "group")
        p = _provider(tmp_path)

        assert p._chat_is_shared() is True
        assert p._visible_levels() == {"public"}

    @pytest.mark.parametrize("value", ["dm", "", None, "thread"])
    def test_a_non_group_session_is_not_shared(self, tmp_path, value):
        _write_state_db(tmp_path, value)

        assert _provider(tmp_path)._chat_is_shared() is False

    @pytest.mark.parametrize("value", ["group", "channel", "supergroup", " GUILD "])
    def test_group_aliases_are_recognised(self, tmp_path, value):
        _write_state_db(tmp_path, value)

        assert _provider(tmp_path)._visible_levels() == {"public"}

    def test_a_host_chat_type_wins_over_the_database(self, tmp_path):
        _write_state_db(tmp_path, "dm")

        assert _provider(tmp_path, chat_type="group")._chat_is_shared() is True

    def test_no_database_is_undeterminable_and_warns_once(self, tmp_path, caplog):
        p = _provider(tmp_path)

        with caplog.at_level(logging.WARNING, logger=plugin.__name__):
            assert p._chat_is_shared() is None
            p._chat_sharing_state = None          # next turn: a fresh lookup
            p._chat_shared_cache = plugin._UNSET
            assert p._chat_is_shared() is None

        assert p._visible_levels() == {"public", "trusted", "private"}
        warnings = [r for r in caplog.records if "chat type unknown" in r.getMessage()]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]

    def test_the_session_database_is_opened_read_only(self, tmp_path):
        _write_state_db(tmp_path, "dm")
        before = (tmp_path / "state.db").stat().st_mtime_ns

        assert _provider(tmp_path)._chat_is_shared() is False

        assert (tmp_path / "state.db").stat().st_mtime_ns == before

    def test_a_corrupt_database_is_unknown_not_a_crash(self, tmp_path, caplog):
        (tmp_path / "state.db").write_bytes(b"this is not a sqlite database")
        p = _provider(tmp_path)

        with caplog.at_level(logging.WARNING, logger=plugin.__name__):
            assert p._chat_is_shared() is None

        assert p._visible_levels() == {"public", "trusted", "private"}

    def test_an_author_suppresses_the_database_lookup(self, tmp_path):
        """With an observation the chat type is irrelevant — even a group."""
        _write_state_db(tmp_path, "group")
        p = _owner_provider(tmp_path)

        assert _turn(p, author_id=OWNER) is False

    def test_initialize_wires_the_turn_signals(self):
        source = inspect.getsource(NexusMemoryProvider.initialize)
        assert "_apply_turn_signals" in source
        assert "_owner_user_id" in source
        assert "private_chat_ids" in source


# ── Read paths ─────────────────────────────────────────────────────────────


class TestReadPaths:
    def test_prefetch_injects_only_public_points_in_a_shared_room(self):
        p = _read_provider(shared=True)

        p._do_prefetch("wallbox")
        block = p.prefetch("wallbox")

        assert "public note" in block
        assert "private note" not in block
        assert "trusted note" not in block
        assert "legacy note" not in block  # missing level counts as private

    def test_prefetch_in_a_private_room_is_unchanged(self):
        p = _read_provider(shared=False)

        p._do_prefetch("wallbox")
        block = p.prefetch("wallbox")

        for text in ("public note", "trusted note", "private note", "legacy note"):
            assert text in block

    def test_prefetch_with_an_undetermined_room_is_unchanged(self):
        p = _read_provider(shared=None)

        p._do_prefetch("wallbox")
        block = p.prefetch("wallbox")

        for text in ("public note", "trusted note", "private note", "legacy note"):
            assert text in block

    def test_recall_injects_only_public_points_in_a_shared_room(self):
        p = _read_provider(shared=True)

        results = p._recall("wallbox")

        assert [r["access_level"] for r in results] == ["public"]
        assert [r["text"] for r in results] == ["public note"]

    def test_recall_in_a_private_room_is_unchanged(self):
        p = _read_provider(shared=False)

        results = p._recall("wallbox")

        assert [r["access_level"] for r in results] == [
            "public",
            "trusted",
            "private",
            None,  # legacy point without a level — unchanged
        ]

    @pytest.mark.parametrize("tool", ["_graph_traverse", "_find_entities",
                                      "_get_subgraph", "_get_related"])
    def test_graph_tools_are_narrowed_through_the_agent_level(self, tool):
        """All four graph tools feed ``_agent_level()`` into GraphTraversal."""
        assert "self._agent_level()" in inspect.getsource(
            getattr(NexusMemoryProvider, tool)
        )

        p = _bare()
        p._chat_shared_cache = True
        assert p._agent_level() == "public"

    def test_the_graph_allowance_is_unchanged_in_a_private_room(self):
        p = _bare()
        p._chat_shared_cache = False

        assert p._can_read({"access_level": "trusted"}, {"public", "trusted"})
        assert not p._can_read({"access_level": "private"}, {"public", "trusted"})

    def test_a_shared_room_narrows_even_the_graph_allowance(self):
        p = _bare()
        p._chat_shared_cache = True

        assert not p._can_read({"access_level": "trusted"})
        assert not p._can_read({"access_level": "private"})
        assert p._can_read({"access_level": "public"})
        assert not p._can_read({})  # missing level fails closed

    def test_an_undetermined_room_keeps_full_visibility(self):
        p = _bare()
        p._chat_shared_cache = None

        assert p._readable_levels() is None
        assert p._can_read({"access_level": "private"})
        assert p._visible_levels() == {"public", "trusted", "private"}


# ── Hermes' per-turn ingress: on_turn_start ─────────────────────────────────
# `agent/turn_context.py` calls this BEFORE `prefetch_all` for the same turn,
# with author_id / author_name / author_is_bot (measured, not guessed). The
# room status it observes must already hold when that turn's prefetch runs.


def _turn_start(provider: NexusMemoryProvider, **kwargs):
    """One turn as Hermes announces it — no chat id, the room is the known one."""
    kwargs.setdefault("turn_number", 1)
    kwargs.setdefault("message", "hallo")
    return provider.on_turn_start(**kwargs)


class TestTurnStartIngress:
    def test_a_foreign_author_makes_the_room_shared_in_the_same_turn(self, tmp_path):
        p = _owner_provider(tmp_path, chat_id=CHANNEL_A)

        _turn_start(p, author_id=STRANGER, author_is_bot=False)

        assert p._chat_is_shared() is True
        assert p._visible_levels() == {"public"}

    def test_the_owner_writing_keeps_the_room_private(self, tmp_path):
        p = _provider(tmp_path, user_id=OWNER, chat_id=CHANNEL_A)

        _turn_start(p, author_id=OWNER, author_is_bot=False)

        assert p._chat_is_shared() is False
        assert p._visible_levels() == {"public", "trusted", "private"}

    def test_a_foreign_bot_does_not_make_the_room_shared(self, tmp_path):
        p = _owner_provider(tmp_path, chat_id=CHANNEL_A)

        _turn_start(p, author_id=BOT, author_is_bot=True)

        assert p._chat_is_shared() is False
        assert not _state_path(tmp_path).exists()

    def test_no_signals_change_nothing(self, tmp_path):
        p = _owner_provider(tmp_path)
        before = dict(p._turn_signals)

        _turn_start(p)

        assert p._turn_signals == before
        assert not _state_path(tmp_path).exists()

    def test_the_chat_id_is_the_one_initialize_reported(self, tmp_path):
        """Hermes passes no chat id per turn — the process knows the room."""
        p = _owner_provider(tmp_path, chat_id=CHANNEL_A)

        _turn_start(p, author_id=STRANGER)

        assert list(_state(tmp_path)["chats"]) == [CHANNEL_A]

    def test_a_chat_id_initialized_late_is_taken_over(self, tmp_path):
        """initialize() had no chat_id; a later observe still names it."""
        p = _owner_provider(tmp_path)
        p.observe_turn(chat_id=CHANNEL_A)

        _turn_start(p, author_id=STRANGER)

        assert list(_state(tmp_path)["chats"]) == [CHANNEL_A]

    def test_the_message_and_the_author_name_are_never_stored(self, tmp_path):
        p = _owner_provider(tmp_path, chat_id=CHANNEL_A)

        _turn_start(p, message="meine IBAN ist DE89 3704 0044 0532 0130 00",
                    author_id=STRANGER, author_name="Fremder Name")

        raw = _state_path(tmp_path).read_text()
        assert "IBAN" not in raw
        assert "Fremder Name" not in raw
        assert "IBAN" not in json.dumps(p._turn_signals)
        assert "Fremder Name" not in json.dumps(p._turn_signals)

    @pytest.mark.parametrize("value", [{"id": STRANGER}, [STRANGER], None])
    def test_an_unusable_author_id_is_ignored(self, tmp_path, value):
        """A dict/list/None is "not reported" — never an accidental stranger."""
        p = _owner_provider(tmp_path, chat_id=CHANNEL_A)

        _turn_start(p, author_id=value)

        assert p._chat_is_shared() is None
        assert not _state_path(tmp_path).exists()

    def test_a_numeric_author_id_does_not_raise(self, tmp_path):
        """Discord ids arrive as numbers: a number is a valid author."""
        p = _owner_provider(tmp_path, chat_id=CHANNEL_A)

        _turn_start(p, author_id=1497111222333)

        assert p._chat_is_shared() is True

    def test_the_signature_matches_the_hermes_call(self, tmp_path):
        parameters = inspect.signature(NexusMemoryProvider.on_turn_start).parameters

        for name in ("turn_number", "message", "model", "author_id",
                     "author_name", "author_is_bot"):
            assert name in parameters, name
        assert parameters["turn_number"].default is inspect.Parameter.empty
        assert parameters["model"].kind is inspect.Parameter.KEYWORD_ONLY

        # Exactly the call `agent/turn_context.py` makes.
        p = _owner_provider(tmp_path, chat_id=CHANNEL_A)
        assert p.on_turn_start(
            1, "hallo", model="claude-opus-5-5",
            author_id=STRANGER, author_name="Someone", author_is_bot=False,
        ) is None
        assert p._chat_is_shared() is True

    def test_it_never_raises_when_the_core_fails(self, tmp_path, caplog):
        p = _owner_provider(tmp_path, chat_id=CHANNEL_A)

        def _boom(*args, **kwargs):
            raise RuntimeError("signal trouble")

        p.observe_turn = _boom
        with caplog.at_level(logging.WARNING, logger=plugin.__name__):
            assert _turn_start(p, author_id=STRANGER) is None
            assert _turn_start(p, author_id=STRANGER) is None

        warnings = [r for r in caplog.records if "on_turn_start ignored" in r.getMessage()]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
    def test_an_unwritable_state_file_does_not_break_the_turn(self, tmp_path, caplog):
        p = _owner_provider(tmp_path, chat_id=CHANNEL_A)
        (tmp_path / "nexus").mkdir(parents=True, exist_ok=True)
        (tmp_path / "nexus").chmod(0o500)          # read-only room state folder
        try:
            with caplog.at_level(logging.WARNING, logger=plugin.__name__):
                _turn_start(p, author_id=STRANGER)
                _turn_start(p, author_id=STRANGER)     # warns once only
        finally:
            (tmp_path / "nexus").chmod(0o700)

        assert p._chat_is_shared() is True
        assert p._visible_levels() == {"public"}
        warnings = [r for r in caplog.records if "could not record chat" in r.getMessage()]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]


class TestTurnStartOrdering:
    def test_the_same_turn_is_not_served_the_previous_turns_block(self, tmp_path):
        """A block built before a stranger spoke must not be injected after."""
        p = _read_provider(shared=False)
        p._hermes_home = str(tmp_path)
        p._host_user_id = OWNER
        p._owner_ids_cache = plugin._UNSET
        p._do_prefetch("wallbox")
        assert "private note" in p.prefetch("wallbox")

        _turn_start(p, turn_number=2, author_id=STRANGER, author_is_bot=False)

        block = p.prefetch("wallbox")          # same turn, same query
        assert "public note" in block
        for text in ("private note", "trusted note", "legacy note"):
            assert text not in block

    def test_an_unchanged_room_keeps_the_cached_block(self, tmp_path):
        p = _read_provider(shared=False)
        p._hermes_home = str(tmp_path)
        p._host_user_id = OWNER
        p._owner_ids_cache = plugin._UNSET
        p._do_prefetch("wallbox")
        first = p.prefetch("wallbox")

        _turn_start(p, turn_number=2, author_id=OWNER, author_is_bot=False)

        assert p.prefetch("wallbox") == first
